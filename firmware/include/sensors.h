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

// Capture every wired I2C reading (SHT4x, INA219, MAX17048, NAU7802 scales) on
// the known-good bus BEFORE any WiFi/BLE radio activity. On the ESP32-C6 radio
// start-up wedges the I2C peripheral into ESP_ERR_INVALID_STATE — a state
// Wire.end()/begin() cannot clear — so a read attempted after WiFi connects
// fails. Call this from setup() after device init and before initializeTime();
// createMeasurementJson() then uploads the cached snapshot instead of reading
// live post-radio.
void prefetchWiredSensors();

String createMeasurementJson();
