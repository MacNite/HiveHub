// device_prefs.cpp — implementation of NVS / WiFi credential helpers.
#include "device_prefs.h"
#include "globals.h"
#include "config.h"
#include "secrets.h"
#include "beehive_gatt.h"

String prefString(const char* key, const char* fallback) {
  prefs.begin("hivescale", true);
  String value = prefs.getString(key, fallback);
  prefs.end();
  return value;
}

void putPrefString(const char* key, const String& value) {
  prefs.begin("hivescale", false);
  prefs.putString(key, value);
  prefs.end();
}

String wifiSsidKey(int index) { return String("wifi") + index + "_ssid"; }
String wifiPassKey(int index) { return String("wifi") + index + "_pass"; }

void seedPrefsFromSecretsIfNeeded() {
  prefs.begin("hivescale", false);
  bool seeded = prefs.getBool("seeded", false);

  if (!seeded) {
    Serial.println("[PREFS] First boot seed from secrets.h");

    prefs.putString("api_base", API_BASE_URL);
    prefs.putString("api_key", API_KEY);
    prefs.putString("device_id", DEVICE_ID);

    #ifdef CLAIM_CODE
      prefs.putString("claim_code", CLAIM_CODE);
    #endif

    #ifdef CLAIM_CODE_REVISION
      prefs.putUInt("claim_rev", CLAIM_CODE_REVISION);
    #else
      prefs.putUInt("claim_rev", 1);
    #endif

    int wifiCount = 0;

    // --- NEW: support WIFI1 / WIFI2 / WIFI3 ---
    #ifdef WIFI1_SSID
      prefs.putString("wifi0_ssid", WIFI1_SSID);
      prefs.putString("wifi0_pass", WIFI1_PASS);
      wifiCount++;
    #endif

    #ifdef WIFI2_SSID
      prefs.putString("wifi1_ssid", WIFI2_SSID);
      prefs.putString("wifi1_pass", WIFI2_PASS);
      wifiCount++;
    #endif

    #ifdef WIFI3_SSID
      prefs.putString("wifi2_ssid", WIFI3_SSID);
      prefs.putString("wifi2_pass", WIFI3_PASS);
      wifiCount++;
    #endif

    // --- fallback: old single-WiFi format ---
    #if defined(WIFI_SSID) && defined(WIFI_PASS)
      if (wifiCount == 0 && String(WIFI_SSID).length() > 0) {
        prefs.putString("wifi0_ssid", WIFI_SSID);
        prefs.putString("wifi0_pass", WIFI_PASS);
        wifiCount = 1;
      }
    #endif

    prefs.putUInt("wifi_count", wifiCount);

    prefs.putBool("seeded", true);
    prefs.putBool("provisioned", true);
  }

  // Seed paired beehivemonitoring.com GATT MACs from secrets.h so a device can
  // ship pre-paired without visiting the provisioning portal. This runs on every
  // boot (NOT gated behind the `seeded` flag above) so that devices already
  // seeded by an older firmware — which predates these keys — still pick up the
  // MACs after an upgrade. We only write a key when it is ABSENT, so any pairing
  // (or deliberate un-pairing) done later from the portal is always preserved.
  #if ENABLE_BEEHIVE_GATT
    // Normalize seeded MACs to the canonical uppercase colon-separated form
    // (same as portal-entered MACs) so the GATT connect path gets a parseable
    // address regardless of how the configurator emitted them. If normalization
    // fails (malformed value) we fall back to the raw string rather than
    // silently dropping a configured pairing.
    auto seedMac = [&](const char* key, const char* raw) {
      if (prefs.isKey(key)) return;
      String norm = bhgatt::normalizeMac(String(raw));
      prefs.putString(key, norm.length() ? norm : String(raw));
    };
    #ifdef INHIVE_1_MAC
      seedMac("heart_mac0", INHIVE_1_MAC);
    #endif
    #ifdef INHIVE_2_MAC
      seedMac("heart_mac1", INHIVE_2_MAC);
    #endif
    #ifdef WSCALE_1_MAC
      seedMac("scale_mac0", WSCALE_1_MAC);
    #endif
    #ifdef WSCALE_2_MAC
      seedMac("scale_mac1", WSCALE_2_MAC);
    #endif
  #endif

  #ifdef CLAIM_CODE
    uint32_t storedClaimRevision = prefs.getUInt("claim_rev", 0);
    #ifdef CLAIM_CODE_REVISION
      uint32_t firmwareClaimRevision = CLAIM_CODE_REVISION;
    #else
      uint32_t firmwareClaimRevision = 1;
    #endif

    if (FORCE_RESEED || storedClaimRevision < firmwareClaimRevision) {
      Serial.println("[PREFS] Updating claim code from secrets.h revision");
      prefs.putString("claim_code", CLAIM_CODE);
      prefs.putUInt("claim_rev", firmwareClaimRevision);
      prefs.putBool("claim_reg", false);  // force the new code to be sent once
    }
  #endif

  prefs.end();
}

void loadConfigFromPrefs() {
  prefs.begin("hivescale", false);

  apiBaseUrl = prefs.getString("api_base", API_BASE_URL);
  apiKey = prefs.getString("api_key", API_KEY);
  deviceId = prefs.getString("device_id", DEVICE_ID);
  claimCode = prefs.getString("claim_code", CLAIM_CODE);
  claimRegistered = prefs.getBool("claim_reg", false);

  sendIntervalMs = prefs.getUInt("interval", 600) * 1000UL;
  scale1Offset = prefs.getLong("s1_offset", 0);
  scale2Offset = prefs.getLong("s2_offset", 0);
  scale1Factor = prefs.getFloat("s1_factor", -7050.0f);
  scale2Factor = prefs.getFloat("s2_factor", -7050.0f);

#if ENABLE_HOLYIOT_BLE
  bleSensorMac0 = prefs.getString("ble_mac0", "");
  bleSensorMac1 = prefs.getString("ble_mac1", "");
#endif

#if ENABLE_BEEHIVE_GATT
  heartMac0 = prefs.getString("heart_mac0", "");
  heartMac1 = prefs.getString("heart_mac1", "");
  scaleMac0 = prefs.getString("scale_mac0", "");
  scaleMac1 = prefs.getString("scale_mac1", "");
#endif

  prefs.end();

  Serial.println("[PREFS] Loaded config");
  Serial.printf("[PREFS] device_id: %s\n", deviceId.c_str());
  Serial.printf("[PREFS] claim_code present: %s\n", claimCode.length() > 0 ? "yes" : "no");
  Serial.printf("[PREFS] api_base: %s\n", apiBaseUrl.c_str());
  Serial.printf("[PREFS] interval ms: %lu\n", sendIntervalMs);
  Serial.printf("[PREFS] scale1 offset: %ld factor: %.6f\n", scale1Offset, scale1Factor);
  Serial.printf("[PREFS] scale2 offset: %ld factor: %.6f\n", scale2Offset, scale2Factor);
}

void markClaimRegistered() {
  if (claimRegistered) return;
  claimRegistered = true;
  prefs.begin("hivescale", false);
  prefs.putBool("claim_reg", true);
  prefs.end();
  Serial.println("[PREFS] Claim registered; claim_code will no longer be sent");
}

void saveScaleConfig() {
  prefs.begin("hivescale", false);
  prefs.putUInt("interval", sendIntervalMs / 1000UL);
  prefs.putLong("s1_offset", scale1Offset);
  prefs.putLong("s2_offset", scale2Offset);
  prefs.putFloat("s1_factor", scale1Factor);
  prefs.putFloat("s2_factor", scale2Factor);
  prefs.end();
}

int getWifiCount() {
  prefs.begin("hivescale", true);
  uint32_t count = prefs.getUInt("wifi_count", 0);
  prefs.end();
  if (count > MAX_WIFI_NETWORKS) count = MAX_WIFI_NETWORKS;
  return (int)count;
}

bool saveWifiNetwork(int index, const String& ssid, const String& pass) {
  if (index < 0 || index >= MAX_WIFI_NETWORKS || ssid.length() == 0) return false;

  prefs.begin("hivescale", false);
  prefs.putString(wifiSsidKey(index).c_str(), ssid);
  prefs.putString(wifiPassKey(index).c_str(), pass);

  int count = prefs.getUInt("wifi_count", 0);
  if (index + 1 > count) prefs.putUInt("wifi_count", index + 1);
  prefs.putBool("provisioned", true);
  prefs.end();
  return true;
}

void clearWifiCredentials() {
  prefs.begin("hivescale", false);
  for (int i = 0; i < MAX_WIFI_NETWORKS; i++) {
    prefs.remove(wifiSsidKey(i).c_str());
    prefs.remove(wifiPassKey(i).c_str());
  }
  prefs.putUInt("wifi_count", 0);
  prefs.end();
  activeWifiSsid = "";
}

void factoryResetPreferences() {
  Serial.println("[PREFS] Factory reset requested");
  prefs.begin("hivescale", false);
  prefs.clear();
  prefs.end();
  delay(300);
  ESP.restart();
}
