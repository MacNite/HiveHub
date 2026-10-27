// gatt_rotation.h — a fair share of the per-wake GATT read budget.
//
// GATT sensors (HiveTraffic, HiveHeart, wireless HiveScale) are read serially,
// a connect → read → disconnect of seconds each, so every wake caps them at
// MAX_GATT_READS_PER_CYCLE attempts per module. The read loops used to start at
// the first registry entry every wake. With more paired sensors than the
// budget, the ones at the back were then never read at all — not "skipped this
// wake", but on every wake, forever — and a dead sensor near the front spent a
// slot on every wake too.
//
// So each module keeps a cursor across deep sleep (RTC memory) and starts where
// the previous wake stopped. With n candidates and a budget of b, every sensor
// is attempted at least once every ceil(n / b) wakes, whatever the others do.
//
// Plain integer logic with no Arduino dependency, so host_test/
// test_gatt_rotation.cpp can check it on a host compiler.
#pragma once

#include <stdint.h>

namespace gattrot {

struct Plan {
  uint8_t start = 0;  // index of the first candidate to read this wake
  uint8_t count = 0;  // how many candidates to read, starting there
};

// Plan this wake's reads over `n` candidates (in registry order) with `budget`
// attempts, resuming at `cursor` (the value next() returned last wake). The
// candidate list may have changed size since: the cursor is simply wrapped.
inline Plan plan(uint32_t cursor, uint8_t n, uint8_t budget) {
  Plan p;
  if (n == 0) return p;
  p.start = (uint8_t)(cursor % n);
  p.count = n < budget ? n : budget;
  return p;
}

// The candidate index of the k-th read (0 <= k < p.count).
inline uint8_t at(const Plan& p, uint8_t k, uint8_t n) {
  uint16_t i = (uint16_t)p.start + k;
  return (uint8_t)(i >= n ? i - n : i);
}

// The cursor to store for the next wake.
inline uint32_t next(const Plan& p, uint8_t n) {
  if (n == 0) return 0;
  return ((uint32_t)p.start + p.count) % n;
}

}  // namespace gattrot
