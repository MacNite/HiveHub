// gatt_ota.h — streaming firmware-over-BLE relay (GATT client), shared by the
// HiveInside and HiveTraffic sub-devices.
//
// HiveHub is the only node with WiFi, so it relays firmware to its BLE
// sub-devices: it STREAMS the HTTPS download straight into the device's OTA
// characteristics, chunk by chunk, so a >1 MB image never has to fit in the
// WROOM's RAM. The bytes are forwarded opaquely — this path never applies the
// ESP32 self-OTA architecture guard (esp_image_header magic / chip-id), which is
// only meaningful for the hub's own image. Each device verifies the end-to-end
// CRC-32 (sent in BEGIN) against what it received before swapping its OTA slot,
// so a corrupted relay can never brick one; it aborts and keeps running the old
// image.
//
// HiveInside (nRF54 MCUboot) and HiveTraffic (ESP32-C6) deliberately expose the
// SAME framing — BEGIN/DATA/END/ABORT plus a 6-byte STATUS — so one state
// machine drives both. They differ only in UUIDs and in how the peer is
// located, which is what Target below captures.
//
// Session lifecycle (see hivehub_network.cpp::relayFirmwareOverGatt):
//   begin(target,mac,size,crc) → write(chunk)… → finish() → cleanup()
// The NimBLE stack is brought up inside begin() and torn down in cleanup(),
// so it coexists with the WiFi download rather than holding the radio open.

#pragma once

#include <Arduino.h>
#include "config.h"

#if GATT_OTA_ENABLED

namespace gattota {

// One relay-able sub-device kind. Instances are defined in gatt_ota.cpp and
// selected by the command handler; nothing else in the firmware hard-codes an
// OTA UUID.
struct Target {
  const char* service;          // service holding the three OTA characteristics
  const char* ctrl;             // WRITE: framed control (BEGIN/END/ABORT)
  const char* data;             // WRITE: payload stream
  const char* status;           // READ/NOTIFY: state(1) + received(4) + err(1)
  uint32_t    connectTimeoutS;  // per connection attempt
  size_t      chunkMax;         // hard cap on a DATA write, before MTU clamping
  // Locate the peer with a short scan before connecting, to learn its address
  // type. HiveInside needs this (it is otherwise only ever a passive beacon, so
  // nothing else establishes the type); HiveTraffic does not — the relay just
  // tries public then random, exactly as bee_counter_client.cpp does for
  // measurements. Honoured only when ENABLE_BLE_SCAN is compiled in.
  bool        locateByScan;
  const char* logTag;           // e.g. "HI-OTA" — prefixes every serial line
  const char* deviceLabel;      // e.g. "HiveInside" — appears in error messages
};

#if ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED
extern const Target HIVEINSIDE;
#endif
#if ENABLE_WIRELESS_BEECOUNTER && BEECOUNTER_OTA_ENABLED
extern const Target BEECOUNTER;
#endif

// Bring up the BLE stack, locate/connect `mac`, resolve the target's three
// characteristics and send BEGIN (image size + end-to-end CRC-32). Returns false
// (and cleans up) on any failure. On success the session stays open for
// write()/finish().
bool begin(const Target& target, const String& mac, uint32_t totalLen,
           uint32_t crc32);
// Relay one buffer of firmware bytes, in order. Splits it across as many GATT
// writes as the negotiated MTU needs; each write is flow-controlled by the
// device's ATT response, which it only sends once the chunk is flashed. Returns
// false on the first failed write.
bool write(const uint8_t* data, size_t len);
// Send END and wait for the device to confirm DONE (size + CRC verified, slot
// swapped). Returns true only on a confirmed DONE.
bool finish();
// Best-effort cancel of an in-progress transfer, so a partial relay leaves the
// device on its old image.
void abort();
// Disconnect and tear the BLE stack down. Always call this to end a session,
// success or failure.
void cleanup();
// Human-readable reason for the most recent failure (e.g. "HiveInside not found
// in scan", "BLE connect to HiveTraffic counter failed", "…rejected image"). Set
// by begin/write/finish and cleared at the start of each begin(), so the caller
// can report a specific cause instead of a bare boolean.
const String& lastError();

}  // namespace gattota

#endif  // GATT_OTA_ENABLED
