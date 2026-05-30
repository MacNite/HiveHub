// network.cpp — WiFi, HTTP, upload, OTA and command-queue implementation.
#include "network.h"
#include "globals.h"
#include "config.h"
#include "device_prefs.h"
#include "storage_power.h"
#include "portal.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Update.h>
#include <esp_heap_caps.h>

#include "bee_counter_client.h"

String apiUrl(const String& path) {
  String base = trimTrailingSlash(apiBaseUrl);
  return base + path;
}

bool connectWifi(unsigned long timeoutMs) {
  if (WiFi.status() == WL_CONNECTED) return true;

  int count = getWifiCount();
  if (count <= 0) {
    Serial.println("[WIFI] No saved WiFi credentials");
    return false;
  }

  String ssids[MAX_WIFI_NETWORKS];
  String passes[MAX_WIFI_NETWORKS];
  prefs.begin("hivescale", true);
  for (int i = 0; i < count; i++) {
    ssids[i] = prefs.getString(wifiSsidKey(i).c_str(), "");
    passes[i] = prefs.getString(wifiPassKey(i).c_str(), "");
  }
  prefs.end();

  WiFi.mode(WIFI_STA);
  WiFi.setSleep(true);

  for (int i = 0; i < count; i++) {
    String ssid = ssids[i];
    String pass = passes[i];

    if (ssid.length() == 0) continue;

    Serial.printf("[WIFI] Trying saved network %d/%d: %s\n", i + 1, count, ssid.c_str());
    WiFi.disconnect(true, true);
    delay(200);
    WiFi.begin(ssid.c_str(), pass.c_str());

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < timeoutMs) {
      Serial.print(".");
      delay(500);
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
      activeWifiSsid = ssid;
      Serial.println("[WIFI] Connected");
      Serial.print("[WIFI] IP: ");
      Serial.println(WiFi.localIP());
      Serial.printf("[WIFI] RSSI: %d dBm\n", WiFi.RSSI());
      return true;
    }

    Serial.printf("[WIFI] Failed network: %s status=%d\n", ssid.c_str(), WiFi.status());
  }

  Serial.println("[WIFI] All saved networks failed. Not starting AP automatically for power saving.");
  return false;
}

bool connectNetwork() {
  return connectWifi();
}

void addAuthHeader(HTTPClient& http) {
  if (apiKey.length() > 0) http.addHeader("X-API-Key", apiKey);
}

bool httpGetJson(const String& url, JsonDocument& doc) {
  if (!connectWifi()) return false;

  Serial.println("[HTTP GET]");
  Serial.println(url);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP GET] http.begin failed");
    return false;
  }

  addAuthHeader(http);

  int code = http.GET();
  String body = http.getString();

  Serial.printf("[HTTP GET] Status: %d\n", code);
  Serial.print("[HTTP GET] Body: ");
  Serial.println(body);

  http.end();

  if (code < 200 || code >= 300) return false;

  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    Serial.print("[HTTP GET] JSON parse error: ");
    Serial.println(err.c_str());
    return false;
  }

  return true;
}

bool httpPostJson(const String& url, const String& json, String* response) {
  if (!connectWifi()) {
    Serial.println("[HTTP POST] No WiFi");
    return false;
  }

  Serial.println("[HTTP POST]");
  Serial.print("[HTTP POST] URL: ");
  Serial.println(url);
  Serial.print("[HTTP POST] Payload: ");
  Serial.println(json);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP POST] http.begin failed");
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  addAuthHeader(http);

  int code = http.POST((uint8_t*)json.c_str(), json.length());
  String body = http.getString();

  Serial.printf("[HTTP POST] Status: %d\n", code);
  Serial.print("[HTTP POST] Response: ");
  Serial.println(body);

  if (response) *response = body;

  http.end();

  if (code >= 200 && code < 300) {
    Serial.println("[HTTP POST] SUCCESS");
    return true;
  }

  Serial.println("[HTTP POST] FAILED");
  return false;
}

bool uploadLine(const String& line) {
  String response;
  bool ok = httpPostJson(apiUrl("/api/v1/measurements"), line, &response);

  if (!ok) Serial.println("[UPLOAD] Upload failed");
  else Serial.println("[UPLOAD] Upload accepted by server");

  return ok;
}

bool uploadCachedLines() {
  if (!sdOk) {
    Serial.println("[CACHE] No SD card, skipping cached upload");
    return true;
  }

  if (!SD.exists(CACHE_FILE)) {
    Serial.println("[CACHE] No cache file");
    return true;
  }

  if (!cacheFileLooksSane()) {
    Serial.println("[CACHE] Cache file was quarantined or removed; skipping cached upload this cycle");
    return false;
  }

  File in = SD.open(CACHE_FILE, FILE_READ);
  if (!in) {
    Serial.println("[CACHE] Failed to open cache file for read");
    return false;
  }

  SD.remove(TEMP_FILE);
  File out = SD.open(TEMP_FILE, FILE_WRITE);
  if (!out) {
    Serial.println("[CACHE] Failed to open temp cache file");
    in.close();
    return false;
  }

  bool encounteredFailure = false;
  bool hitUploadLimit = false;
  int total = 0;
  int uploaded = 0;
  int kept = 0;
  int dropped = 0;

  while (in.available()) {
    String line = in.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    total++;

    if (line.length() > CACHE_MAX_LINE_BYTES) {
      dropped++;
      Serial.printf("[CACHE] Dropping oversized cached line %d (%u bytes)\n", total, (unsigned)line.length());
      continue;
    }

    bool mayUpload = !encounteredFailure && uploaded < CACHE_UPLOAD_MAX_LINES_PER_CYCLE;

    if (mayUpload) {
      Serial.printf("[CACHE] Uploading cached line %d\n", total);
      if (uploadLine(line)) {
        uploaded++;
        delay(100);
        continue;
      }

      encounteredFailure = true;
      Serial.println("[CACHE] Cached upload failed; keeping this and remaining cached lines");
    } else if (!encounteredFailure && uploaded >= CACHE_UPLOAD_MAX_LINES_PER_CYCLE) {
      hitUploadLimit = true;
    }

    kept++;
    size_t written = out.println(line);
    if (written == 0) {
      Serial.println("[CACHE] Failed to write retained line to temp cache");
      encounteredFailure = true;
    }
  }

  in.close();
  out.flush();
  out.close();

  if (!SD.remove(CACHE_FILE)) {
    Serial.println("[CACHE] Warning: failed to remove old cache file");
  }

  if (kept > 0) {
    if (!SD.rename(TEMP_FILE, CACHE_FILE)) {
      Serial.println("[CACHE] ERROR: failed to rename temp cache file back to cache file");
      return false;
    }
  } else {
    SD.remove(TEMP_FILE);
  }

  Serial.printf(
    "[CACHE] Total=%d Uploaded=%d Kept=%d Dropped=%d Limit=%s\n",
    total,
    uploaded,
    kept,
    dropped,
    hitUploadLimit ? "yes" : "no"
  );

  return kept == 0 && !encounteredFailure;
}

void fetchRemoteConfig() {
  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/config");

  Serial.println("[CONFIG] Fetching remote config");

  if (!httpGetJson(url, doc)) {
    Serial.println("[CONFIG] Failed to fetch config");
    return;
  }

  sendIntervalMs = (unsigned long)(doc["send_interval_seconds"] | 600) * 1000UL;
  scale1Offset = doc["scale1_offset"] | scale1Offset;
  scale1Factor = doc["scale1_factor"] | scale1Factor;
  scale2Offset = doc["scale2_offset"] | scale2Offset;
  scale2Factor = doc["scale2_factor"] | scale2Factor;

  if (doc["claim_code"].is<const char*>()) {
    String remoteClaimCode = doc["claim_code"].as<String>();
    remoteClaimCode.trim();
    if (remoteClaimCode.length() > 0 && remoteClaimCode != claimCode) {
      Serial.println("[CONFIG] Updating claim code from remote config");
      claimCode = remoteClaimCode;
      putPrefString("claim_code", claimCode);
    }
  }

  saveScaleConfig();
  Serial.println("[CONFIG] Remote config applied");
}

String absoluteUrl(String maybeRelativeUrl) {
  maybeRelativeUrl.trim();
  if (maybeRelativeUrl.startsWith("http://") || maybeRelativeUrl.startsWith("https://")) return maybeRelativeUrl;
  if (!maybeRelativeUrl.startsWith("/")) maybeRelativeUrl = "/" + maybeRelativeUrl;
  return trimTrailingSlash(apiBaseUrl) + maybeRelativeUrl;
}

bool performFirmwareUpdate(const String& firmwareUrl) {
  if (!connectWifi()) return false;

  String url = absoluteUrl(firmwareUrl);
  Serial.print("[OTA] Downloading firmware: ");
  Serial.println(url);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);

  if (!http.begin(client, url)) {
    Serial.println("[OTA] http.begin failed");
    return false;
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[OTA] Download failed. HTTP %d\n", code);
    http.end();
    return false;
  }

  int contentLength = http.getSize();
  if (contentLength <= 0) {
    Serial.println("[OTA] Invalid content length");
    http.end();
    return false;
  }

  if (!Update.begin(contentLength)) {
    Serial.printf("[OTA] Update.begin failed. Error %d\n", Update.getError());
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  size_t written = Update.writeStream(*stream);

  if (written != (size_t)contentLength) {
    Serial.printf("[OTA] Written only %u/%d bytes\n", (unsigned)written, contentLength);
  }

  bool ok = Update.end();
  if (!ok) {
    Serial.printf("[OTA] Update.end failed. Error %d\n", Update.getError());
    http.end();
    return false;
  }

  if (!Update.isFinished()) {
    Serial.println("[OTA] Update not finished");
    http.end();
    return false;
  }

  http.end();
  Serial.println("[OTA] Update successful, rebooting");
  delay(1000);
  ESP.restart();
  return true;
}

// Download a BeeCounter firmware image fully into RAM, then relay it to the
// BeeCounter slave over I2C. `expectedCrc32` is the image CRC the backend
// computed at release time (0 = compute locally, less safe). Returns true on a
// confirmed update.
bool updateBeeCounter(uint8_t address, const String& firmwareUrl, uint32_t expectedCrc32) {
  if (!connectWifi()) return false;

  String url = absoluteUrl(firmwareUrl);
  Serial.print("[BEE-OTA] Downloading BeeCounter firmware: ");
  Serial.println(url);

  WiFiClientSecure client;
  client.setInsecure();
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) {
    Serial.println("[BEE-OTA] http.begin failed");
    return false;
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[BEE-OTA] Download failed. HTTP %d\n", code);
    http.end();
    return false;
  }

  int contentLength = http.getSize();
  if (contentLength <= 0 || contentLength > 1024 * 1024) {
    Serial.printf("[BEE-OTA] Invalid content length %d\n", contentLength);
    http.end();
    return false;
  }

  // Allocate the image buffer. Prefer PSRAM if the board has it; fall back to
  // internal heap. 400 KB fits comfortably in C6 PSRAM; on a no-PSRAM build
  // confirm ESP.getFreeHeap() has room before relying on this.
  uint8_t* buf = (uint8_t*)heap_caps_malloc(contentLength, MALLOC_CAP_SPIRAM);
  if (!buf) buf = (uint8_t*)malloc(contentLength);
  if (!buf) {
    Serial.printf("[BEE-OTA] OOM for %d bytes (free heap %u)\n",
                  contentLength, (unsigned)ESP.getFreeHeap());
    http.end();
    return false;
  }

  WiFiClient* stream = http.getStreamPtr();
  int read = 0;
  unsigned long lastData = millis();
  while (read < contentLength && (http.connected() || stream->available())) {
    size_t avail = stream->available();
    if (avail) {
      int r = stream->readBytes(buf + read,
                                min((size_t)(contentLength - read), avail));
      read += r;
      lastData = millis();
    } else if (millis() - lastData > 10000) {
      Serial.println("[BEE-OTA] download stalled");
      break;
    } else {
      delay(1);
    }
  }
  http.end();

  if (read != contentLength) {
    Serial.printf("[BEE-OTA] short download %d/%d\n", read, contentLength);
    free(buf);
    return false;
  }

  uint32_t crc = beecnt::crc32_buf(buf, read);
  if (expectedCrc32 != 0 && crc != expectedCrc32) {
    Serial.printf("[BEE-OTA] download CRC mismatch: got 0x%08X expected 0x%08X\n",
                  (unsigned)crc, (unsigned)expectedCrc32);
    free(buf);
    return false;
  }

  bool ok = beecnt::pushFirmwareToBeeCounter(address, buf, read, crc);
  free(buf);
  return ok;
}

void checkForOtaUpdate() {
  if (!connectNetwork()) {
    Serial.println("[OTA] Skipping: network unavailable");
    return;
  }

  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/firmware?version=" + FIRMWARE_VERSION);

  Serial.println("[OTA] Checking for update");
  if (!httpGetJson(url, doc)) {
    Serial.println("[OTA] Check failed");
    return;
  }

  bool updateAvailable = doc["update"] | false;
  if (!updateAvailable) {
    Serial.println("[OTA] No update available");
    return;
  }

  String version = doc["version"] | "unknown";
  String fwUrl = doc["url"] | "";

  if (fwUrl.length() == 0) {
    Serial.println("[OTA] Update response missing url");
    return;
  }

  Serial.printf("[OTA] Update available: %s\n", version.c_str());
  performFirmwareUpdate(fwUrl);
}

void postCommandResult(int commandId, bool success, const String& message) {
  JsonDocument result;
  result["success"] = success;
  result["message"] = message;

  String payload;
  serializeJson(result, payload);

  httpPostJson(apiUrl(String("/api/v1/devices/") + deviceId + "/commands/" + commandId + "/result"), payload);
}

void checkCommands() {
  if (!connectNetwork()) return;

  JsonDocument doc;
  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/commands/next");

  Serial.println("[CMD] Checking for command");
  if (!httpGetJson(url, doc)) {
    Serial.println("[CMD] Command check failed");
    return;
  }

  bool hasCommand = doc["command"] | false;
  if (!hasCommand) {
    Serial.println("[CMD] No pending command");
    return;
  }

  int commandId = doc["id"] | 0;
  String type = doc["command_type"] | "";
  JsonObject payload = doc["payload"].as<JsonObject>();
  Serial.printf("[CMD] Received command %d: %s\n", commandId, type.c_str());

  if (type == "reset_preferences" || type == "factory_reset") {
    postCommandResult(commandId, true, "Preferences reset; rebooting");
    delay(500);
    factoryResetPreferences();
  } else if (type == "reset_wifi") {
    clearWifiCredentials();
    postCommandResult(commandId, true, "WiFi credentials cleared");
    delay(500);
    ESP.restart();
  } else if (type == "check_ota" || type == "ota_update") {
    postCommandResult(commandId, true, "OTA check started");
    checkForOtaUpdate();
  } else if (type == "update_beecounter") {
    // payload: { "slot": 1|2, "url": "/firmware/bee-x.bin", "crc32": <uint32> }
    int slot = payload["slot"] | 1;
    uint8_t addr = (slot == 2) ? beecnt::SLAVE_ADDR_SLOT_2 : beecnt::SLAVE_ADDR_SLOT_1;
    String fwUrl = payload["url"] | "";
    uint32_t crc = (uint32_t)(payload["crc32"] | 0);
    if (fwUrl.length() == 0) {
      postCommandResult(commandId, false, "update_beecounter missing url");
    } else {
      postCommandResult(commandId, true, "BeeCounter OTA started");
      bool ok = updateBeeCounter(addr, fwUrl, crc);
      Serial.printf("[BEE-OTA] update result: %s\n", ok ? "OK" : "FAIL");
      // Optionally report a second, final result here via a follow-up endpoint
      // if you want the backend to record success/failure after the fact.
    }
  } else if (type == "start_provisioning") {
    // This only makes sense while someone is physically near the device.
    postCommandResult(commandId, true, "Provisioning AP started");
    startProvisioningPortal();
  } else if (type == "start_calibration_mode") {
    unsigned long intervalSeconds = payload["interval_seconds"] | (CALIBRATION_MODE_DEFAULT_INTERVAL_MS / 1000UL);
    unsigned long timeoutSeconds = payload["timeout_seconds"] | (CALIBRATION_MODE_DEFAULT_TIMEOUT_MS / 1000UL);
    startCalibrationMode(intervalSeconds, timeoutSeconds);
    postCommandResult(commandId, true, "Calibration mode started");
  } else if (type == "stop_calibration_mode") {
    stopCalibrationMode("command received");
    postCommandResult(commandId, true, "Calibration mode stopped");
  } else {
    postCommandResult(commandId, false, String("Unknown command: ") + type);
  }
}
