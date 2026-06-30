// ruuvi_decode.h — pure decoder for RuuviTag BLE sensor advertisements.
//
// The RuuviTag (Ruuvi Innovations) is a battery BLE beacon with an on-board
// temperature / humidity / pressure sensor and a 3-axis accelerometer — the
// same four-in-one measurement set as the HolyIot 25015, so it folds into the
// HiveHub passive-scan bridge and the existing ble_{slot}_* / accel_{slot}_*
// measurement fields with no new server columns.
//
// Unlike the HolyIot (which splits its readings across several manufacturer-data
// frames), a RuuviTag packs everything into ONE manufacturer-specific AD under
// the registered Ruuvi company id 0x0499. Two on-air formats are in the wild:
//
//   Data Format 5 (RAWv2) — the current default, 24-byte payload, all fields
//     big-endian, per-field "invalid" sentinels. Documented at
//     https://docs.ruuvi.com/communication/bluetooth-advertisements/data-format-5-rawv2
//   Data Format 3 (RAWv1) — the legacy format, 14-byte payload.
//
// This header is deliberately free of Arduino / NimBLE deps so it can be
// unit-tested on the host (see test-data/test_ruuvi_decode.cpp); ble_sensor.cpp
// calls ruuvi::decode() from inside the NimBLE scan callback. The byte layout
// below is validated against Ruuvi's own published test vectors.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <math.h>

namespace ruuvi {

// 16-bit Bluetooth SIG company identifier assigned to Ruuvi Innovations Ltd.
// Carried little-endian in the manufacturer-specific AD (bytes [0..1]).
static constexpr uint16_t COMPANY_ID = 0x0499;

struct Reading {
  bool  present       = false;
  uint8_t format      = 0;     // 3 or 5 — the data format that decoded

  float temp_c        = NAN;   // NAN when the field carried its invalid sentinel
  float humidity_pct  = NAN;
  float pressure_hpa  = NAN;
  float accel_x_mg    = NAN;
  float accel_y_mg    = NAN;
  float accel_z_mg    = NAN;

  uint16_t battery_mv = 0;     // 0 = not reported / invalid
  int   tx_power_dbm  = 0;     // Format 5 only (0 when absent)
  int   movement_count = -1;   // Format 5 only (-1 when absent)
  long  sequence       = -1;   // Format 5 measurement sequence (-1 when absent)
};

// Big-endian field readers (RuuviTag sensor data is big-endian; the company id
// in the AD header is little-endian, read separately).
inline uint16_t rd_u16be(const uint8_t* p, size_t off) {
  return (uint16_t)(((uint16_t)p[off] << 8) | (uint16_t)p[off + 1]);
}
inline int16_t rd_i16be(const uint8_t* p, size_t off) {
  return (int16_t)rd_u16be(p, off);
}

// Map a RuuviTag coin-cell voltage to a coarse percent for the shared
// ble_{slot}_battery_percent field. The CR2477 used by the RuuviTag reads
// ~3.0 V fresh and is effectively flat by ~2.0 V under Ruuvi's pulsed load, so a
// clamped linear map over that window is a reasonable, monotonic estimate.
// Returns -1 when the voltage is unknown (0).
inline int batteryPercent(uint16_t mv) {
  if (mv == 0) return -1;
  float pct = (mv - 2000.0f) / (3000.0f - 2000.0f) * 100.0f;
  if (pct < 0.0f)   pct = 0.0f;
  if (pct > 100.0f) pct = 100.0f;
  return (int)lroundf(pct);
}

// Decode the Data Format 5 (RAWv2) payload. `d` points at the manufacturer data
// (company id included); `d[2]` is the format byte. Per-field 0x8000 / 0xFFFF
// sentinels are left as NAN.
inline bool decodeFormat5(const uint8_t* d, size_t len, Reading& out) {
  // 24-byte payload: company id (2) + format (1) + 23 data bytes. The mandatory
  // sensor fields run through the power-info word at d[15..16]; MAC/sequence at
  // the tail are optional, so accept a slightly short frame but never a long one.
  if (len < 17 || len > 31) return false;

  out.format = 5;

  int16_t tRaw = rd_i16be(d, 3);
  if ((uint16_t)tRaw != 0x8000) out.temp_c = tRaw * 0.005f;

  uint16_t hRaw = rd_u16be(d, 5);
  if (hRaw != 0xFFFF) out.humidity_pct = hRaw * 0.0025f;

  uint16_t pRaw = rd_u16be(d, 7);
  if (pRaw != 0xFFFF) out.pressure_hpa = (pRaw + 50000u) / 100.0f;

  int16_t ax = rd_i16be(d, 9), ay = rd_i16be(d, 11), az = rd_i16be(d, 13);
  if ((uint16_t)ax != 0x8000) out.accel_x_mg = ax;
  if ((uint16_t)ay != 0x8000) out.accel_y_mg = ay;
  if ((uint16_t)az != 0x8000) out.accel_z_mg = az;

  uint16_t power = rd_u16be(d, 15);     // 11 bits battery mV-offset | 5 bits TX
  uint16_t vbat  = power >> 5;
  uint16_t txp   = power & 0x1F;
  if (vbat != 0x7FF) out.battery_mv  = (uint16_t)(vbat + 1600);
  if (txp  != 0x1F)  out.tx_power_dbm = (int)txp * 2 - 40;

  if (len > 17) out.movement_count = d[17];
  if (len > 19) out.sequence       = rd_u16be(d, 18);

  // No extra range guard: the registered Ruuvi company id (0x0499) plus the
  // format byte already authenticate the frame, and each field carries its own
  // "invalid" sentinel (handled above), so a corrupt reading surfaces as NAN
  // rather than as a plausible-looking wrong number.
  out.present = true;
  return true;
}

// Decode the legacy Data Format 3 (RAWv1) payload (14 data bytes; no sentinels,
// no TX power / sequence). Battery is a plain big-endian millivolt word.
inline bool decodeFormat3(const uint8_t* d, size_t len, Reading& out) {
  if (len < 16 || len > 20) return false;   // company id (2) + 14 data bytes

  out.format = 3;
  out.humidity_pct = d[3] * 0.5f;

  // Temperature: byte 4 is the integer part with the sign in bit 7; byte 5 is
  // the fractional part in hundredths.
  float t = (d[4] & 0x7F) + d[5] / 100.0f;
  out.temp_c = (d[4] & 0x80) ? -t : t;

  out.pressure_hpa = (rd_u16be(d, 6) + 50000u) / 100.0f;
  out.accel_x_mg   = rd_i16be(d, 8);
  out.accel_y_mg   = rd_i16be(d, 10);
  out.accel_z_mg   = rd_i16be(d, 12);
  out.battery_mv   = rd_u16be(d, 14);

  out.present = true;
  return true;
}

// Decode a RuuviTag manufacturer-data advertisement. `d` must include the 2-byte
// company id. Returns false (leaving out.present false) for a foreign company id,
// an unknown/unsupported data format, or a too-short/implausible frame.
inline bool decode(const uint8_t* d, size_t len, Reading& out) {
  out = Reading{};
  if (d == nullptr || len < 3) return false;
  uint16_t company = (uint16_t)((uint16_t)d[0] | ((uint16_t)d[1] << 8));
  if (company != COMPANY_ID) return false;

  switch (d[2]) {
    case 0x05: return decodeFormat5(d, len, out);
    case 0x03: return decodeFormat3(d, len, out);
    default:   return false;   // DF2/DF4 (Eddystone-URL) carry no sensor data
  }
}

}  // namespace ruuvi
