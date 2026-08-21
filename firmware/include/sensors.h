// sensors.h — time keeping, load-cell reads and assembly of the per-cycle
// measurement JSON payload (the heart of each upload cycle).
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include "config.h"
#if ENABLE_HX711
#include <HX711.h>
#endif

String timestampNow();
void syncTime();

// Local wall-clock minutes since midnight (0..1439), honouring the configured
// POSIX timezone, or nightmode::MINUTES_PER_DAY when there is no trustworthy
// clock. Only the HiveTraffic night-mode window uses local time; everything
// else in this firmware is UTC. See the comment above the definition for why
// the timezone is load-bearing rather than cosmetic.
uint16_t localMinuteOfDay();
void initializeTime(bool wokeFromDeepSleep);

float weightFromRaw(long raw, long offset, float factor);

// Wired I2C acquisition is split into two phases so the ambient/device-level
// sensors are read BEFORE the scale bus probes its optional TCA9548A mux, and
// all of it runs BEFORE any WiFi/BLE radio activity. On the ESP32-C6 a
// transaction to an absent device (the mux probe) or radio start-up wedges the
// I2C peripheral into ESP_ERR_INVALID_STATE — a state Wire.end()/begin() alone
// cannot clear — so the ambient SHT4x must be captured first, on the known-good
// bus. buildMeasurementDoc() then uploads the cached snapshot instead of
// reading live post-radio.
//
// Phase 1 — device-level ambient sensors (SHT4x, INA219, MAX17048, DS18B20
// request). Resets the per-cycle snapshot; call this FIRST, before
// scalebus::begin().
void prefetchAmbientSensors();

// Phase 3 — wired scales (NAU7802/HX711). Call AFTER scalebus::begin() so no
// scale read is attempted before scale state has been initialized.
void prefetchWiredScales();

// Payload assembly is a two-step API on purpose. buildMeasurementDoc() fills the
// document while the SD card is still down (SD.begin() is the boundary after
// which the C6 I2C-NG driver may reject the RTC read); the caller then brings
// the card up, stamps sd_ok from the now-known state, and calls
// finalizeMeasurementJson() to serialize. See runUploadCycle() in main.cpp.
void buildMeasurementDoc(JsonDocument& doc);
String finalizeMeasurementJson(JsonDocument& doc);
