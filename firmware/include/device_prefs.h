// device_prefs.h — NVS (Preferences) seeding, loading, saving and WiFi
// credential management.
#pragma once

#include <Arduino.h>

String prefString(const char* key, const char* fallback = "");
void putPrefString(const char* key, const String& value);
String wifiSsidKey(int index);
String wifiPassKey(int index);

void seedPrefsFromSecretsIfNeeded();
void loadConfigFromPrefs();
void saveScaleConfig();
void markClaimRegistered();

// Local scale calibration changed offline (a tare/span on the provisioning
// portal, which has no internet) and should be pushed to the server on the next
// cycle that has WiFi. Set by the portal, consumed by reportScaleCalibration().
void markScaleCalibrationDirty();
bool scaleCalibrationReportPending();
void clearScaleCalibrationReport();

int getWifiCount();
bool saveWifiNetwork(int index, const String& ssid, const String& pass);
void clearWifiCredentials();
void factoryResetPreferences();
