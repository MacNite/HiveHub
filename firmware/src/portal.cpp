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
  html += "<p>All nearby BLE devices are listed. HolyIot 25015 beacons and HiveInside sensors show their type in the last column. GATT sensors (HiveHeart, wireless HiveScale) also appear — copy their MAC and paste it into the GATT pairing fields on the <a href='/'>setup page</a>.</p>";

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
          "button,a.button{display:inline-block;padding:11px 18px;margin:4px 6px 4px 0;font-size:1rem;text-decoration:none;border-radius:8px;border:1px solid var(--border);background:#fff;color:var(--fg);cursor:pointer}"
          "button:hover,a.button:hover{filter:brightness(.97)}"
          "button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}"
          "button.danger{color:#b42318;border-color:#f0c2bc;background:#fff7f6}"
          "table{border-collapse:collapse;width:100%}"
          "th,td{text-align:left;border-bottom:1px solid var(--border);padding:8px 6px}"
          "th{width:48%;font-weight:500;color:var(--muted)}"
          ".meta{color:var(--muted);font-size:.86em}p{margin:8px 0}"
          "@media(prefers-color-scheme:dark){:root{--bg:#161618;--card:#1f1f23;--fg:#ececf1;--muted:#9aa0aa;--border:#33343a;--link:#f5b54a}input{background:#26262b}button,a.button{background:#26262b}button.danger{background:#2a1d1d;border-color:#5a2d2a;color:#f7a59c}}"
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
  html += "<form method='POST' action='/save'>";
  html += "<fieldset><legend>Backend</legend>";
  html += "<label>Device ID</label><input name='device_id' value='" + htmlEscape(deviceId) + "'>";
  html += "<label>Claim code</label><input name='claim_code' value='" + htmlEscape(claimCode) + "'>";
  html += "<label>API base URL</label><input name='api_base' value='" + htmlEscape(apiBaseUrl) + "'>";
  html += "<label>API key</label><input name='api_key' value='" + htmlEscape(apiKey) + "'>";
  html += "</fieldset>";

#if ENABLE_BLE_SCAN
  html += "<fieldset><legend>In-hive BLE sensors (HolyIot 25015 / HiveInside)</legend>";
  html += "<p>Pair up to two sensors. Slot 1 maps to hive 1, slot 2 to hive 2. ";
  html += "The HolyIot 25015 beacon is supported and auto-detected. ";
  html += "When a paired sensor reports temperature, vibration or sound, the matching wired sensor for that hive is disabled automatically to avoid duplicate readings. ";
  html += "Enter each sensor's MAC address, or <a href='/ble/scan'>scan for nearby sensors</a> and copy a MAC below.</p>";
  html += "<label>Sensor 1 MAC (hive 1)</label><input name='ble_mac0' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(bleSensorMac0) + "'>";
  html += "<label>Sensor 2 MAC (hive 2)</label><input name='ble_mac1' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(bleSensorMac1) + "'>";
  html += "</fieldset>";
#endif

#if ENABLE_BEEHIVE_GATT
  html += "<fieldset><legend>beehivemonitoring.com sensors (HiveHeart / HiveScale)</legend>";
  html += "<p>Pair by MAC address. HiveScale connects to each device, reads one GATT notification and disconnects. ";
  html += "HiveHeart slot 1/2 map to hive 1/2; the wireless scales are independent. Leave a slot blank if you do not use it.";
#if ENABLE_BLE_SCAN
  html += " You can <a href='/ble/scan'>scan for nearby sensors</a> and copy a MAC below.";
#else
  html += " Read each device's MAC with a phone BLE scanner (e.g. nRF Connect) and paste it below.";
#endif
  html += "</p>";
  html += "<label>HiveHeart 1 MAC (hive 1)</label><input name='heart_mac0' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(heartMac0) + "'>";
  html += "<label>HiveHeart 2 MAC (hive 2)</label><input name='heart_mac1' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(heartMac1) + "'>";
  html += "<label>HiveScale 1 MAC</label><input name='scale_mac0' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(scaleMac0) + "'>";
  html += "<label>HiveScale 2 MAC</label><input name='scale_mac1' placeholder='AA:BB:CC:DD:EE:FF' value='" + htmlEscape(scaleMac1) + "'>";
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

#if ENABLE_BLE_SCAN
  // Persist the paired HolyIot 25015 MACs. An empty field clears that slot.
  // Normalising here means an invalid entry is stored as "" (unpaired) rather
  // than a string that can never match an advertisement.
  String bleMac0 = blesensor::normalizeMac(setupServer.arg("ble_mac0"));
  String bleMac1 = blesensor::normalizeMac(setupServer.arg("ble_mac1"));
  prefs.putString("ble_mac0", bleMac0);
  prefs.putString("ble_mac1", bleMac1);
  bleSensorMac0 = bleMac0;
  bleSensorMac1 = bleMac1;
#endif

#if ENABLE_BEEHIVE_GATT
  // Persist the paired HiveHeart / HiveScale MACs. An empty field clears that
  // slot; invalid entries normalise to "" so they can never match a device.
  heartMac0 = bhgatt::normalizeMac(setupServer.arg("heart_mac0"));
  heartMac1 = bhgatt::normalizeMac(setupServer.arg("heart_mac1"));
  scaleMac0 = bhgatt::normalizeMac(setupServer.arg("scale_mac0"));
  scaleMac1 = bhgatt::normalizeMac(setupServer.arg("scale_mac1"));
  prefs.putString("heart_mac0", heartMac0);
  prefs.putString("heart_mac1", heartMac1);
  prefs.putString("scale_mac0", scaleMac0);
  prefs.putString("scale_mac1", scaleMac1);
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
