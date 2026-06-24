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
#include <HX711.h>
#if ENABLE_NAU7802
#include <SparkFun_Qwiic_Scale_NAU7802_Arduino_Library.h>
#endif
#if ENABLE_DS18B20_HIVE_TEMP
#include <OneWire.h>
#include <DallasTemperature.h>
#endif
#include <Adafruit_SHT4x.h>
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
// Up to two HX711 load-cell amps on dedicated pin pairs (legacy / first hives).
extern HX711 scale1;
extern HX711 scale2;
#if ENABLE_NAU7802
// One shared NAU7802 I2C ADC object. Every NAU7802 lives at the same address
// (0x2A); the TCA9548A mux routes this object to one physical chip at a time, so
// a single instance serves all I2C scale channels. See scale_bus.cpp.
extern NAU7802 nau;
#endif
#if ENABLE_DS18B20_HIVE_TEMP
extern OneWire oneWire;
extern DallasTemperature ds18b20;
#endif
extern Adafruit_SHT4x sht4;
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
extern unsigned long lastCommandCheckMs;
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

#if ENABLE_WIRELESS_BEECOUNTER
// Paired HiveTraffic (wireless entrance bee counter) GATT MACs ("" when
// unpaired). Slot 0 -> counter 1, slot 1 -> counter 2. Stored under the
// portal's counter_mac{0,1} keys; seeded from WBEECNT_n_MAC in secrets.h.
extern String trafficMac0;
extern String trafficMac1;
#endif

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

// ---- Small shared utilities ----------------------------------------------
void debugLine();
bool isBlank(const String& s);
String trimTrailingSlash(String value);
