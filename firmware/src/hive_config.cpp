// hive_config.cpp — load/save the dynamic hive registry to NVS. Before any
// portal-saved registry exists, seeds it either from secrets.h HIVE_i_JSON
// macros (seedHivesFromSecrets, any hive count up to MAX_HIVES) or, absent
// those, by migrating the legacy two-slot keys (migrateLegacy, 2 hives only).
#include "hive_config.h"
#include "globals.h"
#include "config.h"
#include "scale_topology.h"
#include "scale_math.h"

#include <ArduinoJson.h>
#include <Preferences.h>

namespace hivecfg {

Hive    gHives[MAX_HIVES];
uint8_t gHiveCount = 0;

bool BlePairing::isGatt() const {
  // Connection-based sensors that count against MAX_GATT_READS_PER_CYCLE. HolyIot,
  // RuuviTag and the nRF54LM20A HiveInside ("hiveinside_nrf54") are passive
  // beacons (caught by the single shared scan) and never counted. The legacy
  // ESP32-C6 HiveInside prototype ("hiveinside") serves measurements over GATT,
  // so it stays connection-based here.
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
      // No "addr": the NAU7802 address is fixed at 0x2A and not configurable.
      // Legacy blobs carrying an addr are normalized on parse (hiveFromJson).
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
      c.backend = ScaleBackend::NAU7802;
      // Preserve out-of-range values in a detectable form instead of masking
      // them (& 0x07 could silently map two hives to one physical channel):
      // anything below -1 becomes -2, anything above 7 becomes 8; both fail
      // validateRegistry() and the channel is never operated.
      int mux = o["mux"] | -1;
      c.muxChannel = (int8_t)(mux < -1 ? -2 : (mux > 7 ? 8 : mux));
      int adc = o["adc"] | 1;
      c.adcChannel = (uint8_t)((adc == 1 || adc == 2) ? adc : 0);  // 0 = invalid
      // Legacy blobs carried a configurable "addr"; the NAU7802 address is
      // fixed at 0x2A. Normalize and log — any other value was never reachable.
      int addr = o["addr"] | (int)NAU7802_I2C_ADDRESS;
      if (addr != (int)NAU7802_I2C_ADDRESS) {
        Serial.printf("[HIVECFG] hive %u: legacy scale addr 0x%02X normalized to fixed 0x%02X\n",
                      out.index, (unsigned)addr, NAU7802_I2C_ADDRESS);
      }
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
    }
#endif
    c.offset = (long)(o["off"] | 0);
    c.factor = (float)(o["fac"] | -7050.0);
    out.scaleCount++;
  }

  const char* ds = d["ds"] | (const char*)nullptr;
  if (ds && romFromHex(String(ds), out.dsRom)) out.hasDsRom = true;

  bool scaleAssigned = out.scaleCount > 0;
  bool inHiveSensorAssigned = out.hasDsRom;

  JsonArray bl = d["bl"].as<JsonArray>();
  for (JsonObject o : bl) {
    if (out.bleCount >= MAX_BLE_PER_HIVE) break;
    String type = o["t"] | "";
    String mac  = o["m"] | "";
    if (!mac.length()) continue;

    // A beehivemonitoring.com HiveScale is a scale source, not an in-hive
    // auxiliary sensor. Keep at most one scale source per hive: either the
    // single wired channel above, or one wireless HiveScale pairing.
    if (type == "hivescale") {
      if (scaleAssigned) continue;
      scaleAssigned = true;
    } else {
      // The backend has one nested `ble` object per hive_readings row, so keep
      // only one in-hive BLE sensor and do not allow it together with DS18B20.
      if (inHiveSensorAssigned) continue;
      inHiveSensorAssigned = true;
    }

    out.ble[out.bleCount].type = type;
    out.ble[out.bleCount].mac  = mac;
    out.bleCount++;
  }
  return out.index >= 1;
}

// ── Legacy migration ───────────────────────────────────────────────────────
// Build a two-hive registry from the pre-0.20 fixed keys so an upgraded device
// keeps working until the owner re-maps hives in the new portal. Hive N gets the
// HX711 instance N-1 (with its stored offset/factor) and the slot-N BLE pairings
// from the old fixed-key fan-out (ble_mac, heart_mac, scale_mac, counter_mac).
static void migrateLegacy(Preferences& p) {
  auto addInHiveBle = [](Hive& h, const char* type, const String& mac) {
    if (mac.length() == 0 || h.hasDsRom || h.bleCount >= MAX_BLE_PER_HIVE) return;
    for (uint8_t b = 0; b < h.bleCount; b++)
      if (h.ble[b].type != "hivescale") return;
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
#endif
    h.scales[0].offset  = p.getLong(i == 0 ? "s1_offset" : "s2_offset", 0);
    h.scales[0].factor  = p.getFloat(i == 0 ? "s1_factor" : "s2_factor", -7050.0f);
    // No ROM was stored pre-0.20; sensors.cpp falls back to probe index order
    // (hive.index-1) when hasDsRom is false, preserving the old behaviour.
    // Keep one non-scale in-hive sensor in the new registry. Legacy wireless
    // HiveScale pairings were secondary scale sources, so they are not migrated
    // when the hive already has a wired scale channel.
    addInHiveBle(h, "holyiot",    p.getString(i == 0 ? "ble_mac0"     : "ble_mac1",     ""));
    addInHiveBle(h, "hiveheart",  p.getString(i == 0 ? "heart_mac0"   : "heart_mac1",   ""));
    addInHiveBle(h, "beecounter", p.getString(i == 0 ? "counter_mac0" : "counter_mac1", ""));
  }
  gHiveCount = 2;
  Serial.println("[HIVECFG] Migrated legacy 2-slot config into the hive registry");
}

// Populate the pre-0.20 per-slot globals from the registry's first two hives for
// legacy two-slot code paths that still consume them (beacon compatibility). The
// beehivemonitoring.com HiveHeart/HiveScale GATT client and the HiveTraffic bee
// counter both read the dynamic registry directly, so those sensors can be
// mapped to any hive up to MAX_HIVES.
static void bridgeLegacyGlobals() {
  auto find = [](const Hive& h, const char* type) -> String {
    for (uint8_t b = 0; b < h.bleCount; b++)
      if (h.ble[b].type == type) return h.ble[b].mac;
    return String("");
  };
  auto firstBeacon = [](const Hive& h) -> String {
    for (uint8_t b = 0; b < h.bleCount; b++) {
      const String& t = h.ble[b].type;
      if (t == "holyiot" || t == "ruuvitag" ||
          t == "hiveinside" || t == "hiveinside_nrf54") return h.ble[b].mac;
    }
    return String("");
  };
  String beacon[2] = {"", ""}, heart[2] = {"", ""}, wscale[2] = {"", ""};
  for (uint8_t h = 0; h < gHiveCount && h < 2; h++) {
    beacon[h] = firstBeacon(gHives[h]);
    heart[h]  = find(gHives[h], "hiveheart");
    wscale[h] = find(gHives[h], "hivescale");
  }
#if ENABLE_BLE_SCAN
  bleSensorMac0 = beacon[0]; bleSensorMac1 = beacon[1];
#endif
#if ENABLE_BEEHIVE_GATT
  heartMac0 = heart[0]; heartMac1 = heart[1];
  scaleMac0 = wscale[0]; scaleMac1 = wscale[1];
#endif
  // HiveTraffic bee counters are read straight from the registry by
  // bee_counter_client.cpp (any hive up to MAX_HIVES), so no per-slot bridge.
}

// ── Secrets.h pre-seed (HIVE_COUNT / HIVE_i_JSON) ──────────────────────────
// Builds the registry straight from up to MAX_HIVES HIVE_i_JSON macros, each
// the exact blob shape hiveToJson() emits / hiveFromJson() parses (see the
// struct comment above and website/assets/configurator.js, which generates
// this format). Runs ONLY on a device that has never been configured from the
// on-device portal (loadHiveConfig() below gates this on the absence of the
// "hive_count" NVS key) and only when HIVE_COUNT > 0; otherwise migrateLegacy()
// keeps producing the historical 2-hive registry. Like migrateLegacy(), this
// is NOT persisted to NVS — it is cheap to re-derive from the compiled-in
// macros every boot until the owner saves anything from the portal.
static void seedHivesFromSecrets() {
  uint8_t want = (uint8_t)((HIVE_COUNT) > MAX_HIVES ? MAX_HIVES : (HIVE_COUNT));
  gHiveCount = 0;

#ifdef HIVE_1_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_1_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_2_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_2_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_3_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_3_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_4_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_4_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_5_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_5_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_6_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_6_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_7_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_7_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_8_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_8_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_9_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_9_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_10_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_10_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_11_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_11_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_12_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_12_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_13_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_13_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_14_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_14_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_15_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_15_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_16_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_16_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_17_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_17_JSON, h)) gHives[gHiveCount++] = h; }
#endif
#ifdef HIVE_18_JSON
  if (gHiveCount < want) { Hive h; if (hiveFromJson(HIVE_18_JSON, h)) gHives[gHiveCount++] = h; }
#endif

  Serial.printf("[HIVECFG] Seeded %u hive(s) from secrets.h (HIVE_COUNT=%d)\n",
                gHiveCount, (int)(HIVE_COUNT));
}

// ── Authoritative registry validation ────────────────────────────────────────
// Runs after every load AND after every portal save. The portal page's dropdown
// logic is convenience only; this is what actually prevents an invalid or
// crafted topology from operating hardware. Pure rules live in
// scale_topology.h (host-tested); this wrapper maps them onto gHives, drops
// hives with bad/duplicate indices, flags bad channels with configError (the
// stored blob is preserved so the owner can fix it in the portal) and logs
// every problem precisely.
bool validateRegistry() {
  bool clean = true;

  // 1) Hive indices: in range, unique. Offenders are dropped from the live
  //    registry — operating a hive under a colliding index could attribute its
  //    readings to another hive.
  uint8_t idx[MAX_HIVES];
  uint8_t bad[MAX_HIVES];
  for (uint8_t i = 0; i < gHiveCount; i++) idx[i] = gHives[i].index;
  if (!scaletopo::validateHiveIndices(idx, gHiveCount, MAX_HIVES, bad)) {
    clean = false;
    uint8_t kept = 0;
    for (uint8_t i = 0; i < gHiveCount; i++) {
      if (bad[i]) {
        Serial.printf("[HIVECFG] REJECTED hive slot %u: index %u is %s (valid: unique, 1..%u)\n",
                      i, gHives[i].index,
                      (gHives[i].index < 1 || gHives[i].index > MAX_HIVES) ? "out of range"
                                                                           : "a duplicate",
                      MAX_HIVES);
        continue;
      }
      if (kept != i) gHives[kept] = gHives[i];
      kept++;
    }
    gHiveCount = kept;
  }

  // 2) Wired channel topology: range checks (no masking), duplicate physical
  //    channels, direct+muxed NAU7802 conflicts.
  scaletopo::ChanSpec specs[MAX_HIVES * MAX_SCALES_PER_HIVE];
  uint16_t issues[MAX_HIVES * MAX_SCALES_PER_HIVE];
  struct Ref { uint8_t hive; uint8_t slot; };
  Ref refs[MAX_HIVES * MAX_SCALES_PER_HIVE];
  uint8_t n = 0;
  for (uint8_t h = 0; h < gHiveCount; h++) {
    for (uint8_t s = 0; s < gHives[h].scaleCount; s++) {
      ScaleChannel& c = gHives[h].scales[s];
      c.configError = false;
      if (c.backend == ScaleBackend::None) continue;
      specs[n].hiveIndex  = gHives[h].index;
      specs[n].backend    = (c.backend == ScaleBackend::HX711) ? scaletopo::Backend::HX711
                                                               : scaletopo::Backend::NAU7802;
      specs[n].hxIndex    = c.hxIndex;
      specs[n].muxChannel = c.muxChannel;
      specs[n].adcChannel = c.adcChannel;
      refs[n] = {h, s};
      n++;
    }
  }
  scaletopo::TopologyResult topo = scaletopo::validateChannels(specs, n, issues);
  if (!topo.ok) {
    clean = false;
    for (uint8_t i = 0; i < n; i++) {
      if (!issues[i]) continue;
      ScaleChannel& c = gHives[refs[i].hive].scales[refs[i].slot];
      c.configError = true;
      Serial.printf("[HIVECFG] DISABLED scale channel (hive %u, slot %u, %s mux=%d adc=%u hx=%u):",
                    specs[i].hiveIndex, refs[i].slot,
                    specs[i].backend == scaletopo::Backend::HX711 ? "hx711" : "nau7802",
                    (int)specs[i].muxChannel, (unsigned)specs[i].adcChannel, specs[i].hxIndex);
      if (issues[i] & scaletopo::CHAN_BAD_MUX)      Serial.print(" mux out of range (-1..7)");
      if (issues[i] & scaletopo::CHAN_BAD_ADC)      Serial.print(" ADC channel not 1/2");
      if (issues[i] & scaletopo::CHAN_BAD_HX_INDEX) Serial.print(" HX711 index not 0/1");
      if (issues[i] & scaletopo::CHAN_DUPLICATE)    Serial.print(" duplicate physical channel");
      if (issues[i] & scaletopo::CHAN_DIRECT_MUX_CONFLICT)
        Serial.print(" direct + muxed NAU7802 cannot coexist (shared fixed 0x2A)");
      Serial.println(" — channel will not be operated; fix it in the portal");
    }
  }

  // 3) Calibration sanity. An implausible factor blocks weight conversion
  //    (sensors.cpp) but the channel stays readable so the portal /calibrate
  //    page can fix it — configError is NOT set for this.
  for (uint8_t h = 0; h < gHiveCount; h++) {
    for (uint8_t s = 0; s < gHives[h].scaleCount; s++) {
      const ScaleChannel& c = gHives[h].scales[s];
      if (c.backend == ScaleBackend::None || c.configError) continue;
      if (!scalemath::factorPlausible(c.factor)) {
        clean = false;
        Serial.printf("[HIVECFG] hive %u scale %u: calibration factor %.3f is implausible; "
                      "no weight will be reported until it is recalibrated\n",
                      gHives[h].index, s, (double)c.factor);
      }
    }
  }

  return clean;
}

void loadHiveConfig() {
  prefs.begin("hivescale", true);

  if (!prefs.isKey("hive_count")) {
#if HIVE_COUNT > 0
    seedHivesFromSecrets();
#else
    migrateLegacy(prefs);
#endif
    prefs.end();
    validateRegistry();
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
  validateRegistry();
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
