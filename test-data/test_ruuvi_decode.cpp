// test_ruuvi_decode.cpp — host unit test for the RuuviTag advertisement decoder.
// Builds with plain g++ (no Arduino / NimBLE), so it runs in CI and on a dev box:
//
//   g++ -std=c++17 -I firmware/include test-data/test_ruuvi_decode.cpp -o /tmp/t && /tmp/t
//
// The payloads are Ruuvi's own published Data Format 5 (RAWv2) test vectors plus
// a legacy Data Format 3 (RAWv1) example. Each buffer is prefixed with the Ruuvi
// company id (0x0499, little-endian) exactly as it appears in the manufacturer
// data, since ruuvi::decode() verifies that header itself.
#include "ruuvi_decode.h"

#include <cstdio>
#include <cmath>

static int g_failures = 0;

static void approx(const char* what, float got, float want, float tol) {
  bool ok = std::fabs(got - want) <= tol;
  std::printf("  %-14s got=%-10.4f want=%-10.4f %s\n", what, got, want, ok ? "OK" : "FAIL");
  if (!ok) g_failures++;
}

static void eqi(const char* what, long got, long want) {
  bool ok = got == want;
  std::printf("  %-14s got=%-10ld want=%-10ld %s\n", what, got, want, ok ? "OK" : "FAIL");
  if (!ok) g_failures++;
}

static void isnanf(const char* what, float got) {
  bool ok = std::isnan(got);
  std::printf("  %-14s got=%-10.4f want=NaN      %s\n", what, got, ok ? "OK" : "FAIL");
  if (!ok) g_failures++;
}

int main() {
  // ── Data Format 5 (RAWv2) — Ruuvi's "valid data" published test vector ──────
  // 99 04 = company id (LE); 05 = format; then the 24-byte payload.
  const uint8_t df5_valid[] = {
    0x99, 0x04,
    0x05, 0x12, 0xFC, 0x53, 0x94, 0xC3, 0x7C, 0x00, 0x04, 0xFF, 0xFC, 0x04,
    0x0C, 0xAC, 0x36, 0x42, 0x00, 0xCD, 0xCB, 0xB8, 0x33, 0x4C, 0x88, 0x4F
  };
  ruuvi::Reading r;
  std::printf("RuuviTag DF5 (valid):\n");
  if (!ruuvi::decode(df5_valid, sizeof(df5_valid), r)) { std::printf("  decode FAILED\n"); return 1; }
  eqi("format", r.format, 5);
  approx("temp_c", r.temp_c, 24.30f, 0.001f);
  approx("humidity_pct", r.humidity_pct, 53.49f, 0.001f);
  approx("pressure_hpa", r.pressure_hpa, 1000.44f, 0.001f);
  approx("accel_x_mg", r.accel_x_mg, 4.0f, 0.001f);
  approx("accel_y_mg", r.accel_y_mg, -4.0f, 0.001f);
  approx("accel_z_mg", r.accel_z_mg, 1036.0f, 0.001f);
  eqi("battery_mv", r.battery_mv, 2977);
  eqi("tx_power_dbm", r.tx_power_dbm, 4);
  eqi("movement_count", r.movement_count, 66);
  eqi("sequence", r.sequence, 205);
  eqi("battery_pct", ruuvi::batteryPercent(r.battery_mv), 98);  // (2977-2000)/10

  // ── Data Format 5 — Ruuvi's "maximum values" vector ─────────────────────────
  const uint8_t df5_max[] = {
    0x99, 0x04,
    0x05, 0x7F, 0xFF, 0xFF, 0xFE, 0xFF, 0xFE, 0x7F, 0xFF, 0x7F, 0xFF, 0x7F,
    0xFF, 0xFF, 0xDE, 0xFE, 0xFF, 0xFE, 0xCB, 0xB8, 0x33, 0x4C, 0x88, 0x4F
  };
  std::printf("RuuviTag DF5 (max):\n");
  if (!ruuvi::decode(df5_max, sizeof(df5_max), r)) { std::printf("  decode FAILED\n"); return 1; }
  approx("temp_c", r.temp_c, 163.835f, 0.01f);
  approx("humidity_pct", r.humidity_pct, 163.8350f, 0.01f);
  approx("pressure_hpa", r.pressure_hpa, 1155.34f, 0.01f);
  approx("accel_z_mg", r.accel_z_mg, 32767.0f, 0.5f);
  eqi("battery_mv", r.battery_mv, 3646);

  // ── Data Format 5 — Ruuvi's "invalid values" vector (all sentinels) ─────────
  const uint8_t df5_inval[] = {
    0x99, 0x04,
    0x05, 0x80, 0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0x80, 0x00, 0x80, 0x00, 0x80,
    0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF
  };
  std::printf("RuuviTag DF5 (invalid sentinels):\n");
  if (!ruuvi::decode(df5_inval, sizeof(df5_inval), r)) { std::printf("  decode FAILED\n"); return 1; }
  isnanf("temp_c", r.temp_c);
  isnanf("humidity_pct", r.humidity_pct);
  isnanf("pressure_hpa", r.pressure_hpa);
  isnanf("accel_x_mg", r.accel_x_mg);
  eqi("battery_mv", r.battery_mv, 0);
  eqi("battery_pct", ruuvi::batteryPercent(r.battery_mv), -1);

  // ── Data Format 3 (RAWv1) — legacy example ──────────────────────────────────
  // humidity 0x3F (=31.5%), temp +21.45 C, pressure 0xC3FC+50000=1001.72 hPa,
  // accel (1000,1255,1510) mg, battery 0x0A8B = 2699 mV.
  const uint8_t df3[] = {
    0x99, 0x04,
    0x03, 0x3F, 0x15, 0x2D, 0xC3, 0xFC, 0x03, 0xE8, 0x04, 0xE7, 0x05, 0xE6,
    0x0A, 0x8B
  };
  std::printf("RuuviTag DF3:\n");
  if (!ruuvi::decode(df3, sizeof(df3), r)) { std::printf("  decode FAILED\n"); return 1; }
  eqi("format", r.format, 3);
  approx("humidity_pct", r.humidity_pct, 31.5f, 0.01f);
  approx("temp_c", r.temp_c, 21.45f, 0.01f);
  approx("pressure_hpa", r.pressure_hpa, 1001.72f, 0.01f);
  approx("accel_x_mg", r.accel_x_mg, 1000.0f, 0.5f);
  approx("accel_y_mg", r.accel_y_mg, 1255.0f, 0.5f);
  approx("accel_z_mg", r.accel_z_mg, 1510.0f, 0.5f);
  eqi("battery_mv", r.battery_mv, 2699);

  // ── Foreign company id is rejected ──────────────────────────────────────────
  const uint8_t foreign[] = { 0xFF, 0xFF, 0x05, 0x12, 0xFC };
  std::printf("Foreign company id:\n");
  eqi("rejected", ruuvi::decode(foreign, sizeof(foreign), r) ? 1 : 0, 0);

  std::printf("\n%s (%d failure%s)\n", g_failures ? "FAILURES" : "ALL PASS",
              g_failures, g_failures == 1 ? "" : "s");
  return g_failures ? 1 : 0;
}
