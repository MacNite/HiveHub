// gatt_ota.cpp — streaming firmware-over-BLE relay shared by HiveInside and
// HiveTraffic. See gatt_ota.h for the contract and the rationale.
//
// This was previously blesensor::ota* inside ble_sensor.cpp, hard-coded to the
// HiveInside UUIDs. It lives in its own translation unit now because the
// BeeCounter relay must exist on devices built WITHOUT ENABLE_BLE_SCAN — the
// configurator only enables the scan for beacon sensors, so a HiveTraffic-only
// hub has it off — and ble_sensor.cpp is compiled out entirely in that case.

#include "gatt_ota.h"

#if GATT_OTA_ENABLED

#include <NimBLEDevice.h>

#include "ble_stack.h"

#if ENABLE_BLE_SCAN
#include "ble_sensor.h"   // normalizeMac + the locate-scan helper
#endif

namespace gattota {

#if ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED
// Must match HiveInside firmware/src/ble_link.cpp. HiveInside puts OTA in its
// own service, separate from the measurement beacon.
const Target HIVEINSIDE = {
    "8e8b0001-7a1c-4b9e-9a2f-1d6e0b9c1a01",
    "8e8b0010-7a1c-4b9e-9a2f-1d6e0b9c1a01",
    "8e8b0011-7a1c-4b9e-9a2f-1d6e0b9c1a01",
    "8e8b0013-7a1c-4b9e-9a2f-1d6e0b9c1a01",
    HIVEINSIDE_GATT_CONNECT_TIMEOUT_S,
    HIVEINSIDE_OTA_CHUNK_MAX,
    /*locateByScan=*/true,
    "HI-OTA",
    "HiveInside",
};
#endif

#if ENABLE_WIRELESS_BEECOUNTER && BEECOUNTER_OTA_ENABLED
// Must match HiveTraffic Firmware/src/ble_link.cpp. The OTA characteristics sit
// in the measurement service — HiveTraffic exposes only one.
const Target BEECOUNTER = {
    BEECOUNTER_GATT_SERVICE_UUID,
    BEECOUNTER_GATT_OTA_CTRL_UUID,
    BEECOUNTER_GATT_OTA_DATA_UUID,
    BEECOUNTER_GATT_OTA_STATUS_UUID,
    BEECOUNTER_GATT_CONNECT_TIMEOUT_S,
    BEECOUNTER_OTA_CHUNK_MAX,
    // No locate-scan: HiveTraffic is a connectable peripheral that the
    // measurement path already reaches by trying public then random on a direct
    // connect, and that costs one fewer radio phase than a scan.
    /*locateByScan=*/false,
    "BC-OTA",
    "HiveTraffic counter",
};
#endif

// Control opcodes (first byte of a CTRL write). Identical on both devices.
static constexpr uint8_t OP_BEGIN = 0x01;  // + size(4 LE) + crc32(4 LE)
static constexpr uint8_t OP_END   = 0x03;  // finalize + verify + reboot
static constexpr uint8_t OP_ABORT = 0x04;  // cancel, stay on the current image

// STATUS state byte. Anything >= 0x10 is an error code.
static constexpr uint8_t STATE_DONE      = 0x02;
static constexpr uint8_t STATE_ERR_FIRST = 0x10;

namespace {
const Target*               s_target = nullptr;
NimBLEClient*               s_client = nullptr;
NimBLERemoteCharacteristic* s_ctrl   = nullptr;
NimBLERemoteCharacteristic* s_data   = nullptr;
NimBLERemoteCharacteristic* s_status = nullptr;
size_t                      s_chunk  = 20;  // negotiated DATA payload size
String                      s_lastError;

size_t                      s_sentTotal = 0;  // bytes accepted this session

const char* tag() { return s_target ? s_target->logTag : "GATT-OTA"; }
const char* label() { return s_target ? s_target->deviceLabel : "device"; }

// Plain-English name for a STATUS error byte. Both devices share the code
// space (see gatt_ota.h), so one table serves both.
const char* stateText(uint8_t state) {
  switch (state) {
    case 0x10: return "BEGIN rejected (image too big for its slot, or flash init failed)";
    case 0x11: return "out of sequence (session not armed, or already ended)";
    case 0x12: return "flash write failed";
    case 0x13: return "CRC mismatch";
    case 0x14: return "size mismatch";
    case 0x15: return "END/verify failed";
    default:   return "unknown error";
  }
}

// Ask the device what it thinks happened. Returns false when STATUS could not
// be read at all — which is itself the answer, because it means the link is
// gone rather than the device having rejected anything.
bool readStatus(uint8_t& state, uint8_t& err, uint32_t& received) {
  if (!s_status || !s_status->canRead()) return false;
  if (!s_client || !s_client->isConnected()) return false;
  std::string s = s_status->readValue();
  if (s.size() < 6) return false;
  state = (uint8_t)s[0];
  err = (uint8_t)s[5];
  received = ((uint32_t)(uint8_t)s[1]) | ((uint32_t)(uint8_t)s[2] << 8) |
             ((uint32_t)(uint8_t)s[3] << 16) | ((uint32_t)(uint8_t)s[4] << 24);
  return true;
}
}  // namespace

const String& lastError() { return s_lastError; }

void cleanup() {
  if (s_client) {
    if (s_client->isConnected()) s_client->disconnect();
    NimBLEDevice::deleteClient(s_client);
    s_client = nullptr;
  }
  s_ctrl = s_data = s_status = nullptr;
  s_target = nullptr;
  // See ble_stack.h for why this is never deinit(true). The controller is
  // fully freed either way.
  blestack::release();
}

// Open the link. Two strategies, per Target::locateByScan — see gatt_ota.h.
// Returns false with s_lastError set.
static bool connectTo(const String& mac) {
// Guarded on HIVEINSIDE_OTA_ENABLED too, not just the scan: locateByScan is
// declared under that flag, and HIVEINSIDE is the only target that sets
// locateByScan, so a beecounter-only build with the scan on must not reference it.
#if ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED
  if (s_target->locateByScan) {
    uint8_t addrType = BLE_ADDR_PUBLIC;
    if (!blesensor::locateByScan(mac, addrType)) {
      Serial.printf("[%s] device %s not found in scan\n", tag(), mac.c_str());
      s_lastError = String(label()) +
                    " not found in scan (powered off or out of range?)";
      // The caller cleans up (which deinits); nothing to undo here.
      return false;
    }
    NimBLEAddress addr(mac.c_str(), addrType);
    s_client = NimBLEDevice::createClient();
    s_client->setConnectTimeout(s_target->connectTimeoutS * 1000UL);
    Serial.printf("[%s] connecting to %s ...\n", tag(), addr.toString().c_str());
    if (!s_client->connect(addr)) {
      Serial.printf("[%s] connect failed\n", tag());
      s_lastError = String("BLE connect to ") + label() + " failed";
      return false;
    }
    return true;
  }
#endif
  // Direct connect. A seeded MAC may be either type and nothing has told us
  // which, so try both — the same two-pass approach bee_counter_client.cpp uses.
  s_client = NimBLEDevice::createClient();
  s_client->setConnectTimeout(s_target->connectTimeoutS * 1000UL);
  const uint8_t addrTypes[2] = { BLE_ADDR_PUBLIC, BLE_ADDR_RANDOM };
  for (int t = 0; t < 2; t++) {
    NimBLEAddress addr(std::string(mac.c_str()), addrTypes[t]);
    Serial.printf("[%s] connecting to %s (addr type %u) ...\n",
                  tag(), mac.c_str(), addrTypes[t]);
    if (s_client->connect(addr)) return true;
  }
  Serial.printf("[%s] connect failed for %s\n", tag(), mac.c_str());
  s_lastError = String("BLE connect to ") + label() +
                " failed (powered off or out of range?)";
  return false;
}

bool begin(const Target& target, const String& mac, uint32_t totalLen,
           uint32_t crc32) {
  s_lastError = "";
  s_sentTotal = 0;
  s_target = &target;
#if ENABLE_BLE_SCAN
  String m = blesensor::normalizeMac(mac);
#else
  String m = mac;
  m.toUpperCase();
#endif
  if (m.length() == 0 || totalLen == 0) {
    Serial.printf("[%s] bad arguments\n", tag());
    s_lastError = "bad OTA arguments (mac/size)";
    s_target = nullptr;
    return false;
  }

  // A relay only ever connects — it never scans — so it is safe in any port
  // lifetime. Locating the peer may still need a scan; blesensor::locateByScan
  // answers from this cycle's measurement scan instead wherever it can.
  blestack::acquire();
  NimBLEDevice::setMTU(247);  // ask for a larger ATT MTU; the device may grant less

  if (!connectTo(m)) {
    cleanup();
    return false;
  }

  NimBLERemoteService* svc = s_client->getService(target.service);
  s_ctrl   = svc ? svc->getCharacteristic(target.ctrl)   : nullptr;
  s_data   = svc ? svc->getCharacteristic(target.data)   : nullptr;
  s_status = svc ? svc->getCharacteristic(target.status) : nullptr;
  if (!svc || !s_ctrl || !s_data) {
    Serial.printf("[%s] OTA service/characteristics not found "
                  "(%s firmware too old for BLE OTA?)\n", tag(), label());
    s_lastError = String("OTA characteristics not found (") + label() +
                  " firmware too old?)";
    cleanup();
    return false;
  }

  uint16_t mtu = s_client->getMTU();
  s_chunk = (mtu > 3) ? (size_t)(mtu - 3) : 20;
  if (s_chunk > target.chunkMax) s_chunk = target.chunkMax;
  Serial.printf("[%s] connected: MTU=%u chunk=%u image=%u bytes crc=0x%08X\n",
                tag(), (unsigned)mtu, (unsigned)s_chunk, (unsigned)totalLen,
                (unsigned)crc32);

  uint8_t beg[9];
  beg[0] = OP_BEGIN;
  beg[1] = totalLen & 0xFF;         beg[2] = (totalLen >> 8) & 0xFF;
  beg[3] = (totalLen >> 16) & 0xFF; beg[4] = (totalLen >> 24) & 0xFF;
  beg[5] = crc32 & 0xFF;            beg[6] = (crc32 >> 8) & 0xFF;
  beg[7] = (crc32 >> 16) & 0xFF;    beg[8] = (crc32 >> 24) & 0xFF;
  if (!s_ctrl->writeValue(beg, sizeof(beg), /*response=*/true)) {
    Serial.printf("[%s] BEGIN write failed\n", tag());
    s_lastError = "OTA BEGIN write failed";
    cleanup();
    return false;
  }
  return true;
}

bool write(const uint8_t* data, size_t len) {
  if (!s_data) return false;
  size_t sent = 0;
  while (sent < len) {
    size_t n = len - sent;
    if (n > s_chunk) n = s_chunk;
    if (!s_data->writeValue(data + sent, n, /*response=*/true)) {
      // writeValue() returns false for two very different situations: the link
      // is gone, or the device answered with an ATT error because it rejected
      // the stream (wrong state, size overflow, flash write failure — see
      // stateText). Reporting "BLE link lost?" for both sent every one of them
      // to the dashboard as a radio problem. Ask the device before guessing.
      s_sentTotal += sent;
      Serial.printf("[%s] DATA write failed after %u bytes this buffer "
                    "(%u total)\n", tag(), (unsigned)sent,
                    (unsigned)s_sentTotal);

      uint8_t state = 0, err = 0;
      uint32_t received = 0;
      if (!readStatus(state, err, received)) {
        Serial.printf("[%s] STATUS unreadable — link is down\n", tag());
        s_lastError = String("OTA DATA write failed after ") + s_sentTotal +
                      " bytes (BLE link lost)";
      } else if (state >= STATE_ERR_FIRST) {
        Serial.printf("[%s] device rejected the stream: state=0x%02X err=0x%02X "
                      "received=%u (%s)\n", tag(), state, err,
                      (unsigned)received, stateText(state));
        s_lastError = String(label()) + " rejected the stream after " +
                      received + " bytes: " + stateText(state) + " (state=0x" +
                      String(state, HEX) + ")";
      } else {
        // Link up, device not in an error state, write still refused. Nothing
        // is being guessed here — say exactly that.
        Serial.printf("[%s] write refused but device reports state=0x%02X "
                      "received=%u\n", tag(), state, (unsigned)received);
        s_lastError = String("OTA DATA write failed after ") + received +
                      " bytes; " + label() + " reports no error (state=0x" +
                      String(state, HEX) + ")";
      }
      return false;
    }
    sent += n;
  }
  s_sentTotal += sent;
  return true;
}

// Send END, then poll STATUS for DONE. Both devices reboot ~1.5 s after they
// report DONE, so a dropped link right after a DONE read is still success.
bool finish() {
  if (!s_ctrl) return false;
  uint8_t op = OP_END;
  if (!s_ctrl->writeValue(&op, 1, /*response=*/true)) {
    Serial.printf("[%s] END write failed\n", tag());
    s_lastError = "OTA END write failed";
    return false;
  }
  if (!s_status || !s_status->canRead()) {
    Serial.printf("[%s] no STATUS characteristic — assuming success\n", tag());
    return true;
  }
  for (int i = 0; i < 25; i++) {
    std::string s = s_status->readValue();
    if (s.size() >= 6) {
      uint8_t state = (uint8_t)s[0];
      uint8_t err   = (uint8_t)s[5];
      if (state == STATE_DONE) {
        Serial.printf("[%s] device reports DONE — it will reboot into the new image\n",
                      tag());
        return true;
      }
      if (state >= STATE_ERR_FIRST) {
        Serial.printf("[%s] device reported error state=0x%02X err=0x%02X (%s)\n",
                      tag(), state, err, stateText(state));
        // Name the actual code rather than offering "CRC/size mismatch?" as a
        // guess — the device already told us which of the six it was.
        s_lastError = String(label()) + " rejected image: " + stateText(state) +
                      " (state=0x" + String(state, HEX) + " err=0x" +
                      String(err, HEX) + ")";
        return false;
      }
    }
    if (!s_client || !s_client->isConnected()) {
      // Link dropped before we read DONE; treat as an inconclusive failure so
      // the caller reports it and the backend can re-queue.
      Serial.printf("[%s] link dropped before DONE confirmation\n", tag());
      s_lastError = "BLE link dropped before DONE confirmation";
      return false;
    }
    delay(200);
  }
  Serial.printf("[%s] timed out waiting for DONE\n", tag());
  s_lastError = String("timed out waiting for ") + label() + " DONE";
  return false;
}

void abort() {
  if (s_ctrl) {
    uint8_t op = OP_ABORT;
    s_ctrl->writeValue(&op, 1, true);
  }
}

}  // namespace gattota

#endif  // GATT_OTA_ENABLED
