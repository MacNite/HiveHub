// network.cpp — WiFi, HTTP, upload, OTA and command-queue implementation.
#include "hivehub_network.h"
#include "globals.h"
#include "config.h"
#include "device_prefs.h"
#include "storage_power.h"
#include "portal.h"
#include "ca_cert.h"
#include "hive_config.h"

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <Update.h>
#include <esp_heap_caps.h>
#include <esp_app_format.h>   // esp_image_header_t / ESP_CHIP_ID_* for the OTA arch guard
#include <time.h>

#if ENABLE_BLE_SCAN
#include "ble_sensor.h"
#endif

// NTP sync — called once after WiFi connects each wake cycle.
// Certificate validation requires the device clock to be accurate.
static bool timeSynced = false;

static void syncTimeIfNeeded() {
  if (timeSynced) return;
  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.print("[NTP] Syncing time");
  struct tm t;
  unsigned long start = millis();
  while (millis() - start < 8000) {
    if (getLocalTime(&t, 0)) {
      timeSynced = true;
      Serial.printf(" OK (%04d-%02d-%02d %02d:%02d:%02d UTC)\n",
        t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
        t.tm_hour, t.tm_min, t.tm_sec);
      return;
    }
    Serial.print(".");
    delay(200);
  }
  Serial.println();
  Serial.println("[NTP] Time sync timed out — TLS cert validation may fail");
}

static void applyTlsConfig(WiFiClientSecure& client) {
  client.setCACert(SERVER_CA_CERT);
}

String apiUrl(const String& path) {
  String base = trimTrailingSlash(apiBaseUrl);
  return base + path;
}

bool connectWifi(unsigned long timeoutMs) {
  if (WiFi.status() == WL_CONNECTED) {
    syncTimeIfNeeded();
    return true;
  }

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
      syncTimeIfNeeded();
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
  applyTlsConfig(client);
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
  applyTlsConfig(client);
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

bool httpPatchJson(const String& url, const String& json, String* response) {
  if (!connectWifi()) {
    Serial.println("[HTTP PATCH] No WiFi");
    return false;
  }

  Serial.println("[HTTP PATCH]");
  Serial.print("[HTTP PATCH] URL: ");
  Serial.println(url);
  Serial.print("[HTTP PATCH] Payload: ");
  Serial.println(json);

  WiFiClientSecure client;
  applyTlsConfig(client);
  HTTPClient http;

  if (!http.begin(client, url)) {
    Serial.println("[HTTP PATCH] http.begin failed");
    return false;
  }

  http.addHeader("Content-Type", "application/json");
  addAuthHeader(http);

  int code = http.sendRequest("PATCH", (uint8_t*)json.c_str(), json.length());
  String body = http.getString();

  Serial.printf("[HTTP PATCH] Status: %d\n", code);
  Serial.print("[HTTP PATCH] Response: ");
  Serial.println(body);

  if (response) *response = body;

  http.end();

  if (code >= 200 && code < 300) {
    Serial.println("[HTTP PATCH] SUCCESS");
    return true;
  }

  Serial.println("[HTTP PATCH] FAILED");
  return false;
}

bool uploadLine(const String& line, bool* claimConfirmed) {
  String response;
  bool ok = httpPostJson(apiUrl("/api/v1/measurements"), line, &response);

  if (!ok) Serial.println("[UPLOAD] Upload failed");
  else Serial.println("[UPLOAD] Upload accepted by server");

  // Report whether the server considers this device claimed, so the caller can
  // decide when it is safe to stop sending the claim code (see markClaimRegistered
  // / device_prefs.cpp). We must NOT latch the claim on a merely-successful upload:
  // a rebuilt or restored backend has no record of the device yet, and if the
  // device stopped sending its claim code it could never be claimed again.
  // Older servers do not return "claimed"; fall back to treating a successful
  // upload as confirmation so behaviour against them is unchanged.
  if (claimConfirmed) {
    bool confirmed = ok;
    if (ok && response.length()) {
      JsonDocument doc;
      if (deserializeJson(doc, response) == DeserializationError::Ok &&
          doc["claimed"].is<bool>()) {
        confirmed = doc["claimed"].as<bool>();
      }
    }
    *claimConfirmed = confirmed;
  }

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

  // Bridge calibration into the hive registry (the authoritative source for the
  // read path) ONLY when the server config actually changed since it was last
  // applied. /config always returns scale1/2 offset+factor (DB defaults
  // 0 / -7050), and the server never learns a calibration done locally on the
  // portal's /calibrate page — so applying these fields unconditionally every
  // cycle silently reverts a portal tare/span one cycle later. config_version
  // increments on every server-side config edit (dashboard PATCH or command
  // result) and is therefore the change signal:
  //   - same version as last applied  -> nothing changed server-side; keep the
  //     local (possibly portal-calibrated) values;
  //   - different version             -> a deliberate server-side edit; apply it;
  //   - no version ever applied (fresh device, first boot after upgrade, or
  //     after a factory reset) -> record the version but do NOT bridge: the
  //     server only holds defaults for a device it has never calibrated, while
  //     the device may already carry a portal calibration.
  // The legacy scale1/2 fields map to hives 1–2 scale[0]; an optional per-hive
  // array calibrates the rest:
  //   "hive_scales": [ { "index": 5, "scale": 0, "offset": 123, "factor": -7100 }, … ]
  uint32_t remoteVersion = doc["config_version"] | 0;
  prefs.begin("hivescale", true);
  uint32_t appliedVersion = prefs.getUInt("cfg_applied", 0);
  prefs.end();
  bool versionChanged = remoteVersion != 0 && remoteVersion != appliedVersion;
  bool applyCalibration = versionChanged && appliedVersion != 0;

  if (applyCalibration) {
    scale1Offset = doc["scale1_offset"] | scale1Offset;
    scale1Factor = doc["scale1_factor"] | scale1Factor;
    scale2Offset = doc["scale2_offset"] | scale2Offset;
    scale2Factor = doc["scale2_factor"] | scale2Factor;

    bool regChanged = false;
    auto applyToHive = [&regChanged](uint8_t hiveIndex, uint8_t scaleIdx, long off, float fac) {
      for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
        if (hivecfg::gHives[h].index != hiveIndex) continue;
        if (scaleIdx < hivecfg::gHives[h].scaleCount) {
          auto& ch = hivecfg::gHives[h].scales[scaleIdx];
          if (ch.offset != off || ch.factor != fac) {
            ch.offset = off; ch.factor = fac; regChanged = true;
          }
        }
        return;
      }
    };
    applyToHive(1, 0, scale1Offset, scale1Factor);
    applyToHive(2, 0, scale2Offset, scale2Factor);
    for (JsonObject o : doc["hive_scales"].as<JsonArray>()) {
      uint8_t idx = (uint8_t)(o["index"] | 0);
      uint8_t sc  = (uint8_t)(o["scale"] | 0);
      if (idx < 1) continue;
      for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
        if (hivecfg::gHives[h].index != idx || sc >= hivecfg::gHives[h].scaleCount) continue;
        auto& ch = hivecfg::gHives[h].scales[sc];
        if (!o["offset"].isNull()) {
          long v = (long)(o["offset"] | 0L);
          if (ch.offset != v) { ch.offset = v; regChanged = true; }
        }
        if (!o["factor"].isNull()) {
          float v = (float)(o["factor"] | -7050.0);
          if (ch.factor != v) { ch.factor = v; regChanged = true; }
        }
      }
    }
    if (regChanged) hivecfg::saveHiveConfig();
  }

  if (doc["claim_code"].is<const char*>()) {
    String remoteClaimCode = doc["claim_code"].as<String>();
    remoteClaimCode.trim();
    if (remoteClaimCode.length() > 0 && remoteClaimCode != claimCode) {
      Serial.println("[CONFIG] Updating claim code from remote config");
      claimCode = remoteClaimCode;
      putPrefString("claim_code", claimCode);
    }
  }

  // Persist interval + legacy scale keys, and remember which server version was
  // applied, only when the version moved — fetchRemoteConfig runs every cycle,
  // and rewriting NVS each time would wear the flash for no reason.
  if (versionChanged) {
    saveScaleConfig();
    prefs.begin("hivescale", false);
    prefs.putUInt("cfg_applied", remoteVersion);
    prefs.end();
  }
  Serial.printf("[CONFIG] Remote config applied (version %lu, calibration %s)\n",
                (unsigned long)remoteVersion,
                applyCalibration ? "applied" : "unchanged");
}

void reportScaleCalibration() {
  // Report the device's calibration to the server so a tare/span done offline on
  // the portal is reflected server-side. Two storage shapes, matching the reverse
  // bridge in fetchRemoteConfig():
  //   - hives 1–2  -> the legacy scale1/2 offset+factor columns;
  //   - hives 3–18 -> the hive_scales[] array (server keeps these per hive).
  // Only a hive that actually carries a valid scale is reported, so a device that
  // uses (say) only hive 2 never clobbers another slot with a default.
  JsonDocument body;
  int reported = 0;
  auto addLegacy = [&](uint8_t hiveIndex, const char* offKey, const char* facKey) {
    for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
      if (hivecfg::gHives[h].index != hiveIndex) continue;
      if (hivecfg::gHives[h].scaleCount == 0) return;
      const hivecfg::ScaleChannel& ch = hivecfg::gHives[h].scales[0];
      if (!ch.valid()) return;
      body[offKey] = ch.offset;
      body[facKey] = ch.factor;
      reported++;
      return;
    }
  };
  addLegacy(1, "scale1_offset", "scale1_factor");
  addLegacy(2, "scale2_offset", "scale2_factor");

  JsonArray hiveScales = body["hive_scales"].to<JsonArray>();
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    if (hive.index < 3) continue;   // 1–2 are covered by the legacy fields above
    if (hive.scaleCount == 0) continue;
    const hivecfg::ScaleChannel& ch = hive.scales[0];
    if (!ch.valid()) continue;
    JsonObject o = hiveScales.add<JsonObject>();
    o["index"] = hive.index;
    o["scale"] = 0;              // one scale per hive today (MAX_SCALES_PER_HIVE)
    o["offset"] = ch.offset;
    o["factor"] = ch.factor;
    reported++;
  }
  if (hiveScales.size() == 0) body.remove("hive_scales");

  if (reported == 0) {
    Serial.println("[CONFIG] No local scale calibration to report; clearing pending flag");
    clearScaleCalibrationReport();
    return;
  }

  String payload;
  serializeJson(body, payload);

  String url = apiUrl(String("/api/v1/devices/") + deviceId + "/config");
  String response;
  if (!httpPatchJson(url, payload, &response)) {
    // Keep the pending flag set so the report is retried on the next cycle.
    Serial.println("[CONFIG] Failed to report scale calibration; will retry next cycle");
    return;
  }

  // The PATCH incremented config_version and the response echoes the new config.
  // Record that version as the one we've applied so the fetchRemoteConfig() right
  // after this sees "unchanged" and does not bridge these very values back into
  // the registry (harmless, but it keeps cfg_applied honest and skips a needless
  // NVS write).
  JsonDocument doc;
  if (!deserializeJson(doc, response) && !doc["config_version"].isNull()) {
    uint32_t newVersion = doc["config_version"] | 0;
    if (newVersion != 0) {
      prefs.begin("hivescale", false);
      prefs.putUInt("cfg_applied", newVersion);
      prefs.end();
    }
  }

  clearScaleCalibrationReport();
  Serial.println("[CONFIG] Scale calibration reported to server");
}

String absoluteUrl(String maybeRelativeUrl) {
  maybeRelativeUrl.trim();
  if (maybeRelativeUrl.startsWith("http://") || maybeRelativeUrl.startsWith("https://")) return maybeRelativeUrl;
  if (!maybeRelativeUrl.startsWith("/")) maybeRelativeUrl = "/" + maybeRelativeUrl;
  return trimTrailingSlash(apiBaseUrl) + maybeRelativeUrl;
}

// The esp-image chip_id this build belongs to. Used to reject a firmware image
// compiled for a different SoC (e.g. an ESP32-C6/RISC-V image arriving at a 30-pin
// ESP32/Xtensa) before a single byte is written to the OTA partition. Returns
// ESP_CHIP_ID_INVALID when the target is unknown, in which case the check is
// skipped rather than blocking OTA on an unrecognised build.
static uint16_t expectedImageChipId() {
#if defined(CONFIG_IDF_TARGET_ESP32C6)
  return ESP_CHIP_ID_ESP32C6;
#elif defined(CONFIG_IDF_TARGET_ESP32)
  return ESP_CHIP_ID_ESP32;
#else
  return ESP_CHIP_ID_INVALID;
#endif
}

// Rolling CRC-32 (IEEE 802.3, reflected 0xEDB88320 — the same algorithm as the
// backend's zlib.crc32). Start with crc=0 and feed each
// chunk the previous call's return value; the final value is the file CRC.
static uint32_t crc32Update(uint32_t crc, const uint8_t* data, size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t b = 0; b < 8; b++)
      crc = (crc & 1) ? (crc >> 1) ^ 0xEDB88320u : (crc >> 1);
  }
  return ~crc;
}

// Adapts Update to the Stream interface HTTPClient::writeToStream() wants.
// writeToStream is used instead of reading the raw socket because it is the
// only HTTPClient download path that de-frames a Transfer-Encoding: chunked
// body (a proxy/CDN in front of the backend may re-frame the response that
// way); reading the raw stream would interleave chunk-size lines into the
// image. The sink also holds back the first esp_image header for the
// architecture guard and keeps a rolling CRC-32 of everything it forwards, so
// the caller can verify the download before committing the OTA partition.
// Returning 0 from write() makes writeToStream abort the transfer.
class OtaUpdateSink : public Stream {
 public:
  explicit OtaUpdateSink(uint16_t expectedChipId) : _expectedChip(expectedChipId) {}

  size_t write(const uint8_t* data, size_t len) override {
    if (_failed) return 0;
    size_t consumed = 0;
    while (_headerFill < sizeof(_header) && consumed < len)
      ((uint8_t*)&_header)[_headerFill++] = data[consumed++];
    if (_headerFill == sizeof(_header) && !_headerChecked) {
      _headerChecked = true;
      if (_header.magic != ESP_IMAGE_HEADER_MAGIC) {
        Serial.printf("[OTA] Bad image magic 0x%02X (expected 0x%02X); aborting\n",
                      _header.magic, ESP_IMAGE_HEADER_MAGIC);
        _failed = true;
        return 0;
      }
      if (_expectedChip != ESP_CHIP_ID_INVALID && _header.chip_id != _expectedChip) {
        Serial.printf("[OTA] Wrong chip: image chip_id=0x%04X, this device=0x%04X (%s) — refusing to flash\n",
                      (unsigned)_header.chip_id, (unsigned)_expectedChip, HIVEHUB_BOARD_LABEL);
        _failed = true;
        return 0;
      }
      if (Update.write((uint8_t*)&_header, sizeof(_header)) != sizeof(_header)) {
        _failed = true;
        return 0;
      }
      _crc = crc32Update(_crc, (const uint8_t*)&_header, sizeof(_header));
      _written += sizeof(_header);
    }
    if (consumed < len) {
      size_t n = Update.write(const_cast<uint8_t*>(data) + consumed, len - consumed);
      _crc = crc32Update(_crc, data + consumed, n);
      _written += n;
      consumed += n;
      if (consumed != len) _failed = true;
    }
    return consumed;
  }
  size_t write(uint8_t b) override { return write(&b, 1); }

  // Stream demands a read side; the sink is write-only.
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}

  bool failed() const { return _failed; }
  size_t written() const { return _written; }
  uint32_t crc32() const { return _crc; }

 private:
  esp_image_header_t _header{};
  size_t _headerFill = 0;
  bool _headerChecked = false;
  bool _failed = false;
  size_t _written = 0;
  uint32_t _crc = 0;
  uint16_t _expectedChip;
};

bool performFirmwareUpdate(const String& firmwareUrl, int expectedSize,
                           uint32_t expectedCrc32) {
  if (!connectWifi()) return false;

  String url = absoluteUrl(firmwareUrl);
  Serial.print("[OTA] Downloading firmware: ");
  Serial.println(url);

  WiFiClientSecure client;
  applyTlsConfig(client);
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

  // A reverse proxy/CDN in front of the backend (e.g. Cloudflare) may deliver
  // the image with Transfer-Encoding: chunked, i.e. without a Content-Length
  // header — getSize() is then -1 even though the download itself is fine.
  // Fall back to the size the backend reported in the OTA check response; when
  // even that is unknown (older backend) proceed with UPDATE_SIZE_UNKNOWN, but
  // only if a CRC is available to prove the download arrived complete.
  int contentLength = http.getSize();
  int totalSize = contentLength > 0 ? contentLength : expectedSize;
  if (totalSize <= 0 && expectedCrc32 == 0) {
    // Without a length OR a checksum a truncated download is indistinguishable
    // from a complete one — refuse rather than flash blind.
    Serial.printf("[OTA] Invalid content length %d and no expected size/CRC from backend; aborting\n",
                  contentLength);
    http.end();
    return false;
  }
  if (totalSize > 0 && totalSize < (int)sizeof(esp_image_header_t)) {
    Serial.println("[OTA] Image smaller than its header; aborting");
    http.end();
    return false;
  }

  if (!Update.begin(totalSize > 0 ? (size_t)totalSize : UPDATE_SIZE_UNKNOWN)) {
    Serial.printf("[OTA] Update.begin failed. Error %d\n", Update.getError());
    http.end();
    return false;
  }

  // The sink enforces the architecture guard (esp-image magic + chip_id) on the
  // leading header bytes before anything reaches the OTA partition — a
  // cross-architecture image (e.g. an ESP32-C6/RISC-V build reaching a 30-pin
  // ESP32/Xtensa) would not boot, so a mislabeled or misrouted release never
  // starts a write. The server's per-board OTA matching is the primary defence;
  // this is the on-device backstop.
  OtaUpdateSink sink(expectedImageChipId());
  int streamed = http.writeToStream(&sink);
  http.end();

  if (streamed < 0 && !sink.failed()) {
    Serial.printf("[OTA] Download failed: %s\n", HTTPClient::errorToString(streamed).c_str());
  }
  if (streamed < 0 || sink.failed()) {
    Update.abort();
    return false;
  }

  if (totalSize > 0 && sink.written() != (size_t)totalSize) {
    Serial.printf("[OTA] Written only %u/%d bytes; aborting\n",
                  (unsigned)sink.written(), totalSize);
    Update.abort();
    return false;
  }
  if (sink.written() < sizeof(esp_image_header_t)) {
    Serial.printf("[OTA] Image truncated at %u bytes; aborting\n", (unsigned)sink.written());
    Update.abort();
    return false;
  }
  if (expectedCrc32 != 0 && sink.crc32() != expectedCrc32) {
    Serial.printf("[OTA] CRC mismatch: got 0x%08X expected 0x%08X; aborting\n",
                  (unsigned)sink.crc32(), (unsigned)expectedCrc32);
    Update.abort();
    return false;
  }

  // With UPDATE_SIZE_UNKNOWN the updater can never observe "finished" on its
  // own, so pass evenIfRemaining=true; the size/CRC checks above already vouch
  // for the image being complete, and end() still runs esp_image validation
  // before the boot partition is switched.
  if (!Update.end(true)) {
    Serial.printf("[OTA] Update.end failed. Error %d\n", Update.getError());
    return false;
  }

  Serial.println("[OTA] Update successful, rebooting");
  delay(1000);
  ESP.restart();
  return true;
}

// NOTE: the BeeCounter OTA-over-the-wire relay was removed with the rest of the
// wired BeeCounter path. BeeCounter firmware updates will eventually run over
// BLE/GATT, but that is NOT implemented yet — there is currently no remote
// BeeCounter update path. The obsolete update_beecounter command from older
// servers is rejected explicitly in checkCommands() below.

#if ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED
// Relay a HiveInside firmware image to the paired sensor at `mac` over BLE GATT.
//
// A HiveInside ESP32-C6 image is >1 MB and will NOT fit in the
// WROOM's RAM. So this STREAMS: it opens the HTTPS download, opens the BLE OTA
// session, then pumps the body straight from the socket into the GATT DATA
// characteristic a chunk at a time. The HiveInside device verifies the
// end-to-end CRC-32 (passed in BEGIN) before swapping its OTA slot, so a
// corrupted relay can never brick the sensor — it just aborts and keeps running
// the old image.
bool updateHiveInside(const String& mac, const String& firmwareUrl,
                      uint32_t expectedCrc32, String* outMsg) {
  auto setMsg = [&](const String& m) { if (outMsg) *outMsg = m; };

  if (!connectWifi()) { setMsg("WiFi connect failed"); return false; }

  String url = absoluteUrl(firmwareUrl);
  Serial.print("[HI-OTA] Downloading HiveInside firmware: ");
  Serial.println(url);

  WiFiClientSecure client;
  applyTlsConfig(client);
  HTTPClient http;
  http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  if (!http.begin(client, url)) {
    Serial.println("[HI-OTA] http.begin failed");
    setMsg("firmware download init failed");
    return false;
  }

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[HI-OTA] Download failed. HTTP %d\n", code);
    http.end();
    setMsg(String("firmware download failed (HTTP ") + code + ")");
    return false;
  }

  int contentLength = http.getSize();
  if (contentLength <= 0 || contentLength > 4 * 1024 * 1024) {
    Serial.printf("[HI-OTA] Invalid content length %d\n", contentLength);
    http.end();
    setMsg(String("invalid firmware content length ") + contentLength);
    return false;
  }

  // Open the BLE OTA session (locates the device, connects, sends BEGIN). Do
  // this AFTER we have the Content-Length so the device sizes its OTA slot.
  if (!blesensor::otaBegin(mac, (uint32_t)contentLength, expectedCrc32)) {
    Serial.println("[HI-OTA] otaBegin failed");
    http.end();
    setMsg(blesensor::otaLastError().length() ? blesensor::otaLastError()
                                              : String("BLE OTA begin failed"));
    return false;
  }

  // Pump the HTTPS body straight into the GATT DATA characteristic. The relay
  // buffer is tiny (one MTU-sized chunk is written per otaWrite call internally);
  // 1 KB here just amortises socket reads.
  static const size_t RELAY_BUF = 1024;
  uint8_t buf[RELAY_BUF];
  WiFiClient* stream = http.getStreamPtr();
  int read = 0;
  unsigned long lastData = millis();
  bool relayOk = true;
  bool stalled = false;
  while (read < contentLength && (http.connected() || stream->available())) {
    size_t avail = stream->available();
    if (avail) {
      int r = stream->readBytes(buf, min(min(avail, RELAY_BUF), (size_t)(contentLength - read)));
      if (r > 0) {
        if (!blesensor::otaWrite(buf, (size_t)r)) {
          Serial.printf("[HI-OTA] relay write failed at %d/%d\n", read, contentLength);
          relayOk = false;
          break;
        }
        read += r;
        lastData = millis();
        if ((read % (32 * 1024)) < (size_t)r) {
          Serial.printf("[HI-OTA] relayed %d/%d bytes\n", read, contentLength);
        }
      }
    } else if (millis() - lastData > 15000) {
      Serial.println("[HI-OTA] download stalled");
      relayOk = false;
      stalled = true;
      break;
    } else {
      delay(1);
    }
  }
  http.end();

  bool ok = false;
  if (relayOk && read == contentLength) {
    ok = blesensor::otaFinish();
    if (!ok) {
      setMsg(blesensor::otaLastError().length() ? blesensor::otaLastError()
                                                : String("OTA finalize/verify failed"));
    }
  } else {
    Serial.printf("[HI-OTA] incomplete relay %d/%d — aborting\n", read, contentLength);
    blesensor::otaAbort();
    if (stalled) {
      setMsg(String("firmware download stalled at ") + read + "/" + contentLength + " bytes");
    } else {
      setMsg(blesensor::otaLastError().length()
                 ? blesensor::otaLastError()
                 : String("incomplete relay ") + read + "/" + contentLength + " bytes");
    }
  }
  blesensor::otaCleanup();

  if (ok) setMsg("HiveInside OTA completed");
  Serial.printf("[HI-OTA] result: %s\n", ok ? "OK" : "FAIL");
  return ok;
}
#endif  // ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED

void checkForOtaUpdate() {
  if (!connectNetwork()) {
    Serial.println("[OTA] Skipping: network unavailable");
    return;
  }

  JsonDocument doc;
  // Report the board/architecture so the backend only offers an image built for
  // this SoC (see HIVEHUB_BOARD_LABEL in config.h). Without it the server cannot
  // tell a 30-pin ESP32 from an ESP32-C6 and could hand over a non-bootable image.
  String url = apiUrl(String("/api/v1/devices/") + deviceId +
                      "/firmware?version=" + FIRMWARE_VERSION +
                      "&board=" + HIVEHUB_BOARD_LABEL);

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
  // Image size + CRC-32, sent by newer backends. The size stands in for a
  // missing Content-Length header (proxy/CDN chunking) and the CRC lets the
  // download be verified before flashing; both default to 0 (= unknown) when
  // the backend predates them.
  int fwSize = doc["size"] | 0;
  uint32_t fwCrc = doc["crc32"] | 0U;

  if (fwUrl.length() == 0) {
    Serial.println("[OTA] Update response missing url");
    return;
  }

  Serial.printf("[OTA] Update available: %s\n", version.c_str());
  performFirmwareUpdate(fwUrl, fwSize, fwCrc);
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
    // Obsolete command from an older server. The wired I2C BeeCounter path —
    // including its OTA relay — was removed, and BeeCounter OTA over BLE/GATT
    // is not implemented yet. Fail EXPLICITLY (never pretend to have updated,
    // never touch the bus) so the queued command surfaces as failed in the
    // backend instead of hanging or faking success.
    Serial.println("[CMD] Rejecting obsolete update_beecounter command (wired I2C BeeCounter support removed)");
    postCommandResult(commandId, false,
                      "update_beecounter is no longer supported: the wired I2C BeeCounter "
                      "path was removed and BeeCounter OTA over BLE/GATT is not implemented yet");
  }
#if ENABLE_BLE_SCAN && HIVEINSIDE_OTA_ENABLED
  else if (type == "update_hiveinside") {
    // payload: { "slot": 1|2, "url": "/firmware/hiveinside-x.y.bin", "crc32": <uint32> }
    // The MAC is resolved locally from the paired BLE sensor for that slot, so
    // the backend never needs to know the device address.
    int slot = payload["slot"] | 1;
    String mac = (slot == 2) ? bleSensorMac1 : bleSensorMac0;
    String fwUrl = payload["url"] | "";
    uint32_t crc = (uint32_t)(payload["crc32"] | 0);
    if (fwUrl.length() == 0) {
      postCommandResult(commandId, false, "update_hiveinside missing url");
    } else if (mac.length() == 0) {
      postCommandResult(commandId, false, String("No HiveInside paired in slot ") + slot);
    } else {
      // Report the FINAL result with a specific cause, not "started": the relay
      // runs synchronously (it can take minutes for a >1 MB image) and we only
      // know the real outcome once updateHiveInside returns. Reporting "started"
      // eagerly was the main reason a failed OTA still looked successful.
      String resultMsg;
      bool ok = updateHiveInside(mac, fwUrl, crc, &resultMsg);
      Serial.printf("[HI-OTA] update result: %s (%s)\n",
                    ok ? "OK" : "FAIL", resultMsg.c_str());
      postCommandResult(commandId, ok,
                        resultMsg.length() ? resultMsg
                                           : (ok ? "HiveInside OTA completed"
                                                 : "HiveInside OTA failed"));
    }
  }
#endif
  else if (type == "start_provisioning") {
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
