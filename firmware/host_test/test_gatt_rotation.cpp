// test_gatt_rotation.cpp — host-level tests for the GATT read-budget rotation.
//
// Exercises the EXACT production helper (gatt_rotation.h) that the HiveTraffic
// and HiveHeart/HiveScale read loops use to share MAX_GATT_READS_PER_CYCLE. The
// property that matters is the one the old loops lacked: with more paired
// sensors than the budget, every sensor is still read within ceil(n / b) wakes,
// instead of the ones at the back of the registry never being read at all.
//
// No Arduino toolchain needed. Build & run:
//   g++ -std=gnu++17 -I../include -o test_gatt_rotation test_gatt_rotation.cpp && ./test_gatt_rotation
#include <cstdio>
#include <vector>

#include "gatt_rotation.h"

static int gFailures = 0;
static int gChecks = 0;

#define CHECK(cond) do { \
    gChecks++; \
    if (!(cond)) { gFailures++; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

// Simulate `wakes` wake cycles; return how often each candidate was read.
static std::vector<int> simulate(uint8_t n, uint8_t budget, int wakes, uint32_t cursor = 0) {
  std::vector<int> reads(n, 0);
  for (int w = 0; w < wakes; w++) {
    gattrot::Plan p = gattrot::plan(cursor, n, budget);
    for (uint8_t k = 0; k < p.count; k++) reads[gattrot::at(p, k, n)]++;
    cursor = gattrot::next(p, n);
  }
  return reads;
}

int main() {
  // ── No candidates: nothing to read, and no division by zero ──────────────
  {
    gattrot::Plan p = gattrot::plan(7, 0, 4);
    CHECK(p.count == 0);
    CHECK(gattrot::next(p, 0) == 0);
  }

  // ── Under budget: everything every wake, cursor stays put ────────────────
  {
    std::vector<int> r = simulate(3, 4, 5);
    for (int c : r) CHECK(c == 5);
  }

  // ── Exactly at budget ────────────────────────────────────────────────────
  {
    std::vector<int> r = simulate(4, 4, 3);
    for (int c : r) CHECK(c == 3);
  }

  // ── The audit case: a fifth counter used to be skipped on every wake ─────
  {
    std::vector<int> r = simulate(5, 4, 1);
    CHECK(r[4] == 0);  // first wake reads 0..3
    r = simulate(5, 4, 2);
    CHECK(r[4] == 1);  // the second wake starts at the fifth
  }

  // ── Every candidate within ceil(n / b) wakes, for every size up to 36 ───
  for (uint8_t n = 1; n <= 36; n++) {
    for (uint8_t b = 1; b <= 8; b++) {
      int window = (n + b - 1) / b;
      for (uint32_t start = 0; start < n; start++) {
        std::vector<int> r = simulate(n, b, window, start);
        for (int c : r) CHECK(c >= 1);
      }
      // Over many wakes the reads spread evenly (no candidate more than one
      // read ahead of another).
      std::vector<int> r = simulate(n, b, 100);
      int lo = r[0], hi = r[0];
      for (int c : r) { if (c < lo) lo = c; if (c > hi) hi = c; }
      CHECK(hi - lo <= 1);
    }
  }

  // ── A stale cursor after the registry shrank is wrapped, not trusted ─────
  {
    gattrot::Plan p = gattrot::plan(17, 5, 4);
    CHECK(p.start == 2);
    CHECK(gattrot::at(p, 3, 5) == 0);  // wraps past the end
    CHECK(gattrot::next(p, 5) == 1);
  }

  std::printf("%d checks, %d failures\n", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
