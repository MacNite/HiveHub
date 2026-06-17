// beehive_decode.h — pure decoders for beehivemonitoring.com GATT sensors.
//
// HiveHeart (in-hive) and HiveScale (weight) both stream their state as a single
// GATT notification on characteristic 513849EB-913D-4F80-8C44-3F0685533D6E of
// service 0D01C3B8-EFF2-44BC-9260-3256EB957268. The byte layout below was
// reverse-engineered from beehivemonitoring's own HiveGateway decoder and
// validated against real captures:
//
//   Heart (20 B): BE 9B EE 00 | 89 86 F3 D0 29 00 00 00 | 6E FB 7B 53 44 34 63 24
//                 -> V=2.81  H=52.5%  T=24.3C  f=66.9Hz  energy=0  peak=0
//   Scale (15 B): 01 1F 3B 01 | CC 72 E2 00 00 68 00 26 12 00 00
//                 -> V=4.10  H=44.7%  T=22.6C  P=1000.0hPa  W=1.04kg  raw=4646
//
// Bytes 0..3 are a header/timestamp ("Time" in HiveGateway); the sensor fields
// start at byte 4. This header is deliberately free of Arduino / NimBLE deps so
// it can be unit-tested on the host (see tests/test_beehive_decode.cpp).
#pragma once

#include <stdint.h>
#include <stddef.h>
#include <math.h>

namespace bhgatt {

struct HeartReading {
  bool  present      = false;
  float temp_c       = NAN;
  float humidity_pct = NAN;
  float battery_v    = NAN;
  float frequency_hz = NAN;
  long  energy       = 0;
  int   peak         = 0;
  uint8_t fft[8]     = {0};
  bool  fft_present  = false;
};

struct ScaleReading {
  bool  present      = false;
  float temp_c       = NAN;
  float humidity_pct = NAN;
  float pressure_hpa = NAN;
  float battery_v    = NAN;
  float weight_kg    = NAN;
  long  raw_weight   = 0;
};

// Sign-extend the low `bits` of `v` to a signed int (e.g. the 12-bit temperature
// fields use the <<20>>20 trick in the original C#).
inline int signExtend(uint32_t v, unsigned bits) {
  const uint32_t mask = 1u << (bits - 1);
  return (int)((v ^ mask) - mask);
}

// Decode a HiveHeart notification. Returns false (and leaves `out.present`
// false) when the payload is too short for the mandatory fields (bytes 4..11).
inline bool decodeHeart(const uint8_t* p, size_t len, HeartReading& out) {
  if (p == nullptr || len < 12) return false;

  // Battery: the longer (FFT-bearing) payload uses a different scale.
  if (len > 11)
    out.battery_v = (2000.0f + p[4] * 1500.0f / 255.0f) / 1000.0f;
  else
    out.battery_v = (2500.0f + p[4] * 1000.0f / 255.0f) / 1000.0f;

  out.humidity_pct = p[5] * 100.0f / 255.0f;

  uint32_t tRaw = (uint32_t)p[6] | ((uint32_t)(p[7] & 0x0F) << 8);
  out.temp_c = signExtend(tRaw, 12) / 10.0f;

  uint32_t fRaw = (uint32_t)(p[7] >> 4) | ((uint32_t)p[8] << 4) | ((uint32_t)(p[9] & 0x03) << 12);
  out.frequency_hz = fRaw / 10.0f;

  out.energy = ((uint32_t)p[9] >> 2) | ((uint32_t)p[10] << 6);
  out.peak   = p[11];

  if (len >= 20) {
    for (int i = 0; i < 8; i++) out.fft[i] = p[12 + i];
    out.fft_present = true;
  }

  out.present = true;
  return true;
}

// Decode a HiveScale notification. Returns false when the payload is too short
// for the mandatory fields (bytes 4..13).
inline bool decodeScale(const uint8_t* p, size_t len, ScaleReading& out) {
  if (p == nullptr || len < 14) return false;

  out.battery_v    = (2500.0f + p[4] * 2000.0f / 255.0f) / 1000.0f;
  out.humidity_pct = p[5] * 100.0f / 255.0f;

  uint32_t tRaw = (uint32_t)p[6] | ((uint32_t)(p[7] & 0x0F) << 8);
  out.temp_c = signExtend(tRaw, 12) / 10.0f;

  uint32_t pRaw = (uint32_t)(p[7] >> 4) | ((uint32_t)p[8] << 4);
  out.pressure_hpa = (10000 + signExtend(pRaw, 12)) / 10.0f;

  int16_t w = (int16_t)((uint16_t)p[9] | ((uint16_t)p[10] << 8));
  out.weight_kg = w / 100.0f;

  uint32_t rwRaw = (uint32_t)p[11] | ((uint32_t)p[12] << 8) | ((uint32_t)p[13] << 16);
  out.raw_weight = signExtend(rwRaw, 24);

  out.present = true;
  return true;
}

}  // namespace bhgatt
