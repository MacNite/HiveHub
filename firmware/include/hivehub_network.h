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
bool httpPatchJson(const String& url, const String& json, String* response = nullptr);

// ---- Upload ---------------------------------------------------------------
bool uploadLine(const String& line);
bool uploadCachedLines();

// ---- Config / OTA / commands ---------------------------------------------
void fetchRemoteConfig();
// Push the device's local scale calibration up to the backend with a
// PATCH /config, so a tare/span done offline on the provisioning portal is
// reflected server-side (the server otherwise only ever holds its defaults for a
// portal-calibrated device). The legacy scale1/2 columns are the only per-scale
// calibration the server stores, so only hives 1–2 (scale[0]) are reported — the
// same mapping fetchRemoteConfig() bridges in reverse. The PATCH bumps the
// server config_version; the returned version is recorded as last-applied so the
// following fetchRemoteConfig() sees "unchanged" and does not bridge these same
// values straight back. Called from the upload cycle only when a report is
// pending (scaleCalibrationReportPending()); the pending flag is cleared on a
// successful report and kept for a retry otherwise.
void reportScaleCalibration();
// Download and flash a HiveHub self-update image. `expectedSize`/`expectedCrc32`
// come from the backend's OTA check response: the size substitutes for a missing
// Content-Length header (a proxy/CDN may deliver the image chunked) and the CRC
// verifies the received bytes before the OTA partition is committed. Either may
// be 0 when the backend predates them; the corresponding check is then skipped.
bool performFirmwareUpdate(const String& firmwareUrl, int expectedSize = 0,
                           uint32_t expectedCrc32 = 0);
bool updateBeeCounter(uint8_t address, const String& firmwareUrl, uint32_t expectedCrc32 = 0);
// Stream a HiveInside firmware image from `firmwareUrl` to the paired HiveInside
// sensor at `mac` over BLE GATT. The image is never fully buffered; bytes are
// relayed straight from the HTTPS download into the device's OTA service, which
// verifies `expectedCrc32` end-to-end before swapping its OTA slot.
//
// When `outMsg` is non-null it receives a human-readable result/cause (e.g.
// "HiveInside OTA completed", "HiveInside not found in scan", "firmware download
// failed (HTTP 404)") so the caller can report the real outcome to the backend
// instead of a bare boolean.
bool updateHiveInside(const String& mac, const String& firmwareUrl,
                      uint32_t expectedCrc32 = 0, String* outMsg = nullptr);
void checkForOtaUpdate();
void postCommandResult(int commandId, bool success, const String& message);
void checkCommands();
