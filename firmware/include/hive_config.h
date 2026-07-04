// hive_config.h — dynamic registry of up to MAX_HIVES hives and the sensors
// mapped to each one. Replaces the old fixed "hive 1 / hive 2" model.
//
// Each hive owns:
//   - one SCALE, backed by an HX711 (legacy pins), a NAU7802 I2C channel
//     (optionally behind a TCA9548A mux), or a paired beehivemonitoring.com
//     HiveScale GATT sensor;
//   - at most one in-hive sensor: either a wired DS18B20 temperature probe
//     addressed by its 1-Wire ROM, or one wireless BLE/GATT pairing;
//
// The whole registry is persisted as one compact JSON blob per hive in NVS
// ("h0_cfg" .. "h17_cfg") plus a "hive_count" key, written by the provisioning
// portal (portal.cpp) and parsed back here at boot. Before that NVS key ever
// exists, a device instead seeds gHives from either up to MAX_HIVES HIVE_i_JSON
// secrets.h macros (same blob shape, see hive_config.cpp's seedHivesFromSecrets)
// or, absent those, migrates the legacy two-slot keys into a 2-hive registry —
// either way existing devices keep working without a portal visit.
#pragma once

#include <Arduino.h>
#include "config.h"

namespace hivecfg {

// Which hardware drives a scale channel.
enum class ScaleBackend : uint8_t {
  None    = 0,
  HX711   = 1,   // legacy load-cell amp on a dedicated pin pair
  NAU7802 = 2,   // 24-bit I2C ADC (optionally behind a TCA9548A mux)
};

struct ScaleChannel {
  ScaleBackend backend = ScaleBackend::None;

  // HX711 backend: which of the two board HX711 instances (0 -> HX1_*, 1 -> HX2_*).
  uint8_t hxIndex = 0;

  // NAU7802 backend:
  uint8_t i2cAddr    = NAU7802_I2C_ADDRESS;  // fixed 0x2A in practice
  int8_t  muxChannel = -1;   // -1 = directly on the main bus; 0..7 behind the mux
  uint8_t adcChannel = 1;    // NAU7802 input mux: 1 -> CH1, 2 -> CH2

  // Calibration (raw -> kg), same convention as the HX711 path: kg = (raw-offset)/factor.
  long  offset = 0;
  float factor = -7050.0f;

  bool valid() const { return backend != ScaleBackend::None; }
};

// One wireless pairing. `type` matches the portal vocabulary
// (holyiot|ruuvitag|hiveinside|hiveheart|hivescale|beecounter). A `hivescale`
// pairing is a scale source; every other type is the hive's one in-hive BLE
// sensor. `isGatt()` marks connection-based types that count against
// MAX_GATT_READS_PER_CYCLE.
struct BlePairing {
  String type;
  String mac;   // canonical "AA:BB:CC:DD:EE:FF"
  bool isGatt() const;
};

static const uint8_t MAX_SCALES_PER_HIVE = 1;
// Up to two BLE pairings may be persisted when a hive uses a wireless HiveScale
// as its scale source plus one separate in-hive BLE sensor. Without a wireless
// scale, hiveFromJson() and the portal accept only one in-hive BLE sensor.
static const uint8_t MAX_BLE_PER_HIVE     = 2;
static const uint8_t MAX_INHIVE_BLE_PER_HIVE = 1;

struct Hive {
  uint8_t index = 0;          // 1..MAX_HIVES; 0 means "slot unused"
  String  name;

  uint8_t      scaleCount = 0;
  ScaleChannel scales[MAX_SCALES_PER_HIVE];

  bool    hasDsRom = false;   // a DS18B20 ROM is mapped to this hive
  uint8_t dsRom[8] = {0};     // 1-Wire ROM address

  uint8_t    bleCount = 0;
  BlePairing ble[MAX_BLE_PER_HIVE];

  bool used() const { return index >= 1; }
};

// The live registry. gHiveCount entries of gHives[] are populated (index 1..N).
extern Hive    gHives[MAX_HIVES];
extern uint8_t gHiveCount;

// Parse the NVS per-hive blobs (and hive_count) into gHives. When no blobs exist
// yet, migrate the legacy fixed-slot keys (scale offsets/factors, ble_mac0/1,
// wcount/wtypeN/wmacN/wslotN) into a two-hive registry so an upgraded device keeps
// working without re-provisioning.
void loadHiveConfig();

// Serialize gHives back to NVS (used after a calibration writeback).
void saveHiveConfig();

// Serialize a single hive to its compact JSON blob string (used by the portal).
String hiveToJson(const Hive& h);
// Parse one hive blob into `out`; returns false on malformed input.
bool   hiveFromJson(const String& json, Hive& out);

// Convenience: total configured wired scale channels across all hives (<= MAX_SCALES).
uint8_t totalScaleChannels();

// Format/parse a DS18B20 ROM as 16 hex chars ("28FF64...") for the portal/NVS.
String   romToHex(const uint8_t rom[8]);
bool     romFromHex(const String& hex, uint8_t rom[8]);

}  // namespace hivecfg
