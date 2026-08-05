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
// What the server said about this device's claim in its upload response. The
// three cases must stay distinct: a server that says nothing (old builds) must
// not be read as "not claimed", or the device would re-offer its claim code
// forever.
enum class ClaimStatus : uint8_t {
  Unknown = 0,   // upload failed, or the response carried no "claimed" field
  Claimed,       // server reports the device as claimed
  Unclaimed,     // server explicitly reports the device as NOT claimed
};

// claimStatus (optional out-param) lets the caller defer latching the local
// "claim registered" flag until the claim actually exists server-side, and
// clear it again when the claim goes away (device removed in the app).
bool uploadLine(const String& line, ClaimStatus* claimStatus = nullptr);
bool uploadCachedLines();

// ---- Config / OTA / commands ---------------------------------------------
void fetchRemoteConfig();
// Push the device's local scale calibration up to the backend with a
// PATCH /config, so a tare/span done offline on the provisioning portal is
// reflected server-side (the server otherwise only ever holds its defaults for a
// portal-calibrated device). Hives 1–2 go in the legacy scale1/2 offset+factor
// fields; hives 3–18 go in the hive_scales[] array — the same two shapes
// fetchRemoteConfig() bridges in reverse. The PATCH bumps the server
// config_version; the returned version is recorded as last-applied so the
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
// Stream a firmware image from `firmwareUrl` to a paired BLE sub-device at
// `mac` over GATT. The image is never fully buffered; bytes are relayed
// straight from the HTTPS download into the device's OTA characteristics, and
// the device verifies `expectedCrc32` end-to-end before swapping its OTA slot.
// Both share one implementation (relayFirmwareOverGatt + src/gatt_ota.cpp) and
// differ only in the UUIDs and how the peer is reached.
//
// When `outMsg` is non-null it receives a human-readable result/cause (e.g.
// "HiveInside OTA completed", "HiveInside not found in scan", "firmware download
// failed (HTTP 404)") so the caller can report the real outcome to the backend
// instead of a bare boolean.
bool updateHiveInside(const String& mac, const String& firmwareUrl,
                      uint32_t expectedCrc32 = 0, String* outMsg = nullptr);
// The HiveTraffic counter equivalent. Note the counter STOPS COUNTING for the
// duration of the transfer (it parks the IR emitters and pauses gate polling
// while writing flash), so this is not something to run speculatively.
bool updateBeeCounter(const String& mac, const String& firmwareUrl,
                      uint32_t expectedCrc32 = 0, String* outMsg = nullptr);
void checkForOtaUpdate();
void postCommandResult(int commandId, bool success, const String& message);
void checkCommands();
