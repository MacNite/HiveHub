// ble_lanes.h — which per-hive slot a BLE pairing occupies, used by
// hive_config.cpp for both NVS-loaded and portal-submitted registries. The
// add-button logic in the portal page is convenience only; THIS is the
// authoritative rule, so a crafted /save request or a corrupt stored blob can
// never map two pairings of the same family onto one hive.
//
// A hive used to accept exactly ONE non-scale BLE pairing, which made a
// HiveInside beacon and a HiveTraffic counter mutually exclusive on the same
// hive even though nothing downstream required that. Each sensor family owns
// its OWN nested object in the hives[] measurement payload:
//
//   Beacon  -> hives[].ble (+ .accel/.mic)  ble_sensor.cpp
//   Counter -> hives[].bee_counter          bee_counter_client.cpp
//   Heart   -> hives[].hiveheart            sensors.cpp
//   Scale   -> hives[].hivescale            sensors.cpp — a SCALE source
//
// so two pairings can share a hive exactly when they sit in different lanes.
// Only the beacon formats genuinely collide: HolyIot, RuuviTag and HiveInside
// all decode into the single `ble` object, so a hive still takes just one.
//
// Arduino-free (const char*, no String) so the host test suite verifies the
// exact production rules — see firmware/host_test/test_ble_lanes.cpp.
#pragma once

#include <stdint.h>
#include <string.h>

namespace blelanes {

enum class Lane : uint8_t {
  Beacon  = 0,   // holyiot | ruuvitag | hiveinside_nrf54  (passive scan)
  Counter = 1,   // beecounter — HiveTraffic entrance gates (GATT)
  Heart   = 2,   // hiveheart — beehivemonitoring.com       (GATT)
  Scale   = 3,   // hivescale — a scale source, not an in-hive sensor
};

static const uint8_t LANE_COUNT = 4;

// Non-scale lanes a hive may fill at once: one beacon + one counter + one heart.
static const uint8_t INHIVE_LANE_COUNT = LANE_COUNT - 1;

// Map the portal's type vocabulary onto a lane.
//
// An UNRECOGNISED type falls into the beacon lane on purpose. It must not get a
// free extra slot: that is exactly where an unknown type sat back when there was
// a single in-hive slot, so a corrupt or future-dated blob can still only ever
// cost one lane instead of silently escaping the limit.
inline Lane laneFor(const char* type) {
  if (!type)                       return Lane::Beacon;
  if (!strcmp(type, "hivescale"))  return Lane::Scale;
  if (!strcmp(type, "beecounter")) return Lane::Counter;
  if (!strcmp(type, "hiveheart"))  return Lane::Heart;
  return Lane::Beacon;
}

inline bool isScaleSource(const char* type) { return laneFor(type) == Lane::Scale; }

// Which lanes a hive has filled so far. Pairings are claimed in stored order, so
// the FIRST pairing of a lane wins and any later duplicate is rejected — the
// same first-wins behaviour the single-slot rule had.
struct LaneSet {
  bool filled[LANE_COUNT];

  LaneSet() { for (uint8_t i = 0; i < LANE_COUNT; i++) filled[i] = false; }

  bool taken(Lane lane) const { return filled[(uint8_t)lane]; }

  // Take `lane` for this hive. Returns false when it was already occupied, in
  // which case the caller drops the pairing.
  bool claim(Lane lane) {
    uint8_t i = (uint8_t)lane;
    if (filled[i]) return false;
    filled[i] = true;
    return true;
  }

  bool claim(const char* type) { return claim(laneFor(type)); }

  // Non-scale lanes currently filled (0..INHIVE_LANE_COUNT).
  uint8_t inHiveCount() const {
    uint8_t n = 0;
    for (uint8_t i = 0; i < LANE_COUNT; i++)
      if (filled[i] && (Lane)i != Lane::Scale) n++;
    return n;
  }
};

}  // namespace blelanes
