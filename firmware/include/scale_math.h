// scale_math.h — pure sample-set statistics and validity rules for 24-bit
// bridge-ADC (NAU7802 / HX711) readings. Arduino-free so the host test suite
// (firmware/host_test/) exercises the exact production filter.
//
// Filter choice (documented per the I2C-hardening spec): a *trimmed mean
// anchored on the median*. The sample set is sorted, the lowest and highest
// SCALE_TRIM_EACH_SIDE samples are discarded, and the remainder is averaged.
// This is cheap (one insertion sort of ≤32 longs), needs no floating point,
// kills single-sample glitches (SPS spikes, a residual conversion from the
// previous input channel) and — unlike a plain mean — cannot be dragged to a
// rail by one corrupted conversion. The spread of the *trimmed* set doubles as
// the stability metric: a spread above the caller's threshold marks the whole
// reading invalid instead of averaging noise into a plausible-looking weight.
#pragma once

#include <stdint.h>
#include <stddef.h>

namespace scalemath {

// A 24-bit bridge ADC rails to within a few counts of its signed full scale
// (±8388607) when the differential input is open (no load cell wired). Exactly
// 0 is what a dead/absent chip's "conversion" would decode to. Neither is a
// real weight.
constexpr long ADC24_FULLSCALE_MARGIN = 8388600L;  // ~0x7FFFF8 (2^23 - 8)

// Samples dropped from each end of the sorted set before averaging.
constexpr uint8_t SCALE_TRIM_EACH_SIDE = 2;

inline bool rawIsRailedOrZero(long raw) {
  return raw == 0 || raw >= ADC24_FULLSCALE_MARGIN || raw <= -ADC24_FULLSCALE_MARGIN;
}

struct FilterResult {
  bool ok        = false;  // enough samples, no rails, spread within threshold
  long value     = 0;      // trimmed mean (only meaningful when ok)
  long median    = 0;
  long spread    = 0;      // max-min of the trimmed set (stability metric)
  bool railed    = false;  // at least one sample at/near a rail or exactly 0
  bool unstable  = false;  // trimmed spread exceeded maxSpread
  bool tooFew    = false;  // fewer samples than needed to trim + average
};

// Filter `n` raw samples in place-copy (n <= 32). Requires enough samples to
// trim both sides and still average at least 3; the caller enforces the *full*
// requested count upstream — this guard only keeps the math well-defined.
inline FilterResult filterSamples(const long* samples, uint8_t n, long maxSpread) {
  FilterResult r;
  if (n == 0 || n > 32 || n < (uint8_t)(2 * SCALE_TRIM_EACH_SIDE + 3)) {
    r.tooFew = true;
    return r;
  }

  long s[32];
  for (uint8_t i = 0; i < n; i++) s[i] = samples[i];
  // Insertion sort — n is tiny (≤32).
  for (uint8_t i = 1; i < n; i++) {
    long v = s[i];
    int8_t j = (int8_t)(i - 1);
    while (j >= 0 && s[j] > v) { s[j + 1] = s[j]; j--; }
    s[j + 1] = v;
  }

  for (uint8_t i = 0; i < n; i++) {
    if (rawIsRailedOrZero(s[i])) { r.railed = true; }
  }

  const uint8_t lo = SCALE_TRIM_EACH_SIDE;
  const uint8_t hi = (uint8_t)(n - SCALE_TRIM_EACH_SIDE);  // exclusive
  int64_t sum = 0;
  for (uint8_t i = lo; i < hi; i++) sum += s[i];
  r.value  = (long)(sum / (hi - lo));
  r.median = s[n / 2];
  r.spread = s[hi - 1] - s[lo];
  r.unstable = (maxSpread > 0) && (r.spread > maxSpread);

  r.ok = !r.railed && !r.unstable && !rawIsRailedOrZero(r.value);
  return r;
}

// Calibration factor sanity: finite, nonzero, and within a plausible magnitude
// for counts-per-kg on a 24-bit ADC. (|factor| of 1 would mean 1 count = 1 kg —
// nonsense for any real load cell; 10 million would exceed full scale for 2 kg.)
inline bool factorPlausible(float factor) {
  if (!(factor == factor)) return false;                      // NaN
  if (factor == 0.0f) return false;
  float mag = factor < 0 ? -factor : factor;
  if (mag > 3.4e37f) return false;                            // inf-ish
  return mag >= 10.0f && mag <= 10000000.0f;
}

}  // namespace scalemath
