// portal.cpp — captive setup portal, calibration mode and button handling.
#include "portal.h"
#include "globals.h"
#include "config.h"
#include "device_prefs.h"
#include "storage_power.h"

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

  html += "<table>";
  addMeasurementRow(html, "Timestamp", jsonStringOrNA(doc, "timestamp"));
  addMeasurementRow(html, "Scale 1 weight", jsonNumberOrNA(doc, "scale_1_weight_kg", 3, "kg"));
  addMeasurementRow(html, "Scale 2 weight", jsonNumberOrNA(doc, "scale_2_weight_kg", 3, "kg"));
  addMeasurementRow(html, "Hive 1 temperature", jsonNumberOrNA(doc, "hive_1_temp_c", 2, "C"));
  addMeasurementRow(html, "Hive 2 temperature", jsonNumberOrNA(doc, "hive_2_temp_c", 2, "C"));
  addMeasurementRow(html, "Ambient temperature", jsonNumberOrNA(doc, "ambient_temp_c", 2, "C"));
  addMeasurementRow(html, "Ambient humidity", jsonNumberOrNA(doc, "ambient_humidity_percent", 1, "%"));
  addMeasurementRow(html, "Scale 1 raw", jsonNumberOrNA(doc, "scale_1_raw", 0, ""));
  addMeasurementRow(html, "Scale 2 raw", jsonNumberOrNA(doc, "scale_2_raw", 0, ""));

  // Wireless beehivemonitoring.com GATT sensors are reported on their own
  // hivescale_N_* / hiveheart_N_* fields (not the wired scale_N_* / hive_N_*
  // rows above), so surface them here when a paired device produced a reading.
  if (!doc["hivescale_1_weight_kg"].isNull() || !doc["hivescale_2_weight_kg"].isNull()) {
    addMeasurementRow(html, "HiveScale 1 weight", jsonNumberOrNA(doc, "hivescale_1_weight_kg", 2, "kg"));
    addMeasurementRow(html, "HiveScale 2 weight", jsonNumberOrNA(doc, "hivescale_2_weight_kg", 2, "kg"));
  }
  if (!doc["hiveheart_1_temp_c"].isNull() || !doc["hiveheart_2_temp_c"].isNull()) {
    addMeasurementRow(html, "HiveHeart 1 temperature", jsonNumberOrNA(doc, "hiveheart_1_temp_c", 2, "C"));
    addMeasurementRow(html, "HiveHeart 1 humidity", jsonNumberOrNA(doc, "hiveheart_1_humidity_percent", 1, "%"));
    addMeasurementRow(html, "HiveHeart 2 temperature", jsonNumberOrNA(doc, "hiveheart_2_temp_c", 2, "C"));
    addMeasurementRow(html, "HiveHeart 2 humidity", jsonNumberOrNA(doc, "hiveheart_2_humidity_percent", 1, "%"));
  }

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

  if (!doc["mic_left_rms_dbfs"].isNull() || !doc["mic_right_rms_dbfs"].isNull()) {
    addMeasurementRow(html, "Mic left RMS", jsonNumberOrNA(doc, "mic_left_rms_dbfs", 1, "dBFS"));
    addMeasurementRow(html, "Mic right RMS", jsonNumberOrNA(doc, "mic_right_rms_dbfs", 1, "dBFS"));
  }

  if (!doc["ble_1_pressure_hpa"].isNull() || !doc["ble_1_humidity_percent"].isNull()) {
    addMeasurementRow(html, "Hive 1 BLE humidity", jsonNumberOrNA(doc, "ble_1_humidity_percent", 1, "%"));
    addMeasurementRow(html, "Hive 1 BLE pressure", jsonNumberOrNA(doc, "ble_1_pressure_hpa", 1, "hPa"));
    addMeasurementRow(html, "Hive 1 BLE battery", jsonNumberOrNA(doc, "ble_1_battery_percent", 0, "%"));
  }
  if (!doc["ble_2_pressure_hpa"].isNull() || !doc["ble_2_humidity_percent"].isNull()) {
    addMeasurementRow(html, "Hive 2 BLE humidity", jsonNumberOrNA(doc, "ble_2_humidity_percent", 1, "%"));
    addMeasurementRow(html, "Hive 2 BLE pressure", jsonNumberOrNA(doc, "ble_2_pressure_hpa", 1, "hPa"));
    addMeasurementRow(html, "Hive 2 BLE battery", jsonNumberOrNA(doc, "ble_2_battery_percent", 0, "%"));
  }

  html += "</table>";
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

#if (ENABLE_BLE_SCAN || ENABLE_BEEHIVE_GATT || ENABLE_WIRELESS_BEECOUNTER)
  // Dynamic wireless-sensor list. Instead of fixed per-slot fields, the user
  // adds rows and picks each sensor's type; the categories and limits mirror the
  // secrets.h configurator (in-hive / scale / bee counter, max 2 each, 6 total).
  // The default type is an in-hive sensor. handleSetupSave() persists this list
  // and recomputes the per-transport slot keys the firmware actually reads.
  html += "<fieldset><legend>Wireless sensors</legend>";
  html += "<p>Add up to six wireless BLE sensors &mdash; at most two in-hive sensors, two scales and two bee counters. ";
  html += "For each one choose its type and enter its MAC address";
#if ENABLE_BLE_SCAN
  html += ", or <a href='/ble/scan'>scan for wireless sensors</a> and copy a MAC below";
#endif
  html += ". Use each sensor's <em>Maps to</em> dropdown to pick which hive / scale / counter it serves &mdash; ";
  html += "so you can, for example, keep hive 1 on its wired probe and add a wireless sensor for hive 2 only. ";
  html += "When a paired in-hive sensor reports temperature, vibration or sound, the matching wired sensor for that hive is disabled automatically to avoid duplicate readings.</p>";
  html += "<div id='wlist'></div>";
  html += "<p id='wempty' class='meta'>No wireless sensors added yet.</p>";
  html += "<p><button type='button' class='button' id='waddbtn'>&#10133; Add wireless sensor</button></p>";
  html += "<p id='wfull' class='meta' style='display:none'>Maximum of 6 wireless sensors reached (2 per category).</p>";
  html += "<input type='hidden' name='wcount' id='wcount' value='0'>";

  // Seed the in-browser list from stored pairings. Prefer the canonical
  // wcount/wtypeN/wmacN list; fall back to the legacy fixed-slot keys so devices
  // provisioned before this change still show their existing pairings.
  html += "<script>var INITIAL=[";
  {
    prefs.begin("hivescale", true);
    bool first = true;
    auto emit = [&](const char* type, const String& mac, int slot) {
      if (mac.length() == 0) return;
      if (!first) html += ",";
      first = false;
      html += "{t:'"; html += type; html += "',m:'"; html += mac;
      html += "',s:"; html += String(slot); html += "}";
    };
    if (prefs.isKey("wcount")) {
      int wc = (int)prefs.getUInt("wcount", 0);
      if (wc > 6) wc = 6;
      // Derive a slot when an entry stored by older firmware has no wslotN key.
      int seenInhive = 0, seenScale = 0, seenCnt = 0;
      for (int i = 0; i < wc; i++) {
        String t = prefs.getString((String("wtype") + i).c_str(), "");
        String m = prefs.getString((String("wmac") + i).c_str(), "");
        if (t.length() == 0) t = "holyiot";
        int slot = (int)prefs.getUInt((String("wslot") + i).c_str(), 0);
        if (slot < 1 || slot > 2) {
          if (t == "hivescale") slot = ++seenScale;
          else if (t == "beecounter") slot = ++seenCnt;
          else slot = ++seenInhive;
          if (slot > 2) slot = 2;
        }
        emit(t.c_str(), m, slot);
      }
    } else {
      emit("holyiot",    prefs.getString("ble_mac0", ""), 1);
      emit("holyiot",    prefs.getString("ble_mac1", ""), 2);
      emit("hiveheart",  prefs.getString("heart_mac0", ""), 1);
      emit("hiveheart",  prefs.getString("heart_mac1", ""), 2);
      emit("hivescale",  prefs.getString("scale_mac0", ""), 1);
      emit("hivescale",  prefs.getString("scale_mac1", ""), 2);
      emit("beecounter", prefs.getString("counter_mac0", ""), 1);
      emit("beecounter", prefs.getString("counter_mac1", ""), 2);
    }
    prefs.end();
  }
  html += "];</script>";

  // Inline list controller (no external assets — works on the offline portal).
  html += R"WSJS(<script>(function(){
var TYPES={
holyiot:{label:"HolyIot 25015 - in-hive (BLE beacon)",cat:"inhive"},
ruuvitag:{label:"RuuviTag - in-hive (BLE beacon)",cat:"inhive"},
hiveinside:{label:"HiveInside - in-hive (GATT)",cat:"inhive"},
hiveheart:{label:"HiveHeart - in-hive (GATT)",cat:"inhive"},
hivescale:{label:"HiveScale - wireless scale (GATT)",cat:"scale"},
beecounter:{label:"HiveTraffic - entrance bee counter (GATT)",cat:"beecounter"}};
var ORDER=["holyiot","ruuvitag","hiveinside","hiveheart","hivescale","beecounter"];
var LIMIT={inhive:2,scale:2,beecounter:2},MAXT=6;
var LAB={inhive:"In-hive sensor, hive",scale:"Scale, scale",beecounter:"Bee counter, counter"};
var SLOTLAB={inhive:["Hive 1","Hive 2"],scale:["Scale 1","Scale 2"],beecounter:["Counter 1","Counter 2"]};
var list=document.getElementById("wlist"),addbtn=document.getElementById("waddbtn"),form=document.getElementById("cfgform");
function rows(){return Array.prototype.slice.call(list.querySelectorAll(".wsrow"));}
function catCount(cat,except){var n=0;rows().forEach(function(r){if(r===except)return;if(TYPES[r.querySelector("[data-wtype]").value].cat===cat)n++;});return n;}
function firstFree(except){for(var i=0;i<ORDER.length;i++){var k=ORDER[i];if(catCount(TYPES[k].cat,except)<LIMIT[TYPES[k].cat])return k;}return null;}
function opts(sel){return ORDER.map(function(k){return "<option value='"+k+"'"+(k===sel?" selected":"")+">"+TYPES[k].label+"</option>";}).join("");}
function slotTaken(cat,except){var t={};rows().forEach(function(r){if(r===except)return;if(TYPES[r.querySelector("[data-wtype]").value].cat!==cat)return;t[r.querySelector("[data-wslot]").value]=true;});return t;}
function freeSlot(cat,except){var t=slotTaken(cat,except);for(var n=1;n<=LIMIT[cat];n++)if(!t[String(n)])return n;return LIMIT[cat];}
function slotOpts(cat,sel){return SLOTLAB[cat].map(function(l,i){var n=i+1;return "<option value='"+n+"'"+(n===sel?" selected":"")+">"+l+"</option>";}).join("");}
function buildSlots(r,want){var cat=TYPES[r.querySelector("[data-wtype]").value].cat,ss=r.querySelector("[data-wslot]"),w=want||freeSlot(cat,r);if(slotTaken(cat,r)[String(w)])w=freeSlot(cat,r);ss.innerHTML=slotOpts(cat,w);}
function addRow(type,mac,slot){
var key=type||firstFree(null);if(!key)return;
var r=document.createElement("div");r.className="wsrow";
r.innerHTML="<label>Sensor type</label><select data-wtype>"+opts(key)+"</select>"+
"<label>Maps to</label><select data-wslot></select>"+
"<label>MAC address</label><input data-wmac placeholder='AA:BB:CC:DD:EE:FF'>"+
"<p class='wnote'></p>"+
"<button type='button' class='button' data-wrem>Remove</button>";
list.appendChild(r);
buildSlots(r,slot);
if(mac)r.querySelector("[data-wmac]").value=mac;
r.querySelector("[data-wtype]").addEventListener("change",function(){onType(r);});
r.querySelector("[data-wslot]").addEventListener("change",function(){onSlot(r);});
r.querySelector("[data-wrem]").addEventListener("click",function(){list.removeChild(r);refresh();});
refresh();}
function onType(r){var sel=r.querySelector("[data-wtype]"),cat=TYPES[sel.value].cat;
if(catCount(cat,r)>=LIMIT[cat])sel.value=firstFree(r)||ORDER[0];
buildSlots(r);refresh();}
function onSlot(r){var cat=TYPES[r.querySelector("[data-wtype]").value].cat,ss=r.querySelector("[data-wslot]");
if(slotTaken(cat,r)[ss.value])ss.value=freeSlot(cat,r);refresh();}
function refresh(){var rs=rows();
rs.forEach(function(r){var d=TYPES[r.querySelector("[data-wtype]").value],s=r.querySelector("[data-wslot]").value;
r.querySelector(".wnote").textContent=LAB[d.cat]+" "+s;});
document.getElementById("wempty").style.display=rs.length?"none":"block";
var full=rs.length>=MAXT||firstFree(null)===null;
addbtn.disabled=full;document.getElementById("wfull").style.display=full?"block":"none";}
addbtn.addEventListener("click",function(){addRow(null,"");});
form.addEventListener("submit",function(){var rs=rows();document.getElementById("wcount").value=rs.length;
rs.forEach(function(r,i){r.querySelector("[data-wtype]").name="wtype"+i;r.querySelector("[data-wslot]").name="wslot"+i;r.querySelector("[data-wmac]").name="wmac"+i;});});
INITIAL.forEach(function(e){addRow(e.t,e.m,e.s);});
refresh();
})();</script>)WSJS";
  html += "</fieldset>";
#endif

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

#if (ENABLE_BLE_SCAN || ENABLE_BEEHIVE_GATT || ENABLE_WIRELESS_BEECOUNTER)
  // Dynamic wireless-sensor list. The portal submits wcount plus a wtypeN /
  // wmacN pair per row. We store that canonical list (so the page can repaint
  // the exact rows next time) AND fan each row out to the per-transport slot
  // keys the firmware reads: in-hive beacons/GATT -> ble_mac{0,1}, HiveHeart ->
  // heart_mac{0,1}, HiveScale -> scale_mac{0,1}, BeeCounter -> counter_mac{0,1}
  // (stored only; no firmware support yet). Each row also submits a wslotN (the
  // "Maps to" choice, 1 or 2) that decides the target index, so a sensor can be
  // mapped to hive/scale/counter 2 even when slot 1 is left to a wired sensor.
  int wcount = setupServer.arg("wcount").toInt();
  if (wcount < 0) wcount = 0;
  if (wcount > 6) wcount = 6;

  String bleMac[2]   = {"", ""};
  String heartMac[2] = {"", ""};
  String scaleMac[2] = {"", ""};
  String cntMac[2]   = {"", ""};

  prefs.putUInt("wcount", (uint32_t)wcount);
  for (int i = 0; i < wcount; i++) {
    String type = setupServer.arg(String("wtype") + i);
    String mac = portalNormalizeMac(setupServer.arg(String("wmac") + i));
    type.trim();
    int slot = setupServer.arg(String("wslot") + i).toInt();
    if (slot < 1) slot = 1;
    if (slot > 2) slot = 2;
    int idx = slot - 1;

    prefs.putString((String("wtype") + i).c_str(), type);
    prefs.putString((String("wmac") + i).c_str(), mac);
    prefs.putUInt((String("wslot") + i).c_str(), (uint32_t)slot);

    if (type == "hiveheart") {
      heartMac[idx] = mac;
    } else if (type == "hivescale") {
      scaleMac[idx] = mac;
    } else if (type == "beecounter") {
      cntMac[idx] = mac;
    } else {
      // holyiot / ruuvitag / hiveinside and any unknown type -> in-hive bridge.
      bleMac[idx] = mac;
    }
  }
  // Clear any stale list entries left over from a previously longer list.
  for (int i = wcount; i < 6; i++) {
    prefs.remove((String("wtype") + i).c_str());
    prefs.remove((String("wmac") + i).c_str());
    prefs.remove((String("wslot") + i).c_str());
  }

#if ENABLE_BLE_SCAN
  prefs.putString("ble_mac0", bleMac[0]);
  prefs.putString("ble_mac1", bleMac[1]);
  bleSensorMac0 = bleMac[0];
  bleSensorMac1 = bleMac[1];
#endif
#if ENABLE_BEEHIVE_GATT
  prefs.putString("heart_mac0", heartMac[0]);
  prefs.putString("heart_mac1", heartMac[1]);
  prefs.putString("scale_mac0", scaleMac[0]);
  prefs.putString("scale_mac1", scaleMac[1]);
  heartMac0 = heartMac[0];
  heartMac1 = heartMac[1];
  scaleMac0 = scaleMac[0];
  scaleMac1 = scaleMac[1];
#endif
  // HiveTraffic (wireless bee counter) MACs — consumed when ENABLE_WIRELESS_BEECOUNTER.
  prefs.putString("counter_mac0", cntMac[0]);
  prefs.putString("counter_mac1", cntMac[1]);
#if ENABLE_WIRELESS_BEECOUNTER
  trafficMac0 = cntMac[0];
  trafficMac1 = cntMac[1];
#endif
#endif

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
