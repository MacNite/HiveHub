// portal.cpp — captive setup portal, calibration mode and button handling.
#include "portal.h"
#include "globals.h"
#include "config.h"
#include "device_prefs.h"
#include "storage_power.h"
#include "hive_config.h"
#include "scale_bus.h"

#include <WiFi.h>
#include <ArduinoJson.h>

#if ENABLE_BLE_SCAN
#include "ble_sensor.h"
#endif

#if ENABLE_BEEHIVE_GATT
#include "beehive_gatt.h"
#endif

// ---- small JSON-to-display helpers (used only by the last-sensor panel) ---
static String jsonStringOrNA(JsonDocument& doc, const char* key);
static String jsonNumberOrNA(JsonDocument& doc, const char* key, uint8_t decimals, const char* unit);
static String jsonBoolOrNA(JsonDocument& doc, const char* key);
static void addMeasurementRow(String& html, const String& label, const String& value);

// Normalise a MAC to the canonical "AA:BB:CC:DD:EE:FF" form (or "" when
// invalid), independent of which wireless feature is compiled in. Both the
// HolyIot bridge and the beehivemonitoring GATT client expose the same
// normaliser; we just forward to whichever is built.
static String portalNormalizeMac(const String& raw) {
#if ENABLE_BEEHIVE_GATT
  return bhgatt::normalizeMac(raw);
#elif ENABLE_BLE_SCAN
  return blesensor::normalizeMac(raw);
#else
  (void)raw;
  return String("");
#endif
}

bool calibrationModeExpired() {
  if (!calibrationModeActive) return false;
  return millis() - calibrationModeStartedMs >= calibrationModeTimeoutMs;
}

void stopCalibrationMode(const String& reason) {
  if (!calibrationModeActive) return;
  calibrationModeActive = false;
  Serial.print("[CAL] Calibration mode stopped");
  if (reason.length() > 0) {
    Serial.print(": ");
    Serial.print(reason);
  }
  Serial.println();
}

void startCalibrationMode(unsigned long intervalSeconds, unsigned long timeoutSeconds) {
  unsigned long intervalMs = intervalSeconds * 1000UL;
  unsigned long timeoutMs = timeoutSeconds * 1000UL;

  if (intervalMs < CALIBRATION_MODE_MIN_INTERVAL_MS) intervalMs = CALIBRATION_MODE_MIN_INTERVAL_MS;
  if (intervalMs > CALIBRATION_MODE_MAX_INTERVAL_MS) intervalMs = CALIBRATION_MODE_MAX_INTERVAL_MS;
  if (timeoutMs == 0) timeoutMs = CALIBRATION_MODE_DEFAULT_TIMEOUT_MS;
  if (timeoutMs > CALIBRATION_MODE_MAX_TIMEOUT_MS) timeoutMs = CALIBRATION_MODE_MAX_TIMEOUT_MS;

  calibrationModeActive = true;
  calibrationModeStartedMs = millis();
  calibrationModeIntervalMs = intervalMs;
  calibrationModeTimeoutMs = timeoutMs;

  Serial.printf(
    "[CAL] Calibration mode started: interval=%lu sec timeout=%lu sec\n",
    calibrationModeIntervalMs / 1000UL,
    calibrationModeTimeoutMs / 1000UL
  );
}

String htmlEscape(String s) {
  s.replace("&", "&amp;");
  s.replace("<", "&lt;");
  s.replace(">", "&gt;");
  s.replace("\"", "&quot;");
  // Single quotes are escaped too because the portal renders these values
  // inside single-quoted HTML attributes (value='...'). Without this an SSID
  // such as "Bob's WiFi" would terminate the attribute early, corrupt the
  // form, and get truncated when the page is submitted back.
  s.replace("'", "&#39;");
  return s;
}

IPAddress provisioningPortalIp() {
  return IPAddress(192, 168, 4, 1);
}

String provisioningPortalUrl() {
  return String("http://") + provisioningPortalIp().toString() + "/";
}

void sendNoCacheHeaders() {
  setupServer.sendHeader("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0");
  setupServer.sendHeader("Pragma", "no-cache");
  setupServer.sendHeader("Expires", "0");
}

void sendPortalRedirect() {
  sendNoCacheHeaders();
  setupServer.sendHeader("Location", provisioningPortalUrl(), true);
  setupServer.send(302, "text/plain", "Redirecting to HiveScale setup portal");
}

void handleCaptivePortalProbe() {
  sendPortalRedirect();
}

static String jsonStringOrNA(JsonDocument& doc, const char* key) {
  if (doc[key].isNull()) return "n/a";
  String value = doc[key].as<String>();
  value.trim();
  return value.length() > 0 ? value : String("n/a");
}

static String jsonNumberOrNA(JsonDocument& doc, const char* key, uint8_t decimals, const char* unit) {
  if (doc[key].isNull()) return "n/a";
  double value = doc[key].as<double>();
  if (isnan(value)) return "n/a";

  String text = String(value, static_cast<unsigned int>(decimals));
  if (unit != nullptr && unit[0] != '\0') {
    text += " ";
    text += unit;
  }
  return text;
}

static String jsonBoolOrNA(JsonDocument& doc, const char* key) {
  if (doc[key].isNull()) return "n/a";
  return doc[key].as<bool>() ? "yes" : "no";
}

static void addMeasurementRow(String& html, const String& label, const String& value) {
  html += "<tr><th>" + htmlEscape(label) + "</th><td>" + htmlEscape(value) + "</td></tr>";
}

// Format a JSON value (e.g. a nested hives[].* member) for the last-sensor panel.
static String valNumberOrNA(JsonVariantConst v, uint8_t decimals, const char* unit) {
  if (v.isNull()) return "n/a";
  double value = v.as<double>();
  if (isnan(value)) return "n/a";
  String text = String(value, static_cast<unsigned int>(decimals));
  if (unit != nullptr && unit[0] != '\0') { text += " "; text += unit; }
  return text;
}
static String valStringOrNA(JsonVariantConst v) {
  if (v.isNull()) return "n/a";
  String s = v.as<String>();
  s.trim();
  return s.length() > 0 ? s : String("n/a");
}

void appendLastSensorPanel(String& html) {
  ensureLastMeasurementLoaded();

  html += "<fieldset><legend>Last sensor values</legend>";

  if (lastMeasurementJson.length() == 0) {
    html += "<p>No saved sensor values are available yet. After the next measurement cycle this panel will show the latest stored reading.</p>";
    html += "</fieldset>";
    return;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, lastMeasurementJson);
  if (err) {
    html += "<p>The last stored measurement could not be parsed.</p>";
    html += "</fieldset>";
    return;
  }

  // Device-level values.
  html += "<table>";
  addMeasurementRow(html, "Timestamp", jsonStringOrNA(doc, "timestamp"));
  addMeasurementRow(html, "Hives configured", jsonNumberOrNA(doc, "hive_count", 0, ""));
  addMeasurementRow(html, "Ambient temperature", jsonNumberOrNA(doc, "ambient_temp_c", 2, "C"));
  addMeasurementRow(html, "Ambient humidity", jsonNumberOrNA(doc, "ambient_humidity_percent", 1, "%"));
  addMeasurementRow(html, "WiFi RSSI", jsonNumberOrNA(doc, "rssi_dbm", 0, "dBm"));
  addMeasurementRow(html, "SD card OK", jsonBoolOrNA(doc, "sd_ok"));
  addMeasurementRow(html, "RTC OK", jsonBoolOrNA(doc, "rtc_ok"));
  addMeasurementRow(html, "SHT4x OK", jsonBoolOrNA(doc, "sht_ok"));

  if (!doc["solar_load_voltage_v"].isNull() || !doc["solar_current_ma"].isNull() || !doc["solar_power_mw"].isNull()) {
    addMeasurementRow(html, "Solar voltage", jsonNumberOrNA(doc, "solar_load_voltage_v", 3, "V"));
    addMeasurementRow(html, "Solar current", jsonNumberOrNA(doc, "solar_current_ma", 1, "mA"));
    addMeasurementRow(html, "Solar power", jsonNumberOrNA(doc, "solar_power_mw", 1, "mW"));
  }
  if (!doc["battery_voltage_v"].isNull() || !doc["battery_soc_percent"].isNull()) {
    addMeasurementRow(html, "Battery voltage", jsonNumberOrNA(doc, "battery_voltage_v", 3, "V"));
    addMeasurementRow(html, "Battery state of charge", jsonNumberOrNA(doc, "battery_soc_percent", 1, "%"));
    addMeasurementRow(html, "Battery alert", jsonBoolOrNA(doc, "battery_alert"));
  }
  html += "</table>";

  // Per-hive values from the hives[] array.
  JsonArray hives = doc["hives"].as<JsonArray>();
  if (hives.size() == 0) {
    html += "<p class='meta'>No per-hive data in the last measurement.</p>";
  }
  for (JsonObject h : hives) {
    int idx = h["index"] | 0;
    html += "<h3>Hive " + String(idx) + "</h3><table>";
    if (!h["name"].isNull()) addMeasurementRow(html, "Name", valStringOrNA(h["name"]));
    addMeasurementRow(html, "Weight", valNumberOrNA(h["weight_kg"], 3, "kg"));
    addMeasurementRow(html, "Scale source", valStringOrNA(h["scale_source"]));
    addMeasurementRow(html, "Raw weight", valNumberOrNA(h["raw_weight"], 0, ""));
    addMeasurementRow(html, "Temperature", valNumberOrNA(h["temp_c"], 2, "C"));
    addMeasurementRow(html, "Temp source", valStringOrNA(h["temp_source"]));
    if (!h["humidity_percent"].isNull())
      addMeasurementRow(html, "Humidity", valNumberOrNA(h["humidity_percent"], 1, "%"));
    JsonObject ble = h["ble"];
    if (!ble.isNull()) {
      addMeasurementRow(html, "BLE sensor", valStringOrNA(ble["sensor_type"]));
      if (!ble["pressure_hpa"].isNull()) addMeasurementRow(html, "BLE pressure", valNumberOrNA(ble["pressure_hpa"], 1, "hPa"));
      if (!ble["battery_percent"].isNull()) addMeasurementRow(html, "BLE battery", valNumberOrNA(ble["battery_percent"], 0, "%"));
    }
    JsonObject accel = h["accel"];
    if (!accel.isNull() && !accel["rms_mg"].isNull())
      addMeasurementRow(html, "Vibration RMS", valNumberOrNA(accel["rms_mg"], 1, "mg"));
    JsonObject bc = h["bee_counter"];
    if (!bc.isNull() && (bc["ok"] | false)) {
      addMeasurementRow(html, "Bee counter in/out", valStringOrNA(bc["total_in"]) + " / " + valStringOrNA(bc["total_out"]));
    }
    html += "</table>";
  }
  html += "<p class='meta'>Shown from the latest measurement in memory or from ";
  html += BACKUP_FILE;
  html += " on the SD card. Refresh this page after a new cycle to update it.</p>";
  html += "</fieldset>";
}

void handleSdDownloadAll() {
  if (!initSdCard()) {
    setupServer.send(503, "text/plain", "SD card not available");
    return;
  }

  File root = SD.open("/");
  if (!root || !root.isDirectory()) {
    setupServer.send(500, "text/plain", "Could not open SD root directory");
    return;
  }

  uint64_t tarSize = tarDirectorySize(root, "") + 1024;
  root.close();

  if (tarSize > 0xFFFFFFFFULL) {
    setupServer.send(413, "text/plain", "SD data is too large to stream in one download on this firmware");
    return;
  }

  root = SD.open("/");
  if (!root || !root.isDirectory()) {
    setupServer.send(500, "text/plain", "Could not reopen SD root directory");
    return;
  }

  setupServer.sendHeader("Content-Disposition", "attachment; filename=\"hivescale-sd-data.tar\"");
  setupServer.sendHeader("Connection", "close");
  setupServer.setContentLength((size_t)tarSize);
  setupServer.send(200, "application/x-tar", "");

  WiFiClient client = setupServer.client();
  streamTarDirectory(client, root, "");

  uint8_t zeros[1024];
  memset(zeros, 0, sizeof(zeros));
  client.write(zeros, sizeof(zeros));
  root.close();
  Serial.println("[SD] Download-all TAR completed");
}

#if ENABLE_BLE_SCAN
// Blocking BLE discovery page: scans for a few seconds and lists nearby devices
// so the user can copy a MAC into the pairing fields. Runs while the AP is up;
// ESP32 BLE + SoftAP coexist, though throughput dips during the scan.
void handleBleScan() {
  sendNoCacheHeaders();

  std::vector<blesensor::Discovered> found = blesensor::discover(HOLYIOT_BLE_SCAN_SECONDS);

  String html;
  html += "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>BLE scan</title>";
  html += "<style>"
          ":root{--bg:#f6f7f8;--card:#fff;--fg:#1a1d23;--muted:#667085;--border:#e4e7ec;--accent:#f59e0b;--link:#b46b00}"
          "*{box-sizing:border-box}"
          "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px;background:var(--bg);color:var(--fg);line-height:1.5}"
          ".wrap{max-width:760px;margin:0 auto}"
          "h1{font-size:1.6rem;margin:0 0 8px}a{color:var(--link)}"
          "table{border-collapse:collapse;width:100%;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden}"
          "th,td{text-align:left;border-bottom:1px solid var(--border);padding:8px 10px}th{color:var(--muted);font-weight:600}"
          "code{background:#eef0f2;border-radius:4px;padding:2px 5px}"
          "@media(prefers-color-scheme:dark){:root{--bg:#161618;--card:#1f1f23;--fg:#ececf1;--muted:#9aa0aa;--border:#33343a;--link:#f5b54a}code{background:#2a2a30}}"
          "</style>";
  html += "</head><body><div class='wrap'><h1>Nearby BLE devices</h1>";
  html += "<p>All nearby BLE devices are listed. HolyIot 25015, RuuviTag and HiveInside sensors show their type in the last column. GATT sensors (HiveHeart, wireless HiveScale) also appear — copy their MAC and paste it into the GATT pairing fields on the <a href='/'>setup page</a>.</p>";

  if (found.empty()) {
    html += "<p>No BLE devices were seen during the scan. Make sure the sensor is powered and in range, then <a href='/ble/scan'>scan again</a>.</p>";
  } else {
    html += "<table><tr><th>MAC</th><th>Name</th><th>RSSI</th><th>Sensor type</th></tr>";
    for (const auto& d : found) {
      html += "<tr><td><code>" + htmlEscape(d.mac) + "</code></td><td>" + htmlEscape(d.name) + "</td><td>" + String(d.rssi_dbm) + " dBm</td><td>" + htmlEscape(blesensor::sensorTypeName(d.type)) + "</td></tr>";
    }
    html += "</table>";
    html += "<p><a href='/ble/scan'>Scan again</a></p>";
  }
  html += "</div></body></html>";
  setupServer.send(200, "text/html", html);
}
#endif

// ── I2C / DS18B20 discovery (used to populate the hive-mapping dropdowns) ─────
// A JS array literal of every detected I2C load-cell channel: each NAU7802 (main
// bus + behind each TCA9548A channel) exposes two ADC inputs, plus the two HX711
// pin channels are always offered. Runs a quick bus probe each page render.
static String detectedScalesJs() {
  String js = "[";
  bool first = true;
  auto add = [&](int mux, int adc) {
    if (!first) js += ",";
    first = false;
    js += "{b:'nau',mux:"; js += String(mux); js += ",adc:"; js += String(adc);
    js += ",addr:"; js += String((int)NAU7802_I2C_ADDRESS);
    js += ",label:'NAU7802 ";
    js += (mux < 0) ? String("main bus") : (String("mux ch") + mux);
    js += " CH"; js += String(adc); js += "'}";
  };
#if ENABLE_NAU7802
  scalebus::muxDisableAll();
  Wire.beginTransmission(NAU7802_I2C_ADDRESS);
  if (Wire.endTransmission() == 0) { add(-1, 1); add(-1, 2); }
  if (scalebus::muxPresent()) {
    for (int ch = 0; ch < 8; ch++) {
      scalebus::muxSelect((uint8_t)ch);
      Wire.beginTransmission(NAU7802_I2C_ADDRESS);
      if (Wire.endTransmission() == 0) { add(ch, 1); add(ch, 2); }
    }
    scalebus::muxDisableAll();
  }
#endif
  if (!first) js += ",";
  js += "{b:'hx',hx:0,label:'HX711 #1 (pins)'},{b:'hx',hx:1,label:'HX711 #2 (pins)'}";
  js += "]";
  return js;
}

// A JS array literal of detected DS18B20 ROM addresses (16 hex chars each).
static String detectedProbesJs() {
  String js = "[";
#if ENABLE_DS18B20_HIVE_TEMP
  bool first = true;
  uint8_t n = ds18b20.getDeviceCount();
  for (uint8_t i = 0; i < n; i++) {
    DeviceAddress rom;
    if (!ds18b20.getAddress(rom, i)) continue;
    if (!first) js += ",";
    first = false;
    js += "'"; js += hivecfg::romToHex(rom); js += "'";
  }
#endif
  js += "]";
  return js;
}

// The current registry as a JS array in the same shape the page edits and submits
// (and that hivecfg::hiveFromJson parses back), so existing mappings — including
// stored calibration offset/factor — repaint and survive a save.
static String initialHivesJs() {
  String js = "[";
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    if (h) js += ",";
    js += "{i:"; js += String(hive.index);
    js += ",n:'"; js += htmlEscape(hive.name); js += "',s:[";
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const hivecfg::ScaleChannel& c = hive.scales[s];
      if (s) js += ",";
      if (c.backend == hivecfg::ScaleBackend::NAU7802)
        js += String("{b:'nau',mux:") + (int)c.muxChannel + ",adc:" + (int)c.adcChannel +
              ",addr:" + (int)c.i2cAddr + ",off:" + c.offset + ",fac:" + String(c.factor, 4) + "}";
      else
        js += String("{b:'hx',hx:") + (int)c.hxIndex + ",off:" + c.offset +
              ",fac:" + String(c.factor, 4) + "}";
    }
    js += "],ds:";
    if (hive.hasDsRom) { js += "'"; js += hivecfg::romToHex(hive.dsRom); js += "'"; }
    else js += "null";
    js += ",bl:[";
    for (uint8_t b = 0; b < hive.bleCount; b++) {
      if (b) js += ",";
      js += "{t:'"; js += hive.ble[b].type; js += "',m:'"; js += hive.ble[b].mac; js += "'}";
    }
    js += "]}";
  }
  js += "]";
  return js;
}

// Informational I2C-scan page (the main page already auto-scans for the dropdowns;
// this just shows the raw findings for troubleshooting).
void handleI2cScan() {
  sendNoCacheHeaders();
  String html;
  html += "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>I2C scan</title></head><body style='font-family:system-ui;margin:24px'>";
  html += "<h1>I2C scale scan</h1>";
#if ENABLE_I2C_MUX
  html += "<p>TCA9548A mux: <b>";
  html += scalebus::muxPresent() ? "detected (0x70)" : "absent";
  html += "</b></p>";
#endif
  html += "<p>Detected load-cell channels:</p><ul>";
  {
    JsonDocument d;
    deserializeJson(d, detectedScalesJs());
    for (JsonObject o : d.as<JsonArray>())
      html += "<li>" + htmlEscape(String((const char*)(o["label"] | ""))) + "</li>";
  }
  html += "</ul>";
#if ENABLE_DS18B20_HIVE_TEMP
  html += "<p>Detected DS18B20 probes (ROM):</p><ul>";
  {
    JsonDocument d;
    deserializeJson(d, detectedProbesJs());
    for (JsonVariant v : d.as<JsonArray>())
      html += "<li><code>" + htmlEscape(v.as<String>()) + "</code></li>";
  }
  html += "</ul>";
#endif
  html += "<p><a href='/'>Back to setup</a></p></body></html>";
  setupServer.send(200, "text/html", html);
}

void handleSetupRoot() {
  sendNoCacheHeaders();

  String html;
  html += "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>HiveScale Setup</title>";
  // Self-contained inline theme (no external fonts/CSS/JS so it works on the
  // offline captive portal). Brand accent matches Hive Pal (amber #f59e0b).
  // This is just a longer style string in flash; it adds no SD/SPIFFS assets.
  html += "<style>"
          ":root{--bg:#f6f7f8;--card:#fff;--fg:#1a1d23;--muted:#667085;--border:#e4e7ec;--accent:#f59e0b;--link:#b46b00}"
          "*{box-sizing:border-box}"
          "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:24px;background:var(--bg);color:var(--fg);line-height:1.5}"
          ".wrap{max-width:760px;margin:0 auto}"
          "h1{font-size:1.8rem;margin:0 0 2px}h3{margin:18px 0 6px;font-size:1rem}"
          ".sub{color:var(--muted);font-size:.92em;margin:2px 0}"
          "a{color:var(--link)}"
          "fieldset{border:1px solid var(--border);background:var(--card);border-radius:12px;margin:18px 0;padding:18px 18px 6px;box-shadow:0 1px 2px rgba(16,24,40,.06)}"
          "legend{font-weight:600;padding:0 8px;color:var(--accent)}"
          "label{display:block;font-size:.88em;font-weight:500;margin-top:10px;color:var(--muted)}"
          "input{width:100%;padding:10px 12px;margin:6px 0 12px;border:1px solid var(--border);border-radius:8px;font-size:1rem;background:#fff;color:var(--fg)}"
          "input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(245,158,11,.25)}"
          "select{width:100%;padding:10px 12px;margin:6px 0 12px;border:1px solid var(--border);border-radius:8px;font-size:1rem;background:#fff;color:var(--fg)}"
          ".wsrow{border:1px dashed var(--border);border-radius:10px;padding:8px 12px;margin:10px 0;background:rgba(245,158,11,.03)}"
          ".wnote{color:var(--muted);font-size:.82em;margin:4px 0 8px}"
          "button,a.button{display:inline-block;padding:11px 18px;margin:4px 6px 4px 0;font-size:1rem;text-decoration:none;border-radius:8px;border:1px solid var(--border);background:#fff;color:var(--fg);cursor:pointer}"
          "button:hover,a.button:hover{filter:brightness(.97)}"
          "button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}"
          "button.danger{color:#b42318;border-color:#f0c2bc;background:#fff7f6}"
          "table{border-collapse:collapse;width:100%}"
          "th,td{text-align:left;border-bottom:1px solid var(--border);padding:8px 6px}"
          "th{width:48%;font-weight:500;color:var(--muted)}"
          ".meta{color:var(--muted);font-size:.86em}p{margin:8px 0}"
          "@media(prefers-color-scheme:dark){:root{--bg:#161618;--card:#1f1f23;--fg:#ececf1;--muted:#9aa0aa;--border:#33343a;--link:#f5b54a}input{background:#26262b}select{background:#26262b}button,a.button{background:#26262b}button.danger{background:#2a1d1d;border-color:#5a2d2a;color:#f7a59c}}"
          "</style>";
  html += "</head><body><div class='wrap'><h1>HiveScale Setup</h1>";
  html += "<p class='sub'>Firmware: " + String(FIRMWARE_VERSION) + "</p>";
  html += "<p class='sub'>Setup portal: <a href='" + provisioningPortalUrl() + "'>" + provisioningPortalUrl() + "</a></p>";
  appendLastSensorPanel(html);
  html += "<fieldset><legend>SD card data</legend>";
  if (sdOk) {
    html += "<p><a class='button' href='/sd/download-all'>Download all SD data (.tar)</a></p>";
    html += "<p>This streams the SD card contents directly; large cards can take a while.</p>";
  } else {
    html += "<p>SD card not available.</p>";
  }
  html += "</fieldset>";
  html += "<form method='POST' action='/save' id='cfgform'>";
  html += "<fieldset><legend>Backend</legend>";
  html += "<label>Device ID</label><input name='device_id' value='" + htmlEscape(deviceId) + "'>";
  html += "<label>Claim code</label><input name='claim_code' value='" + htmlEscape(claimCode) + "'>";
  html += "<label>API base URL</label><input name='api_base' value='" + htmlEscape(apiBaseUrl) + "'>";
  html += "<label>API key</label><input name='api_key' value='" + htmlEscape(apiKey) + "'>";
  html += "</fieldset>";

  // ── Hive-centric mapping ───────────────────────────────────────────────────
  // Top-level "+ Add hive"; each hive maps its scale(s) (HX711 / NAU7802 channels
  // detected by the I2C scan that ran when this page loaded) and its in-hive
  // sensors (BLE beacons/GATT + a wired DS18B20 probe). The page submits one JSON
  // blob per hive in exactly the shape hivecfg::hiveFromJson() parses.
  html += "<fieldset><legend>Hives &amp; sensors</legend>";
  html += "<p>Add a hive, then map its scale(s) and in-hive sensors. An I2C scan ran when this page loaded; ";
  html += "use <a href='/ble/scan'>Scan BLE</a> to find wireless sensors and <a href='/i2c/scan'>I2C scan details</a> to verify wiring. ";
  html += "Beacon sensors (HolyIot / RuuviTag) are recommended for in-hive readings &mdash; any number share one quick scan and keep deep sleep effective. ";
  html += "Connection-based GATT sensors are read serially and capped at " + String(MAX_GATT_READS_PER_CYCLE) + " per wake cycle.</p>";
  html += "<div id='hives'></div>";
  html += "<p id='hempty' class='meta'>No hives yet. Add one to begin.</p>";
  html += "<p><button type='button' class='button primary' id='addhive'>&#10133; Add hive</button> ";
  html += "<span id='hfull' class='meta' style='display:none'>Maximum number of hives reached.</span></p>";
  html += "<input type='hidden' name='hives_json' id='hives_json'>";

  html += "<script>var DETECTED_SCALES=" + detectedScalesJs() + ";";
  html += "var DETECTED_PROBES=" + detectedProbesJs() + ";";
  html += "var INITIAL_HIVES=" + initialHivesJs() + ";";
  html += "var MAX_HIVES=" + String(MAX_HIVES) + ";var MAX_BLE=" + String(hivecfg::MAX_BLE_PER_HIVE) + ";";
  html += "var MAX_SCALES_PER_HIVE=" + String(hivecfg::MAX_SCALES_PER_HIVE) + ";</script>";

  // Inline controller (no external assets — works on the offline captive portal).
  html += R"HVJS(<script>(function(){
var TYPES=[["holyiot","HolyIot 25015 — beacon"],["ruuvitag","RuuviTag — beacon"],["hiveinside","HiveInside — GATT"],["hiveheart","HiveHeart — GATT"],["hivescale","HiveScale wireless scale — GATT"],["beecounter","HiveTraffic counter — GATT"]];
var host=document.getElementById("hives"),form=document.getElementById("cfgform");
function clone(o){return JSON.parse(JSON.stringify(o));}
var HIVES=(INITIAL_HIVES||[]).map(function(h){return {i:h.i,n:h.n||"",s:(h.s||[]).slice(),ds:h.ds||null,bl:(h.bl||[]).slice()};});
function scaleKey(o){return o.b=="nau"?("nau:"+o.mux+":"+o.adc):("hx:"+o.hx);}
function usedScales(){var u={};HIVES.forEach(function(h){h.s.forEach(function(o){u[scaleKey(o)]=1;});});return u;}
function usedProbes(){var u={};HIVES.forEach(function(h){if(h.ds)u[h.ds]=1;});return u;}
function nextScale(){var u=usedScales();for(var i=0;i<DETECTED_SCALES.length;i++){if(!u[scaleKey(DETECTED_SCALES[i])])return clone(DETECTED_SCALES[i]);}return DETECTED_SCALES.length?clone(DETECTED_SCALES[0]):{b:"hx",hx:0,label:"HX711 #1"};}
function nextProbe(){var u=usedProbes();for(var i=0;i<DETECTED_PROBES.length;i++){if(!u[DETECTED_PROBES[i]])return DETECTED_PROBES[i];}return DETECTED_PROBES[0]||"";}
function nextIndex(){var m=0;HIVES.forEach(function(h){if(h.i>m)m=h.i;});return m+1;}
function findScale(k){for(var i=0;i<DETECTED_SCALES.length;i++)if(scaleKey(DETECTED_SCALES[i])==k)return clone(DETECTED_SCALES[i]);return null;}
function scaleOpts(sel){return DETECTED_SCALES.map(function(o){var k=scaleKey(o);return "<option value='"+k+"'"+(k==scaleKey(sel)?" selected":"")+">"+o.label+"</option>";}).join("");}
function probeOpts(sel){if(!DETECTED_PROBES.length)return "<option value=''>(no DS18B20 detected)</option>";return DETECTED_PROBES.map(function(r){return "<option value='"+r+"'"+(r==sel?" selected":"")+">"+r+"</option>";}).join("");}
function typeOpts(sel){return TYPES.map(function(t){return "<option value='"+t[0]+"'"+(t[0]==sel?" selected":"")+">"+t[1]+"</option>";}).join("");}
function render(){
host.innerHTML="";
HIVES.forEach(function(h,hi){
var c=document.createElement("fieldset");c.className="wsrow";
c.innerHTML="<legend>Hive "+h.i+"</legend>"+
"<label>Name (optional)</label><input data-hn placeholder='e.g. Apiary A #3'>"+
"<h3>Scales</h3><div data-scales></div><p><button type='button' class='button' data-addscale>&#10133; Add scale</button></p>"+
"<h3>In-hive sensors</h3><div data-sensors></div><p><button type='button' class='button' data-addble>&#10133; Add BLE sensor</button> <button type='button' class='button' data-addds>&#10133; Add DS18B20</button></p>"+
"<p><button type='button' class='button danger' data-delhive>Remove hive</button></p>";
host.appendChild(c);
var nm=c.querySelector("[data-hn]");nm.value=h.n||"";nm.addEventListener("input",function(){h.n=this.value;});
var sc=c.querySelector("[data-scales]");
h.s.forEach(function(o,si){var row=document.createElement("p");row.className="wnote";
row.innerHTML="<select data-sc>"+scaleOpts(o)+"</select> <button type='button' class='button' data-rm>Remove</button>";
sc.appendChild(row);
row.querySelector("[data-sc]").addEventListener("change",function(){var n=findScale(this.value);if(n){n.off=o.off||0;n.fac=o.fac||-7050;h.s[si]=n;}render();});
row.querySelector("[data-rm]").addEventListener("click",function(){h.s.splice(si,1);render();});});
c.querySelector("[data-addscale]").addEventListener("click",function(){if(h.s.length<MAX_SCALES_PER_HIVE){h.s.push(nextScale());render();}});
var sn=c.querySelector("[data-sensors]");
if(h.ds){var row=document.createElement("p");row.className="wnote";
row.innerHTML="<b>Wired DS18B20</b> <select data-ds>"+probeOpts(h.ds)+"</select> <button type='button' class='button' data-rm>Remove</button>";
sn.appendChild(row);
row.querySelector("[data-ds]").addEventListener("change",function(){h.ds=this.value;});
row.querySelector("[data-rm]").addEventListener("click",function(){h.ds=null;render();});}
h.bl.forEach(function(b,bi){var row=document.createElement("p");row.className="wnote";
row.innerHTML="<select data-bt>"+typeOpts(b.t)+"</select> <input data-bm placeholder='AA:BB:CC:DD:EE:FF'> <button type='button' class='button' data-rm>Remove</button>";
sn.appendChild(row);
row.querySelector("[data-bt]").addEventListener("change",function(){b.t=this.value;});
var mi=row.querySelector("[data-bm]");mi.value=b.m||"";mi.addEventListener("input",function(){b.m=this.value;});
row.querySelector("[data-rm]").addEventListener("click",function(){h.bl.splice(bi,1);render();});});
c.querySelector("[data-addble]").addEventListener("click",function(){if(h.bl.length<MAX_BLE){h.bl.push({t:"holyiot",m:""});render();}});
c.querySelector("[data-addds]").addEventListener("click",function(){if(!h.ds)h.ds=nextProbe();render();});
c.querySelector("[data-delhive]").addEventListener("click",function(){HIVES.splice(hi,1);render();});
});
document.getElementById("hempty").style.display=HIVES.length?"none":"block";
document.getElementById("hfull").style.display=HIVES.length>=MAX_HIVES?"inline":"none";
document.getElementById("addhive").disabled=HIVES.length>=MAX_HIVES;
}
document.getElementById("addhive").addEventListener("click",function(){if(HIVES.length<MAX_HIVES){HIVES.push({i:nextIndex(),n:"",s:[],ds:null,bl:[]});render();}});
form.addEventListener("submit",function(){
var out=HIVES.map(function(h){var o={i:h.i,n:h.n||""};
o.s=h.s.map(function(s){return s.b=="nau"?{b:"nau",mux:s.mux,adc:s.adc,addr:s.addr||42,off:s.off||0,fac:s.fac||-7050}:{b:"hx",hx:s.hx,off:s.off||0,fac:s.fac||-7050};});
if(h.ds)o.ds=h.ds;
o.bl=h.bl.filter(function(b){return b.m&&b.m.length;}).map(function(b){return {t:b.t,m:b.m};});
return o;});
document.getElementById("hives_json").value=JSON.stringify(out);});
render();
})();</script>)HVJS";
  html += "</fieldset>";

  html += "<fieldset><legend>WiFi networks</legend>";
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    prefs.begin("hivescale", true);
    String ssid = prefs.getString(wifiSsidKey(i).c_str(), "");
    prefs.end();
    html += "<h3>Network " + String(i + 1) + "</h3>";
    html += "<label>SSID</label><input name='ssid" + String(i) + "' value='" + htmlEscape(ssid) + "'>";
    html += "<label>Password</label><input type='password' name='pass" + String(i) + "' placeholder='Blank keeps the current password (only if you do not change the SSID above)'>";
  }
  html += "</fieldset>";
  html += "<button class='primary' type='submit'>Save and reboot</button></form>";
  html += "<form method='POST' action='/reset' onsubmit='return confirm(\"Reset all Preferences?\")'><button class='danger' type='submit'>Factory reset Preferences</button></form>";
  html += "</div></body></html>";
  setupServer.send(200, "text/html", html);
}

void handleSetupSave() {
  prefs.begin("hivescale", false);

  String newDeviceId = setupServer.arg("device_id");
  String newClaimCode = setupServer.arg("claim_code");
  String newApiBase = trimTrailingSlash(setupServer.arg("api_base"));
  String newApiKey = setupServer.arg("api_key");

  newClaimCode.trim();

  if (newDeviceId.length() > 0) prefs.putString("device_id", newDeviceId);
  prefs.putString("claim_code", newClaimCode);
  if (newApiBase.length() > 0) prefs.putString("api_base", newApiBase);
  if (newApiKey.length() > 0) prefs.putString("api_key", newApiKey);

  // Parse the hive-mapping JSON the portal submitted. Each element is already in
  // the shape hivecfg::hiveFromJson() expects, so we load them straight into the
  // in-memory registry here; the NVS write happens once after prefs.end() below
  // (saveHiveConfig opens its own handle, so it must not nest with this one).
  {
    String hivesJson = setupServer.arg("hives_json");
    JsonDocument hd;
    if (hivesJson.length() && !deserializeJson(hd, hivesJson)) {
      uint8_t count = 0;
      for (JsonObject o : hd.as<JsonArray>()) {
        if (count >= MAX_HIVES) break;
        // Normalise any MAC values in place before re-serialising for hiveFromJson.
        for (JsonObject b : o["bl"].as<JsonArray>())
          b["m"] = portalNormalizeMac(b["m"] | "");
        String one;
        serializeJson(o, one);
        hivecfg::Hive hive;
        if (hivecfg::hiveFromJson(one, hive)) hivecfg::gHives[count++] = hive;
      }
      hivecfg::gHiveCount = count;
    }
  }

  int savedCount = 0;
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    String ssid = setupServer.arg("ssid" + String(i));
    String pass = setupServer.arg("pass" + String(i));
    ssid.trim();

    if (ssid.length() == 0) {
      prefs.remove(wifiSsidKey(i).c_str());
      prefs.remove(wifiPassKey(i).c_str());
      continue;
    }

    // A blank password field means "keep the current password". That is only
    // safe when the SSID is unchanged: if the slot now points at a different
    // network, the previously stored password belongs to the old network and
    // must not be carried over (doing so silently pairs the new SSID with the
    // wrong password and every connection attempt fails). When the SSID
    // changes and no new password was supplied, clear the stored password so
    // the network is treated as open rather than keeping a stale secret.
    String existingSsid = prefs.getString(wifiSsidKey(i).c_str(), "");
    bool ssidChanged = (ssid != existingSsid);

    prefs.putString(wifiSsidKey(i).c_str(), ssid);
    if (pass.length() > 0) {
      prefs.putString(wifiPassKey(i).c_str(), pass);
    } else if (ssidChanged) {
      prefs.remove(wifiPassKey(i).c_str());
    }
    savedCount = i + 1;
  }

  prefs.putUInt("wifi_count", savedCount);
  prefs.putBool("provisioned", true);
  prefs.putBool("seeded", true);
  prefs.end();

  // Persist the hive registry parsed above (opens/closes its own NVS handle).
  hivecfg::saveHiveConfig();

  setupServer.send(200, "text/html", "<html><body><h1>Saved</h1><p>Device will reboot now.</p></body></html>");
  delay(1000);
  ESP.restart();
}

void handleSetupReset() {
  setupServer.send(200, "text/html", "<html><body><h1>Resetting</h1></body></html>");
  delay(500);
  factoryResetPreferences();
}

void startProvisioningPortal() {
  if (provisioningActive) return;

  Serial.println("[SETUP] Starting provisioning AP");
  WiFi.disconnect(true, true);
  delay(200);
  WiFi.mode(WIFI_AP);

  IPAddress apIp = provisioningPortalIp();
  IPAddress subnet(255, 255, 255, 0);
  if (!WiFi.softAPConfig(apIp, apIp, subnet)) {
    Serial.println("[SETUP] softAPConfig failed; continuing with default AP configuration");
  }

  String suffix = String((uint32_t)ESP.getEfuseMac(), HEX);
  suffix.toUpperCase();
  String apName = "HiveScale-Setup-" + suffix.substring(suffix.length() - 4);

  bool ok = WiFi.softAP(apName.c_str());
  if (!ok) {
    Serial.println("[SETUP] softAP failed");
    return;
  }

  setupServer.on("/", HTTP_GET, handleSetupRoot);
  setupServer.on("/save", HTTP_POST, handleSetupSave);
  setupServer.on("/reset", HTTP_POST, handleSetupReset);
  setupServer.on("/sd/download-all", HTTP_GET, handleSdDownloadAll);
#if ENABLE_BLE_SCAN
  setupServer.on("/ble/scan", HTTP_GET, handleBleScan);
#endif
  setupServer.on("/i2c/scan", HTTP_GET, handleI2cScan);

  // Common captive-portal probe URLs used by Android, iOS/macOS, Windows, and Firefox.
  // Redirecting these makes most phones/laptops show the setup page automatically
  // after they connect to the HiveScale AP. Devices that suppress captive portals
  // can still open http://192.168.4.1/ manually.
  setupServer.on("/generate_204", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/gen_204", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/hotspot-detect.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/library/test/success.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/connecttest.txt", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/ncsi.txt", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/canonical.html", HTTP_GET, handleCaptivePortalProbe);
  setupServer.on("/fwlink", HTTP_GET, handleCaptivePortalProbe);
  setupServer.onNotFound(handleCaptivePortalProbe);
  setupServer.begin();

  setupDnsServer.start(CAPTIVE_DNS_PORT, "*", WiFi.softAPIP());

  provisioningActive = true;
  provisioningStartedMs = millis();

  Serial.printf("[SETUP] AP SSID: %s\n", apName.c_str());
  Serial.print("[SETUP] Open ");
  Serial.println(provisioningPortalUrl());
  Serial.println("[SETUP] Captive DNS redirect enabled for AP clients");
}

void stopProvisioningPortal() {
  if (!provisioningActive) return;
  Serial.println("[SETUP] Stopping provisioning AP");
  setupDnsServer.stop();
  setupServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  provisioningActive = false;
}

void handleButton() {
  bool down = digitalRead(SETUP_BUTTON_PIN) == LOW;
  unsigned long now = millis();

  if (down && !buttonWasDown) {
    buttonWasDown = true;
    buttonDownMs = now;
    longPressHandled = false;
  }

  if (down && buttonWasDown && !longPressHandled && now - buttonDownMs >= BUTTON_LONG_PRESS_MS) {
    longPressHandled = true;
    Serial.println("[BUTTON] Long press detected: factory reset Preferences");
    factoryResetPreferences();
  }

  if (!down && buttonWasDown) {
    unsigned long held = now - buttonDownMs;
    buttonWasDown = false;

    if (held > BUTTON_DEBOUNCE_MS && held < BUTTON_LONG_PRESS_MS && !longPressHandled) {
      Serial.println("[BUTTON] Short press detected: start provisioning AP");
      startProvisioningPortal();
    }
  }
}
