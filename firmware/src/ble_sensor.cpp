// ble_sensor.cpp — HolyIot 25015 passive BLE bridge (NimBLE scanner + parser).
#include "ble_sensor.h"

#if ENABLE_BLE_SCAN

#include <NimBLEDevice.h>
#include <math.h>

#include "ble_stack.h"
#include "ruuvi_decode.h"

// Declared at global scope so the blesensor namespace sees ::sendIntervalMs,
// not blesensor::sendIntervalMs.  Defined in globals.cpp.
extern unsigned long sendIntervalMs;

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
// HiveInside manufacturer-data layout (scan-response blob)
// ───────────────────────────────────────────────────────────────────────────
// Distinct from HolyIot by company id (HIVEINSIDE_COMPANY_ID, default Espressif
// 0x02E5) plus a magic byte, so the dispatcher can tell the two apart even
// though both ride the same passive-scan bridge. The HiveInside device runs the
// vibration and acoustic FFTs on board, so it broadcasts finished RMS + band
// values (no raw axes). HiveInside is the XIAO nRF54LM20A Sense node, which
// advertises this exact 26-byte frame CONTINUOUSLY as a beacon (no GATT
// measurement service — passive scan is the whole ingest). Layout is documented
// in HiveInside/firmware-nrf54lm20a/src/beacon.c and mirrored here:
//
//   off 0..1  : company id (LE)        == HIVEINSIDE_COMPANY_ID
//   off 2     : magic                   == 0x48 ('H')
//   off 3     : version                 (currently 0x01)
//   off 4     : flags  bit0 sht bit1 accel bit2 mic bit3 batt
//   off 5..6  : temperature  int16 LE, 0.1 °C   (valid only if flags bit0 set)
//   off 7..8  : humidity      uint16 LE, 0.1 %RH (valid only if flags bit0 set)
//   off 9..10 : battery       uint16 LE, milli-volt (valid only if bit3 set)
//   off 11    : battery percent (uint8)             (valid only if bit3 set)
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
// Flags byte (offset 4): a group's fields are only valid when its bit is set.
// A cleared bit means the on-board sensor was absent or failed this cycle, so
// the corresponding fields must be reported as "not present" (NAN / battery -1)
// rather than the raw zeros the encoder leaves in the frame.
static constexpr uint8_t HI_FLAG_SHT    = 1 << 0;  // temperature + humidity valid
static constexpr uint8_t HI_FLAG_ACCEL  = 1 << 1;  // vibration RMS + bands valid
static constexpr uint8_t HI_FLAG_MIC    = 1 << 2;  // acoustic RMS + bands valid
static constexpr uint8_t HI_FLAG_BATT   = 1 << 3;  // battery mV + percent valid
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

// ───────────────────────────────────────────────────────────────────────────
// HiveInside identity record (scan-response manufacturer element)
// ───────────────────────────────────────────────────────────────────────────
// A beacon-only nRF54LM20A node never accepts a GATT connection (outside an OTA
// window), so its board and running firmware version can't be read from a GATT
// version characteristic. Instead the node rides a second manufacturer-data
// element in its scan response: same company id as the measurement frame, but a
// distinct magic ('I' vs 'H') so the two never alias. HiveHub active-scans by
// default (HOLYIOT_BLE_ACTIVE_SCAN), so the scan response arrives in the same
// combined payload as the primary advertisement.
//
//   off 0..1 : company id (LE)  == HIVEINSIDE_COMPANY_ID
//   off 2    : magic            == 0x49 ('I')
//   off 3    : record version   (currently 0x01)
//   off 4    : board id         uint8  2 = nrf54lm20a
//   off 5..7 : firmware version uint8  major, minor, patch
static constexpr size_t  HI_ID_MIN_LEN   = 8;
static constexpr uint8_t HI_ID_MAGIC     = 0x49;  // 'I'
static constexpr size_t  HI_ID_OFF_MAGIC = 2;
static constexpr size_t  HI_ID_OFF_BOARD = 4;
static constexpr size_t  HI_ID_OFF_FW_MAJ = 5;
static constexpr size_t  HI_ID_OFF_FW_MIN = 6;
static constexpr size_t  HI_ID_OFF_FW_PATCH = 7;

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
  int   battery_mv  = -1;                     // HiveInside raw cell voltage (-1 = absent)
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

// HiveInside (nRF54LM20A): on-board vibration + acoustic
// FFT, no raw axes. The flags byte (offset 4) says which sensor groups produced
// a valid reading this cycle; a cleared bit means that sensor was absent or
// failed, so the group is reported as "not present" instead of the raw zeros
// the encoder leaves in the frame (a failed SHT40 must not read as 0.0 °C).
static Parsed parseHiveInside(const uint8_t* d, size_t len) {
  Parsed out;
  if (d == nullptr || len < HI_MIN_LEN) return out;
  if (rd_u16(d, HOLYIOT_OFF_COMPANY) != (uint16_t)HIVEINSIDE_COMPANY_ID) return out;
  if (d[HI_OFF_MAGIC] != HI_MAGIC) return out;

  const uint8_t flags = d[HI_OFF_FLAGS];
  out.type = SensorType::HiveInside;

  if (flags & HI_FLAG_SHT) {
    out.temp_c       = rd_i16(d, HI_OFF_TEMP)  * TEMP_SCALE;
    out.humidity_pct = rd_u16(d, HI_OFF_HUMID) * HUMID_SCALE;
  }
  if (flags & HI_FLAG_BATT) {
    out.battery_pct = d[HI_OFF_BATT_PCT];
    out.battery_mv  = rd_u16(d, HI_OFF_BATT_MV);
  }
  if (flags & HI_FLAG_ACCEL) {
    out.accel_rms_mg          = rd_u16(d, HI_OFF_ACC_RMS)      * HI_ACCEL_SCALE;
    out.accel_band_swarm_mg   = rd_u16(d, HI_OFF_ACC_SWARM)    * HI_ACCEL_SCALE;
    out.accel_band_fanning_mg = rd_u16(d, HI_OFF_ACC_FANNING)  * HI_ACCEL_SCALE;
    out.accel_band_activity_mg= rd_u16(d, HI_OFF_ACC_ACTIVITY) * HI_ACCEL_SCALE;
  }
  if (flags & HI_FLAG_MIC) {
    out.mic_present   = true;
    out.mic_rms_dbfs       = (int8_t)d[HI_OFF_MIC_RMS];
    out.mic_sub_bass_dbfs  = (int8_t)d[HI_OFF_MIC_SUB];
    out.mic_hum_dbfs       = (int8_t)d[HI_OFF_MIC_HUM];
    out.mic_piping_dbfs    = (int8_t)d[HI_OFF_MIC_PIPING];
    out.mic_stress_dbfs    = (int8_t)d[HI_OFF_MIC_STRESS];
    out.mic_high_dbfs      = (int8_t)d[HI_OFF_MIC_HIGH];
  }
  out.ok = true;
  return out;
}

// HiveInside identity record (board + firmware version) from the scan-response
// manufacturer element. Distinct from the measurement frame by its 'I' magic, so
// it is only ever matched here and never mistaken for a measurement.
struct HiIdentity {
  bool   ok = false;
  String board;        // "nrf54lm20a" (server HIVEINSIDE_BOARDS)
  String fw_version;   // "major.minor.patch"
};

static const char* hiBoardName(uint8_t id) {
  switch (id) {
    case 2:  return "nrf54lm20a";
    default: return "";   // board id 1 (esp32-c6) was the deprecated prototype
  }
}

static HiIdentity parseHiveInsideIdentity(const uint8_t* d, size_t len) {
  HiIdentity out;
  if (d == nullptr || len < HI_ID_MIN_LEN) return out;
  if (rd_u16(d, HOLYIOT_OFF_COMPANY) != (uint16_t)HIVEINSIDE_COMPANY_ID) return out;
  if (d[HI_ID_OFF_MAGIC] != HI_ID_MAGIC) return out;
  out.board = String(hiBoardName(d[HI_ID_OFF_BOARD]));
  out.fw_version = String((int)d[HI_ID_OFF_FW_MAJ]) + "." +
                   String((int)d[HI_ID_OFF_FW_MIN]) + "." +
                   String((int)d[HI_ID_OFF_FW_PATCH]);
  out.ok = out.board.length() > 0 || out.fw_version.length() > 0;
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

#if HIVEINSIDE_GATT_CLIENT
// ── address types learned from any scan this boot ──────────────────────────
// The OTA relay needs a node's address type before it can connect, and used to
// run a scan of its own to learn it. That scan is unsafe after the stack has
// been torn down and re-initialised (see ble_stack.h), and it was also pure
// duplication: the measurement scan earlier in the same cycle already saw the
// node and captured exactly this. Keep it instead of throwing it away with the
// per-slot accumulators. Plain RAM, so deep sleep clears it — which is correct,
// since a re-pairing between cycles must not be answered from a stale entry.
struct AddrTypeEntry {
  String  mac;
  uint8_t type;
};
std::vector<AddrTypeEntry> g_addrTypes;

void rememberAddrType(const String& mac, uint8_t type) {
  for (AddrTypeEntry& e : g_addrTypes) {
    if (e.mac == mac) { e.type = type; return; }
  }
  g_addrTypes.push_back(AddrTypeEntry{mac, type});
}

bool recallAddrType(const String& mac, uint8_t& typeOut) {
  for (const AddrTypeEntry& e : g_addrTypes) {
    if (e.mac == mac) { typeOut = e.type; return true; }
  }
  return false;
}
#endif  // HIVEINSIDE_GATT_CLIENT

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
  int     battery_mv  = -1;    // HiveInside raw cell voltage (-1 = absent)
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
  // HiveInside identity (from the scan-response record, not the measurement).
  String  board;        // "nrf54lm20a"
  String  fw_version;   // "major.minor.patch"
  // Advertised local name ("HiveInside-8A3F"). Like the identity record it
  // rides in the scan response, so it only arrives on an active scan.
  String  device_name;
};

namespace {
// One accumulator per paired in-hive MAC for the current scan. Sized by the
// caller (scanPairedSensorsMulti / the 2-slot wrapper / discover / OTA) before a
// scan; the callback matches advertisements against every entry, so any number of
// beacons is handled by the single shared scan window.
std::vector<Accumulator> g_slot;
std::vector<Discovered>* g_discover = nullptr;  // non-null during discover()

class ScanCallbacks : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    String mac = String(dev->getAddress().toString().c_str());
    mac = normalizeMac(mac);

    // Walk every manufacturer-specific (0xFF) AD structure in the combined
    // advertisement + scan-response payload. A HiveInside beacon splits its data
    // across two such elements — the measurement frame ('H') in the primary
    // packet and the identity record ('I') in the scan response — so a single
    // getManufacturerData() (first element only) could miss either one depending
    // on ordering. Walking the raw payload matches both regardless of order and
    // still handles the single-element HolyIot / RuuviTag beacons unchanged.
    Parsed p;
    HiIdentity ident;
    {
      auto payload = dev->getPayload();
      size_t i = 0;
      while (i + 1 < payload.size()) {
        uint8_t len = payload[i];
        if (len == 0 || i + 1 + len > payload.size()) break;
        if (payload[i + 1] == 0xFF && len >= 1) {
          const uint8_t* d = reinterpret_cast<const uint8_t*>(&payload[i + 2]);
          size_t n = (size_t)(len - 1);
          if (!p.ok) { Parsed q = parsePayload(d, n); if (q.ok) p = q; }
          if (!ident.ok) ident = parseHiveInsideIdentity(d, n);
        }
        i += 1 + len;
      }
    }

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
    // Capture the address type for any paired MAC so the OTA relay can connect
    // by the node's identity address. This is only needed during an OTA locate
    // scan (a beacon nRF54 becomes connectable only for its OTA window); the
    // routine measurement path never opens a connection.
    for (size_t s = 0; s < g_slot.size(); s++) {
      if (g_slot[s].mac.length() > 0 && g_slot[s].mac == mac) {
        g_slot[s].found_by_mac = true;
        g_slot[s].ble_addr_type = (uint8_t)dev->getAddress().getType();
        // Outlives the accumulators, so a later relay in this same boot does
        // not have to scan again to learn what this scan already knows.
        rememberAddrType(mac, g_slot[s].ble_addr_type);
      }
    }
#endif

    // The identity record can arrive on its own scan-response report; capture it
    // for any matched slot independently of the measurement so a beacon node's
    // board/firmware are learned without ever opening a GATT connection.
    if (ident.ok) {
      for (size_t s = 0; s < g_slot.size(); s++) {
        if (g_slot[s].mac.length() == 0 || g_slot[s].mac != mac) continue;
        if (ident.board.length())      g_slot[s].board = ident.board;
        if (ident.fw_version.length()) g_slot[s].fw_version = ident.fw_version;
      }
    }

    // Local name, captured on the same terms and for the same reason: it comes
    // from the scan response, it can arrive on a report that carries no
    // measurement, and it is the only thing on the air that distinguishes two
    // otherwise identical nodes. Every beacon type may carry one, so this is not
    // gated on the HiveInside identity record.
    if (dev->haveName()) {
      String advName = String(dev->getName().c_str());
      if (advName.length()) {
        for (size_t s = 0; s < g_slot.size(); s++) {
          if (g_slot[s].mac.length() == 0 || g_slot[s].mac != mac) continue;
          g_slot[s].device_name = advName;
        }
      }
    }

    if (!p.ok) return;
    for (size_t s = 0; s < g_slot.size(); s++) {
      if (g_slot[s].mac.length() == 0 || g_slot[s].mac != mac) continue;
      Accumulator& a = g_slot[s];
      a.present = true;
      a.type = p.type;
      a.rssi_dbm = dev->getRSSI();
      if (p.type == SensorType::HiveInside) {
        // HiveInside: single manufacturer blob carries everything including
        // battery. parseHiveInside() already applied the flags byte, so absent
        // groups arrive as NAN / -1 and are copied through verbatim.
        a.battery_pct = p.battery_pct;
        a.battery_mv  = p.battery_mv;
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

// NOTE: the HIVEINSIDE_USE_GATT measurement path was removed with the ESP32-C6
// HiveInside prototype. That path connected to a GATT server to read the JSON
// measurement characteristic and to write the wake-sync hint, plus a one-off
// connect to a version characteristic for board/firmware discovery. The only
// remaining HiveInside board is the nRF54LM20A, a NON-connectable beacon whose
// measurements and identity ('H'/'I' manufacturer elements) come entirely from
// the passive scan above; it accepts a GATT connection only for the OTA relay
// (HIVEINSIDE_OTA_ENABLED), which lives in src/gatt_ota.cpp and reaches it via
// the locate-scan helper near the bottom of this file.

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
  // Identity, not measurement: set before the HiveInside early-return below so
  // HolyIot and Ruuvi carry it too — a beekeeper telling two beacons apart has
  // the same problem whichever kind they are.
  s.mac          = a.mac;
  s.device_name  = a.device_name;
  s.rssi_dbm     = a.rssi_dbm;
  s.battery_pct  = a.battery_pct;
  s.battery_mv   = a.battery_mv;
  s.temp_c       = a.temp_c;
  s.humidity_pct = a.humidity_pct;
  s.pressure_hpa = a.pressure_hpa;

  if (a.type == SensorType::HiveInside) {
    // The HiveInside device already computed RMS + bands on board.
    s.sample_count = a.present ? 1 : 0;
    // Board + firmware learned from the scan-response identity record (a
    // beacon-only node advertises them instead of serving them over GATT).
    if (a.board.length())      s.board = a.board;
    if (a.fw_version.length()) s.fw_version = a.fw_version;
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
    case SensorType::HiveInside: return "HiveInside";
    case SensorType::Ruuvi:      return "RuuviTag";
    default:                     return "";
  }
}

// Returns nullptr when a scan cannot safely run in this port lifetime — see
// ble_stack.h. Every caller must handle that; it is not an error condition that
// can be papered over, because the alternative is a load access fault inside
// esp_timer_stop() on the scan singleton's stale callout.
static NimBLEScan* startScan(ScanCallbacks& cb, uint32_t seconds) {
  blestack::acquire();
  if (!blestack::scanAllowed()) {
    Serial.printf("[BLE] refusing to scan: the scan singleton belongs to port "
                  "lifetime %u and this is %u; a scan here would fault in "
                  "esp_timer_stop()\n",
                  (unsigned)blestack::scanGeneration(),
                  (unsigned)blestack::generation());
    return nullptr;
  }
  blestack::noteScanStarted();
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

void scanPairedSensorsMulti(const std::vector<String>& macs,
                            std::vector<Snapshot>& out) {
  out.assign(macs.size(), Snapshot{});

  g_slot.assign(macs.size(), Accumulator{});
  size_t paired = 0;
  for (size_t i = 0; i < macs.size(); i++) {
    String m = normalizeMac(macs[i]);
    g_slot[i].mac = m;
    if (m.length()) paired++;
  }
  g_discover = nullptr;

  if (paired == 0) {
    Serial.println("[BLE] No in-hive sensors paired; skipping scan");
    g_slot.clear();
    return;
  }

  // ONE shared scan window catches every paired beacon at once, so any number of
  // beacon sensors costs the same airtime and deep sleep stays effective.
  Serial.printf("[BLE] Scanning %us for %u paired in-hive sensor(s) at boot+%lums\n",
                (unsigned)HOLYIOT_BLE_SCAN_SECONDS, (unsigned)paired, millis());

  ScanCallbacks cb;
  NimBLEScan* scan = startScan(cb, HOLYIOT_BLE_SCAN_SECONDS);
  if (!scan) {
    // First scan of the boot, so this is unreachable in practice; handled
    // rather than asserted because "unreachable" is what the previous
    // teardown assumption claimed too.
    Serial.println("[BLE] measurement scan unavailable this boot");
    blestack::release();
    g_slot.clear();
    return;
  }
  scan->clearResults();

  // Every in-hive sensor on this bridge (HolyIot, RuuviTag, nRF54 HiveInside) is
  // a passive beacon, so the single shared scan window above captured everything:
  // measurements from the 'H' manufacturer element and, for HiveInside, board +
  // firmware from the 'I' identity element — both applied here by copyToSnapshot.
  // No post-scan GATT connect is needed (the deprecated ESP32-C6 HiveInside was
  // the only sensor that required one; its measurement-read path is gone).
  for (size_t s = 0; s < g_slot.size(); s++) copyToSnapshot(g_slot[s], out[s]);

  // Teardown rule and its consequences now live in one place: ble_stack.h.
  // Briefly: deinit(true) faults in ~NimBLEScan(), and deinit(false) leaves the
  // singleton holding a callout from this port lifetime — so nothing may scan
  // again until the next boot. The controller is fully freed for WiFi either
  // way, which is the reason this call is here at all.
  blestack::release();
  g_slot.clear();
}

void scanPairedSensors(const String& mac0, const String& mac1,
                       Snapshot& slot1, Snapshot& slot2) {
  // Back-compat 2-slot wrapper over the generalized multi-hive scan.
  std::vector<String> macs = {mac0, mac1};
  std::vector<Snapshot> out;
  scanPairedSensorsMulti(macs, out);
  slot1 = out.size() > 0 ? out[0] : Snapshot{};
  slot2 = out.size() > 1 ? out[1] : Snapshot{};

  Serial.printf("[BLE] slot1 present=%d (%u adv) | slot2 present=%d (%u adv)\n",
                slot1.present, slot1.sample_count, slot2.present, slot2.sample_count);
}

std::vector<Discovered> discover(uint32_t seconds) {
  std::vector<Discovered> found;
  g_slot.clear();   // discover mode matches against g_discover, not g_slot
  g_discover = &found;

  ScanCallbacks cb;
  NimBLEScan* scan = startScan(cb, seconds);
  if (!scan) {
    // The one caller that can legitimately want to scan late in a boot. It is
    // reachable only on a hub that already has a sensor paired — an unpaired
    // one never runs the measurement scan, so discovery still gets port
    // lifetime 1 and the usual first-pairing flow is unaffected. Adding a
    // second sensor to a paired hub now needs a reboot into the portal. By the
    // same mechanism that killed the relay, the alternative here is a fault
    // rather than an empty list.
    Serial.println("[BLE] discovery scan unavailable — reboot and retry before "
                   "the first measurement cycle");
    blestack::release();
    g_discover = nullptr;
    return found;
  }
  scan->clearResults();
  blestack::release();

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
  if (snap.battery_mv >= 0)      doc[bp + "battery_mv"]       = snap.battery_mv;
  doc[bp + "rssi_dbm"] = snap.rssi_dbm;
  // HiveInside advertises its running firmware version in its beacon identity
  // record ("fw"); surface it as ble_{slot}_firmware_version so the backend and
  // HivePal can display it next to the HiveHub node's own firmware. HolyIot/Ruuvi
  // leave this empty.
  if (snap.fw_version.length()) doc[bp + "firmware_version"] = snap.fw_version;
  // Board/architecture (now only "nrf54lm20a") so the backend can confirm it is
  // relaying the nRF54 HiveInside image.
  if (snap.board.length())      doc[bp + "board"]            = snap.board;

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

void writeSnapshotToHive(JsonObject hive, const Snapshot& snap) {
  // Nested per-hive form used by the hives[] array (server maps these onto the
  // hive_readings accel_*/ble_*/mic_* columns). Temperature is owned by
  // sensors.cpp (DS18B20-vs-BLE arbitration), so it is not written here.
  JsonObject accel = hive["accel"].to<JsonObject>();
  accel["ok"] = snap.present;
  if (!snap.present) return;

  JsonObject ble = hive["ble"].to<JsonObject>();
  ble["present"]     = true;
  ble["sensor_type"] = sensorTypeName(snap.type);
  if (!isnan(snap.humidity_pct)) ble["humidity_percent"] = snap.humidity_pct;
  if (!isnan(snap.pressure_hpa)) ble["pressure_hpa"]     = snap.pressure_hpa;
  if (!isnan(snap.accel_x_mg))   ble["accel_x_mg"]       = snap.accel_x_mg;
  if (!isnan(snap.accel_y_mg))   ble["accel_y_mg"]       = snap.accel_y_mg;
  if (!isnan(snap.accel_z_mg))   ble["accel_z_mg"]       = snap.accel_z_mg;
  if (snap.battery_pct >= 0)     ble["battery_percent"]  = snap.battery_pct;
  if (snap.battery_mv >= 0)      ble["battery_mv"]       = snap.battery_mv;
  ble["rssi_dbm"] = snap.rssi_dbm;
  if (snap.fw_version.length())  ble["firmware_version"] = snap.fw_version;
  if (snap.board.length())       ble["board"]            = snap.board;
  // Which physical node this is. Both are nested-only: they never change
  // between readings and nothing charts them, so they ride in
  // hive_readings.raw_json rather than earning a column or a flat ble_{n}_*
  // alias (the same treatment the HiveTraffic image version gets).
  //
  // Unlike the HiveTraffic counter's pair, these sit AFTER the !present return
  // above: a hive with no beacon heard emits no "ble" object at all, and
  // creating one just to carry an address would change what the presence of
  // that object means for every reader of hive_readings. The dashboard reads
  // the newest reading that HAS the field, so a node that missed one scan
  // window keeps its identity anyway.
  if (snap.device_name.length()) ble["device_name"]      = snap.device_name;
  if (snap.mac.length())         ble["mac"]              = snap.mac;

  if (snap.sample_count > 0) {
    accel["sample_count"]   = snap.sample_count;
    accel["sample_rate_hz"] = 0;   // beacon: no fixed sample rate
    accel["range_g"]        = 2;   // LIS2DH12 / LIS3DH default ±2 g
    if (!isnan(snap.accel_rms_mg))  accel["rms_mg"]  = snap.accel_rms_mg;
    if (!isnan(snap.accel_peak_mg)) accel["peak_mg"] = snap.accel_peak_mg;
    if (!isnan(snap.accel_band_swarm_mg))    accel["band_swarm_mg"]    = snap.accel_band_swarm_mg;
    if (!isnan(snap.accel_band_fanning_mg))  accel["band_fanning_mg"]  = snap.accel_band_fanning_mg;
    if (!isnan(snap.accel_band_activity_mg)) accel["band_activity_mg"] = snap.accel_band_activity_mg;
  }

  if (snap.mic_present) {
    JsonObject mic = hive["mic"].to<JsonObject>();
    mic["ok"] = true;
    if (!isnan(snap.mic_rms_dbfs))      mic["rms_dbfs"]           = snap.mic_rms_dbfs;
    if (!isnan(snap.mic_sub_bass_dbfs)) mic["band_sub_bass_dbfs"] = snap.mic_sub_bass_dbfs;
    if (!isnan(snap.mic_hum_dbfs))      mic["band_hum_dbfs"]      = snap.mic_hum_dbfs;
    if (!isnan(snap.mic_piping_dbfs))   mic["band_piping_dbfs"]   = snap.mic_piping_dbfs;
    if (!isnan(snap.mic_stress_dbfs))   mic["band_stress_dbfs"]   = snap.mic_stress_dbfs;
    if (!isnan(snap.mic_high_dbfs))     mic["band_high_dbfs"]     = snap.mic_high_dbfs;
  }
}

// ===========================================================================
// Locate-scan helper for the firmware-over-BLE relay (see src/gatt_ota.cpp)
// ===========================================================================
// The relay itself moved to gatt_ota.cpp so it can also serve HiveTraffic on
// devices built without ENABLE_BLE_SCAN. What stays here is the one piece that
// genuinely needs this file's scan machinery: an nRF54 HiveInside is otherwise
// only ever a passive beacon, so nothing establishes which address type to
// connect with until it has been seen advertising.
#if HIVEINSIDE_OTA_ENABLED

bool locateByScan(const String& mac, uint8_t& addrTypeOut) {
  String m = normalizeMac(mac);
  if (m.length() == 0) return false;

  // Fast path, and on a healthy cycle the only path: the measurement scan ran
  // minutes ago in this same wake cycle and already recorded this node's
  // address type. Reusing it skips a redundant 4 s scan AND avoids scanning in
  // a later port lifetime, which is what faulted in esp_timer_stop() when the
  // relay tried to locate a node it had just finished measuring.
  if (recallAddrType(m, addrTypeOut)) {
    Serial.printf("[BLE] %s address type %u known from this boot's scan; "
                  "no locate scan needed\n", m.c_str(), (unsigned)addrTypeOut);
    return true;
  }

  // Not heard this boot. A scan is the only way to learn the type — but only if
  // this port lifetime owns the scan singleton.
  if (!blestack::scanAllowed()) {
    Serial.printf("[BLE] %s was not seen by this cycle's measurement scan and a "
                  "locate scan cannot run safely now\n", m.c_str());
    return false;
  }

  // Reuse the measurement path's scan callback and slot accumulator: a hit sets
  // found_by_mac and captures the address type off the advertisement.
  g_slot.assign(1, Accumulator{});
  g_slot[0].mac = m;
  g_discover = nullptr;
  {
    ScanCallbacks cb;
    NimBLEScan* scan = startScan(cb, 4);  // 4 s is plenty for a connectable peer
    if (!scan) { g_slot.clear(); return false; }
    scan->clearResults();
  }
  if (!g_slot[0].found_by_mac) { g_slot.clear(); return false; }
  addrTypeOut = g_slot[0].ble_addr_type;
  g_slot.clear();
  return true;
}

#endif  // HIVEINSIDE_OTA_ENABLED

}  // namespace blesensor

#endif  // ENABLE_BLE_SCAN
