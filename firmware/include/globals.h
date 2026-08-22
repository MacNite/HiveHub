// globals.h — hardware driver objects and mutable runtime state shared across
// modules. Declarations only; definitions live in globals.cpp.
#pragma once

// config.h (and secrets.h it pulls in) must come first so that feature flags
// like ENABLE_DS18B20_HIVE_TEMP are defined before the conditional includes below.
#include "config.h"

#include <Arduino.h>
#include <Wire.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#if ENABLE_HX711
#include <HX711.h>
#endif
#if ENABLE_DS18B20_HIVE_TEMP
#include <OneWire.h>
#include <DallasTemperature.h>
#endif
// Exactly one ambient temp/humidity[/pressure] sensor family (see config.h).
#if ENABLE_SHT4X_AMBIENT
#include <Adafruit_SHT4x.h>
#endif
#if ENABLE_SHT3X_AMBIENT
#include <Adafruit_SHT31.h>
#endif
#if ENABLE_BME280_AMBIENT
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#endif
#include <RTClib.h>
#include <Preferences.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>

#if ENABLE_INA219_SOLAR
#include <Adafruit_INA219.h>
#endif
#if ENABLE_MAX17048_BATTERY
#include <SparkFun_MAX1704x_Fuel_Gauge_Arduino_Library.h>
#endif

extern const char* const FIRMWARE_VERSION;

// ---- Hardware driver instances -------------------------------------------
#if ENABLE_HX711
// Up to two HX711 load-cell amps on dedicated pin pairs (legacy / first hives).
// Compiled out on the XIAO C6 (ENABLE_HX711 0), which uses I2C NAU7802 scales.
extern HX711 scale1;
extern HX711 scale2;
#endif
// NAU7802 access goes through HiveHub's own checked driver (nau7802_checked.h,
// instantiated inside scale_bus.cpp) — there is no shared library object here.
#if ENABLE_DS18B20_HIVE_TEMP
extern OneWire oneWire;
extern DallasTemperature ds18b20;
#endif
// The selected ambient sensor object (only one family is compiled — see config.h).
#if ENABLE_SHT4X_AMBIENT
extern Adafruit_SHT4x sht4;
#endif
#if ENABLE_SHT3X_AMBIENT
extern Adafruit_SHT31 sht3;
#endif
#if ENABLE_BME280_AMBIENT
extern Adafruit_BME280 bme;
#endif
extern RTC_DS3231 rtc;
extern Preferences prefs;
extern WebServer setupServer;
extern DNSServer setupDnsServer;

#if ENABLE_INA219_SOLAR
extern Adafruit_INA219 solarMonitor;
extern bool solarMonitorOk;
#endif
#if ENABLE_MAX17048_BATTERY
extern SFE_MAX1704X batteryGauge;
extern bool batteryMonitorOk;
#endif

// ---- Runtime flags --------------------------------------------------------
extern bool sdOk;
extern bool sdBusInitialized;
extern bool shtOk;
extern bool rtcOk;
extern bool provisioningActive;
extern bool calibrationModeActive;
extern bool claimRegistered;

// ---- Timing / scheduling --------------------------------------------------
extern unsigned long lastCycleMs;
extern unsigned long lastOtaCheckMs;
extern unsigned long provisioningStartedMs;
extern unsigned long sendIntervalMs;
extern unsigned long calibrationModeStartedMs;
extern unsigned long calibrationModeIntervalMs;
extern unsigned long calibrationModeTimeoutMs;

// ---- Config / identity ----------------------------------------------------
extern String timeSource;
extern String apiBaseUrl;
extern String apiKey;
extern String deviceId;
extern String claimCode;
extern String activeWifiSsid;
extern String lastMeasurementJson;
extern unsigned long lastMeasurementUpdatedMs;

#if ENABLE_BLE_SCAN
// Paired HolyIot 25015 BLE sensor MAC addresses, "AA:BB:.." or "" when unpaired.
// Slot 0 -> hive 1, slot 1 -> hive 2. Set from the provisioning portal.
extern String bleSensorMac0;
extern String bleSensorMac1;
#endif

#if ENABLE_BEEHIVE_GATT
// Paired beehivemonitoring.com GATT device MACs ("" when unpaired). HiveHeart
// slot 0 -> hive 1, slot 1 -> hive 2; HiveScale slot 0/1 are the wireless
// scales. Seeded from secrets.h and/or set in the provisioning portal.
extern String heartMac0;
extern String heartMac1;
extern String scaleMac0;
extern String scaleMac1;
#endif

// HiveTraffic (wireless entrance bee counter) MACs are no longer bridged into
// per-slot globals: bee_counter_client.cpp reads them straight from the hive
// registry (gHives[].ble "beecounter" pairings), so a counter works on any hive
// up to MAX_HIVES. Paired in the portal, seeded via HIVE_i_JSON, or via the
// legacy WBEECNT_n_MAC / counter_mac{0,1} keys (migrated into the registry).

// ---- HiveTraffic night mode ----------------------------------------------
// When to tell a paired HiveTraffic counter to stop sensing, because honey bees
// are diurnal and its 48 IR emitters are the largest item in an off-grid power
// budget. Delivered by /api/v1/devices/{id}/config, persisted in NVS so a hub
// that boots without WiFi still applies the last known window, and decided per
// cycle by night_mode.h. OFF unless deliberately enabled.
//
// The window is LOCAL wall-clock minutes since midnight and may wrap midnight
// (20:00 -> 06:00 is start 1200, end 360). See nightTimezone below: without a
// TZ the device clock is UTC, and "20:00" would drift by an hour twice a year.
extern bool     nightModeEnabled;
extern uint16_t nightStartMinute;
extern uint16_t nightEndMinute;
// Crossings (in + out) in the last cycle above which night mode is postponed to
// the next one. 0 disables the check.
extern uint32_t nightMaxTraffic;
// POSIX TZ string (e.g. "CET-1CEST,M3.5.0,M10.5.0/3"), applied with setenv()/
// tzset() so localtime() honours it. Empty means UTC.
extern String   nightTimezone;

// ---- HiveTraffic emitter banks -------------------------------------------
// Which of a paired counter's three emitter MOSFETs may light. Bit 0 = bank 1
// (gates 00..07), bit 1 = bank 2 (10..17), bit 2 = bank 3 (20..27). Delivered
// by /api/v1/devices/{id}/config as three booleans and assembled into this mask;
// persisted in NVS alongside the night window, and written to each paired
// counter once per upload cycle.
//
// The counter itself never persists it, so this re-assert is not belt and
// braces — it is the mechanism. A counter that reset comes back running all 24
// gates and stays that way until we tell it otherwise, which costs at most one
// cycle of extra current and never a counter blind on eight gates for reasons
// nobody can reconstruct.
//
// BEECOUNTER_BANK_MASK_ALL (all three on) unless the dashboard says otherwise.
// A mask of 0 is never sent: the dashboard refuses to store it and the counter
// refuses to apply it.
extern uint8_t  beeBankMask;

// ---- Scale calibration ----------------------------------------------------
extern long scale1Offset;
extern long scale2Offset;
extern float scale1Factor;
extern float scale2Factor;

// ---- Button state ---------------------------------------------------------
extern bool buttonWasDown;
extern unsigned long buttonDownMs;
extern bool longPressHandled;

// ---- Values that survive deep sleep --------------------------------------
// The RTC_DATA_ATTR (section) attribute belongs only on the definitions in
// globals.cpp. Repeating it here would generate a second, auto-numbered RTC
// section that conflicts with the definition's, which the compiler then
// discards with a -Wattributes warning. Plain extern declarations are enough.
extern uint32_t rtcCyclesUntilOta;
extern uint32_t rtcBootCount;
// Set while a firmware relay to a BLE sub-device is running, so the command can
// be failed explicitly after a reset instead of silently timing out server-side.
// See markRelayInFlight()/reportInterruptedRelay() in hivehub_network.cpp.
extern uint32_t rtcRelayCommandId;
extern uint32_t rtcRelayMagic;
// Previous cycle's HiveTraffic lifetime totals, per hive index, so the night
// mode traffic gate can difference them into "crossings since the last cycle".
// In RTC memory because HiveHub deep-sleeps between cycles: in plain RAM there
// would never be a previous reading to compare against, and the gate would
// postpone night mode forever. rtcCounterTotalsValid is a per-hive bitmask, so
// a hive that has never been read is "unknown" rather than "zero" — which the
// gate treats very differently.
extern uint32_t rtcCounterTotalIn[MAX_HIVES];
extern uint32_t rtcCounterTotalOut[MAX_HIVES];
extern uint32_t rtcCounterTotalsValid;

// ---- Small shared utilities ----------------------------------------------
void debugLine();
// Why this boot happened, as a short phrase ("panic/exception", "deep-sleep
// wake", "brownout", …). Printed at boot and quoted when reporting a command
// that a reset interrupted.
const char* resetReasonName();
bool isBlank(const String& s);
String trimTrailingSlash(String value);
