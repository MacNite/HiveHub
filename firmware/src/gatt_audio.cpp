// gatt_audio.cpp — see gatt_audio.h for the contract and the protocol.

#include "gatt_audio.h"

#if HIVEINSIDE_AUDIO_ENABLED

#include <NimBLEDevice.h>
#include <mbedtls/md.h>

#include <cstdlib>
#include <cstring>

#include "audio_ring.h"
#include "ble_stack.h"
#include "heap_diag.h"
#include "hivehub_network.h"  // crc32Update

#if ENABLE_BLE_SCAN
#include "ble_sensor.h"  // normalizeMac + locateByScan
#endif

namespace gattaudio {

namespace {

const char* BASE = "-7a1c-4b9e-9a2f-1d6e0b9c1a01";
String uuid(const char* prefix) { return String(prefix) + BASE; }

constexpr uint8_t OP_START = 0x01;
constexpr uint8_t OP_STOP = 0x02;

// DATA framing: seq(2 LE) flags(1) reserved(1), then PCM16 LE.
constexpr size_t HEADER_LEN = 4;
constexpr uint8_t FLAG_FINAL = 0x01;  // last packet of the session
constexpr uint8_t FLAG_GAP = 0x02;    // node dropped audio just before this one

// STATUS is exactly 30 bytes; anything shorter is a firmware mismatch.
constexpr size_t STATUS_LEN = 30;

constexpr uint8_t STATE_DONE = 0x03;
constexpr uint8_t ERR_FIRST = 0x10;

NimBLEClient* s_client = nullptr;
NimBLERemoteCharacteristic* s_ctrl = nullptr;
NimBLERemoteCharacteristic* s_data = nullptr;
NimBLERemoteCharacteristic* s_status = nullptr;
String s_lastError;

// ── The ring ───────────────────────────────────────────────────────────────
//
// Notifications land on the NimBLE host task; the caller drains from its own
// loop while it writes TLS. On the classic dual-core ESP32 the NimBLE host task
// is pinned to core 0 and the Arduino loop task runs on core 1, so those two
// are not merely interleaved — they execute at the same instant. Everything the
// callback touches is therefore guarded by a spinlock, and the ring itself
// (audio_ring.h) refuses to touch memory once it has been detached, so a
// notification arriving during teardown is counted and dropped rather than
// writing into a buffer cleanup() is in the middle of freeing.
//
// Sizing: the node emits 32 kB/s and never waits for us. The ring is the only
// thing absorbing a TLS write that takes longer than usual, so it is measured
// in *seconds of audio*, not in kilobytes — see HIVEINSIDE_AUDIO_RING_BYTES.
// An overrun is counted rather than hidden: dropping audio silently would
// produce a recording with an inaudible seam and no way to explain it.
audioring::Ring s_ring;
portMUX_TYPE s_ringMux = portMUX_INITIALIZER_UNLOCKED;

// `volatile` only where a reader takes the value WITHOUT the spinlock:
// streaming() and finish() both check these outside it. Everything below them
// is read and written under s_ringMux, which already orders the access — and
// `volatile` there is not merely redundant, it makes `s_gaps++` a deprecated
// expression in C++20 and a warning today.
volatile bool s_streaming = false;
volatile bool s_sawFinal = false;
uint32_t s_gaps = 0;
bool s_haveSeq = false;
uint16_t s_nextSeq = 0;
uint32_t s_crc = 0;
bool s_stopSent = false;

void onNotify(NimBLERemoteCharacteristic* /*chr*/, uint8_t* data, size_t len,
              bool /*isNotify*/) {
  if (len < HEADER_LEN) return;
  const uint16_t seq = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
  const uint8_t flags = data[2];
  const uint8_t* pcm = data + HEADER_LEN;
  const size_t pcmLen = len - HEADER_LEN;

  // portENTER_CRITICAL, not the _ISR variant: this is the NimBLE host TASK, not
  // an interrupt. Both alias the same spinlock on the ESP32, but the task macro
  // is the one whose contract this call site actually satisfies.
  portENTER_CRITICAL(&s_ringMux);
  // A sequence jump is loss on the air; FLAG_GAP is loss inside the node. Both
  // mean the audio is not continuous across this point, and the backend marks
  // the recording accordingly.
  if (s_haveSeq && seq != s_nextSeq) s_gaps++;
  if (flags & FLAG_GAP) s_gaps++;
  s_nextSeq = seq + 1;
  s_haveSeq = true;

  // One bounded memcpy (two at the wrap), not a byte loop with a modulo per
  // byte: this runs with interrupts disabled on the core the BLE controller
  // shares, and the old loop held them off for long enough to cost connection
  // events on exactly the weak links that need them most.
  s_ring.write(pcm, pcmLen);

  if (flags & FLAG_FINAL) {
    s_sawFinal = true;
    s_streaming = false;
  }
  portEXIT_CRITICAL(&s_ringMux);
}

void resetSession() {
  portENTER_CRITICAL(&s_ringMux);
  // Detached, with the byte and seam counters back at zero. begin() is only
  // ever entered after the previous session's cleanup(), which freed the
  // storage, so there is nothing here to leak.
  s_ring.attach(nullptr, 0);
  s_streaming = false;
  s_sawFinal = false;
  s_gaps = 0;
  s_haveSeq = false;
  s_nextSeq = 0;
  portEXIT_CRITICAL(&s_ringMux);
  s_crc = 0;
  s_stopSent = false;
}

// Read STATUS into `out`. False when the link is gone or the value is short —
// a short STATUS means the node is running firmware older than 0.6.0, which is
// worth saying plainly rather than mis-parsing.
bool readStatus(uint8_t out[STATUS_LEN]) {
  if (!s_status || !s_client || !s_client->isConnected()) return false;
  std::string s = s_status->readValue();
  if (s.size() < STATUS_LEN) return false;
  memcpy(out, s.data(), STATUS_LEN);
  return true;
}

// Parse the 32-byte pre-shared key from its hex form in secrets.h. Returns
// false when it is absent, malformed, or all zero — every one of which must
// fail closed, because the node will refuse the session anyway and a clear
// message here beats an opaque "authentication failed" from the far end.
bool loadPsk(uint8_t psk[32]) {
  const char* hex = HIVEINSIDE_AUDIO_PSK_HEX;
  if (!hex || strlen(hex) != 64) return false;
  uint8_t any = 0;
  for (size_t i = 0; i < 32; i++) {
    char byteHex[3] = {hex[i * 2], hex[i * 2 + 1], 0};
    char* end = nullptr;
    long v = strtol(byteHex, &end, 16);
    if (end != byteHex + 2) return false;
    psk[i] = (uint8_t)v;
    any |= psk[i];
  }
  return any != 0;
}

}  // namespace

const String& lastError() { return s_lastError; }

const char* errorText(uint8_t error) {
  switch (error) {
    case 0x10: return "bad parameters";
    case 0x11: return "authentication failed (PSK mismatch between hub and node?)";
    case 0x12: return "node busy (an OTA or another session holds the link)";
    case 0x13: return "microphone failure";
    case 0x14: return "node says DATA was not subscribed";
    case 0x15: return "notification stall";
    case 0x16: return "unsupported format";
    default:   return "unknown error";
  }
}

// Tear the session down. The ORDER here is the whole point, and getting it
// wrong is what reset hubs in the field: a notification can be executing on the
// NimBLE host task — on the classic ESP32 that is the other core, genuinely
// simultaneously — for as long as the client exists. On a weak link that is the
// normal case rather than a rare race, because the session ends on a stall or a
// dropped link with the node still transmitting.
//
// So: make the callback harmless, THEN take away its source, THEN free.
void cleanup() {
  // 1. Detach under the lock. From here a late notification is counted and
  //    dropped by the ring instead of writing through a pointer this function
  //    is about to hand to free().
  portENTER_CRITICAL(&s_ringMux);
  s_streaming = false;
  uint8_t* storage = s_ring.detach();
  portEXIT_CRITICAL(&s_ringMux);

  // 2. Stop the source. unsubscribe() only means anything while the link is
  //    up; on a dropped link it is a CCCD write to a dead connection, so skip
  //    it the way the OTA relay does rather than waiting out its timeout. The
  //    node ends the session by itself when it sees the disconnect.
  if (s_data && s_client && s_client->isConnected()) s_data->unsubscribe();
  if (s_client) {
    if (s_client->isConnected()) s_client->disconnect();
    // deleteClient() unregisters the client and its characteristics, so once it
    // returns NimBLE has nothing left to dispatch a notification against.
    NimBLEDevice::deleteClient(s_client);
    s_client = nullptr;
  }
  s_ctrl = s_data = s_status = nullptr;

  // 3. Only now is the storage genuinely unreachable.
  if (storage) free(storage);

  // See ble_stack.h for why this is never deinit(true).
  blestack::release();
}

bool begin(const String& mac, uint16_t durationDs, int8_t gainDb) {
  s_lastError = "";
  resetSession();

  uint8_t psk[32];
  if (!loadPsk(psk)) {
    s_lastError = "no HiveInside audio key configured on this hub "
                  "(set HIVEINSIDE_AUDIO_PSK_HEX in secrets.h)";
    Serial.printf("[HI-AUD] %s\n", s_lastError.c_str());
    return false;
  }

#if ENABLE_BLE_SCAN
  String m = blesensor::normalizeMac(mac);
#else
  String m = mac;
  m.toUpperCase();
#endif
  if (m.length() == 0) {
    s_lastError = "no HiveInside MAC for this hive";
    return false;
  }

  // Bring BLE up here, with the caller's TLS session already open, and NOT
  // ahead of it. 0.30.6 hoisted this above the socket on the theory that the
  // controller is the least flexible allocation and should bid first; the
  // field answered in one cycle with "TLS connect to the backend failed" 1.28 s
  // after the command was claimed, against a 15 s connect timeout — mbedtls
  // failing to allocate, before it reached the network. See the long note in
  // hivehub_network.cpp: mbedtls needs two 16 kB CONTIGUOUS record buffers on
  // this framework, and contiguity is what a resident BT controller destroys.
  // The fragile allocation goes first; this one is the robust one.
  if (!blestack::acquire()) {
    s_lastError = "BLE stack would not start (out of memory with WiFi and TLS up?)";
    Serial.printf("[HI-AUD] %s (free heap %u, largest block %u)\n",
                  s_lastError.c_str(), (unsigned)ESP.getFreeHeap(),
                  (unsigned)ESP.getMaxAllocHeap());
    return false;
  }
  NimBLEDevice::setMTU(247);  // 240 PCM bytes per notification once granted

  // What the heap looks like with BOTH the TLS session and the controller
  // resident — the number every question on this path turns out to need, and
  // the one 0.30.6 had no way to print. logDiag(), not probe(): nothing is
  // streaming yet, but checkIntegrity() walks every block and this runs inside
  // the node's ten-second arm window once the connect below happens.
  heapdiag::logDiag("audio-radio-up");

  // Size the ring against what the heap can actually give, not against what the
  // config asks for. This runs last of the three big allocations on this path —
  // controller, TLS session, ring — which is the right way round: it is the
  // only one of them that can shrink and still produce a recording.
  // HIVEINSIDE_AUDIO_RING_BYTES is a cushion — how long a TLS write may stall
  // before audio is lost — so halving it costs gaps under load, while insisting
  // on the full figure costs the whole recording. Never take more than half the
  // largest contiguous block: mbedtls still has to allocate record buffers
  // underneath this for every chunk of the upload.
  size_t want = HIVEINSIDE_AUDIO_RING_BYTES;
  const size_t largest = ESP.getMaxAllocHeap();
  while (want > HIVEINSIDE_AUDIO_RING_MIN_BYTES && want > largest / 2) {
    want /= 2;
  }
  uint8_t* storage = nullptr;
  for (;;) {
    storage = (uint8_t*)malloc(want);
    if (storage || want <= HIVEINSIDE_AUDIO_RING_MIN_BYTES) break;
    want /= 2;
    if (want < HIVEINSIDE_AUDIO_RING_MIN_BYTES) want = HIVEINSIDE_AUDIO_RING_MIN_BYTES;
  }
  if (!storage) {
    s_lastError = String("out of memory for the audio buffer (needed at least ") +
                  (HIVEINSIDE_AUDIO_RING_MIN_BYTES / 1024) + " kB)";
    Serial.printf("[HI-AUD] %s (free heap %u, largest block %u)\n",
                  s_lastError.c_str(), (unsigned)ESP.getFreeHeap(),
                  (unsigned)largest);
    return false;
  }
  portENTER_CRITICAL(&s_ringMux);
  s_ring.attach(storage, want);
  portEXIT_CRITICAL(&s_ringMux);
  if (want < (size_t)HIVEINSIDE_AUDIO_RING_BYTES) {
    Serial.printf("[HI-AUD] audio buffer trimmed to %u B (asked %u, largest "
                  "free block %u): a slow upload will cost gaps sooner\n",
                  (unsigned)want, (unsigned)HIVEINSIDE_AUDIO_RING_BYTES,
                  (unsigned)largest);
  }

#if ENABLE_BLE_SCAN
  // HiveInside is otherwise only ever a passive beacon, so a short scan is what
  // establishes its address type — the same reason the OTA relay does this.
  uint8_t addrType = BLE_ADDR_PUBLIC;
  if (!blesensor::locateByScan(m, addrType)) {
    s_lastError = "HiveInside not found in scan (powered off or out of range?)";
    Serial.printf("[HI-AUD] %s\n", s_lastError.c_str());
    return false;
  }
  NimBLEAddress addr(m.c_str(), addrType);
  s_client = NimBLEDevice::createClient();
  if (!s_client) {
    s_lastError = "no free BLE client slot for the audio session";
    Serial.printf("[HI-AUD] %s\n", s_lastError.c_str());
    return false;
  }
  s_client->setConnectTimeout(HIVEINSIDE_GATT_CONNECT_TIMEOUT_S * 1000UL);
  if (!s_client->connect(addr)) {
    s_lastError = "BLE connect to HiveInside failed";
    Serial.printf("[HI-AUD] %s\n", s_lastError.c_str());
    return false;
  }
#else
  s_lastError = "audio relay needs ENABLE_BLE_SCAN";
  return false;
#endif

  NimBLERemoteService* svc = s_client->getService(NimBLEUUID(uuid("8e8b0002").c_str()));
  if (!svc) {
    s_lastError = "audio service not found (HiveInside firmware older than 0.6.0?)";
    Serial.printf("[HI-AUD] %s\n", s_lastError.c_str());
    return false;
  }
  s_ctrl = svc->getCharacteristic(NimBLEUUID(uuid("8e8b0020").c_str()));
  s_data = svc->getCharacteristic(NimBLEUUID(uuid("8e8b0021").c_str()));
  s_status = svc->getCharacteristic(NimBLEUUID(uuid("8e8b0023").c_str()));
  if (!s_ctrl || !s_data || !s_status) {
    s_lastError = "audio characteristics missing";
    return false;
  }

  // Subscribe BEFORE START: the node refuses to start (error 0x14) if DATA is
  // not subscribed, because notifying an unsubscribed characteristic silently
  // does nothing and would read as a dead microphone.
  if (!s_data->subscribe(true, onNotify)) {
    s_lastError = "subscribe to audio DATA failed";
    return false;
  }

  // The nonce is fresh per connection and rotates after every session and every
  // failed attempt, so it must be read immediately before signing.
  uint8_t status[STATUS_LEN];
  if (!readStatus(status)) {
    s_lastError = "could not read audio STATUS (firmware older than 0.6.0?)";
    return false;
  }
  const uint8_t* nonce = &status[2];

  // START: op, duration_ds(2 LE), rate_code, format, gain_db, hmac(8).
  // The HMAC covers the nonce followed by those six request bytes exactly.
  uint8_t req[14];
  req[0] = OP_START;
  req[1] = (uint8_t)(durationDs & 0xFF);
  req[2] = (uint8_t)(durationDs >> 8);
  req[3] = 0;  // rate_code: 16 kHz
  req[4] = 0;  // format: PCM16 LE mono
  req[5] = (uint8_t)gainDb;

  uint8_t message[14];
  memcpy(message, nonce, 8);
  memcpy(message + 8, req, 6);

  uint8_t mac_out[32];
  const mbedtls_md_info_t* md = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
  if (!md || mbedtls_md_hmac(md, psk, sizeof(psk), message, sizeof(message),
                             mac_out) != 0) {
    s_lastError = "HMAC computation failed";
    return false;
  }
  memcpy(req + 6, mac_out, 8);

  portENTER_CRITICAL(&s_ringMux);
  s_streaming = true;
  portEXIT_CRITICAL(&s_ringMux);

  if (!s_ctrl->writeValue(req, sizeof(req), /*response=*/true)) {
    portENTER_CRITICAL(&s_ringMux);
    s_streaming = false;
    portEXIT_CRITICAL(&s_ringMux);
    s_lastError = "audio START write failed";
    return false;
  }

  // Held in a named String: a temporary built inside the printf argument list
  // would be destroyed before printf read the pointer.
  String durationLabel = durationDs ? (String(durationDs / 10.0f, 1) + "s")
                                    : String("open-ended");
  Serial.printf("[HI-AUD] session started: %s, %s, gain %+d dB, MTU %u\n",
                m.c_str(), durationLabel.c_str(), (int)gainDb,
                (unsigned)s_client->getMTU());
  return true;
}

size_t read(uint8_t* out, size_t max) {
  if (!out || max == 0) return 0;
  portENTER_CRITICAL(&s_ringMux);
  const size_t n = s_ring.read(out, max);
  portEXIT_CRITICAL(&s_ringMux);
  // CRC outside the lock: it is the caller's own data by now, and there is no
  // reason to hold interrupts off across it.
  if (n) s_crc = crc32Update(s_crc, out, n);
  return n;
}

bool streaming() {
  if (!s_client || !s_client->isConnected()) return false;
  return s_streaming;
}

void stop() {
  if (s_stopSent || !s_ctrl || !s_client || !s_client->isConnected()) return;
  s_stopSent = true;
  uint8_t op = OP_STOP;
  s_ctrl->writeValue(&op, 1, /*response=*/true);
}

bool finish(Stats* out) {
  Stats stats;
  portENTER_CRITICAL(&s_ringMux);
  stats.bytes = s_ring.accepted();
  stats.ringOverruns = s_ring.dropEvents();
  stats.ringDroppedBytes = s_ring.dropped();
  stats.gaps = s_gaps;
  portEXIT_CRITICAL(&s_ringMux);
  stats.crc32 = s_crc;

  uint8_t status[STATUS_LEN];
  bool haveStatus = readStatus(status);
  if (haveStatus) {
    stats.state = status[0];
    stats.error = status[1];
    stats.deviceBytes = (uint32_t)status[10] | ((uint32_t)status[11] << 8) |
                        ((uint32_t)status[12] << 16) | ((uint32_t)status[13] << 24);
    stats.elapsedMs = (uint32_t)status[14] | ((uint32_t)status[15] << 8) |
                      ((uint32_t)status[16] << 16) | ((uint32_t)status[17] << 24);
    stats.droppedBytes = (uint32_t)status[18] | ((uint32_t)status[19] << 8) |
                         ((uint32_t)status[20] << 16) | ((uint32_t)status[21] << 24);
    stats.deviceCrc = (uint32_t)status[22] | ((uint32_t)status[23] << 8) |
                      ((uint32_t)status[24] << 16) | ((uint32_t)status[25] << 24);
    stats.sampleRate = (uint16_t)status[26] | ((uint16_t)status[27] << 8);
    stats.format = status[28];
    stats.clippedPct = status[29];
  }
  if (out) *out = stats;

  if (!haveStatus) {
    // The link went down before we could ask. Whatever arrived is still a valid
    // recording — say so rather than discarding it.
    s_lastError = "link dropped before the final audio STATUS could be read";
    Serial.printf("[HI-AUD] %s (%u B received)\n", s_lastError.c_str(),
                  (unsigned)stats.bytes);
    return false;
  }

  Serial.printf("[HI-AUD] session end: state=0x%02X err=0x%02X received=%u "
                "node=%u dropped=%u gaps=%u overruns=%u (%u B) clipped=%u%%\n",
                stats.state, stats.error, (unsigned)stats.bytes,
                (unsigned)stats.deviceBytes, (unsigned)stats.droppedBytes,
                (unsigned)stats.gaps, (unsigned)stats.ringOverruns,
                (unsigned)stats.ringDroppedBytes, (unsigned)stats.clippedPct);

  if (stats.error >= ERR_FIRST) {
    s_lastError = String("HiveInside reported: ") + errorText(stats.error) +
                  " (0x" + String(stats.error, HEX) + ")";
    return false;
  }
  if (stats.state != STATE_DONE && !s_sawFinal) {
    s_lastError = "session ended without a final packet";
    return false;
  }
  if (stats.bytes == 0) {
    s_lastError = "no audio received";
    return false;
  }
  return true;
}

}  // namespace gattaudio

#endif  // HIVEINSIDE_AUDIO_ENABLED
