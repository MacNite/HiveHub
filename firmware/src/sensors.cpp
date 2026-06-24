// sensors.cpp — time sync, scale reads and measurement JSON assembly.
#include "sensors.h"
#include "globals.h"
#include "config.h"
#include "hivescale_network.h"
#include "storage_power.h"
#include "bee_counter_client.h"
#include "hive_config.h"
#include "scale_bus.h"

#include <WiFi.h>
#include <time.h>
#include <sys/time.h>
#include <math.h>
#include <vector>

#if ENABLE_INMP441_MICS
#include "mics.h"
#endif

#if ENABLE_BLE_SCAN
#include "ble_sensor.h"
#endif

#if ENABLE_BEEHIVE_GATT
#include "beehive_gatt.h"
#endif

void initializeTime(bool wokeFromDeepSleep) {
  if (rtcHasValidTime()) {
    timeSource = "rtc";
    Serial.print("[TIME] Using RTC: ");
    Serial.println(timestampNow());

    // Refresh RTC from NTP on cold/manual boots, but avoid doing this on every
    // timer wake because WiFi will already be used for upload and RTC is valid.
    if (!wokeFromDeepSleep) {
      syncTime();
    }
    return;
  }

  syncTime();
}

String timestampNow() {
  if (rtcOk) {
    DateTime now = rtc.now();
    if (now.year() >= 2024 && now.year() <= 2099) {
      char buf[25];
      snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", now.year(), now.month(), now.day(), now.hour(), now.minute(), now.second());
      return String(buf);
    }
  }

  struct tm tmNow;
  if (getLocalTime(&tmNow, 100)) {
    char buf[25];
    snprintf(buf, sizeof(buf), "%04d-%02d-%02dT%02d:%02d:%02dZ", tmNow.tm_year + 1900, tmNow.tm_mon + 1, tmNow.tm_mday, tmNow.tm_hour, tmNow.tm_min, tmNow.tm_sec);
    return String(buf);
  }

  return String("1970-01-01T00:00:00Z");
}

void syncTime() {
  if (!connectWifi()) {
    Serial.println("[TIME] Cannot sync time: WiFi unavailable");
    return;
  }

  Serial.println("[TIME] Syncing with NTP...");
  configTime(0, 0, "pool.ntp.org", "time.nist.gov", "time.google.com");

  struct tm tmNow;
  for (int i = 0; i < 20; i++) {
    if (getLocalTime(&tmNow, 500)) {
      time_t nowUnix = mktime(&tmNow);

      if (nowUnix > 1700000000) {
        Serial.println("[TIME] NTP sync OK");
        timeSource = "ntp";

        if (rtcOk) {
          struct tm* utc = gmtime(&nowUnix);
          rtc.adjust(DateTime(utc->tm_year + 1900, utc->tm_mon + 1, utc->tm_mday, utc->tm_hour, utc->tm_min, utc->tm_sec));
          Serial.println("[TIME] RTC updated from NTP");
        }

        Serial.print("[TIME] Current timestamp: ");
        Serial.println(timestampNow());
        return;
      }
    }
    delay(500);
  }

  Serial.println("[TIME] NTP sync FAILED");

  if (rtcOk) {
    DateTime now = rtc.now();
    if (now.year() >= 2024 && now.year() <= 2099) {
      timeSource = "rtc";
      Serial.println("[TIME] Using RTC");
      return;
    }
  }

  timeSource = "invalid";
}

long readAverageRaw(HX711& scale, int samples) {
  if (!scale.wait_ready_timeout(2000)) {
    Serial.println("[HX711] Not ready");
    return 0;
  }
  return scale.read_average(samples);
}

float weightFromRaw(long raw, long offset, float factor) {
  if (factor == 0.0f) return NAN;
  return ((float)(raw - offset)) / factor;
}

static const char* scaleBackendName(hivecfg::ScaleBackend b) {
  switch (b) {
    case hivecfg::ScaleBackend::HX711:   return "hx711";
    case hivecfg::ScaleBackend::NAU7802: return "nau7802";
    default:                             return "none";
  }
}

#if ENABLE_BLE_SCAN
// First present in-hive snapshot mapped to hive-array slot `hiveArrIdx` (else the
// first one paired to that hive even if absent, so callers can still report it).
static const blesensor::Snapshot* snapForHive(
    const std::vector<int>& hiveOfMac,
    const std::vector<blesensor::Snapshot>& snaps,
    int hiveArrIdx) {
  const blesensor::Snapshot* fallback = nullptr;
  for (size_t i = 0; i < snaps.size() && i < hiveOfMac.size(); i++) {
    if (hiveOfMac[i] != hiveArrIdx) continue;
    if (snaps[i].present) return &snaps[i];
    if (!fallback) fallback = &snaps[i];
  }
  return fallback;
}
#endif

String createMeasurementJson() {
  Serial.println("[MEASURE] Reading sensors...");

  powerUpScales();

  // ── In-hive BLE sensors (beacon + capped GATT) for ALL hives ───────────────
  // One passive scan window catches every paired beacon at once; connection-based
  // GATT reads are capped per cycle (MAX_GATT_READS_PER_CYCLE) so deep sleep stays
  // effective even with many hives. Done before the wired sensors so per-hive
  // arbitration (below) can skip a wired probe a BLE sensor already covers.
#if ENABLE_BLE_SCAN
  std::vector<String> bleMacs;
  std::vector<bool>   bleIsGatt;
  std::vector<int>    bleHiveOfMac;   // gHives[] index each MAC belongs to
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t b = 0; b < hive.bleCount; b++) {
      const hivecfg::BlePairing& p = hive.ble[b];
      // In-hive beacon/HiveInside sensors go through this bridge; HiveHeart/
      // HiveScale/HiveTraffic are handled by their own GATT modules below.
      if (p.type == "holyiot" || p.type == "ruuvitag" || p.type == "hiveinside") {
        bleMacs.push_back(p.mac);
        bleIsGatt.push_back(p.isGatt());
        bleHiveOfMac.push_back(h);
      }
    }
  }
  std::vector<blesensor::Snapshot> bleSnaps;
  blesensor::scanPairedSensorsMulti(bleMacs, bleIsGatt, bleSnaps, MAX_GATT_READS_PER_CYCLE);
#endif

  // ── beehivemonitoring.com GATT sensors (HiveHeart / HiveScale) — hives 1–2 ──
#if ENABLE_BEEHIVE_GATT
  bhgatt::CycleResult bh;
  bhgatt::runCycle(bh);
#endif

  // ── Wired DS18B20: a single bus conversion, then per-hive ROM reads below ───
#if ENABLE_DS18B20_HIVE_TEMP
  ds18b20.requestTemperatures();
#endif

  // ── Ambient SHT4x (device-level, outside-hive) ─────────────────────────────
  float ambientTemp = NAN;
  float ambientHumidity = NAN;
  if (shtOk) {
    sensors_event_t humidity, temp;
    if (sht4.getEvent(&humidity, &temp)) {
      ambientTemp = temp.temperature;
      ambientHumidity = humidity.relative_humidity;
    } else {
      Serial.println("[SHT4x] Read failed");
    }
  }

#if ENABLE_INA219_SOLAR
  float solarBusVoltage = NAN, solarShuntVoltageMv = NAN, solarLoadVoltage = NAN;
  float solarCurrentMa = NAN, solarPowerMw = NAN;
  if (solarMonitorOk) {
    solarMonitor.powerSave(false);
    delay(10);
    solarBusVoltage = solarMonitor.getBusVoltage_V();
    solarShuntVoltageMv = solarMonitor.getShuntVoltage_mV();
    solarCurrentMa = solarMonitor.getCurrent_mA();
    solarPowerMw = solarMonitor.getPower_mW();
    solarLoadVoltage = solarBusVoltage + (solarShuntVoltageMv / 1000.0f);
    solarMonitor.powerSave(true);
  }
#endif

#if ENABLE_MAX17048_BATTERY
  float batteryVoltage = NAN, batterySoc = NAN;
  bool batteryAlert = false;
  if (batteryMonitorOk) {
    batteryVoltage = batteryGauge.getVoltage();
    batterySoc = batteryGauge.getSOC();
    batteryAlert = batteryGauge.getAlert();
    if (batteryAlert) batteryGauge.clearAlert();
  }
#endif

  // ── Wired stereo mics (device-level; left=hive 1, right=hive 2) ────────────
#if ENABLE_INMP441_MICS
  bool wiredMicUsed = true;
#if ENABLE_BLE_SCAN
  if (BLE_OVERRIDE_MICS) {
    const blesensor::Snapshot* s1 = snapForHive(bleHiveOfMac, bleSnaps, 0);
    const blesensor::Snapshot* s2 = snapForHive(bleHiveOfMac, bleSnaps, 1);
    if ((s1 && s1->providesMic()) || (s2 && s2->providesMic())) {
      wiredMicUsed = false;
      Serial.println("[ARB] BLE supplies in-hive acoustics; skipping wired INMP441 mics");
    }
  }
#endif
  MicMeasurement micResult;
  if (wiredMicUsed) micResult = readMicSamples();
#endif

  // ── BeeCounter (entrance gates) — hives 1–2, wired I2C or HiveTraffic BLE ──
  beecnt::Snapshot beeSnap1, beeSnap2;
#if ENABLE_WIRELESS_BEECOUNTER
  const bool trafficSlot1 = trafficMac0.length() > 0;
  const bool trafficSlot2 = trafficMac1.length() > 0;
  if (trafficSlot1 || trafficSlot2)
    beecnt::bleRunCycle(trafficMac0, trafficMac1, beeSnap1, beeSnap2);
#else
  const bool trafficSlot1 = false;
  const bool trafficSlot2 = false;
#endif
  if (!trafficSlot1) (void)beecnt::pollSlot(beecnt::SLAVE_ADDR_SLOT_1, beeSnap1);
  if (!trafficSlot2) (void)beecnt::pollSlot(beecnt::SLAVE_ADDR_SLOT_2, beeSnap2);

  // ── Assemble the upload document ───────────────────────────────────────────
  JsonDocument doc;
  doc["device_id"] = deviceId;
  if (claimCode.length() > 0 && !claimRegistered) doc["claim_code"] = claimCode;
  if (timeSource != "invalid") doc["timestamp"] = timestampNow();
  doc["ambient_temp_c"] = ambientTemp;
  doc["ambient_humidity_percent"] = ambientHumidity;
  doc["network_transport"] = "wifi";
  doc["rssi_dbm"] = WiFi.status() == WL_CONNECTED ? WiFi.RSSI() : 0;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["calibration_mode"] = calibrationModeActive;
  doc["boot_count"] = rtcBootCount;
  doc["time_source"] = timeSource;
  doc["hive_count"] = hivecfg::gHiveCount;
  doc["sd_ok"] = sdOk;
  doc["rtc_ok"] = rtcOk;
  doc["sht_ok"] = shtOk;
#if ENABLE_INA219_SOLAR
  doc["solar_monitor_ok"] = solarMonitorOk;
  doc["solar_bus_voltage_v"] = solarBusVoltage;
  doc["solar_shunt_voltage_mv"] = solarShuntVoltageMv;
  doc["solar_load_voltage_v"] = solarLoadVoltage;
  doc["solar_current_ma"] = solarCurrentMa;
  doc["solar_power_mw"] = solarPowerMw;
#endif
#if ENABLE_MAX17048_BATTERY
  doc["battery_monitor_ok"] = batteryMonitorOk;
  doc["battery_voltage_v"] = batteryVoltage;
  doc["battery_soc_percent"] = batterySoc;
  doc["battery_alert"] = batteryAlert;
#endif
#if ENABLE_INMP441_MICS
  if (wiredMicUsed) {
    doc["mic_ok"]                   = micResult.ok;
    doc["mic_sample_rate_hz"]       = (uint32_t)INMP441_SAMPLE_RATE;
    doc["mic_sample_frames"]        = (uint32_t)INMP441_SAMPLE_FRAMES;
    doc["mic_left_ok"]              = micResult.left.ok;
    doc["mic_left_rms_dbfs"]        = micResult.left.rmsDbfs;
    doc["mic_left_peak_dbfs"]       = micResult.left.peakDbfs;
    doc["mic_left_rms_normalized"]  = micResult.left.rmsNormalized;
    doc["mic_right_ok"]             = micResult.right.ok;
    doc["mic_right_rms_dbfs"]       = micResult.right.rmsDbfs;
    doc["mic_right_peak_dbfs"]      = micResult.right.peakDbfs;
    doc["mic_right_rms_normalized"] = micResult.right.rmsNormalized;
    doc["mic_left_band_sub_bass_dbfs"]  = micResult.left.bands.sub_bass_dbfs;
    doc["mic_left_band_hum_dbfs"]       = micResult.left.bands.hum_dbfs;
    doc["mic_left_band_piping_dbfs"]    = micResult.left.bands.piping_dbfs;
    doc["mic_left_band_stress_dbfs"]    = micResult.left.bands.stress_dbfs;
    doc["mic_left_band_high_dbfs"]      = micResult.left.bands.high_dbfs;
    doc["mic_right_band_sub_bass_dbfs"] = micResult.right.bands.sub_bass_dbfs;
    doc["mic_right_band_hum_dbfs"]      = micResult.right.bands.hum_dbfs;
    doc["mic_right_band_piping_dbfs"]   = micResult.right.bands.piping_dbfs;
    doc["mic_right_band_stress_dbfs"]   = micResult.right.bands.stress_dbfs;
    doc["mic_right_band_high_dbfs"]     = micResult.right.bands.high_dbfs;
  }
#endif

  // ── Per-hive array — the heart of the multi-hive upload ────────────────────
  JsonArray hivesArr = doc["hives"].to<JsonArray>();
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    JsonObject ho = hivesArr.add<JsonObject>();
    ho["index"] = hive.index;
    if (hive.name.length()) ho["name"] = hive.name;

#if ENABLE_BLE_SCAN
    const blesensor::Snapshot* sn = snapForHive(bleHiveOfMac, bleSnaps, h);
#endif

    // Scales: sum every channel mapped to this hive (usually one).
    double weightSum = 0.0;
    bool   anyScale = false;
    long   firstRaw = 0;
    const char* scaleSrc = "";
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const hivecfg::ScaleChannel& ch = hive.scales[s];
      if (!ch.valid()) continue;
      long raw = scalebus::readRaw(ch);
      float kg = weightFromRaw(raw, ch.offset, ch.factor);
      if (s == 0) { firstRaw = raw; scaleSrc = scaleBackendName(ch.backend); }
      if (!isnan(kg)) { weightSum += kg; anyScale = true; }
      Serial.printf("[MEASURE] hive %u scale %u (%s) raw=%ld kg=%.3f\n",
                    hive.index, s, scaleBackendName(ch.backend), raw, kg);
    }
    if (anyScale) {
      ho["weight_kg"]    = weightSum;
      ho["raw_weight"]   = firstRaw;
      ho["scale_source"] = scaleSrc;
    }

    // Temperature arbitration: BLE overrides the wired DS18B20 when configured;
    // otherwise the wired probe wins and BLE/HiveHeart are the fallback.
    float t = NAN;
    const char* tsrc = nullptr;
#if ENABLE_BLE_SCAN
    bool bleHasTemp = sn && sn->present && !isnan(sn->temp_c);
#else
    bool bleHasTemp = false;
#endif

#if (ENABLE_BLE_SCAN && BLE_OVERRIDE_DS18B20)
    if (bleHasTemp) { t = sn->temp_c; tsrc = "ble"; }
#endif
#if ENABLE_DS18B20_HIVE_TEMP
    if (isnan(t)) {
      float wired = NAN;
      if (hive.hasDsRom) {
        wired = ds18b20.getTempC((const uint8_t*)hive.dsRom);
      } else if (hive.index >= 1 && hive.index <= 2) {
        // Legacy/migrated hives without a mapped ROM fall back to probe order.
        wired = ds18b20.getTempCByIndex(hive.index - 1);
      }
      if (!isnan(wired) && wired > DEVICE_DISCONNECTED_C) { t = wired; tsrc = "ds18b20"; }
    }
#endif
#if ENABLE_BLE_SCAN
    if (isnan(t) && bleHasTemp) { t = sn->temp_c; tsrc = "ble"; }
#endif
#if ENABLE_BEEHIVE_GATT
    if (isnan(t) && hive.index >= 1 && hive.index <= 2 && bh.heart[hive.index - 1].present) {
      t = bh.heart[hive.index - 1].temp_c;
      tsrc = "hiveheart";
    }
#endif
    if (!isnan(t)) { ho["temp_c"] = t; if (tsrc) ho["temp_source"] = tsrc; }

    // In-hive humidity (BLE sensor, or HiveHeart for hives 1–2).
#if ENABLE_BLE_SCAN
    if (sn && sn->present && !isnan(sn->humidity_pct)) ho["humidity_percent"] = sn->humidity_pct;
#endif
#if ENABLE_BEEHIVE_GATT
    if (!ho["humidity_percent"].is<float>() && hive.index >= 1 && hive.index <= 2 &&
        bh.heart[hive.index - 1].present)
      ho["humidity_percent"] = bh.heart[hive.index - 1].humidity_pct;
#endif

    // Nested wireless/vibration/acoustic data.
#if ENABLE_BLE_SCAN
    if (sn) blesensor::writeSnapshotToHive(ho, *sn);
#endif
    // BeeCounter entrance gates (hives 1–2).
    if (hive.index == 1) beecnt::writeSnapshotToHive(ho, beeSnap1);
    else if (hive.index == 2) beecnt::writeSnapshotToHive(ho, beeSnap2);
  }

  String output;
  serializeJson(doc, output);
  rememberLastMeasurement(output);

  Serial.print("[MEASURE] JSON: ");
  Serial.println(output);
  return output;
}
