// beehive_gatt.h — GATT client for beehivemonitoring.com HiveHeart / HiveScale.
//
// Unlike the HolyIot 25015 bridge (which passively scans advertisements), these
// devices are read by *connecting* to a paired MAC, subscribing to a single
// notify characteristic, taking the one notification they push and then
// disconnecting — exactly the flow beehivemonitoring's own gateway and the
// community ESPHome component use:
//
//   connect(mac) -> discover -> subscribe(513849EB-…-533D6E) -> notify -> close
//
// HiveHeart (in-hive) and HiveScale (wireless weight) devices are resolved from
// the dynamic hive registry, so they work on any hive up to MAX_HIVES. Legacy
// two-slot globals are still populated elsewhere for old modules, but this GATT
// client now reads the registry directly. Heart temperature/humidity feed the
// per-hive JSON object in sensors.cpp; the raw readings also land in
// hiveheart_*/hivescale_* fields for backwards-compatible visibility.
//
// Compiled out unless ENABLE_BEEHIVE_GATT is set.
#pragma once

#include <Arduino.h>
#include "config.h"

#if ENABLE_BEEHIVE_GATT

#include <ArduinoJson.h>
#include "beehive_decode.h"

namespace bhgatt {

struct CycleResult {
  // Entries are keyed by hivecfg::gHives[] array position, not by hive.index - 1.
  // That keeps sparse/non-renumbered hive indexes safe while iterating the live
  // registry in sensors.cpp.
  HeartReading heart[MAX_HIVES];
  ScaleReading scale[MAX_HIVES];
};

// Connect to each paired Heart/Scale MAC in turn, decode one notification each,
// and fill `out`. Initialises and de-initialises the NimBLE stack around the
// whole batch so it coexists with the HolyIot scan and the WiFi upload. Hives
// without a paired device stay !present. Safe to call when no device is paired.
// At most MAX_GATT_READS_PER_CYCLE devices are read per wake cycle.
void runCycle(CycleResult& out);

// Write decoded readings into top-level compatibility fields named by hive index
// (hiveheart_N_* / hivescale_N_*). sensors.cpp separately writes the canonical
// per-hive hives[] values and nested diagnostic objects for all hives.
void writeToJson(JsonDocument& doc, const CycleResult& r);

// Normalise a MAC string to "AA:BB:CC:DD:EE:FF" (uppercase, colon-separated) or
// "" when it is not a valid 6-byte MAC. Used by the provisioning portal.
String normalizeMac(const String& raw);

}  // namespace bhgatt

#endif  // ENABLE_BEEHIVE_GATT
