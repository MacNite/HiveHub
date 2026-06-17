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
// Up to two HiveHeart (in-hive, slot 1 -> hive 1, slot 2 -> hive 2) and up to two
// HiveScale (wireless weight) devices are supported. MACs come from Preferences,
// seeded from secrets.h (INHIVE_n_MAC / WSCALE_n_MAC) and/or set in the
// provisioning portal. Heart temperature/humidity feed the existing
// hive_{slot}_* fields; everything else lands in new hiveheart_*/hivescale_*
// fields (see server migration 009).
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
  HeartReading heart[2];
  ScaleReading scale[2];
};

// Connect to each paired Heart/Scale MAC in turn, decode one notification each,
// and fill `out`. Initialises and de-initialises the NimBLE stack around the
// whole batch so it coexists with the HolyIot scan and the WiFi upload. Slots
// with an empty MAC stay !present. Safe to call when no device is paired.
void runCycle(CycleResult& out);

// Write the decoded readings into the measurement JSON. Heart temperature and
// humidity are NOT written here (sensors.cpp owns hive_{slot}_temp_c /
// hive_{slot}_humidity_percent so it can arbitrate sources); everything else is.
void writeToJson(JsonDocument& doc, const CycleResult& r);

// Normalise a MAC string to "AA:BB:CC:DD:EE:FF" (uppercase, colon-separated) or
// "" when it is not a valid 6-byte MAC. Used by the provisioning portal.
String normalizeMac(const String& raw);

}  // namespace bhgatt

#endif  // ENABLE_BEEHIVE_GATT
