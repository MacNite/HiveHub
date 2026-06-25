// hive_config.cpp — load/save the dynamic hive registry to NVS, with one-time
// migration from the legacy two-slot keys.
#include "hive_config.h"
#include "globals.h"
#include "config.h"

#include <ArduinoJson.h>
#include <Preferences.h>

namespace hivecfg {

Hive    gHives[MAX_HIVES];
uint8_t gHiveCount = 0;

bool BlePairing::isGatt() const {
  // Connection-based sensors that count against MAX_GATT_READS_PER_CYCLE. HolyIot
  // and RuuviTag are passive beacons (caught by the single shared scan) and never
  // counted. HiveInside is GATT in current firmware, so treat it as GATT here.
  return type == "hiveinside" || type == "hiveheart" ||
         type == "hivescale"  || type == "beecounter";
}

static char hexNibble(uint8_t v) { return v < 10 ? char('0' + v) : char('A' + v - 10); }

String romToHex(const uint8_t rom[8]) {
  String s;
  for (int i = 0; i < 8; i++) { s += hexNibble(rom[i] >> 4); s += hexNibble(rom[i] & 0x0F); }
  return s;
}

bool romFromHex(const String& hex, uint8_t rom[8]) {
  if (hex.length() != 16) return false;
  for (int i = 0; i < 8; i++) {
    auto val = [&](char c) -> int {
      if (c >= '0' && c <= '9') return c - '0';
      if (c >= 'a' && c <= 'f') return c - 'a' + 10;
      if (c >= 'A' && c <= 'F') return c - 'A' + 10;
      return -1;
    };
    int hi = val(hex[i * 2]), lo = val(hex[i * 2 + 1]);
    if (hi < 0 || lo < 0) return false;
    rom[i] = (uint8_t)((hi << 4) | lo);
  }
  return true;
}

String hiveToJson(const Hive& h) {
  JsonDocument d;
  d["i"] = h.index;
  if (h.name.length()) d["n"] = h.name;

  JsonArray sc = d["s"].to<JsonArray>();
  for (uint8_t k = 0; k < h.scaleCount && k < MAX_SCALES_PER_HIVE; k++) {
    const ScaleChannel& c = h.scales[k];
    JsonObject o = sc.add<JsonObject>();
    if (c.backend == ScaleBackend::NAU7802) {
      o["b"]    = "nau";
      o["mux"]  = c.muxChannel;
      o["adc"]  = c.adcChannel;
      o["addr"] = c.i2cAddr;
    } else {
      o["b"]  = "hx";
      o["hx"] = c.hxIndex;
    }
    o["off"] = c.offset;
    o["fac"] = c.factor;
  }

  if (h.hasDsRom) d["ds"] = romToHex(h.dsRom);

  JsonArray bl = d["bl"].to<JsonArray>();
  for (uint8_t k = 0; k < h.bleCount && k < MAX_BLE_PER_HIVE; k++) {
    JsonObject o = bl.add<JsonObject>();
    o["t"] = h.ble[k].type;
    o["m"] = h.ble[k].mac;
  }

  String out;
  serializeJson(d, out);
  return out;
}

bool hiveFromJson(const String& json, Hive& out) {
  JsonDocument d;
  if (deserializeJson(d, json)) return false;

  out = Hive{};
  out.index = (uint8_t)(d["i"] | 0);
  out.name  = d["n"] | "";

  JsonArray sc = d["s"].as<JsonArray>();
  for (JsonObject o : sc) {
    if (out.scaleCount >= MAX_SCALES_PER_HIVE) break;
    ScaleChannel& c = out.scales[out.scaleCount];
    String b = o["b"] | "hx";
    if (b == "nau") {
      c.backend    = ScaleBackend::NAU7802;
      c.muxChannel = (int8_t)(o["mux"] | -1);
      c.adcChannel = (uint8_t)(o["adc"] | 1);
      c.i2cAddr    = (uint8_t)(o["addr"] | NAU7802_I2C_ADDRESS);
    }
#if ENABLE_HX711
    else {
      c.backend = ScaleBackend::HX711;
      c.hxIndex = (uint8_t)(o["hx"] | 0);
    }
#else
    else {
      // No HX711 on this build (XIAO C6): treat any legacy/unknown "hx" channel
      // as a direct NAU7802 (CH1) so the hive still reads instead of returning 0.
      c.backend    = ScaleBackend::NAU7802;
      c.muxChannel = -1;
      c.adcChannel = 1;
      c.i2cAddr    = NAU7802_I2C_ADDRESS;
    }
#endif
    c.offset = (long)(o["off"] | 0);
    c.factor = (float)(o["fac"] | -7050.0);
    out.scaleCount++;
  }

  const char* ds = d["ds"] | (const char*)nullptr;
  if (ds && romFromHex(String(ds), out.dsRom)) out.hasDsRom = true;

  JsonArray bl = d["bl"].as<JsonArray>();
  for (JsonObject o : bl) {
    if (out.bleCount >= MAX_BLE_PER_HIVE) break;
    out.ble[out.bleCount].type = o["t"] | "";
    out.ble[out.bleCount].mac  = o["m"] | "";
    if (out.ble[out.bleCount].mac.length()) out.bleCount++;
  }
  return out.index >= 1;
}

// ── Legacy migration ───────────────────────────────────────────────────────
// Build a two-hive registry from the pre-0.20 fixed keys so an upgraded device
// keeps working until the owner re-maps hives in the new portal. Hive N gets the
// HX711 instance N-1 (with its stored offset/factor) and the slot-N BLE pairings
// from the old fixed-key fan-out (ble_mac, heart_mac, scale_mac, counter_mac).
static void migrateLegacy(Preferences& p) {
  auto addBle = [](Hive& h, const char* type, const String& mac) {
    if (mac.length() == 0 || h.bleCount >= MAX_BLE_PER_HIVE) return;
    h.ble[h.bleCount].type = type;
    h.ble[h.bleCount].mac  = mac;
    h.bleCount++;
  };

  for (uint8_t i = 0; i < 2; i++) {
    Hive& h = gHives[i];
    h = Hive{};
    h.index = i + 1;
    h.scaleCount = 1;
#if ENABLE_HX711
    h.scales[0].backend = ScaleBackend::HX711;
    h.scales[0].hxIndex = i;
#else
    // No HX711 on this build (XIAO C6): map the two legacy hives onto the single
    // direct NAU7802's two inputs (hive 1 -> CH1, hive 2 -> CH2). Matches the
    // V0.4 breakout, where one NAU7802 on the main I2C bus carries both cells.
    h.scales[0].backend    = ScaleBackend::NAU7802;
    h.scales[0].muxChannel = -1;
    h.scales[0].adcChannel = i + 1;
    h.scales[0].i2cAddr    = NAU7802_I2C_ADDRESS;
#endif
    h.scales[0].offset  = p.getLong(i == 0 ? "s1_offset" : "s2_offset", 0);
    h.scales[0].factor  = p.getFloat(i == 0 ? "s1_factor" : "s2_factor", -7050.0f);
    // No ROM was stored pre-0.20; sensors.cpp falls back to probe index order
    // (hive.index-1) when hasDsRom is false, preserving the old behaviour.
    addBle(h, "holyiot",    p.getString(i == 0 ? "ble_mac0"     : "ble_mac1",     ""));
    addBle(h, "hiveheart",  p.getString(i == 0 ? "heart_mac0"   : "heart_mac1",   ""));
    addBle(h, "hivescale",  p.getString(i == 0 ? "scale_mac0"   : "scale_mac1",   ""));
    addBle(h, "beecounter", p.getString(i == 0 ? "counter_mac0" : "counter_mac1", ""));
  }
  gHiveCount = 2;
  Serial.println("[HIVECFG] Migrated legacy 2-slot config into the hive registry");
}

// Populate the pre-0.20 per-slot globals from the registry's first two hives for
// legacy two-slot code paths that still consume them (beacon compatibility and
// HiveTraffic bee counter). The beehivemonitoring.com HiveHeart/HiveScale GATT
// client reads the dynamic registry directly, so those sensors can be mapped to
// any hive up to MAX_HIVES.
static void bridgeLegacyGlobals() {
  auto find = [](const Hive& h, const char* type) -> String {
    for (uint8_t b = 0; b < h.bleCount; b++)
      if (h.ble[b].type == type) return h.ble[b].mac;
    return String("");
  };
  auto firstBeacon = [](const Hive& h) -> String {
    for (uint8_t b = 0; b < h.bleCount; b++) {
      const String& t = h.ble[b].type;
      if (t == "holyiot" || t == "ruuvitag" || t == "hiveinside") return h.ble[b].mac;
    }
    return String("");
  };
  String beacon[2] = {"", ""}, heart[2] = {"", ""}, wscale[2] = {"", ""}, cnt[2] = {"", ""};
  for (uint8_t h = 0; h < gHiveCount && h < 2; h++) {
    beacon[h] = firstBeacon(gHives[h]);
    heart[h]  = find(gHives[h], "hiveheart");
    wscale[h] = find(gHives[h], "hivescale");
    cnt[h]    = find(gHives[h], "beecounter");
  }
#if ENABLE_BLE_SCAN
  bleSensorMac0 = beacon[0]; bleSensorMac1 = beacon[1];
#endif
#if ENABLE_BEEHIVE_GATT
  heartMac0 = heart[0]; heartMac1 = heart[1];
  scaleMac0 = wscale[0]; scaleMac1 = wscale[1];
#endif
#if ENABLE_WIRELESS_BEECOUNTER
  trafficMac0 = cnt[0]; trafficMac1 = cnt[1];
#endif
}

void loadHiveConfig() {
  prefs.begin("hivescale", true);

  if (!prefs.isKey("hive_count")) {
    migrateLegacy(prefs);
    prefs.end();
    bridgeLegacyGlobals();
    return;
  }

  uint8_t count = (uint8_t)prefs.getUInt("hive_count", 0);
  if (count > MAX_HIVES) count = MAX_HIVES;

  gHiveCount = 0;
  for (uint8_t i = 0; i < count; i++) {
    String key = String("h") + i + "_cfg";
    String blob = prefs.getString(key.c_str(), "");
    Hive h;
    if (blob.length() && hiveFromJson(blob, h)) {
      gHives[gHiveCount++] = h;
    }
  }
  prefs.end();
  bridgeLegacyGlobals();

  Serial.printf("[HIVECFG] Loaded %u hive(s), %u scale channel(s)\n",
                gHiveCount, totalScaleChannels());
  // Per-hive sensor dump — confirms exactly which type/MAC is stored for each
  // hive (so a "sensor reverted to the wrong type" report can be checked against
  // what is actually in NVS rather than what the portal happens to render).
  for (uint8_t h = 0; h < gHiveCount; h++) {
    const Hive& hive = gHives[h];
    Serial.printf("[HIVECFG]   hive %u \"%s\": %u scale(s), %u BLE sensor(s)\n",
                  hive.index, hive.name.c_str(), hive.scaleCount, hive.bleCount);
    for (uint8_t b = 0; b < hive.bleCount; b++)
      Serial.printf("[HIVECFG]     ble[%u] type=%s mac=%s\n",
                    b, hive.ble[b].type.c_str(), hive.ble[b].mac.c_str());
  }
}

void saveHiveConfig() {
  prefs.begin("hivescale", false);
  prefs.putUInt("hive_count", gHiveCount);
  for (uint8_t i = 0; i < gHiveCount; i++) {
    String key = String("h") + i + "_cfg";
    prefs.putString(key.c_str(), hiveToJson(gHives[i]));
  }
  // Drop any stale blobs from a previously longer list.
  for (uint8_t i = gHiveCount; i < MAX_HIVES; i++) {
    String key = String("h") + i + "_cfg";
    if (prefs.isKey(key.c_str())) prefs.remove(key.c_str());
  }
  prefs.end();
}

uint8_t totalScaleChannels() {
  uint16_t n = 0;
  for (uint8_t i = 0; i < gHiveCount; i++) n += gHives[i].scaleCount;
  return (uint8_t)(n > MAX_SCALES ? MAX_SCALES : n);
}

}  // namespace hivecfg
