// ble_sensor.cpp — HolyIot 25015 passive BLE bridge (NimBLE scanner + parser).
#include "ble_sensor.h"

#if ENABLE_BLE_SCAN

#include <NimBLEDevice.h>
#include <math.h>

#include "ruuvi_decode.h"

namespace blesensor {

// ───────────────────────────────────────────────────────────────────────────
// HolyIot 25015 (HY-25015) advertisement format — CONFIRMED by real packet
// captures (nRF Connect + Chinese companion app, June 2026).
//
// The device broadcasts four simultaneous slots (each ~500 ms interval).
// All share company ID 0xFFFF in the manufacturer-specific AD (type 0xFF).
// Slot type is identified by the frame-type byte at d[2] (first byte after
// the 2-byte company ID in the manufacturer data payload):
//
//   d[0..1]  company id LE                    == HOLYIOT_COMPANY_ID (0xFFFF)
//   d[2]     frame type  0x0A=T&H  0x0B=Accel  0x0C=Baro  (0x02=iBeacon→skip)
//
//   T&H   (0x0A, 7+ B):  d[3..4] temp  int16  BE /10 → °C
//                         d[5..6] humid uint16 BE /10 → %RH
//   Accel (0x0B, 9+ B):  d[3..4] X int16 BE mg
//                         d[5..6] Y int16 BE mg
//                         d[7..8] Z int16 BE mg
//   Baro  (0x0C, 6+ B):  d[3..5] pressure uint24 BE Pa; ×0.01 → hPa
//
//   Battery: Service Data UUID 0x180A, 9 bytes:
//     [frame_type][MAC 6 B][TX power][battery %]  → last byte is battery %.
//
// To correct any field after a firmware update, edit only the constants below.
static constexpr size_t  HOLYIOT_MIN_LEN     = 6;    // shortest valid frame (Baro)
static constexpr size_t  HOLYIOT_OFF_COMPANY = 0;
static constexpr size_t  HOLYIOT_OFF_FRAME   = 2;    // frame-type byte offset
static constexpr uint8_t HOLYIOT_FRAME_TH    = 0x0A; // temperature + humidity slot
static constexpr uint8_t HOLYIOT_FRAME_ACCEL = 0x0B; // accelerometer slot
static constexpr uint8_t HOLYIOT_FRAME_BARO  = 0x0C; // barometer slot

static constexpr float TEMP_SCALE     = 0.1f;   // int16 /10  → °C
static constexpr float HUMID_SCALE    = 0.1f;   // uint16 /10 → %RH
static constexpr float PRESS_PA_SCALE = 0.01f;  // uint24 Pa × 0.01 → hPa
static constexpr float GRAVITY_MG     = 1000.0f; // ~1 g at rest, removed for AC

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

// ── field readers — little-endian (company ID) and big-endian (sensor data) ─
static int16_t  rd_i16(const uint8_t* p, size_t off) {
  return (int16_t)((uint16_t)p[off] | ((uint16_t)p[off + 1] << 8));
}
static uint16_t rd_u16(const uint8_t* p, size_t off) {
  return (uint16_t)((uint16_t)p[off] | ((uint16_t)p[off + 1] << 8));
}
static int16_t  rd_i16_be(const uint8_t* p, size_t off) {
  return (int16_t)(((uint16_t)p[off] << 8) | (uint16_t)p[off + 1]);
}
static uint16_t rd_u16_be(const uint8_t* p, size_t off) {
  return (uint16_t)(((uint16_t)p[off] << 8) | (uint16_t)p[off + 1]);
}
static uint32_t rd_u24_be(const uint8_t* p, size_t off) {
  return ((uint32_t)p[off] << 16) | ((uint32_t)p[off + 1] << 8) | (uint32_t)p[off + 2];
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

// HolyIot 25015: dispatches on frame-type byte; each slot carries a subset of
// sensor fields (the others stay NAN). Battery comes separately from service data.
static Parsed parseHolyIot(const uint8_t* d, size_t len) {
  Parsed out;
  if (d == nullptr || len < HOLYIOT_MIN_LEN) return out;
  if (rd_u16(d, HOLYIOT_OFF_COMPANY) != (uint16_t)HOLYIOT_COMPANY_ID) return out;

  switch (d[HOLYIOT_OFF_FRAME]) {
    case HOLYIOT_FRAME_TH:
      if (len < 7) return out;
      out.type         = SensorType::HolyIot;
      out.temp_c       = rd_i16_be(d, 3) * TEMP_SCALE;
      out.humidity_pct = rd_u16_be(d, 5) * HUMID_SCALE;
      out.ok = true;
      break;
    case HOLYIOT_FRAME_ACCEL:
      if (len < 9) return out;
      out.type = SensorType::HolyIot;
      out.ax   = rd_i16_be(d, 3);
      out.ay   = rd_i16_be(d, 5);
      out.az   = rd_i16_be(d, 7);
      out.ok = true;
      break;
    case HOLYIOT_FRAME_BARO:
      if (len < 6) return out;
      out.type         = SensorType::HolyIot;
      out.pressure_hpa = rd_u24_be(d, 3) * PRESS_PA_SCALE;
      out.ok = true;
      break;
    default:
      // Unknown or iBeacon frame type (0x02) — not a sensor frame, ignore.
      break;
  }
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

// RuuviTag: one manufacturer-data frame carries temp/humidity/pressure + raw
// axes + battery (no on-board FFT). Decoding lives in the dependency-free
// ruuvi_decode.h so it can be host-unit-tested; here we just map it onto Parsed.
static Parsed parseRuuvi(const uint8_t* d, size_t len) {
  Parsed out;
  ruuvi::Reading r;
  if (!ruuvi::decode(d, len, r)) return out;

  out.type         = SensorType::Ruuvi;
  out.temp_c       = r.temp_c;
  out.humidity_pct = r.humidity_pct;
  out.pressure_hpa = r.pressure_hpa;
  out.ax           = r.accel_x_mg;
  out.ay           = r.accel_y_mg;
  out.az           = r.accel_z_mg;
  out.battery_pct  = ruuvi::batteryPercent(r.battery_mv);
  out.ok = true;
  return out;
}

// Dispatcher: try each known in-hive sensor format. Foreign beacons (no company
// match) return ok=false and are ignored.
static Parsed parsePayload(const uint8_t* d, size_t len) {
  Parsed p = parseHolyIot(d, len);
  if (p.ok) return p;
  p = parseRuuvi(d, len);
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
  bool    present = false;     // advertising data parsed successfully
  bool    found_by_mac = false; // device seen during scan (adv data optional)
#if HIVEINSIDE_GATT_CLIENT
  uint8_t ble_addr_type = BLE_ADDR_PUBLIC; // address type for GATT connection
#endif
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

class ScanCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    String mac = String(dev->getAddress().toString().c_str());
    mac = normalizeMac(mac);

    // NimBLE 2.x changed the manufacturer-data getter to return a
    // std::vector<uint8_t> (1.x returned std::string). Using `auto` plus
    // .data()/.size() keeps this working with either return type; parsePayload
    // tolerates a null/empty buffer.
    const uint8_t* mdata = nullptr;
    size_t mlen = 0;
    auto md = dev->getManufacturerData();
    if (dev->haveManufacturerData()) {
      mdata = reinterpret_cast<const uint8_t*>(md.data());
      mlen = md.size();
    }
    Parsed p = parsePayload(mdata, mlen);

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

#if HIVEINSIDE_GATT_CLIENT
    // Capture address type for any paired MAC even when manufacturer data is
    // absent. GATT-mode HiveInside does not embed the measurement blob, so p.ok
    // is false, but we need the address type for the subsequent connection
    // (normal GATT read and/or the OTA relay).
    for (int s = 0; s < 2; s++) {
      if (g_slot[s].mac.length() > 0 && g_slot[s].mac == mac) {
        g_slot[s].found_by_mac = true;
        g_slot[s].ble_addr_type = (uint8_t)dev->getAddress().getType();
      }
    }
#endif

    if (!p.ok) return;
    for (int s = 0; s < 2; s++) {
      if (g_slot[s].mac.length() == 0 || g_slot[s].mac != mac) continue;
      Accumulator& a = g_slot[s];
      a.present = true;
      a.type = p.type;
      a.rssi_dbm = dev->getRSSI();
      if (p.type == SensorType::HiveInside) {
        // HiveInside: single manufacturer blob carries everything including battery.
        a.battery_pct = p.battery_pct;
        a.temp_c = p.temp_c;
        a.humidity_pct = p.humidity_pct;
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
        // HolyIot / RuuviTag: a beacon frame carries a subset of sensor fields —
        // merge, don't overwrite. HolyIot reports battery in service data (UUID
        // 0x180A, last byte); RuuviTag embeds it in the manufacturer payload, so
        // parsePayload already set p.battery_pct for that case.
        if (dev->haveServiceData()) {
          auto sd = dev->getServiceData();
          if (sd.size() == 9)
            a.battery_pct = (uint8_t)sd[8];
        }
        if (p.battery_pct >= 0)     a.battery_pct  = p.battery_pct;
        if (!isnan(p.temp_c))       a.temp_c       = p.temp_c;
        if (!isnan(p.humidity_pct)) a.humidity_pct = p.humidity_pct;
        if (!isnan(p.pressure_hpa)) a.pressure_hpa = p.pressure_hpa;
        if (!isnan(p.ax)) {
          a.ax = p.ax; a.ay = p.ay; a.az = p.az;
          a.magnitudes.push_back(sqrtf(p.ax * p.ax + p.ay * p.ay + p.az * p.az));
        }
      }
    }
  }
};
}  // namespace

#if HIVEINSIDE_USE_GATT
// HiveInside GATT service / characteristic UUIDs — must match ble_link.cpp.
static const char* HIVEINSIDE_GATT_SVC = "8e8b0001-7a1c-4b9e-9a2f-1d6e0b9c1a01";
static const char* HIVEINSIDE_GATT_CHR = "8e8b0002-7a1c-4b9e-9a2f-1d6e0b9c1a01";

// Connect to a HiveInside GATT server, read the JSON measurement characteristic,
// and populate `out`. Returns true on success. Must be called while the NimBLE
// stack is initialised (between NimBLEDevice::init and ::deinit). Disconnects
// and frees the client handle before returning regardless of outcome.
static bool gattReadHiveInside(const NimBLEAddress& addr, Snapshot& out) {
  NimBLEClient* client = NimBLEDevice::createClient();
  // NimBLE 2.x setConnectTimeout() is in milliseconds (1.x used seconds).
  client->setConnectTimeout((uint32_t)HIVEINSIDE_GATT_CONNECT_TIMEOUT_S * 1000UL);

  Serial.printf("[BLE] GATT connecting to %s ...\n", addr.toString().c_str());
  if (!client->connect(addr)) {
    NimBLEDevice::deleteClient(client);
    Serial.println("[BLE] GATT connect failed");
    return false;
  }

  int rssi = client->getRssi();
  bool ok = false;

  NimBLERemoteService* svc = client->getService(HIVEINSIDE_GATT_SVC);
  if (!svc) {
    Serial.println("[BLE] GATT: HiveInside service not found");
  } else {
    NimBLERemoteCharacteristic* chr = svc->getCharacteristic(HIVEINSIDE_GATT_CHR);
    if (!chr || !chr->canRead()) {
      Serial.println("[BLE] GATT: measurement characteristic unavailable");
    } else {
      std::string val = chr->readValue();
      if (val.empty()) {
        Serial.println("[BLE] GATT: empty characteristic value");
      } else {
        JsonDocument doc;
        if (deserializeJson(doc, val) != DeserializationError::Ok) {
          Serial.println("[BLE] GATT: JSON parse error");
        } else {
          out.present      = true;
          out.type         = SensorType::HiveInside;
          out.rssi_dbm     = rssi;
          out.sample_count = 1;
          out.temp_c       = doc["temp_c"]           | NAN;
          out.humidity_pct = doc["humidity_percent"]  | NAN;
          int bpct         = doc["battery_percent"]   | -1;
          out.battery_pct  = bpct;

          if (doc["accel_ok"] | false) {
            out.accel_rms_mg          = doc["accel_rms_mg"]           | NAN;
            out.accel_peak_mg         = doc["accel_peak_mg"]          | NAN;
            out.accel_band_swarm_mg   = doc["accel_band_swarm_mg"]    | NAN;
            out.accel_band_fanning_mg = doc["accel_band_fanning_mg"]  | NAN;
            out.accel_band_activity_mg= doc["accel_band_activity_mg"] | NAN;
          }
          out.mic_present = doc["mic_ok"] | false;
          if (out.mic_present) {
            out.mic_rms_dbfs      = doc["mic_rms_dbfs"]           | NAN;
            out.mic_sub_bass_dbfs = doc["mic_band_sub_bass_dbfs"]  | NAN;
            out.mic_hum_dbfs      = doc["mic_band_hum_dbfs"]       | NAN;
            out.mic_piping_dbfs   = doc["mic_band_piping_dbfs"]    | NAN;
            out.mic_stress_dbfs   = doc["mic_band_stress_dbfs"]    | NAN;
            out.mic_high_dbfs     = doc["mic_band_high_dbfs"]      | NAN;
          }
          Serial.printf("[BLE] GATT read OK: temp=%.1f°C hum=%.1f%% accel=%d mic=%d RSSI=%d\n",
                        out.temp_c, out.humidity_pct,
                        (int)(bool)(doc["accel_ok"] | false),
                        (int)out.mic_present, rssi);
          ok = true;
        }
      }
    }
  }

  // Only terminate if still connected; a peer that already dropped the link
  // would otherwise make ble_gap_terminate() log a benign failure.
  if (client->isConnected()) client->disconnect();
  NimBLEDevice::deleteClient(client);
  return ok;
}
#endif  // HIVEINSIDE_USE_GATT

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
    case SensorType::Ruuvi:      return "RuuviTag";
    default:                     return "";
  }
}

static NimBLEScan* startScan(ScanCallbacks& cb, uint32_t seconds) {
  NimBLEDevice::init("");
  NimBLEScan* scan = NimBLEDevice::getScan();
  // NimBLE 2.x: setAdvertisedDeviceCallbacks() -> setScanCallbacks(); the second
  // arg (wantDuplicates=true) reports every advertisement to onResult, which is
  // what the per-cycle AC RMS/peak accumulation needs.
  scan->setScanCallbacks(&cb, /*wantDuplicates=*/true);
  scan->setActiveScan(HOLYIOT_BLE_ACTIVE_SCAN ? true : false);
  scan->setInterval(100);
  scan->setWindow(99);
  // NimBLE 2.x start() takes a duration in milliseconds (1.x used seconds) BUT,
  // unlike 1.x, start() is asynchronous: it kicks off discovery and returns
  // immediately, leaving onResult() to fire on the host task. The callers used
  // to clearResults()/deinit() right after start(), which tore the controller
  // down before a single advertisement could arrive — so no paired sensor was
  // ever matched and the portal scan came up empty. getResults() runs the same
  // scan but BLOCKS for the full duration, so the callbacks have their window.
  scan->getResults(seconds * 1000, false);
  return scan;
}

void scanPairedSensors(const String& mac0, const String& mac1,
                       Snapshot& slot1, Snapshot& slot2) {
  slot1 = Snapshot{};
  slot2 = Snapshot{};

  String m0 = normalizeMac(mac0);
  String m1 = normalizeMac(mac1);
  if (m0.length() == 0 && m1.length() == 0) {
    Serial.println("[BLE] No in-hive sensors paired; skipping scan");
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

#if HIVEINSIDE_USE_GATT
  // Post-scan GATT phase — NimBLE stack is still alive here; deinit follows.
  // Devices with advertising data (HolyIot, advertising-mode HiveInside) are
  // copied directly. Devices found only by MAC match (GATT-mode HiveInside, no
  // manufacturer blob) get a GATT connection attempt instead.
  for (int s = 0; s < 2; s++) {
    Snapshot& dest = (s == 0) ? slot1 : slot2;
    if (g_slot[s].mac.length() == 0 || !g_slot[s].found_by_mac) continue;
    if (g_slot[s].present) {
      // Advertising data parsed successfully — use it, no GATT needed.
      copyToSnapshot(g_slot[s], dest);
      continue;
    }
    // No advertising data: try GATT (expected for BLE_MODE_GATT HiveInside).
    NimBLEAddress addr(g_slot[s].mac.c_str(), g_slot[s].ble_addr_type);
    Serial.printf("[BLE] GATT: connecting to slot%d (%s)\n", s + 1, g_slot[s].mac.c_str());
    if (!gattReadHiveInside(addr, dest)) {
      Serial.printf("[BLE] GATT slot%d: no data this cycle\n", s + 1);
    }
  }
#else
  copyToSnapshot(g_slot[0], slot1);
  copyToSnapshot(g_slot[1], slot2);
#endif

  NimBLEDevice::deinit(true);  // free the controller before the WiFi upload

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

// ===========================================================================
// HiveInside firmware-over-BLE relay (GATT client → HiveInside OTA service)
// ===========================================================================
// HiveScale streams a firmware image straight from the HTTPS download into the
// HiveInside OTA characteristics, chunk by chunk, so a >1 MB image never has to
// fit in the WROOM's RAM. The HiveInside device buffers nothing either: it
// writes each chunk to its inactive OTA slot, tracks a running CRC-32 and only
// swaps slots if the end-to-end CRC the backend computed matches.
//
// Session lifecycle (all called from network.cpp::updateHiveInside):
//   otaBegin(mac,size,crc) → otaWrite(chunk)… → otaFinish() → otaCleanup()
// The NimBLE stack is brought up inside otaBegin and torn down in otaCleanup,
// mirroring scanPairedSensors so it coexists with the WiFi download.
#if HIVEINSIDE_OTA_ENABLED

// Must match HiveInside firmware/src/ble_link.cpp.
static const char* HI_OTA_SVC    = "8e8b0001-7a1c-4b9e-9a2f-1d6e0b9c1a01";
static const char* HI_OTA_CTRL   = "8e8b0010-7a1c-4b9e-9a2f-1d6e0b9c1a01"; // write: framed control
static const char* HI_OTA_DATA   = "8e8b0011-7a1c-4b9e-9a2f-1d6e0b9c1a01"; // write: payload stream
static const char* HI_OTA_STATUS = "8e8b0013-7a1c-4b9e-9a2f-1d6e0b9c1a01"; // read/notify: state+recv+err

// Control opcodes (first byte of a CTRL write).
static constexpr uint8_t HI_OTA_OP_BEGIN = 0x01; // + size(4 LE) + crc32(4 LE)
static constexpr uint8_t HI_OTA_OP_END   = 0x03; // finalize + verify + reboot
static constexpr uint8_t HI_OTA_OP_ABORT = 0x04; // cancel, stay on current image

// Status state byte values (HiveInside ble_link.cpp). >=0x10 is an error.
static constexpr uint8_t HI_OTA_DONE_STATE = 0x02;

namespace {
NimBLEClient*               s_otaClient = nullptr;
NimBLERemoteCharacteristic* s_otaCtrl   = nullptr;
NimBLERemoteCharacteristic* s_otaData   = nullptr;
NimBLERemoteCharacteristic* s_otaStatus = nullptr;
size_t                      s_otaChunk  = 20;   // negotiated DATA payload size
}  // namespace

void otaCleanup() {
  if (s_otaClient) {
    if (s_otaClient->isConnected()) s_otaClient->disconnect();
    NimBLEDevice::deleteClient(s_otaClient);
    s_otaClient = nullptr;
  }
  s_otaCtrl = s_otaData = s_otaStatus = nullptr;
  NimBLEDevice::deinit(true);
}

bool otaBegin(const String& mac, uint32_t totalLen, uint32_t crc32) {
  String m = normalizeMac(mac);
  if (m.length() == 0 || totalLen == 0) {
    Serial.println("[HI-OTA] bad arguments");
    return false;
  }

  NimBLEDevice::init("");
  NimBLEDevice::setMTU(247);  // ask for a larger ATT MTU; the device may grant less

  // Short locate scan to learn the device's current address + type, reusing the
  // same scan callback the measurement path uses.
  uint8_t addrType = BLE_ADDR_PUBLIC;
  bool found = false;
  g_slot[0] = Accumulator{}; g_slot[0].mac = m;
  g_slot[1] = Accumulator{};
  g_discover = nullptr;
  {
    ScanCallbacks cb;
    NimBLEScan* scan = startScan(cb, 4);  // 4 s is plenty for a connectable peer
    scan->clearResults();
  }
  if (g_slot[0].found_by_mac) { found = true; addrType = g_slot[0].ble_addr_type; }
  if (!found) {
    Serial.printf("[HI-OTA] device %s not found in scan\n", m.c_str());
    NimBLEDevice::deinit(true);
    return false;
  }

  NimBLEAddress addr(m.c_str(), addrType);
  s_otaClient = NimBLEDevice::createClient();
  s_otaClient->setConnectTimeout((uint32_t)HIVEINSIDE_GATT_CONNECT_TIMEOUT_S * 1000UL);
  Serial.printf("[HI-OTA] connecting to %s ...\n", addr.toString().c_str());
  if (!s_otaClient->connect(addr)) {
    Serial.println("[HI-OTA] connect failed");
    otaCleanup();
    return false;
  }

  NimBLERemoteService* svc = s_otaClient->getService(HI_OTA_SVC);
  s_otaCtrl   = svc ? svc->getCharacteristic(HI_OTA_CTRL)   : nullptr;
  s_otaData   = svc ? svc->getCharacteristic(HI_OTA_DATA)   : nullptr;
  s_otaStatus = svc ? svc->getCharacteristic(HI_OTA_STATUS) : nullptr;
  if (!svc || !s_otaCtrl || !s_otaData) {
    Serial.println("[HI-OTA] OTA service/characteristics not found "
                   "(HiveInside firmware too old for BLE OTA?)");
    otaCleanup();
    return false;
  }

  uint16_t mtu = s_otaClient->getMTU();
  s_otaChunk = (mtu > 3) ? (size_t)(mtu - 3) : 20;
  if (s_otaChunk > HIVEINSIDE_OTA_CHUNK_MAX) s_otaChunk = HIVEINSIDE_OTA_CHUNK_MAX;
  Serial.printf("[HI-OTA] connected: MTU=%u chunk=%u image=%u bytes crc=0x%08X\n",
                (unsigned)mtu, (unsigned)s_otaChunk, (unsigned)totalLen, (unsigned)crc32);

  uint8_t beg[9];
  beg[0] = HI_OTA_OP_BEGIN;
  beg[1] = totalLen & 0xFF;        beg[2] = (totalLen >> 8) & 0xFF;
  beg[3] = (totalLen >> 16) & 0xFF; beg[4] = (totalLen >> 24) & 0xFF;
  beg[5] = crc32 & 0xFF;           beg[6] = (crc32 >> 8) & 0xFF;
  beg[7] = (crc32 >> 16) & 0xFF;   beg[8] = (crc32 >> 24) & 0xFF;
  if (!s_otaCtrl->writeValue(beg, sizeof(beg), /*response=*/true)) {
    Serial.println("[HI-OTA] BEGIN write failed");
    otaCleanup();
    return false;
  }
  return true;
}

// Relay one buffer, splitting it across as many DATA writes as the negotiated
// chunk size requires. Each write waits for the ATT response, which the device
// only sends after it has flashed the chunk — natural flow control.
bool otaWrite(const uint8_t* data, size_t len) {
  if (!s_otaData) return false;
  size_t sent = 0;
  while (sent < len) {
    size_t n = len - sent;
    if (n > s_otaChunk) n = s_otaChunk;
    if (!s_otaData->writeValue(data + sent, n, /*response=*/true)) {
      Serial.printf("[HI-OTA] DATA write failed after %u bytes\n", (unsigned)sent);
      return false;
    }
    sent += n;
  }
  return true;
}

// Send END, then poll STATUS for DONE. The device reboots ~1.5 s after it
// reports DONE, so a dropped link right after a DONE read is still success.
bool otaFinish() {
  if (!s_otaCtrl) return false;
  uint8_t op = HI_OTA_OP_END;
  if (!s_otaCtrl->writeValue(&op, 1, /*response=*/true)) {
    Serial.println("[HI-OTA] END write failed");
    return false;
  }
  if (!s_otaStatus || !s_otaStatus->canRead()) {
    Serial.println("[HI-OTA] no STATUS characteristic — assuming success");
    return true;
  }
  for (int i = 0; i < 25; i++) {
    std::string s = s_otaStatus->readValue();
    if (s.size() >= 6) {
      uint8_t state = (uint8_t)s[0];
      uint8_t err   = (uint8_t)s[5];
      if (state == HI_OTA_DONE_STATE) {
        Serial.println("[HI-OTA] device reports DONE — it will reboot into the new image");
        return true;
      }
      if (state >= 0x10) {
        Serial.printf("[HI-OTA] device reported error state=0x%02X err=0x%02X\n", state, err);
        return false;
      }
    }
    if (!s_otaClient || !s_otaClient->isConnected()) {
      // Link dropped before we read DONE; treat as inconclusive failure so the
      // caller reports it and the backend can re-queue.
      Serial.println("[HI-OTA] link dropped before DONE confirmation");
      return false;
    }
    delay(200);
  }
  Serial.println("[HI-OTA] timed out waiting for DONE");
  return false;
}

// Best-effort cancel so a partial transfer leaves the device on its old image.
void otaAbort() {
  if (s_otaCtrl) {
    uint8_t op = HI_OTA_OP_ABORT;
    s_otaCtrl->writeValue(&op, 1, true);
  }
}

#endif  // HIVEINSIDE_OTA_ENABLED

}  // namespace blesensor

#endif  // ENABLE_BLE_SCAN
