// network.h — connectivity layer: WiFi association, HTTP(S) JSON requests,
// measurement upload + retry-cache drain, remote config, OTA and the
// device command queue.
#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>

#include "config.h"   // for WIFI_CONNECT_TIMEOUT_MS (default arg below)

// ---- URL helpers ----------------------------------------------------------
String apiUrl(const String& path);
String absoluteUrl(String maybeRelativeUrl);

// ---- WiFi -----------------------------------------------------------------
bool connectWifi(unsigned long timeoutMs = WIFI_CONNECT_TIMEOUT_MS);
bool connectNetwork();

// ---- HTTP -----------------------------------------------------------------
bool httpGetJson(const String& url, JsonDocument& doc);
bool httpPostJson(const String& url, const String& json, String* response = nullptr);

// ---- Upload ---------------------------------------------------------------
bool uploadLine(const String& line);
bool uploadCachedLines();

// ---- Config / OTA / commands ---------------------------------------------
void fetchRemoteConfig();
bool performFirmwareUpdate(const String& firmwareUrl);
bool updateBeeCounter(uint8_t address, const String& firmwareUrl, uint32_t expectedCrc32 = 0);
// Stream a HiveInside firmware image from `firmwareUrl` to the paired HiveInside
// sensor at `mac` over BLE GATT. The image is never fully buffered; bytes are
// relayed straight from the HTTPS download into the device's OTA service, which
// verifies `expectedCrc32` end-to-end before swapping its OTA slot.
bool updateHiveInside(const String& mac, const String& firmwareUrl, uint32_t expectedCrc32 = 0);
void checkForOtaUpdate();
void postCommandResult(int commandId, bool success, const String& message);
void checkCommands();
