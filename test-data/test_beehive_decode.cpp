// test_beehive_decode.cpp — host unit test for the beehivemonitoring.com
// HiveHeart / HiveScale notification decoders. Builds with plain g++ (no
// Arduino / NimBLE), so it runs in CI and on a dev box:
//
//   g++ -std=c++17 -I firmware/include test-data/test_beehive_decode.cpp -o /tmp/t && /tmp/t
//
// The two payloads are the real captures used to validate the byte layout.
#include "beehive_decode.h"

#include <cstdio>
#include <cmath>
#include <cstdlib>

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

int main() {
  // Heart 1 capture (20 bytes).
  const uint8_t heart[] = {
    0xBE, 0x9B, 0xEE, 0x00, 0x89, 0x86, 0xF3, 0xD0, 0x29, 0x00,
    0x00, 0x00, 0x6E, 0xFB, 0x7B, 0x53, 0x44, 0x34, 0x63, 0x24
  };
  bhgatt::HeartReading h;
  std::printf("HiveHeart:\n");
  if (!bhgatt::decodeHeart(heart, sizeof(heart), h)) { std::printf("  decode FAILED\n"); return 1; }
  approx("battery_v", h.battery_v, 2.806f, 0.01f);
  approx("humidity_pct", h.humidity_pct, 52.5f, 0.2f);
  approx("temp_c", h.temp_c, 24.3f, 0.05f);
  approx("frequency_hz", h.frequency_hz, 66.9f, 0.05f);
  eqi("energy", h.energy, 0);
  eqi("peak", h.peak, 0);
  eqi("fft_present", h.fft_present ? 1 : 0, 1);
  // Raw FFT is payload bytes 12–19, preserved verbatim (the server decodes the
  // packed nibbles). Assert all 8 bytes from the real capture above.
  const uint8_t want_fft[8] = { 0x6E, 0xFB, 0x7B, 0x53, 0x44, 0x34, 0x63, 0x24 };
  for (int i = 0; i < 8; i++) {
    char label[16];
    std::snprintf(label, sizeof(label), "fft[%d]", i);
    eqi(label, h.fft[i], want_fft[i]);
  }

  // Scale 1 capture (15 bytes).
  const uint8_t scale[] = {
    0x01, 0x1F, 0x3B, 0x01, 0xCC, 0x72, 0xE2, 0x00, 0x00, 0x68,
    0x00, 0x26, 0x12, 0x00, 0x00
  };
  bhgatt::ScaleReading s;
  std::printf("HiveScale:\n");
  if (!bhgatt::decodeScale(scale, sizeof(scale), s)) { std::printf("  decode FAILED\n"); return 1; }
  approx("battery_v", s.battery_v, 4.10f, 0.01f);
  approx("humidity_pct", s.humidity_pct, 44.7f, 0.2f);
  approx("temp_c", s.temp_c, 22.6f, 0.05f);
  approx("pressure_hpa", s.pressure_hpa, 1000.0f, 0.05f);
  approx("weight_kg", s.weight_kg, 1.04f, 0.005f);
  eqi("raw_weight", s.raw_weight, 4646);

  // Negative-value sanity: a 12-bit temperature of 0xFFF is -0.1 C.
  bhgatt::HeartReading n;
  const uint8_t cold[] = { 0,0,0,0, 0x80, 0x80, 0xFF, 0x0F, 0,0,0,0 };
  bhgatt::decodeHeart(cold, sizeof(cold), n);
  approx("neg_temp_c", n.temp_c, -0.1f, 0.001f);

  std::printf("\n%s (%d failure%s)\n", g_failures ? "FAILURES" : "ALL PASS",
              g_failures, g_failures == 1 ? "" : "s");
  return g_failures ? 1 : 0;
}
