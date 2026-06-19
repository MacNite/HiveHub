// ble_sensor.h — passive BLE bridge for the HolyIot 25015 in-hive sensor.
//
// The HolyIot 25015 (nRF54L15) is a battery BLE beacon that broadcasts the
// readings of three on-board sensors:
//   - SHT40   : temperature + relative humidity
//   - LPS22HB : barometric pressure
//   - LIS2DH12: 3-axis acceleration
//
// The ESP32 never connects to the device. Once per upload cycle it runs a short
// passive scan (HOLYIOT_BLE_SCAN_SECONDS), matches advertisements against the
// one or two MAC addresses paired in the provisioning portal, parses the
// manufacturer-specific payload and folds the values into the measurement JSON:
//
//   slot 1 -> hive 1   (bleSensorMac0)
//   slot 2 -> hive 2   (bleSensorMac1)
//
// Per the data-model decision, the acceleration is reported through the existing
// accel_{slot}_* measurement fields (ok / rms_mg / peak_mg / sample_count /
// range_g); temperature, humidity and pressure are reported through new
// ble_{slot}_* fields. Because a passive beacon only emits periodic single-shot
// samples, no FFT bands are produced — the server runs a low-rate pre-swarm
// detector on the per-cycle acceleration magnitude instead.
//
// The whole feature is compiled out unless ENABLE_BLE_SCAN is set.
#pragma once

#include <Arduino.h>
#include "config.h"

#if ENABLE_BLE_SCAN

#include <ArduinoJson.h>
#include <vector>

namespace blesensor {

// Which kind of in-hive BLE sensor produced an advertisement. Both share the
// passive scan bridge; the format is auto-detected from the manufacturer data.
enum class SensorType : uint8_t {
  None       = 0,
  HolyIot    = 1,   // HolyIot 25015 beacon: temp/humidity/pressure/raw accel
  HiveInside = 2,   // HiveInside ESP32-C6: + vibration & acoustic FFT bands
  Ruuvi      = 3,   // RuuviTag beacon: temp/humidity/pressure/raw accel
};

const char* sensorTypeName(SensorType t);

// One per-hive sensor snapshot, captured each upload cycle. Acceleration is in
// milli-g (mg); *_rms_mg / *_peak_mg are the AC magnitude (gravity removed)
// across the advertisements seen during the scan window.
struct Snapshot {
  bool       present       = false;  // a matching advertisement was received
  SensorType type          = SensorType::None;
  int        rssi_dbm      = 0;      // last advertisement RSSI
  uint16_t   sample_count  = 0;      // advertisements parsed during the scan

  float    temp_c        = NAN;
  float    humidity_pct  = NAN;
  float    pressure_hpa  = NAN;      // HolyIot only

  float    accel_x_mg    = NAN;    // last raw sample (HolyIot)
  float    accel_y_mg    = NAN;
  float    accel_z_mg    = NAN;
  float    accel_rms_mg  = NAN;    // RMS of |a|-baseline (HolyIot) or device RMS
  float    accel_peak_mg = NAN;    // peak |a|-baseline over the samples seen

  // Vibration FFT bands in mg (HiveInside only; the device runs the FFT).
  float    accel_band_swarm_mg    = NAN;  //   8–30 Hz pre-swarm
  float    accel_band_fanning_mg  = NAN;  //  30–100 Hz fanning
  float    accel_band_activity_mg = NAN;  // 100–200 Hz activity

  // Acoustics in dBFS (HiveInside only).
  bool     mic_present   = false;
  float    mic_rms_dbfs  = NAN;
  float    mic_sub_bass_dbfs = NAN;  //   50–150 Hz
  float    mic_hum_dbfs      = NAN;  //  150–300 Hz
  float    mic_piping_dbfs   = NAN;  //  300–550 Hz
  float    mic_stress_dbfs   = NAN;  //  550–1500 Hz
  float    mic_high_dbfs     = NAN;  // 1500–3000 Hz

  int      battery_pct   = -1;     // -1 = not reported

  // Capability helpers used by the wired/BLE arbitration in sensors.cpp.
  bool providesTemp()  const { return present && !isnan(temp_c); }
  bool providesAccel() const { return present && !isnan(accel_rms_mg); }
  bool providesMic()   const { return present && mic_present; }
};

// One discovered device during a portal pairing scan.
struct Discovered {
  String     mac;
  String     name;
  int        rssi_dbm = 0;
  SensorType type = SensorType::None;  // recognised in-hive sensor format, if any
  bool       looks_like_holyiot = false;  // kept for back-compat (any known type)
};

// Run a single passive scan and fill the snapshots for the two paired MACs.
// Either MAC may be empty (""), in which case that slot stays !present. Safe to
// call every cycle; it initialises and de-initialises the BLE stack each time
// so it coexists cleanly with the WiFi upload that follows.
void scanPairedSensors(const String& mac0, const String& mac1,
                       Snapshot& slot1, Snapshot& slot2);

// Portal helper: scan for all nearby BLE devices so the user can pick which to
// pair. HolyIot-looking devices are flagged. Used by the provisioning portal.
std::vector<Discovered> discover(uint32_t seconds);

// Serialize a snapshot into the measurement JSON. Writes the new ble_{slot}_*
// humidity/pressure/accel-raw/battery fields and mirrors the acceleration into
// the existing accel_{slot}_* fields (ok / rms_mg / peak_mg / sample_count /
// range_g). Temperature is NOT written here — sensors.cpp owns hive_{slot}_temp_c
// so it can choose between the wired DS18B20 and this sensor.
void writeSnapshotToJson(JsonDocument& doc, uint8_t slot, const Snapshot& snap);

// Normalise a MAC string ("aa:bb:..", upper/lower, spaces) to "AA:BB:CC:DD:EE:FF"
// or "" when it is not a valid 6-byte MAC. Shared by the portal and matcher.
String normalizeMac(const String& raw);

#if HIVEINSIDE_OTA_ENABLED
// ── HiveInside firmware-over-BLE relay (streaming GATT-client session) ──────
// Bring up the BLE stack, locate `mac`, connect to its OTA service and send the
// BEGIN frame (image size + end-to-end CRC-32). Returns false (and cleans up) on
// any failure. On success the session stays open for otaWrite()/otaFinish().
bool otaBegin(const String& mac, uint32_t totalLen, uint32_t crc32);
// Relay one buffer of firmware bytes, in order. Splits across as many GATT
// writes as the negotiated MTU needs; each write is flow-controlled by the
// device's ATT response. Returns false on the first failed write.
bool otaWrite(const uint8_t* data, size_t len);
// Send END and wait for the device to confirm DONE (CRC verified, slot swapped).
// Returns true only on a confirmed DONE.
bool otaFinish();
// Best-effort cancel of an in-progress transfer (device keeps its old image).
void otaAbort();
// Disconnect and tear the BLE stack down. Always call this to end a session,
// success or failure.
void otaCleanup();
#endif

}  // namespace blesensor

#endif  // ENABLE_BLE_SCAN
