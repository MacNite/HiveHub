// gatt_audio.h — GATT client for HiveInside's authenticated audio stream.
//
// HiveInside 0.6.0 and later expose an on-request microphone: an authenticated
// START makes the node capture 16 kHz PCM16 and push it out as GATT
// notifications. HiveHub is the only node with WiFi, so it relays that stream
// to the backend exactly as it relays firmware in the other direction — see
// gatt_ota.h, whose Target/begin/…/cleanup shape this mirrors.
//
// The protocol is documented in HiveInside docs/audio-over-ble.md. In short:
//
//   service  8e8b0002-7a1c-4b9e-9a2f-1d6e0b9c1a01
//   CTRL     8e8b0020-…  write   0x01 START | 0x02 STOP
//   DATA     8e8b0021-…  notify  seq(2 LE) flags(1) reserved(1) PCM16 LE
//   STATUS   8e8b0023-…  read/notify  30-byte session record
//
// Three things about this transport differ from the OTA one and drive the
// design here:
//
//   * It is INBOUND. Notifications arrive on the NimBLE host task, which must
//     never block on TLS, so they are copied into a ring buffer that the caller
//     drains from its own loop. Sizing that ring is the whole trick: the node
//     produces 32 kB/s and does not wait for us.
//   * It is AUTHENTICATED. The node issues a nonce in STATUS; START must carry
//     HMAC-SHA256(psk, nonce || op || duration | rate | format | gain)
//     truncated to eight bytes. Without a provisioned key the node fails closed
//     and no recording is possible — that is deliberate, not a bug to work
//     around.
//   * It may be OPEN-ENDED. durationDs == 0 means "stream until STOP", which is
//     what the dashboard's live listening uses. The node still stops itself at
//     60 s, so a lost STOP costs one minute of audio, never an open microphone.
//
// Lifecycle:
//   begin(mac, durationDs, gainDb) → read(buf,n)… while streaming() → stop()
//   → finish(&stats) → cleanup()
// cleanup() releases the NimBLE stack and the ring, and is safe to call at any
// point after begin() — including after a failed begin().
//
// begin() brings the radio up itself, and the caller should already have its
// TLS session open when it does. 0.30.6 inverted that and the handshake failed
// underneath the controller; hivehub_network.cpp carries the field evidence.
#pragma once

#include <Arduino.h>
#include "config.h"

#if HIVEINSIDE_AUDIO_ENABLED

namespace gattaudio {

// What the node reported at the end of a session, next to what we actually
// received. The pairs exist so a caller can tell a slow link from a lossy one:
// bytes/crc32 are ours, deviceBytes/deviceCrc are what the node handed to its
// controller, and droppedBytes is audio the node's ring lost before it was ever
// transmitted. A clean recording has bytes == deviceBytes, crc32 == deviceCrc,
// and droppedBytes == 0.
struct Stats {
  uint32_t bytes = 0;         // PCM bytes this hub received
  uint32_t crc32 = 0;         // CRC-32 over those bytes
  uint32_t deviceBytes = 0;   // STATUS: PCM bytes the node transmitted
  uint32_t deviceCrc = 0;     // STATUS: CRC-32 the node computed
  uint32_t droppedBytes = 0;  // STATUS: audio lost to the node's ring overrun
  uint32_t elapsedMs = 0;     // STATUS: session duration on the node
  uint32_t gaps = 0;          // sequence discontinuities this hub observed
  // Seams this hub introduced: one per notification the staging ring could not
  // take, whether because the upload was behind or because the session was
  // being torn down around it. Counted in packets, like `gaps`, because that is
  // what the two are summed into for the backend.
  uint32_t ringOverruns = 0;
  uint32_t ringDroppedBytes = 0;  // how much PCM those seams cost, for the log
  uint16_t sampleRate = 0;    // STATUS: 16000
  uint8_t  state = 0;         // STATUS: 0 idle, 1 armed, 2 streaming, 3 done
  uint8_t  error = 0;         // STATUS: >= 0x10 is fatal
  uint8_t  format = 0;        // STATUS: 0 = PCM16 LE mono
  uint8_t  clippedPct = 0;    // STATUS: samples that hit the rail, percent
};

// Plain-English name for a STATUS error byte.
const char* errorText(uint8_t error);

// Connect, authenticate and start capture. `durationDs` is deciseconds, 0 for
// open-ended. `gainDb` is clamped by the node to -20..+20. Returns false with
// lastError() set; the caller must still call cleanup().
//
// The node arms a 10-second window at connect and cancels it only when START
// claims the link, so everything between those two points is on a clock: do
// not put unrelated work inside this call.
bool begin(const String& mac, uint16_t durationDs, int8_t gainDb);

// Drain up to `max` PCM bytes. Returns 0 when nothing has arrived yet — that is
// normal and not an error; poll again. Never blocks.
size_t read(uint8_t* out, size_t max);

// True while the node is still expected to send audio: no final packet, no
// error, link still up. Drain until this goes false AND read() returns 0.
bool streaming();

// Ask the node to end the session early (the live-listening stop button). Safe
// to call more than once, and safe on an already-finished session.
void stop();

// Read the final STATUS and fill `out`. Returns false when the node reported an
// error state or STATUS could not be read; lastError() explains which.
bool finish(Stats* out);

// Tear the session down: unsubscribe, disconnect, free the ring, release the
// BLE stack.
void cleanup();

const String& lastError();

}  // namespace gattaudio

#endif  // HIVEINSIDE_AUDIO_ENABLED
