// test_ble_lanes.cpp — host-level tests for the per-hive BLE pairing lanes.
//
// Exercises the EXACT production rules (ble_lanes.h) that hiveFromJson() applies
// on every NVS load and every portal save. The rule that matters: a HiveInside
// beacon and a HiveTraffic counter are DIFFERENT lanes and must be able to share
// one hive — they were mutually exclusive while a hive had a single in-hive slot,
// even though each writes its own nested object in the measurement payload.
// No Arduino toolchain needed. Build & run:
//   g++ -std=gnu++17 -I../include -o test_ble_lanes test_ble_lanes.cpp && ./test_ble_lanes
#include <cstdio>
#include <string>
#include <vector>

#include "ble_lanes.h"

static int gFailures = 0;
static int gChecks = 0;

#define CHECK(cond) do { \
    gChecks++; \
    if (!(cond)) { gFailures++; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

using blelanes::Lane;
using blelanes::LaneSet;
using blelanes::laneFor;

// Model of hiveFromJson()'s pairing loop: pairings are offered in stored order,
// a wired scale pre-claims the scale lane and a DS18B20 pre-claims the beacon
// lane. Returns the types that were ACCEPTED, in order.
static std::vector<std::string> accepted(const std::vector<std::string>& pairings,
                                         bool wiredScale = false,
                                         bool dsRom      = false) {
  LaneSet lanes;
  if (wiredScale) lanes.claim(Lane::Scale);
  if (dsRom)      lanes.claim(Lane::Beacon);

  std::vector<std::string> out;
  for (const std::string& t : pairings) {
    if (out.size() >= blelanes::LANE_COUNT) break;   // MAX_BLE_PER_HIVE
    if (!lanes.claim(t.c_str())) continue;
    out.push_back(t);
  }
  return out;
}

static bool has(const std::vector<std::string>& v, const char* want) {
  for (const std::string& s : v) if (s == want) return true;
  return false;
}

int main() {
  // ── Lane classification ────────────────────────────────────────────────────
  CHECK(laneFor("holyiot")          == Lane::Beacon);
  CHECK(laneFor("ruuvitag")         == Lane::Beacon);
  CHECK(laneFor("hiveinside_nrf54") == Lane::Beacon);
  CHECK(laneFor("beecounter")       == Lane::Counter);
  CHECK(laneFor("hiveheart")        == Lane::Heart);
  CHECK(laneFor("hivescale")        == Lane::Scale);

  // Unknown/absent types must not win a free extra slot: they land in the beacon
  // lane, exactly where an unrecognised type sat under the single-slot rule.
  CHECK(laneFor("")                 == Lane::Beacon);
  CHECK(laneFor(nullptr)            == Lane::Beacon);
  CHECK(laneFor("something_new")    == Lane::Beacon);

  CHECK(blelanes::isScaleSource("hivescale"));
  CHECK(!blelanes::isScaleSource("beecounter"));

  // ── The regression this change fixes ───────────────────────────────────────
  // HiveInside + HiveTraffic on one hive: both must survive.
  {
    auto a = accepted({"hiveinside_nrf54", "beecounter"});
    CHECK(a.size() == 2);
    CHECK(has(a, "hiveinside_nrf54"));
    CHECK(has(a, "beecounter"));
  }
  // Order must not matter.
  {
    auto a = accepted({"beecounter", "hiveinside_nrf54"});
    CHECK(a.size() == 2);
    CHECK(has(a, "hiveinside_nrf54"));
    CHECK(has(a, "beecounter"));
  }

  // ── Three in-hive sensors, one per lane ────────────────────────────────────
  {
    auto a = accepted({"hiveinside_nrf54", "beecounter", "hiveheart"});
    CHECK(a.size() == blelanes::INHIVE_LANE_COUNT);
    CHECK(a.size() == 3);
  }
  // ...plus a wireless HiveScale as the scale source = four stored pairings.
  {
    auto a = accepted({"hivescale", "hiveinside_nrf54", "beecounter", "hiveheart"});
    CHECK(a.size() == blelanes::LANE_COUNT);
    CHECK(a.size() == 4);
    CHECK(has(a, "hivescale"));
  }

  // ── Same-lane duplicates are still rejected, first one wins ────────────────
  {
    // All three beacon formats decode into the one hives[].ble object.
    auto a = accepted({"holyiot", "ruuvitag", "hiveinside_nrf54"});
    CHECK(a.size() == 1);
    CHECK(a[0] == "holyiot");
  }
  {
    auto a = accepted({"beecounter", "beecounter"});
    CHECK(a.size() == 1);
  }
  {
    auto a = accepted({"hiveheart", "hiveheart"});
    CHECK(a.size() == 1);
  }
  {
    // An unknown type collides with a beacon rather than escaping the limit.
    auto a = accepted({"holyiot", "something_new"});
    CHECK(a.size() == 1);
    CHECK(a[0] == "holyiot");
  }

  // ── A wired scale keeps the hive's single scale source ─────────────────────
  {
    auto a = accepted({"hivescale"}, /*wiredScale=*/true);
    CHECK(a.empty());
  }
  {
    // ...but never blocks an in-hive sensor.
    auto a = accepted({"hivescale", "beecounter"}, /*wiredScale=*/true);
    CHECK(a.size() == 1);
    CHECK(a[0] == "beecounter");
  }

  // ── A DS18B20 occupies the beacon lane only ────────────────────────────────
  {
    auto a = accepted({"hiveinside_nrf54"}, /*wiredScale=*/false, /*dsRom=*/true);
    CHECK(a.empty());   // probe and beacon are the same temperature slot
  }
  {
    // The counter and HiveHeart are unaffected by a wired probe — the exact
    // combination the old "DS18B20 blocks every BLE pairing" rule threw away.
    auto a = accepted({"hiveinside_nrf54", "beecounter", "hiveheart"},
                      /*wiredScale=*/false, /*dsRom=*/true);
    CHECK(a.size() == 2);
    CHECK(has(a, "beecounter"));
    CHECK(has(a, "hiveheart"));
    CHECK(!has(a, "hiveinside_nrf54"));
  }

  // ── inHiveCount() never counts the scale source ────────────────────────────
  {
    LaneSet lanes;
    CHECK(lanes.inHiveCount() == 0);
    lanes.claim(Lane::Scale);
    CHECK(lanes.inHiveCount() == 0);
    lanes.claim(Lane::Beacon);
    lanes.claim(Lane::Counter);
    lanes.claim(Lane::Heart);
    CHECK(lanes.inHiveCount() == 3);
    CHECK(lanes.taken(Lane::Counter));
    CHECK(!lanes.claim(Lane::Counter));
  }

  std::printf("%s: %d checks, %d failures\n",
              gFailures ? "FAIL" : "PASS", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
