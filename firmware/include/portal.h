// portal.h — WiFi provisioning / captive setup portal, the physical setup
// button handler, and calibration-mode control. The portal also exposes the
// SD TAR export (implemented in storage_power).
#pragma once

#include <Arduino.h>
#include "config.h"

// ---- Calibration mode -----------------------------------------------------
bool calibrationModeExpired();
void stopCalibrationMode(const String& reason);
void startCalibrationMode(unsigned long intervalSeconds, unsigned long timeoutSeconds);

// ---- HTML / portal helpers ------------------------------------------------
String htmlEscape(String s);
IPAddress provisioningPortalIp();
String provisioningPortalUrl();
void sendNoCacheHeaders();
void sendPortalRedirect();
void handleCaptivePortalProbe();
void appendLastSensorPanel(String& html);

// ---- HTTP route handlers --------------------------------------------------
void handleSdDownloadAll();
#if ENABLE_BLE_SCAN
void handleBleScan();
#endif
void handleSetupRoot();
void handleSetupSave();
void handleSetupReset();

// ---- Portal lifecycle + button -------------------------------------------
void startProvisioningPortal();
void stopProvisioningPortal();
void handleButton();
