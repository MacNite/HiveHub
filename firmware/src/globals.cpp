// globals.cpp — single definition point for everything declared in globals.h.
#include "globals.h"

const char* const FIRMWARE_VERSION = "0.22.0";

#if ENABLE_HX711
HX711 scale1;
HX711 scale2;
#endif
#if ENABLE_NAU7802
NAU7802 nau;
#endif
#if ENABLE_DS18B20_HIVE_TEMP
OneWire oneWire(ONE_WIRE_PIN);
DallasTemperature ds18b20(&oneWire);
#endif
Adafruit_SHT4x sht4;
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
unsigned long lastCommandCheckMs = 0;
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

#if ENABLE_WIRELESS_BEECOUNTER
String trafficMac0;
String trafficMac1;
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

void debugLine() {
  Serial.println("----------------------------------------");
}

bool isBlank(const String& s) {
  return s.length() == 0;
}

String trimTrailingSlash(String value) {
  value.trim();
  while (value.endsWith("/")) value.remove(value.length() - 1);
  return value;
}
