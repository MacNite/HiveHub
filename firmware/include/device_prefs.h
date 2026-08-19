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
// Persist the HiveTraffic night-mode window from the last /config fetch, so an
// offline boot still honours it. Called from fetchRemoteConfig().
void saveNightModePrefs();
// Latch "the server has this device's claim" so the claim code stops riding
// along on every upload.
void markClaimRegistered();
// Drop that latch so the claim code is offered again. Called when the server
// says the device is no longer claimed (removed in the app), and whenever the
// claim code itself is changed from the portal or from remote config — a new
// code is worthless if the device never sends it.
void clearClaimRegistered();

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
