// globals.cpp — single definition point for everything declared in globals.h.
#include "globals.h"

#include <esp_system.h>

const char* const FIRMWARE_VERSION = "0.24.12";

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

// A relay in flight, so a reset that kills it can still be reported. RTC memory
// is the right store: it survives a panic reboot (it is only cleared on a
// power-on reset), and unlike NVS it costs no flash write on the happy path,
// which matters because this is set and cleared around every single relay.
// The magic guards against reading whatever a power-on reset left behind.
RTC_DATA_ATTR uint32_t rtcRelayCommandId = 0;
RTC_DATA_ATTR uint32_t rtcRelayMagic = 0;

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
