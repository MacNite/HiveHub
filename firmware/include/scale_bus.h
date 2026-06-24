// scale_bus.h — low-level reads for the generalized scale registry: HX711 pin
// pairs and NAU7802 I2C channels (optionally behind a TCA9548A 1-to-8 mux).
//
// All NAU7802s share the fixed I2C address 0x2A, so more than one is only
// reachable behind the mux: writing (1<<channel) to the TCA9548A (0x70) connects
// exactly one downstream NAU7802 at a time. A single NAU7802 exposes two
// differential inputs (CH1/CH2), i.e. two load cells. The driver reads RAW counts
// and leaves offset/factor conversion to weightFromRaw() (sensors.cpp), mirroring
// the existing HX711 path so no per-chip calibration state has to survive a mux
// switch.
#pragma once

#include <Arduino.h>
#include "config.h"
#include "hive_config.h"

namespace scalebus {

// Detect the mux (0x70) and configure every NAU7802 referenced by the registry
// (once per physical chip). HX711 channels use the two global HX711 objects and
// are initialised in main.cpp as before. Call once per wake, after Wire.begin().
void begin();

// Read one scale channel as an averaged raw 24-bit count. Selects the mux channel
// and NAU7802 input as needed; returns 0 on timeout / not-ready.
long readRaw(const hivecfg::ScaleChannel& ch);

// Power every NAU7802 down (direct + behind each mux channel) ahead of deep
// sleep — satisfies the "sleep the NAU7802 before the ESP32 sleeps" requirement.
void powerDownAllForSleep();

// TCA9548A control. muxSelect connects exactly one channel; muxDisableAll opens
// all of them (so a NAU7802 on the main bus, if any, is reachable). Both are
// no-ops when no mux was detected.
void muxSelect(uint8_t channel);
void muxDisableAll();

// True once begin() has detected a TCA9548A on the bus.
bool muxPresent();

}  // namespace scalebus
