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

// Quote + escape a string as a strict JSON string literal ("...") — which is also
// a valid JavaScript string. Use this (not htmlEscape) wherever a user-supplied
// value such as a hive name goes into a JSON body or an inline JS literal:
// htmlEscape targets HTML-attribute context (it turns " into &quot;, mangling the
// value, and leaves '\' untouched), so a name containing " \ or a control char
// would corrupt /calibrate/read JSON or break the setup-page script.
static String jsonQuote(const String& s) {
  String out = "\"";
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    switch (c) {
      case '"':  out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n";  break;
      case '\r': out += "\\r";  break;
      case '\t': out += "\\t";  break;
      default:
        if ((uint8_t)c < 0x20) {
          char u[7];
          snprintf(u, sizeof(u), "\\u%04x", (unsigned)(uint8_t)c);
          out += u;
        } else {
          out += c;
        }
    }
  }
  out += "\"";
  return out;
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
  setupServer.send(302, "text/plain", "Redirecting to HiveHub setup portal");
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
  html += "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
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
  setupServer.send(200, "text/html; charset=utf-8", html);
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
  bool directPresent = (Wire.endTransmission() == 0);
  if (directPresent) { add(-1, 1); add(-1, 2); }
  // A NAU7802 on the main bus shares the fixed 0x2A address with every muxed
  // NAU7802 and stays on the bus while a mux channel is enabled, so the two
  // cannot coexist. When a direct chip is present, skip probing behind the mux:
  // every channel would falsely ACK from the direct chip (phantom scales) and any
  // muxed read would collide with it. Use EITHER one main-bus chip (2 scales) OR
  // the mux (up to 16) — see the topology note in config.h / docs/multi-hive.md.
  if (!directPresent && scalebus::muxPresent()) {
    for (int ch = 0; ch < 8; ch++) {
      scalebus::muxSelect((uint8_t)ch);
      Wire.beginTransmission(NAU7802_I2C_ADDRESS);
      if (Wire.endTransmission() == 0) { add(ch, 1); add(ch, 2); }
    }
    scalebus::muxDisableAll();
  }
#endif
#if ENABLE_HX711
  // The two HX711 pin channels are always offered on boards that have them.
  if (!first) js += ",";
  js += "{b:'hx',hx:0,label:'HX711 #1 (pins)'},{b:'hx',hx:1,label:'HX711 #2 (pins)'}";
#endif
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
    js += ",n:"; js += jsonQuote(hive.name); js += ",s:[";
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
  html += "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
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
  setupServer.send(200, "text/html; charset=utf-8", html);
}

// ── Local scale calibration (offline, inside the captive portal) ─────────────
// The provisioning AP has no internet, so the server-side calibration path
// (the calibrate_scale_N command / "calibration mode" frequent uploads) is
// unreachable here. This page reads each configured scale channel live and lets
// you Tare (zero) and set a known weight, writing offset/factor straight into
// the hive registry — the same kg = (raw - offset) / factor convention used on
// the upload path (weightFromRaw). Works for HX711 and NAU7802 channels alike.
static float calKg(long raw, long offset, float factor) {
  if (factor == 0.0f) return NAN;
  return ((float)(raw - offset)) / factor;
}

static hivecfg::ScaleChannel* calChannel(int hiveIndex, int slot) {
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    hivecfg::Hive& hive = hivecfg::gHives[h];
    if (hive.index != hiveIndex) continue;
    if (slot < 0 || slot >= hive.scaleCount) return nullptr;
    return &hive.scales[slot];
  }
  return nullptr;
}

static String calChannelLabel(const hivecfg::ScaleChannel& ch) {
  if (ch.backend == hivecfg::ScaleBackend::NAU7802) {
    String s = "NAU7802 ";
    s += (ch.muxChannel < 0) ? String("main bus") : (String("mux ch") + (int)ch.muxChannel);
    s += " CH"; s += String((int)ch.adcChannel);
    return s;
  }
  if (ch.backend == hivecfg::ScaleBackend::HX711)
    return String("HX711 #") + (int)(ch.hxIndex + 1);
  return String("scale");
}

// GET /calibrate/read — JSON: live raw + kg for every configured scale channel.
static void handleCalibrateRead() {
  sendNoCacheHeaders();
  String js = "{\"ch\":[";
  bool first = true;
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      hivecfg::ScaleChannel& ch = hive.scales[s];
      if (!ch.valid()) continue;
      long raw = scalebus::readRaw(ch);
      float kg = calKg(raw, ch.offset, ch.factor);
      if (!first) js += ",";
      first = false;
      String nm = hive.name.length() ? hive.name : (String("Hive ") + hive.index);
      js += "{\"h\":" + String(hive.index) + ",\"s\":" + String(s);
      js += ",\"name\":" + jsonQuote(nm);
      js += ",\"label\":" + jsonQuote(calChannelLabel(ch));
      js += ",\"raw\":" + String(raw);
      js += ",\"kg\":" + (isnan(kg) ? String("null") : String(kg, 3));
      js += ",\"off\":" + String(ch.offset) + ",\"fac\":" + String(ch.factor, 2) + "}";
    }
  }
  js += "]}";
  setupServer.send(200, "application/json", js);
}

// POST /calibrate/set — op=tare (offset := current raw) or op=span (factor from a
// known weight: factor := (raw - offset) / kg). Persists the registry on success.
static void handleCalibrateSet() {
  sendNoCacheHeaders();
  int h = setupServer.arg("h").toInt();
  int s = setupServer.arg("s").toInt();
  String op = setupServer.arg("op");
  hivecfg::ScaleChannel* ch = calChannel(h, s);
  if (!ch) {
    setupServer.send(404, "application/json", "{\"ok\":false,\"err\":\"no such scale channel\"}");
    return;
  }

  long raw = scalebus::readRaw(*ch);
  String err;
  if (op == "tare") {
    ch->offset = raw;
  } else if (op == "span") {
    float known = setupServer.arg("kg").toFloat();
    if (known == 0.0f) {
      err = "enter a non-zero known weight";
    } else if (raw == ch->offset) {
      err = "reading equals the tare point — Tare empty first, then load the scale";
    } else {
      ch->factor = (float)((double)(raw - ch->offset) / (double)known);
    }
  } else {
    err = "unknown operation";
  }

  if (err.length()) {
    setupServer.send(400, "application/json", String("{\"ok\":false,\"err\":\"") + err + "\"}");
    return;
  }
  hivecfg::saveHiveConfig();
  float kg = calKg(raw, ch->offset, ch->factor);
  String js = "{\"ok\":true,\"raw\":" + String(raw) + ",\"off\":" + String(ch->offset) +
              ",\"fac\":" + String(ch->factor, 2) +
              ",\"kg\":" + (isnan(kg) ? String("null") : String(kg, 3)) + "}";
  setupServer.send(200, "application/json", js);
}

// GET /calibrate — the live calibration UI (polls /calibrate/read; posts to /set).
static void handleCalibratePage() {
  sendNoCacheHeaders();
  String html;
  html += "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>Scale calibration</title><style>"
          "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;padding:20px;background:#f6f7f8;color:#1a1d23;line-height:1.5}"
          ".wrap{max-width:680px;margin:0 auto}h1{font-size:1.5rem;margin:0 0 6px}"
          ".ch{border:1px solid #e4e7ec;background:#fff;border-radius:12px;padding:14px;margin:14px 0}"
          ".rd{font-size:1.7rem;font-weight:600;margin:4px 0}"
          ".raw{color:#667085;font-size:.86em}"
          "input{padding:9px;border:1px solid #e4e7ec;border-radius:8px;width:7rem;font-size:1rem}"
          "button{padding:10px 16px;margin:8px 6px 0 0;border:1px solid #e4e7ec;border-radius:8px;background:#fff;cursor:pointer;font-size:1rem}"
          "button.p{background:#f59e0b;border-color:#f59e0b;color:#fff;font-weight:600}"
          ".msg{font-size:.85em;color:#667085;min-height:1.1em;margin-top:8px}a{color:#b46b00}"
          "@media(prefers-color-scheme:dark){body{background:#161618;color:#ececf1}.ch{background:#1f1f23;border-color:#33343a}input,button{background:#26262b;color:#ececf1;border-color:#33343a}}"
          "</style></head><body><div class='wrap'>";
  html += "<h1>Scale calibration</h1>";
  html += "<p class='raw'>Readings refresh automatically. <b>Tare</b> with the scale empty, then place a known weight, type its mass in kg and press <b>Set weight</b>. Each change is saved to the device immediately.</p>";
  if (hivecfg::gHiveCount == 0)
    html += "<p><b>No hives configured yet.</b> Add a hive with a scale on the setup page first.</p>";
  html += "<div id='list'></div>";
  html += "<p><a href='/'>&larr; Back to setup</a></p>";
  html += R"CAL(<script>
function render(d){var L=document.getElementById('list');
d.ch.forEach(function(c){var id='c'+c.h+'_'+c.s,e=document.getElementById(id);
if(!e){e=document.createElement('div');e.className='ch';e.id=id;
e.innerHTML="<div><b data-nm></b> &middot; <span data-lb></span></div>"+
"<div class='rd' data-kg>--</div><div class='raw' data-raw></div>"+
"<button class='p' data-tare>Tare (zero)</button> "+
"<input type='number' step='any' inputmode='decimal' placeholder='kg' data-known> "+
"<button data-span>Set weight</button><div class='msg' data-msg></div>";
L.appendChild(e);
e.querySelector('[data-nm]').textContent=c.name;
e.querySelector('[data-lb]').textContent=c.label;
e.querySelector('[data-tare]').onclick=function(){setp(c.h,c.s,'tare','',e);};
e.querySelector('[data-span]').onclick=function(){setp(c.h,c.s,'span',e.querySelector('[data-known]').value,e);};}
e.querySelector('[data-kg]').textContent=(c.kg==null?'-- kg':c.kg+' kg');
e.querySelector('[data-raw]').textContent='raw '+c.raw+'  ·  offset '+c.off+'  ·  factor '+c.fac;});}
function poll(){fetch('/calibrate/read').then(function(r){return r.json();}).then(render).catch(function(){}).finally(function(){setTimeout(poll,1500);});}
function setp(h,s,op,kg,e){var m=e.querySelector('[data-msg]');m.textContent='Saving...';
var body='h='+h+'&s='+s+'&op='+op+'&kg='+encodeURIComponent(kg);
fetch('/calibrate/set',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:body})
.then(function(r){return r.json();}).then(function(d){m.textContent=d.ok?('Saved — offset '+d.off+', factor '+d.fac):('Error: '+d.err);});}
poll();
</script>)CAL";
  html += "</div></body></html>";
  setupServer.send(200, "text/html; charset=utf-8", html);
}

void handleSetupRoot() {
  sendNoCacheHeaders();

  String html;
  html += "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<title>HiveHub Setup</title>";
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
  html += "</head><body><div class='wrap'><h1>HiveHub Setup</h1>";
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
  html += "<fieldset><legend>Scale calibration</legend>";
  html += "<p>Tare and set known weights for each scale live, with no server or internet needed. Changes save to the device immediately.</p>";
  html += "<p><a class='button' href='/calibrate'>Open scale calibration</a></p>";
  html += "</fieldset>";
  html += "<form method='POST' action='/save' id='cfgform'>";
  html += "<fieldset><legend>Backend</legend>";
  html += "<label>Device ID</label><input name='device_id' value='" + htmlEscape(deviceId) + "'>";
  html += "<label>Claim code</label><input name='claim_code' value='" + htmlEscape(claimCode) + "'>";
  html += "<label>API base URL</label><input name='api_base' value='" + htmlEscape(apiBaseUrl) + "'>";
  html += "<label>API key</label><input name='api_key' value='" + htmlEscape(apiKey) + "'>";
  html += "</fieldset>";

  // ── Hive-centric mapping ───────────────────────────────────────────────────
  // Top-level "+ Add hive"; each hive maps exactly one scale source (HX711,
  // NAU7802 channel, or beehivemonitoring.com HiveScale) and its in-hive
  // sensors (one BLE beacon/GATT OR one wired DS18B20 probe). The page submits one JSON
  // blob per hive in exactly the shape hivecfg::hiveFromJson() parses.
  html += "<fieldset><legend>Hives &amp; sensors</legend>";
  html += "<p>Add a hive, choose its single scale source, then optionally add exactly one in-hive sensor: either one BLE beacon/GATT sensor or one wired DS18B20 probe. An I2C scan ran when this page loaded; ";
  html += "use <a href='/ble/scan'>Scan BLE</a> to find wireless sensors and <a href='/i2c/scan'>I2C scan details</a> to verify wiring. ";
  html += "BLE HiveScale from Beehivemonitoring remains a scale source and can still have its own MAC field. ";
  html += "Connection-based GATT sensors are read serially and capped at " + String(MAX_GATT_READS_PER_CYCLE) + " per wake cycle.</p>";
  html += "<div id='hives'></div>";
  html += "<p id='hempty' class='meta'>No hives yet. Add one to begin.</p>";
  html += "<p><button type='button' class='button primary' id='addhive'>&#10133; Add hive</button> ";
  html += "<span id='hfull' class='meta' style='display:none'>Maximum number of hives reached.</span></p>";
  html += "<input type='hidden' name='hives_json' id='hives_json'>";
  html += "<datalist id='dsprobeopts'></datalist>";

  html += "<script>var DETECTED_SCALES=" + detectedScalesJs() + ";";
  html += "var DETECTED_PROBES=" + detectedProbesJs() + ";";
  html += "var INITIAL_HIVES=" + initialHivesJs() + ";";
  html += "var MAX_HIVES=" + String(MAX_HIVES) + ";var MAX_BLE=" + String(hivecfg::MAX_BLE_PER_HIVE) + ";var MAX_INHIVE_BLE=" + String(hivecfg::MAX_INHIVE_BLE_PER_HIVE) + ";";
  // Fallback channel used when the bus probe found no NAU7802 but the user still
  // wants to pre-configure one.
#if ENABLE_NAU7802
  html += "var DEFAULT_NAU_SCALE={b:'nau',mux:-1,adc:1,addr:" + String((int)NAU7802_I2C_ADDRESS) +
          ",label:'NAU7802 main bus CH1'};";
#else
  html += "var DEFAULT_NAU_SCALE=null;";
#endif
  html += "</script>";

  // Inline controller (no external assets — works on the offline captive portal).
  html += R"HVJS(<script>(function(){
var SENSOR_TYPES=[["holyiot","HolyIot 25015 — beacon"],["ruuvitag","RuuviTag — beacon"],["hiveinside","HiveInside — GATT"],["hiveheart","HiveHeart — GATT"],["beecounter","HiveTraffic counter — GATT"]];
var host=document.getElementById("hives"),form=document.getElementById("cfgform"),dsList=document.getElementById("dsprobeopts");
if(dsList)dsList.innerHTML=(DETECTED_PROBES||[]).map(function(r){return "<option value='"+r+"'>";}).join("");
function clone(o){return JSON.parse(JSON.stringify(o));}
function scaleKey(o){return o.b=="nau"?("nau:"+o.mux+":"+o.adc):("hx:"+o.hx);}
function labelScale(o,nauCount){if(o.b=="hx")return "HX711 ("+(Number(o.hx)+1)+")";var loc=o.mux>=0?("mux ch "+o.mux+" CH"+o.adc):("main bus CH"+o.adc);return nauCount>1?"NAU7802 — "+loc:"NAU7802";}
function allWiredScales(){var seen={},out=[],nau=0;DETECTED_SCALES.forEach(function(o){if(o.b=="nau")nau++;});var addDefaultNau=!nau&&DEFAULT_NAU_SCALE;if(addDefaultNau)nau=1;
function add(o){var k=scaleKey(o);if(seen[k])return;var c=clone(o);c.label=labelScale(c,nau);seen[k]=1;out.push(c);}
DETECTED_SCALES.forEach(add);if(addDefaultNau)add(DEFAULT_NAU_SCALE);return out;}
function findScale(k){var scales=allWiredScales();for(var i=0;i<scales.length;i++)if(scaleKey(scales[i])==k)return clone(scales[i]);return null;}
function normalizeHive(h){var s=(h.s||[]).slice(0,1),bl=[],wirelessMac="",ds=(typeof h.ds=="string")?h.ds:(h.ds?h.ds:null);(h.bl||[]).forEach(function(b){if(b.t=="hivescale"){if(!wirelessMac)wirelessMac=b.m||"";}else if(ds===null&&bl.length<MAX_INHIVE_BLE){bl.push({t:b.t,m:b.m||""});}});return {i:h.i,n:h.n||"",s:s,ds:ds,bl:bl,sk:s.length?scaleKey(s[0]):(wirelessMac?"ble":"none"),wm:wirelessMac};}
var HIVES=(INITIAL_HIVES||[]).map(normalizeHive);
function usedScales(){var u={};HIVES.forEach(function(h){h.s.forEach(function(o){u[scaleKey(o)]=1;});});return u;}
function usedProbes(){var u={};HIVES.forEach(function(h){if(h.ds)u[h.ds]=1;});return u;}
function nextScale(){var u=usedScales(),scales=allWiredScales();for(var i=0;i<scales.length;i++){if(!u[scaleKey(scales[i])])return clone(scales[i]);}return null;}
function nextProbe(){var u=usedProbes();for(var i=0;i<DETECTED_PROBES.length;i++){if(!u[DETECTED_PROBES[i]])return DETECTED_PROBES[i];}return "";}
function nextIndex(){var u={};HIVES.forEach(function(h){u[h.i]=1;});for(var i=1;i<=MAX_HIVES;i++){if(!u[i])return i;}return MAX_HIVES;}
function hasInhiveSensor(h){return h.ds!==null||h.bl.length>=MAX_INHIVE_BLE;}
function scaleOpts(h){var sel=h.sk||"none",html="<option value='none'"+(sel=="none"?" selected":"")+">(no scale)</option>",scales=allWiredScales(),seen={};scales.forEach(function(o){seen[scaleKey(o)]=1;});if(h.s[0]&&!seen[scaleKey(h.s[0])]){var saved=clone(h.s[0]);saved.label=labelScale(saved,1)+" (saved; not detected)";scales.unshift(saved);}scales.forEach(function(o){var k=scaleKey(o);html+="<option value='"+k+"'"+(k==sel?" selected":"")+">"+o.label+"</option>";});html+="<option value='ble'"+(sel=="ble"?" selected":"")+">BLE HiveScale from Beehivemonitoring</option>";return html;}
function typeOpts(sel){return SENSOR_TYPES.map(function(t){return "<option value='"+t[0]+"'"+(t[0]==sel?" selected":"")+">"+t[1]+"</option>";}).join("");}
function setScaleChoice(h,val){h.sk=val;if(val=="ble"||val=="none"){h.s=[];return;}var n=findScale(val);if(n){var old=h.s[0]||{};n.off=old.off||0;n.fac=old.fac||-7050;h.s=[n];}}
function render(){
host.innerHTML="";
HIVES.forEach(function(h,hi){
var c=document.createElement("fieldset");c.className="wsrow";
c.innerHTML="<legend>Hive "+h.i+"</legend>"+
"<label>Name (optional)</label><input data-hn placeholder='e.g. Apiary A #3'>"+
"<h3>Scale</h3><div data-scale-wrap></div>"+
"<h3>In-hive sensor</h3><div data-sensors></div><p class='meta'>Choose either one BLE sensor or one DS18B20 probe.</p><p><button type='button' class='button' data-addble>&#10133; Add BLE sensor</button> <button type='button' class='button' data-addds>&#10133; Add DS18B20</button></p>"+
"<p><button type='button' class='button danger' data-delhive>Remove hive</button></p>";
host.appendChild(c);
var nm=c.querySelector("[data-hn]");nm.value=h.n||"";nm.addEventListener("input",function(){h.n=this.value;});
var sw=c.querySelector("[data-scale-wrap]"),sr=document.createElement("p");sr.className="wnote";
sr.innerHTML="<select data-scale>"+scaleOpts(h)+"</select>"+(h.sk=="ble"?" <input data-sm placeholder='AA:BB:CC:DD:EE:FF'>":"");
sw.appendChild(sr);
var ss=sr.querySelector("[data-scale]");ss.value=h.sk||"none";ss.addEventListener("change",function(){setScaleChoice(h,ss.value);render();});
var sm=sr.querySelector("[data-sm]");if(sm){sm.value=h.wm||"";sm.addEventListener("input",function(){h.wm=this.value;});}
var sn=c.querySelector("[data-sensors]");
if(h.ds!==null){var row=document.createElement("p");row.className="wnote";
row.innerHTML="<b>Wired DS18B20</b> <input data-ds list='dsprobeopts' pattern='[0-9A-Fa-f]{16}' title='16 hex characters' placeholder='16-char ROM address'> <button type='button' class='button' data-rm>Remove</button>";
sn.appendChild(row);
var dss=row.querySelector("[data-ds]");dss.value=h.ds||"";dss.addEventListener("input",function(){h.ds=dss.value.trim();});
row.querySelector("[data-rm]").addEventListener("click",function(){h.ds=null;render();});}
h.bl.slice(0,MAX_INHIVE_BLE).forEach(function(b,bi){var row=document.createElement("p");row.className="wnote";
row.innerHTML="<select data-bt>"+typeOpts(b.t)+"</select> <input data-bm placeholder='AA:BB:CC:DD:EE:FF'> <button type='button' class='button' data-rm>Remove</button>";
sn.appendChild(row);
var bt=row.querySelector("[data-bt]");bt.value=b.t;bt.addEventListener("change",function(){b.t=bt.value;});
var mi=row.querySelector("[data-bm]");mi.value=b.m||"";mi.addEventListener("input",function(){b.m=this.value;});
row.querySelector("[data-rm]").addEventListener("click",function(){h.bl.splice(bi,1);render();});});
var addBle=c.querySelector("[data-addble]"),addDs=c.querySelector("[data-addds]");
addBle.disabled=hasInhiveSensor(h);addDs.disabled=hasInhiveSensor(h);
addBle.addEventListener("click",function(){if(!hasInhiveSensor(h)){h.ds=null;h.bl=[{t:"holyiot",m:""}];render();}});
addDs.addEventListener("click",function(){if(!hasInhiveSensor(h)){h.bl=[];h.ds=nextProbe();render();}});
c.querySelector("[data-delhive]").addEventListener("click",function(){HIVES.splice(hi,1);render();});
});
document.getElementById("hempty").style.display=HIVES.length?"none":"block";
document.getElementById("hfull").style.display=HIVES.length>=MAX_HIVES?"inline":"none";
document.getElementById("addhive").disabled=HIVES.length>=MAX_HIVES;
}
document.getElementById("addhive").addEventListener("click",function(){if(HIVES.length<MAX_HIVES){var s=nextScale();HIVES.push({i:nextIndex(),n:"",s:s?[s]:[],ds:null,bl:[],sk:s?scaleKey(s):"none",wm:""});render();}});
form.addEventListener("submit",function(){
var out=HIVES.map(function(h){var o={i:h.i,n:h.n||""};
o.s=(h.sk!="ble"&&h.sk!="none"?h.s.slice(0,1):[]).map(function(s){return s.b=="nau"?{b:"nau",mux:s.mux,adc:s.adc,addr:s.addr||42,off:s.off||0,fac:s.fac||-7050}:{b:"hx",hx:s.hx,off:s.off||0,fac:s.fac||-7050};});
if(h.ds&&h.ds.length)o.ds=h.ds;
var bl=(h.ds===null?h.bl.filter(function(b){return b.m&&b.m.length;}).slice(0,MAX_INHIVE_BLE).map(function(b){return {t:b.t,m:b.m};}):[]);
if(h.sk=="ble"&&h.wm&&h.wm.length)bl.push({t:"hivescale",m:h.wm});
o.bl=bl;
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
  setupServer.send(200, "text/html; charset=utf-8", html);
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
  String apName = "HiveHub-Setup-" + suffix.substring(suffix.length() - 4);

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
  setupServer.on("/calibrate", HTTP_GET, handleCalibratePage);
  setupServer.on("/calibrate/read", HTTP_GET, handleCalibrateRead);
  setupServer.on("/calibrate/set", HTTP_POST, handleCalibrateSet);

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
