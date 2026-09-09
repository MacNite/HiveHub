// globals.cpp — single definition point for everything declared in globals.h.
#include "globals.h"

#include <esp_system.h>

const char* const FIRMWARE_VERSION = "0.30.4";

#if ENABLE_HX711
HX711 scale1;
HX711 scale2;
#endif
#if ENABLE_DS18B20_HIVE_TEMP
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature ds18b20(&oneWire);
#endif
#if ENABLE_SHT4X_AMBIENT
Adafruit_SHT4x sht4;
#endif
#if ENABLE_SHT3X_AMBIENT
Adafruit_SHT31 sht3;
#endif
#if ENABLE_BME280_AMBIENT
Adafruit_BME280 bme;
#endif
RTC_DS3231 rtc;
Preferences prefs;
WebServer setupServer(80);
DNSServer setupDnsServer;

#if ENABLE_INA219_SOLAR
Adafruit_INA219 solarMonitor(INA219_I2C_ADDRESS);
bool solarMonitorOk = false;
#endif
#if ENABLE_MAX17048_BATTERY
SFE_MAX1704X batteryGauge(MAX1704X_MAX17048);
bool batteryMonitorOk = false;
#endif

bool sdOk = false;
bool sdBusInitialized = false;
bool shtOk = false;
bool rtcOk = false;
bool provisioningActive = false;
bool calibrationModeActive = false;

unsigned long lastCycleMs = 0;
unsigned long lastOtaCheckMs = 0;
unsigned long provisioningStartedMs = 0;
unsigned long sendIntervalMs = 10UL * 60UL * 1000UL;

// HiveTraffic night mode. Defaults are the feature switched OFF with a
// plausible window behind it, so enabling it in the dashboard is one checkbox
// and never a half-configured state. See night_mode.h for what they mean and
// hivehub_network.cpp::fetchRemoteConfig for where they come from.
bool     nightModeEnabled = false;
uint16_t nightStartMinute = 20 * 60;   // 20:00 local
uint16_t nightEndMinute   = 6 * 60;    // 06:00 local
uint32_t nightMaxTraffic  = 0;         // 0 = no traffic gate
String   nightTimezone    = "";        // empty = UTC

// HiveTraffic emitter banks: all three enabled until /config says otherwise,
// which matches the counter's own power-on default. Assembled from the three
// per-bank booleans in fetchRemoteConfig() and re-asserted over BLE every
// cycle, because the counter deliberately does not persist it.
uint8_t  beeBankMask      = BEECOUNTER_BANK_MASK_ALL;
unsigned long calibrationModeStartedMs = 0;
unsigned long calibrationModeIntervalMs = CALIBRATION_MODE_DEFAULT_INTERVAL_MS;
unsigned long calibrationModeTimeoutMs = CALIBRATION_MODE_DEFAULT_TIMEOUT_MS;

String timeSource = "unknown";
String apiBaseUrl;
String apiKey;
String deviceId;
String claimCode;
String activeWifiSsid;
String lastMeasurementJson;
unsigned long lastMeasurementUpdatedMs = 0;

#if ENABLE_BLE_SCAN
String bleSensorMac0;
String bleSensorMac1;
#endif

#if ENABLE_BEEHIVE_GATT
String heartMac0;
String heartMac1;
String scaleMac0;
String scaleMac1;
#endif

long scale1Offset = 0;
long scale2Offset = 0;
float scale1Factor = -7050.0f;
float scale2Factor = -7050.0f;

bool claimRegistered = false;

bool buttonWasDown = false;
unsigned long buttonDownMs = 0;
bool longPressHandled = false;

RTC_DATA_ATTR uint32_t rtcCyclesUntilOta = 0;
RTC_DATA_ATTR uint32_t rtcBootCount = 0;

// ── Markers that must survive a RESET, not just a deep sleep ───────────────
//
// RTC_NOINIT_ATTR, never RTC_DATA_ATTR, and the difference is the whole point.
// `.rtc.data` (RTC_DATA_ATTR) is a LOADABLE segment of the app image: the
// second-stage bootloader writes it back from flash on every boot it loads
// segments for, and it skips that only on a deep-sleep wake. So an
// RTC_DATA_ATTR variable keeps its value across deep sleep — which is all
// esp_attr.h claims for it — and is reset to its initializer by any reboot:
// esp_restart(), a panic, a brownout, an OTA. `.rtc_noinit` is (NOLOAD) and is
// never written by the bootloader, so it survives a reboot too and is only
// undefined after a true power-on.
//
// Both markers below hand a fact across a reboot, so both need the second
// behaviour. Neither may carry an initializer: `.rtc_noinit` is never loaded,
// so an initializer would be a lie the compiler cannot honour. Whatever a
// power cycle leaves behind is rejected by the magic each one is checked
// against.
//
// RTC rather than NVS because these cost no flash write on the happy path —
// the relay marker is set and cleared around every single firmware relay.

// A relay in flight, so a reset that kills it can still be reported against the
// right command id instead of leaving the row open. See markRelayInFlight() /
// reportInterruptedRelay() in hivehub_network.cpp.
RTC_NOINIT_ATTR uint32_t rtcRelayCommandId;
RTC_NOINIT_ATTR uint32_t rtcRelayMagic;
// Which kind of relay it was, so the report names what actually died. Written
// before the magic and only ever read once the magic matches, so the garbage a
// power-on leaves here is never believed.
RTC_NOINIT_ATTR uint32_t rtcRelayKind;

// A `start_provisioning` request that had to reboot to get a NimBLE port
// lifetime its BLE scan is allowed to run in. See portal.cpp.
RTC_NOINIT_ATTR uint32_t rtcPortalBootMagic;

// Previous cycle's HiveTraffic totals per hive, for the night-mode traffic
// gate. RTC memory because HiveHub deep-sleeps between cycles — in plain RAM
// there would never be a previous reading and the gate would postpone night
// mode forever. The validity bitmask matters: hive N never read yet must be
// "unknown", not "zero crossings", or the gate would wave through a hive it has
// no information about. Zeroed on a cold boot, which is exactly right.
RTC_DATA_ATTR uint32_t rtcCounterTotalIn[MAX_HIVES] = {0};
RTC_DATA_ATTR uint32_t rtcCounterTotalOut[MAX_HIVES] = {0};
RTC_DATA_ATTR uint32_t rtcCounterTotalsValid = 0;

void debugLine() {
  Serial.println("----------------------------------------");
}

const char* resetReasonName() {
  // Distinguishing a panic from a clean deep-sleep wake or a brownout is the
  // whole point: an unexplained command failure reads very differently when the
  // preceding boot ended in ESP_RST_PANIC.
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:  return "power-on";
    case ESP_RST_EXT:      return "external reset";
    case ESP_RST_SW:       return "software restart";
    case ESP_RST_PANIC:    return "panic/exception";
    case ESP_RST_INT_WDT:  return "interrupt watchdog";
    case ESP_RST_TASK_WDT: return "task watchdog";
    case ESP_RST_WDT:      return "other watchdog";
    case ESP_RST_DEEPSLEEP: return "deep-sleep wake";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO:     return "SDIO";
    default:               return "unknown";
  }
}

bool isBlank(const String& s) {
  return s.length() == 0;
}

String trimTrailingSlash(String value) {
  value.trim();
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}
