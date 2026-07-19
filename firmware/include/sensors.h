// sensors.h — time keeping, load-cell reads and assembly of the per-cycle
// measurement JSON payload (the heart of each upload cycle).
#pragma once

#include <Arduino.h>
#include "config.h"
#if ENABLE_HX711
#include <HX711.h>
#endif

String timestampNow();
void syncTime();
void initializeTime(bool wokeFromDeepSleep);

float weightFromRaw(long raw, long offset, float factor);

// Wired I2C acquisition is split into two phases so the ambient/device-level
// sensors are read BEFORE the scale bus probes its optional TCA9548A mux, and
// all of it runs BEFORE any WiFi/BLE radio activity. On the ESP32-C6 a
// transaction to an absent device (the mux probe) or radio start-up wedges the
// I2C peripheral into ESP_ERR_INVALID_STATE — a state Wire.end()/begin() alone
// cannot clear — so the ambient SHT4x must be captured first, on the known-good
// bus. createMeasurementJson() then uploads the cached snapshot instead of
// reading live post-radio.
//
// Phase 1 — device-level ambient sensors (SHT4x, INA219, MAX17048, DS18B20
// request). Resets the per-cycle snapshot; call this FIRST, before
// scalebus::begin().
void prefetchAmbientSensors();

// Phase 3 — wired scales (NAU7802/HX711). Call AFTER scalebus::begin() so no
// scale read is attempted before scale state has been initialized.
void prefetchWiredScales();

String createMeasurementJson();
