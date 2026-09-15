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
  HiveInside = 2,   // HiveInside (nRF54LM20A): + vibration & acoustic FFT bands
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
                                   // (HiveInside reports its own, frame v2+)

  // Vibration FFT bands in mg (HiveInside only; the device runs the FFT).
  float    accel_band_swarm_mg    = NAN;  //   8–30 Hz pre-swarm
  float    accel_band_fanning_mg  = NAN;  //  30–100 Hz fanning
  float    accel_band_activity_mg = NAN;  // 100–200 Hz activity

  // Acoustics in dBFS (HiveInside only).
  bool     mic_present   = false;
  float    mic_rms_dbfs  = NAN;
  float    mic_peak_dbfs = NAN;      // broadband peak deviation (frame v2+)
  float    mic_sub_bass_dbfs = NAN;  //   50–150 Hz
  float    mic_hum_dbfs      = NAN;  //  150–300 Hz
  float    mic_piping_dbfs   = NAN;  //  300–550 Hz
  float    mic_stress_dbfs   = NAN;  //  550–1500 Hz
  float    mic_high_dbfs     = NAN;  // 1500–3000 Hz

  int      battery_pct   = -1;     // -1 = not reported
  int      battery_mv    = -1;     // HiveInside raw cell voltage in mV (-1 = not reported)

  // Running firmware version the nRF54 HiveInside advertises in its scan-response
  // identity record (the 'I' manufacturer element). Empty for HolyIot/Ruuvi
  // beacons, which carry no firmware field.
  String   fw_version;

  // Board/architecture from the same identity record (currently only
  // "nrf54lm20a"). HiveHub forwards it so the backend can confirm it is relaying
  // the nRF54 HiveInside image. Empty for HolyIot/Ruuvi beacons.
  String   board;

  // Local name the node advertises, e.g. "HiveInside-8A3F" — the last two bytes
  // of its BLE address, which is what tells two nodes in the same yard apart.
  // It rides in the scan response, so it is only captured when the scan is
  // active (HOLYIOT_BLE_ACTIVE_SCAN, on by default); a passive scan leaves it
  // empty, as does a node running firmware older than the suffix, which
  // advertises the bare "HiveInside" every other node also uses.
  String   device_name;

  // The paired MAC this snapshot was taken for, normalized. Always known —
  // it is the address HiveHub scanned for — so it is the identity fallback for
  // a node that reports no distinguishing name of its own.
  String   mac;

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

// Multi-hive passive scan. ONE scan window matches every MAC in `macs` (so any
// number of beacons costs the same airtime). Every in-hive sensor on this bridge
// (HolyIot, RuuviTag, nRF54 HiveInside) is a passive beacon, so a single scan
// captures all of them — measurements and, for HiveInside, its board/firmware
// identity record — with no per-sensor GATT connection. out[] is resized to
// macs.size(); out[i] corresponds to macs[i].
void scanPairedSensorsMulti(const std::vector<String>& macs,
                            std::vector<Snapshot>& out);

// Portal helper: scan for all nearby BLE devices so the user can pick which to
// pair. HolyIot-looking devices are flagged. Used by the provisioning portal.
//
// Returns an EMPTY list — not a fault — when a scan cannot safely run in the
// current NimBLE port lifetime (see ble_stack.h). An empty list therefore has
// two very different meanings, and "no sensors nearby" is only one of them; ask
// discoveryAvailable() BEFORE scanning to tell them apart.
std::vector<Discovered> discover(uint32_t seconds);

// True when discover() would actually run a scan right now. False once the
// measurement scan has already claimed the NimBLEScan singleton in a port
// lifetime that has since been torn down — the state every hub with a paired
// sensor is in by the time an upload cycle ends. The portal checks this before
// opening the AP: an "unavailable" answer is a reason to reboot into a clean
// boot, never a reason to show an empty device list as if the air were quiet.
bool discoveryAvailable();

// NOTE: writeSnapshotToJson() — the flat two-slot serializer that wrote
// ble_{slot}_*, accel_{slot}_* and mic_{left,right}_* at the document root — is
// gone. It had not been called since the hives[] array became the upload shape:
// sensors.cpp only ever calls writeSnapshotToHive() below. Because it was the
// only thing that would have written mic_left_*/mic_right_* from a beacon, its
// quiet death is what left the server's acoustic insight detectors with nothing
// to read on every HiveInside deployment — the wired stereo mics that also fill
// those keys default to off, and BLE_OVERRIDE_MICS suppresses them anyway. The
// server now reads the per-hive mic_{n}_* keys the read layer already
// synthesizes from hives[].mic, so there is nothing left for a flat serializer
// to do; resurrecting it would only put the same values under a second name.

// Per-hive form for the hives[] array: writes nested "ble", "accel" and (when
// present) "mic" sub-objects into `hive`. Temperature is owned by the caller
// (sensors.cpp) so the wired/BLE arbitration can pick the source.
void writeSnapshotToHive(JsonObject hive, const Snapshot& snap);

// Normalise a MAC string ("aa:bb:..", upper/lower, spaces) to "AA:BB:CC:DD:EE:FF"
// or "" when it is not a valid 6-byte MAC. Shared by the portal and matcher.
String normalizeMac(const String& raw);

// Compiled for either consumer: the firmware relay (src/gatt_ota.cpp) and the
// audio relay (src/gatt_audio.cpp) both need it, and either one may be switched
// off without the other.
#if HIVEINSIDE_OTA_ENABLED || HIVEINSIDE_AUDIO_ENABLED
// ── Locate-scan for the BLE relays (src/gatt_ota.cpp, src/gatt_audio.cpp) ──
// Run a short scan for `mac` and report the address type it advertised with, so
// the relay can open a GATT connection to it. Returns false when the node was
// not seen (powered off or out of range), leaving `addrTypeOut` untouched.
//
// Only HiveInside needs this: an nRF54 node is otherwise a passive beacon, so
// nothing else establishes its address type. HiveTraffic is reached by a direct
// connect instead — see gattota::Target::locateByScan.
//
// The relay session itself (begin/write/finish/abort/cleanup) is NOT here: it
// moved to gatt_ota.h, which is shared with the BeeCounter relay and must
// compile on devices built without ENABLE_BLE_SCAN.
bool locateByScan(const String& mac, uint8_t& addrTypeOut);
#endif  // HIVEINSIDE_OTA_ENABLED || HIVEINSIDE_AUDIO_ENABLED

}  // namespace blesensor

#endif  // ENABLE_BLE_SCAN
