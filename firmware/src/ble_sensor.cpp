// ble_sensor.cpp — HolyIot 25015 passive BLE bridge (NimBLE scanner + parser).
#include "ble_sensor.h"

#if ENABLE_HOLYIOT_BLE

#include <NimBLEDevice.h>
#include <math.h>

namespace blesensor {

// ───────────────────────────────────────────────────────────────────────────
// Advertisement layout — DOCUMENTED BEST GUESS, edit after a real packet capture
// ───────────────────────────────────────────────────────────────────────────
// HolyIot do not publish the 25015 advertisement format. The manufacturer-
// specific AD structure (AD type 0xFF) is assumed to be:
//
//   off 0..1  : company id (little-endian)          == HOLYIOT_COMPANY_ID
//   off 2     : frame / payload type                  (ignored)
//   off 3     : battery percent (uint8, 0..100)
//   off 4..5  : temperature   int16 LE, units 0.01 °C
//   off 6..7  : humidity       uint16 LE, units 0.01 %RH
//   off 8..9  : pressure       uint16 LE, units 0.1 hPa  (260–1260 hPa)
//   off 10..11: accel X        int16 LE, milli-g
//   off 12..13: accel Y        int16 LE, milli-g
//   off 14..15: accel Z        int16 LE, milli-g
//
// To correct the layout after sniffing one packet (e.g. nRF Connect), change
// only the constants below — nothing else in the codebase depends on them.
static constexpr size_t HOLYIOT_MIN_LEN     = 16;
static constexpr size_t HOLYIOT_OFF_COMPANY = 0;
static constexpr size_t HOLYIOT_OFF_BATTERY = 3;
static constexpr size_t HOLYIOT_OFF_TEMP    = 4;   // int16 LE, /100 -> °C
static constexpr size_t HOLYIOT_OFF_HUMID   = 6;   // uint16 LE, /100 -> %RH
static constexpr size_t HOLYIOT_OFF_PRESS   = 8;   // uint16 LE, /10  -> hPa
static constexpr size_t HOLYIOT_OFF_ACCEL_X = 10;  // int16 LE, mg
static constexpr size_t HOLYIOT_OFF_ACCEL_Y = 12;
static constexpr size_t HOLYIOT_OFF_ACCEL_Z = 14;

static constexpr float TEMP_SCALE  = 0.01f;
static constexpr float HUMID_SCALE = 0.01f;
static constexpr float PRESS_SCALE = 0.1f;
static constexpr float GRAVITY_MG  = 1000.0f;  // ~1 g at rest, removed for AC

// ───────────────────────────────────────────────────────────────────────────
// HiveInside ESP32-C6 manufacturer-data layout (scan-response blob)
// ───────────────────────────────────────────────────────────────────────────
// Distinct from HolyIot by company id (HIVEINSIDE_COMPANY_ID, default Espressif
// 0x02E5) plus a magic byte, so the dispatcher can tell the two apart even
// though both ride the same passive-scan bridge. The HiveInside device runs the
// vibration and acoustic FFTs on board, so it broadcasts finished RMS + band
// values (no raw axes). Layout is documented in
// HiveInside/docs/esp32c6-prototype.md and mirrored here:
//
//   off 0..1  : company id (LE)        == HIVEINSIDE_COMPANY_ID
//   off 2     : magic                   == 0x48 ('H')
//   off 3     : version                 (currently 0x01)
//   off 4     : flags  bit0 sht bit1 accel bit2 mic bit3 batt
//   off 5..6  : temperature  int16 LE, 0.01 °C
//   off 7..8  : humidity      uint16 LE, 0.01 %RH
//   off 9..10 : battery       uint16 LE, milli-volt
//   off 11    : battery percent (uint8)
//   off 12..13: accel RMS      uint16 LE, 0.1 mg
//   off 14..15: accel band swarm    uint16 LE, 0.1 mg   (8–30 Hz)
//   off 16..17: accel band fanning  uint16 LE, 0.1 mg   (30–100 Hz)
//   off 18..19: accel band activity uint16 LE, 0.1 mg   (100–200 Hz)
//   off 20    : mic RMS        int8, dBFS
//   off 21    : mic sub-bass   int8, dBFS   (50–150 Hz)
//   off 22    : mic hum        int8, dBFS   (150–300 Hz)
//   off 23    : mic piping     int8, dBFS   (300–550 Hz)
//   off 24    : mic stress     int8, dBFS   (550–1500 Hz)
//   off 25    : mic high       int8, dBFS   (1500–3000 Hz)
static constexpr size_t HI_MIN_LEN      = 26;
static constexpr uint8_t HI_MAGIC       = 0x48;  // 'H'
static constexpr size_t HI_OFF_MAGIC    = 2;
static constexpr size_t HI_OFF_VERSION  = 3;
static constexpr size_t HI_OFF_FLAGS    = 4;
static constexpr size_t HI_OFF_TEMP     = 5;
static constexpr size_t HI_OFF_HUMID    = 7;
static constexpr size_t HI_OFF_BATT_MV  = 9;
static constexpr size_t HI_OFF_BATT_PCT = 11;
static constexpr size_t HI_OFF_ACC_RMS  = 12;
static constexpr size_t HI_OFF_ACC_SWARM    = 14;
static constexpr size_t HI_OFF_ACC_FANNING  = 16;
static constexpr size_t HI_OFF_ACC_ACTIVITY = 18;
static constexpr size_t HI_OFF_MIC_RMS    = 20;
static constexpr size_t HI_OFF_MIC_SUB    = 21;
static constexpr size_t HI_OFF_MIC_HUM    = 22;
static constexpr size_t HI_OFF_MIC_PIPING = 23;
static constexpr size_t HI_OFF_MIC_STRESS = 24;
static constexpr size_t HI_OFF_MIC_HIGH   = 25;
static constexpr float HI_ACCEL_SCALE = 0.1f;   // 0.1 mg per LSB

// ── little-endian field readers ────────────────────────────────────────────
static int16_t  rd_i16(const uint8_t* p, size_t off) {
  return (int16_t)((uint16_t)p[off] | ((uint16_t)p[off + 1] << 8));
}
static uint16_t rd_u16(const uint8_t* p, size_t off) {
  return (uint16_t)((uint16_t)p[off] | ((uint16_t)p[off + 1] << 8));
}

// Parsed scalar fields from one advertisement payload.
struct Parsed {
  bool       ok = false;
  SensorType type = SensorType::None;
  float temp_c = NAN, humidity_pct = NAN, pressure_hpa = NAN;
  float ax = NAN, ay = NAN, az = NAN;        // HolyIot raw axes
  int   battery_pct = -1;
  // HiveInside on-board FFT results.
  float accel_rms_mg = NAN;
  float accel_band_swarm_mg = NAN, accel_band_fanning_mg = NAN, accel_band_activity_mg = NAN;
  bool  mic_present = false;
  float mic_rms_dbfs = NAN;
  float mic_sub_bass_dbfs = NAN, mic_hum_dbfs = NAN, mic_piping_dbfs = NAN,
        mic_stress_dbfs = NAN, mic_high_dbfs = NAN;
};

// HolyIot 25015: temp/humidity/pressure + raw 3-axis acceleration.
static Parsed parseHolyIot(const uint8_t* d, size_t len) {
  Parsed out;
  if (d == nullptr || len < HOLYIOT_MIN_LEN) return out;
  if (rd_u16(d, HOLYIOT_OFF_COMPANY) != (uint16_t)HOLYIOT_COMPANY_ID) return out;

  out.type         = SensorType::HolyIot;
  out.battery_pct  = d[HOLYIOT_OFF_BATTERY];
  out.temp_c       = rd_i16(d, HOLYIOT_OFF_TEMP)  * TEMP_SCALE;
  out.humidity_pct = rd_u16(d, HOLYIOT_OFF_HUMID) * HUMID_SCALE;
  out.pressure_hpa = rd_u16(d, HOLYIOT_OFF_PRESS) * PRESS_SCALE;
  out.ax           = rd_i16(d, HOLYIOT_OFF_ACCEL_X);
  out.ay           = rd_i16(d, HOLYIOT_OFF_ACCEL_Y);
  out.az           = rd_i16(d, HOLYIOT_OFF_ACCEL_Z);
  out.ok = true;
  return out;
}

// HiveInside ESP32-C6: on-board vibration + acoustic FFT, no raw axes.
static Parsed parseHiveInside(const uint8_t* d, size_t len) {
  Parsed out;
  if (d == nullptr || len < HI_MIN_LEN) return out;
  if (rd_u16(d, HOLYIOT_OFF_COMPANY) != (uint16_t)HIVEINSIDE_COMPANY_ID) return out;
  if (d[HI_OFF_MAGIC] != HI_MAGIC) return out;

  out.type         = SensorType::HiveInside;
  out.temp_c       = rd_i16(d, HI_OFF_TEMP)  * TEMP_SCALE;
  out.humidity_pct = rd_u16(d, HI_OFF_HUMID) * HUMID_SCALE;
  out.battery_pct  = d[HI_OFF_BATT_PCT];
  out.accel_rms_mg          = rd_u16(d, HI_OFF_ACC_RMS)      * HI_ACCEL_SCALE;
  out.accel_band_swarm_mg   = rd_u16(d, HI_OFF_ACC_SWARM)    * HI_ACCEL_SCALE;
  out.accel_band_fanning_mg = rd_u16(d, HI_OFF_ACC_FANNING)  * HI_ACCEL_SCALE;
  out.accel_band_activity_mg= rd_u16(d, HI_OFF_ACC_ACTIVITY) * HI_ACCEL_SCALE;
  out.mic_present   = true;
  out.mic_rms_dbfs       = (int8_t)d[HI_OFF_MIC_RMS];
  out.mic_sub_bass_dbfs  = (int8_t)d[HI_OFF_MIC_SUB];
  out.mic_hum_dbfs       = (int8_t)d[HI_OFF_MIC_HUM];
  out.mic_piping_dbfs    = (int8_t)d[HI_OFF_MIC_PIPING];
  out.mic_stress_dbfs    = (int8_t)d[HI_OFF_MIC_STRESS];
  out.mic_high_dbfs      = (int8_t)d[HI_OFF_MIC_HIGH];
  out.ok = true;
  return out;
}

// Dispatcher: try each known in-hive sensor format. Foreign beacons (no company
// match) return ok=false and are ignored.
static Parsed parsePayload(const uint8_t* d, size_t len) {
  Parsed p = parseHolyIot(d, len);
  if (p.ok) return p;
  return parseHiveInside(d, len);
}

String normalizeMac(const String& raw) {
  String s = raw;
  s.trim();
  s.replace("-", ":");
  s.toUpperCase();
  // Accept either colon-separated or bare 12-hex-digit forms.
  String hex;
  for (size_t i = 0; i < s.length(); i++) {
    char c = s[i];
    if ((c >= '0' && c <= '9') || (c >= 'A' && c <= 'F')) hex += c;
  }
  if (hex.length() != 12) return "";
  String out;
  for (int i = 0; i < 12; i += 2) {
    if (i) out += ":";
    out += hex.substring(i, i + 2);
  }
  return out;
}

// ── per-slot accumulator shared with the scan callback ─────────────────────
struct Accumulator {
  String  mac;                 // normalized target MAC ("" = slot unused)
  bool    present = false;
  SensorType type = SensorType::None;
  int     rssi_dbm = 0;
  int     battery_pct = -1;
  float   temp_c = NAN, humidity_pct = NAN, pressure_hpa = NAN;
  float   ax = NAN, ay = NAN, az = NAN;
  std::vector<float> magnitudes;  // |a| per advertisement (HolyIot AC RMS/peak)
  // HiveInside: latest on-board FFT results (the device already reduced these).
  float   accel_rms_mg = NAN;
  float   accel_band_swarm_mg = NAN, accel_band_fanning_mg = NAN, accel_band_activity_mg = NAN;
  bool    mic_present = false;
  float   mic_rms_dbfs = NAN;
  float   mic_sub_bass_dbfs = NAN, mic_hum_dbfs = NAN, mic_piping_dbfs = NAN,
          mic_stress_dbfs = NAN, mic_high_dbfs = NAN;
};

namespace {
Accumulator g_slot[2];
std::vector<Discovered>* g_discover = nullptr;  // non-null during discover()

class ScanCallbacks : public NimBLEAdvertisedDeviceCallbacks {
  void onResult(NimBLEAdvertisedDevice* dev) override {
    String mac = String(dev->getAddress().toString().c_str());
    mac = normalizeMac(mac);

    std::string md = dev->haveManufacturerData() ? dev->getManufacturerData() : std::string();
    Parsed p = parsePayload(reinterpret_cast<const uint8_t*>(md.data()), md.size());

    if (g_discover != nullptr) {
      String name = dev->haveName() ? String(dev->getName().c_str()) : String("");
      for (auto& d : *g_discover) {
        if (d.mac == mac) {
          d.rssi_dbm = dev->getRSSI();
          if (p.ok) { d.type = p.type; d.looks_like_holyiot = true; }
          return;
        }
      }
      Discovered d;
      d.mac = mac;
      d.name = name;
      d.rssi_dbm = dev->getRSSI();
      d.type = p.type;
      d.looks_like_holyiot = p.ok;
      g_discover->push_back(d);
      return;
    }

    if (!p.ok) return;
    for (int s = 0; s < 2; s++) {
      if (g_slot[s].mac.length() == 0 || g_slot[s].mac != mac) continue;
      Accumulator& a = g_slot[s];
      a.present = true;
      a.type = p.type;
      a.rssi_dbm = dev->getRSSI();
      a.battery_pct = p.battery_pct;
      a.temp_c = p.temp_c;
      a.humidity_pct = p.humidity_pct;
      a.pressure_hpa = p.pressure_hpa;
      if (p.type == SensorType::HiveInside) {
        // Device already ran the FFT — copy its finished values, no magnitudes.
        a.accel_rms_mg          = p.accel_rms_mg;
        a.accel_band_swarm_mg   = p.accel_band_swarm_mg;
        a.accel_band_fanning_mg = p.accel_band_fanning_mg;
        a.accel_band_activity_mg= p.accel_band_activity_mg;
        a.mic_present     = p.mic_present;
        a.mic_rms_dbfs    = p.mic_rms_dbfs;
        a.mic_sub_bass_dbfs = p.mic_sub_bass_dbfs;
        a.mic_hum_dbfs    = p.mic_hum_dbfs;
        a.mic_piping_dbfs = p.mic_piping_dbfs;
        a.mic_stress_dbfs = p.mic_stress_dbfs;
        a.mic_high_dbfs   = p.mic_high_dbfs;
      } else {
        a.ax = p.ax; a.ay = p.ay; a.az = p.az;
        a.magnitudes.push_back(sqrtf(p.ax * p.ax + p.ay * p.ay + p.az * p.az));
      }
    }
  }
};
}  // namespace

// Reduce the accumulated |a| samples to a per-cycle AC RMS/peak (gravity removed).
static void finalizeAccel(const Accumulator& a, Snapshot& s) {
  s.sample_count = (uint16_t)a.magnitudes.size();
  if (a.magnitudes.empty()) return;
  // Baseline = mean |a| when we have several samples; otherwise assume ~1 g so a
  // single still-hive sample reads near zero rather than ~1000 mg.
  double baseline = GRAVITY_MG;
  if (a.magnitudes.size() >= 3) {
    double sum = 0;
    for (float m : a.magnitudes) sum += m;
    baseline = sum / a.magnitudes.size();
  }
  double sumSq = 0;
  float  peak = 0;
  for (float m : a.magnitudes) {
    double dev = (double)m - baseline;
    sumSq += dev * dev;
    if (fabs(dev) > peak) peak = (float)fabs(dev);
  }
  s.accel_rms_mg  = (float)sqrt(sumSq / a.magnitudes.size());
  s.accel_peak_mg = peak;
}

static void copyToSnapshot(const Accumulator& a, Snapshot& s) {
  s.present      = a.present;
  s.type         = a.type;
  s.rssi_dbm     = a.rssi_dbm;
  s.battery_pct  = a.battery_pct;
  s.temp_c       = a.temp_c;
  s.humidity_pct = a.humidity_pct;
  s.pressure_hpa = a.pressure_hpa;

  if (a.type == SensorType::HiveInside) {
    // The HiveInside device already computed RMS + bands on board.
    s.sample_count = a.present ? 1 : 0;
    s.accel_rms_mg          = a.accel_rms_mg;
    s.accel_band_swarm_mg   = a.accel_band_swarm_mg;
    s.accel_band_fanning_mg = a.accel_band_fanning_mg;
    s.accel_band_activity_mg= a.accel_band_activity_mg;
    s.mic_present     = a.mic_present;
    s.mic_rms_dbfs    = a.mic_rms_dbfs;
    s.mic_sub_bass_dbfs = a.mic_sub_bass_dbfs;
    s.mic_hum_dbfs    = a.mic_hum_dbfs;
    s.mic_piping_dbfs = a.mic_piping_dbfs;
    s.mic_stress_dbfs = a.mic_stress_dbfs;
    s.mic_high_dbfs   = a.mic_high_dbfs;
    return;
  }

  // HolyIot: raw axes + magnitude-derived AC RMS/peak across the scan window.
  s.accel_x_mg   = a.ax;
  s.accel_y_mg   = a.ay;
  s.accel_z_mg   = a.az;
  finalizeAccel(a, s);
}

const char* sensorTypeName(SensorType t) {
  switch (t) {
    case SensorType::HolyIot:    return "HolyIot 25015";
    case SensorType::HiveInside: return "HiveInside C6";
    default:                     return "";
  }
}

static NimBLEScan* startScan(ScanCallbacks& cb, uint32_t seconds) {
  NimBLEDevice::init("");
  NimBLEScan* scan = NimBLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(&cb, /*wantDuplicates=*/true);
  scan->setActiveScan(HOLYIOT_BLE_ACTIVE_SCAN ? true : false);
  scan->setDuplicateFilter(false);  // we want every advertisement, for AC RMS
  scan->setInterval(100);
  scan->setWindow(99);
  scan->start(seconds, false);
  return scan;
}

void scanPairedSensors(const String& mac0, const String& mac1,
                       Snapshot& slot1, Snapshot& slot2) {
  slot1 = Snapshot{};
  slot2 = Snapshot{};

  String m0 = normalizeMac(mac0);
  String m1 = normalizeMac(mac1);
  if (m0.length() == 0 && m1.length() == 0) {
    Serial.println("[BLE] No HolyIot sensors paired; skipping scan");
    return;
  }

  g_slot[0] = Accumulator{}; g_slot[0].mac = m0;
  g_slot[1] = Accumulator{}; g_slot[1].mac = m1;
  g_discover = nullptr;

  Serial.printf("[BLE] Scanning %us for paired sensors (slot1=%s slot2=%s)\n",
                (unsigned)HOLYIOT_BLE_SCAN_SECONDS,
                m0.length() ? m0.c_str() : "-",
                m1.length() ? m1.c_str() : "-");

  ScanCallbacks cb;
  NimBLEScan* scan = startScan(cb, HOLYIOT_BLE_SCAN_SECONDS);
  scan->clearResults();
  NimBLEDevice::deinit(true);  // free the controller before the WiFi upload

  copyToSnapshot(g_slot[0], slot1);
  copyToSnapshot(g_slot[1], slot2);

  Serial.printf("[BLE] slot1 present=%d (%u adv) | slot2 present=%d (%u adv)\n",
                slot1.present, slot1.sample_count, slot2.present, slot2.sample_count);
}

std::vector<Discovered> discover(uint32_t seconds) {
  std::vector<Discovered> found;
  g_slot[0] = Accumulator{};
  g_slot[1] = Accumulator{};
  g_discover = &found;

  ScanCallbacks cb;
  NimBLEScan* scan = startScan(cb, seconds);
  scan->clearResults();
  NimBLEDevice::deinit(true);

  g_discover = nullptr;
  return found;
}

void writeSnapshotToJson(JsonDocument& doc, uint8_t slot, const Snapshot& snap) {
  // Index keys with a temporary String so ArduinoJson copies them (same pattern
  // as accel/beecnt::writeSnapshotToJson).
  String bp = "ble_" + String((int)slot) + "_";
  String ap = "accel_" + String((int)slot) + "_";

  // Acceleration is mirrored into the existing accel_{slot}_* fields so the
  // server's vibration insight and storage reuse the accelerometer schema.
  doc[ap + "ok"] = snap.present;

  if (!snap.present) return;

  // ── new ble_{slot}_* fields (type / humidity / pressure / raw accel / link) ─
  doc[bp + "sensor_type"] = sensorTypeName(snap.type);
  if (!isnan(snap.humidity_pct)) doc[bp + "humidity_percent"] = snap.humidity_pct;
  if (!isnan(snap.pressure_hpa)) doc[bp + "pressure_hpa"]     = snap.pressure_hpa;
  if (!isnan(snap.accel_x_mg))   doc[bp + "accel_x_mg"]       = snap.accel_x_mg;
  if (!isnan(snap.accel_y_mg))   doc[bp + "accel_y_mg"]       = snap.accel_y_mg;
  if (!isnan(snap.accel_z_mg))   doc[bp + "accel_z_mg"]       = snap.accel_z_mg;
  if (snap.battery_pct >= 0)     doc[bp + "battery_percent"]  = snap.battery_pct;
  doc[bp + "rssi_dbm"] = snap.rssi_dbm;

  // ── reused accel_{slot}_* fields (per-cycle AC magnitude + FFT bands) ───────
  if (snap.sample_count > 0) {
    doc[ap + "sample_count"]   = snap.sample_count;
    doc[ap + "sample_rate_hz"] = 0;            // beacon: no fixed sample rate
    doc[ap + "range_g"]        = 2;            // LIS2DH12 / LIS3DH default ±2 g
    if (!isnan(snap.accel_rms_mg))  doc[ap + "rms_mg"]  = snap.accel_rms_mg;
    if (!isnan(snap.accel_peak_mg)) doc[ap + "peak_mg"] = snap.accel_peak_mg;
    // HiveInside also reports the three on-board vibration FFT bands; they slot
    // straight into the wired-accelerometer band schema the server already has.
    if (!isnan(snap.accel_band_swarm_mg))    doc[ap + "band_swarm_mg"]    = snap.accel_band_swarm_mg;
    if (!isnan(snap.accel_band_fanning_mg))  doc[ap + "band_fanning_mg"]  = snap.accel_band_fanning_mg;
    if (!isnan(snap.accel_band_activity_mg)) doc[ap + "band_activity_mg"] = snap.accel_band_activity_mg;
  }

  // ── acoustics (HiveInside) mapped onto the wired-mic schema ────────────────
  // The stereo INMP441 build keys acoustics as mic_left_* (hive 1) and
  // mic_right_* (hive 2); a per-slot BLE sensor maps the same way so its bands
  // reuse the existing columns and insight detectors. slot 1 -> left, 2 -> right.
  if (snap.mic_present) {
    String mp = (slot == 1) ? "mic_left_" : "mic_right_";
    doc["mic_ok"] = true;
    doc[mp + "ok"] = true;
    if (!isnan(snap.mic_rms_dbfs))     doc[mp + "rms_dbfs"]           = snap.mic_rms_dbfs;
    if (!isnan(snap.mic_sub_bass_dbfs)) doc[mp + "band_sub_bass_dbfs"] = snap.mic_sub_bass_dbfs;
    if (!isnan(snap.mic_hum_dbfs))     doc[mp + "band_hum_dbfs"]      = snap.mic_hum_dbfs;
    if (!isnan(snap.mic_piping_dbfs))  doc[mp + "band_piping_dbfs"]   = snap.mic_piping_dbfs;
    if (!isnan(snap.mic_stress_dbfs))  doc[mp + "band_stress_dbfs"]   = snap.mic_stress_dbfs;
    if (!isnan(snap.mic_high_dbfs))    doc[mp + "band_high_dbfs"]     = snap.mic_high_dbfs;
  }
}

}  // namespace blesensor

#endif  // ENABLE_HOLYIOT_BLE
