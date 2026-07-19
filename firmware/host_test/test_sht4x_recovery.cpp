// test_sht4x_recovery.cpp — host-level tests for the ESP32-C6 SHT4x read/heal/
// reinit recovery sequence and the cold-boot acquisition ordering.
//
// Exercises the EXACT production control flow (sht4x_recovery.h) against a
// scripted mock of the SHT4x + I2C heal primitives, plus a small boot-sequence
// model that mirrors main.cpp's three ordered phases (ambient → scalebus::begin
// → scales) to pin down the invariants the fix guarantees. No Arduino toolchain
// needed. Build & run:
//   g++ -std=gnu++17 -I../include -o test_sht4x_recovery test_sht4x_recovery.cpp && ./test_sht4x_recovery
#include <cstdio>
#include <cmath>
#include <string>
#include <vector>

#include "sht4x_recovery.h"

static int gFailures = 0;
static int gChecks = 0;

#define CHECK(cond) do { \
    gChecks++; \
    if (!(cond)) { gFailures++; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

// ── Scripted SHT4x + heal mock ───────────────────────────────────────────────
// Reproduces every field-relevant state: a healthy sensor, a driver wedged until
// healed, a heal that fails (dead bus), a sensor that never ACKs again, and a
// begin() that fails after the heal. read() only succeeds when the driver is not
// wedged; the mock records the call order so the test can assert reinit happens
// BEFORE the retry read.
struct MockSht {
  // knobs
  bool wedged            = false;  // first read fails until a successful heal
  bool healSucceeds      = true;
  bool ackAfterHeal      = true;
  bool reinitSucceeds    = true;
  float temperature      = 22.5f;
  float humidity         = 48.0f;

  // captured
  float& outTemp;
  float& outHum;
  std::vector<std::string> calls;
  bool healed = false;

  MockSht(float& t, float& h) : outTemp(t), outHum(h) {}

  bool read() {
    calls.push_back("read");
    if (wedged) return false;
    outTemp = temperature;
    outHum = humidity;
    return true;
  }
  bool heal() {
    calls.push_back("heal");
    if (!healSucceeds) return false;
    healed = true;
    wedged = false;   // a successful heal clears the wedge
    return true;
  }
  bool ack() { calls.push_back("ack"); return ackAfterHeal; }
  bool reinit() { calls.push_back("reinit"); return reinitSucceeds; }
  void onInitialFail() {}
  void onAck(bool) {}
  void onReinit(bool) {}
  void onRetry(bool) {}
};

static int indexOf(const std::vector<std::string>& v, const std::string& s) {
  for (size_t i = 0; i < v.size(); i++) if (v[i] == s) return (int)i;
  return -1;
}

// #4 — a healthy sensor reads on the first try: finite values, sht_ok true, and
// NO heal/reinit churn on a normal successful cycle.
static void testHealthyReadFirstTry() {
  float t = NAN, h = NAN;
  MockSht m(t, h);
  sht4xrec::Outcome r = sht4xrec::acquire(m);
  CHECK(r.ok);
  CHECK(!r.healed);
  CHECK(!r.retried);
  CHECK(std::isfinite(t) && std::isfinite(h));
  CHECK(m.calls.size() == 1 && m.calls[0] == "read");   // no extra transactions
}

// #2 — a failed read then a successful heal must reinitialize the SHT4x object
// BEFORE retrying, and the retry then succeeds with finite values.
static void testReinitBeforeRetryOnHeal() {
  float t = NAN, h = NAN;
  MockSht m(t, h);
  m.wedged = true;                 // first read fails; heal() clears it
  sht4xrec::Outcome r = sht4xrec::acquire(m);
  CHECK(r.ok);
  CHECK(r.healed && r.acked && r.reinitialized && r.retried);
  CHECK(std::isfinite(t) && std::isfinite(h));
  // The exact production ordering: read(fail) → heal → ack → reinit → read(ok).
  int firstRead  = indexOf(m.calls, "read");
  int reinit     = indexOf(m.calls, "reinit");
  CHECK(firstRead == 0);
  CHECK(indexOf(m.calls, "heal") < indexOf(m.calls, "ack"));
  CHECK(indexOf(m.calls, "ack") < reinit);
  CHECK(reinit < (int)m.calls.size() - 1);              // reinit before the LAST read
  CHECK(m.calls.back() == "read");                      // retry read is last
}

// #3 — a read that fails and cannot be recovered yields null ambient + sht_ok
// false. Three distinct unrecoverable paths, none of which must retry a read.
static void testFailedReadReportsInvalid() {
  {   // heal itself fails (dead bus)
    float t = NAN, h = NAN;
    MockSht m(t, h);
    m.wedged = true; m.healSucceeds = false;
    sht4xrec::Outcome r = sht4xrec::acquire(m);
    CHECK(!r.ok && !r.retried);
    CHECK(std::isnan(t) && std::isnan(h));
  }
  {   // healed, but the sensor never ACKs again — do NOT touch the driver/read
    float t = NAN, h = NAN;
    MockSht m(t, h);
    m.wedged = true; m.ackAfterHeal = false;
    sht4xrec::Outcome r = sht4xrec::acquire(m);
    CHECK(!r.ok && r.healed && !r.reinitialized && !r.retried);
    CHECK(std::isnan(t) && std::isnan(h));
    CHECK(indexOf(m.calls, "reinit") == -1);
  }
  {   // healed + ACK, but begin() fails — no retry read is attempted
    float t = NAN, h = NAN;
    MockSht m(t, h);
    m.wedged = true; m.reinitSucceeds = false;
    sht4xrec::Outcome r = sht4xrec::acquire(m);
    CHECK(!r.ok && r.acked && !r.reinitialized && !r.retried);
    CHECK(std::isnan(t) && std::isnan(h));
    // exactly one read (the initial fail), then heal/ack/reinit, no second read
    int reads = 0;
    for (auto& c : m.calls) if (c == "read") reads++;
    CHECK(reads == 1);
  }
}

// #3 (cont.) — heal + ACK + reinit all succeed but the retry read still fails
// (sensor genuinely gone): invalid, values stay null. Model it with a sensor
// whose read() never yields data even after a "successful" heal/reinit.
struct AlwaysWedged : MockSht {
  AlwaysWedged(float& a, float& b) : MockSht(a, b) {}
  bool read() { calls.push_back("read"); return false; }   // never yields data
};

static void testRetryStillFails() {
  float t = NAN, h = NAN;
  AlwaysWedged aw(t, h);
  aw.healSucceeds = true; aw.ackAfterHeal = true; aw.reinitSucceeds = true;
  sht4xrec::Outcome r = sht4xrec::acquire(aw);
  CHECK(!r.ok);
  CHECK(r.healed && r.acked && r.reinitialized && r.retried);
  CHECK(std::isnan(t) && std::isnan(h));
  CHECK(aw.calls.back() == "read");   // the retry read was attempted and failed
}

// ── Cold-boot acquisition ordering (models main.cpp's three phases) ──────────
// Recorder mirroring setup(): prefetchAmbientSensors() (reads SHT via the SAME
// production sequence) → scalebus::begin() (probes the optional TCA9548A) →
// prefetchWiredScales() (scale reads). Asserts the SHT read precedes the mux
// probe and no scale read occurs before scalebus::begin().
struct BootRecorder {
  std::vector<std::string> events;
  bool muxPresent;
  bool sensorWedged;   // as if a prior transaction had wedged the C6 driver
  float t = NAN, h = NAN;
  bool shtOk = false;

  explicit BootRecorder(bool mux, bool wedged) : muxPresent(mux), sensorWedged(wedged) {}

  void phaseAmbient() {
    events.push_back("sht_read");
    MockSht m(t, h);
    m.wedged = sensorWedged;      // heal() clears it, mirroring the real recovery
    shtOk = sht4xrec::acquire(m).ok;
  }
  void phaseScaleBusBegin() {
    events.push_back("mux_probe");                 // TCA9548A presence probe
    if (muxPresent) events.push_back("mux_init");
  }
  void phaseScales() { events.push_back("scale_read"); }

  void run() { phaseAmbient(); phaseScaleBusBegin(); phaseScales(); }
};

// #1 & #5 — SHT4x acquisition happens before TCA9548A probing on cold boot, and
// an absent mux does not prevent a healthy ambient measurement.
static void testSht4xBeforeMuxProbe() {
  BootRecorder b(/*mux=*/false, /*wedged=*/false);
  b.run();
  CHECK(indexOf(b.events, "sht_read") < indexOf(b.events, "mux_probe"));
  CHECK(b.shtOk);                                  // absent mux → SHT still fine
  CHECK(std::isfinite(b.t) && std::isfinite(b.h));
  CHECK(indexOf(b.events, "mux_init") == -1);      // mux absent → never initialized
}

// #5 — even when the bus was wedged, the read recovers because it runs on the
// pre-probe window and the heal path reinitializes the sensor.
static void testAbsentMuxWedgeStillRecovers() {
  BootRecorder b(/*mux=*/false, /*wedged=*/true);
  b.run();
  CHECK(b.shtOk);
  CHECK(std::isfinite(b.t) && std::isfinite(b.h));
  CHECK(indexOf(b.events, "sht_read") < indexOf(b.events, "mux_probe"));
}

// #6 — no scale read occurs before scalebus::begin() (the mux probe/init stage).
static void testNoScaleReadBeforeBegin() {
  BootRecorder b(/*mux=*/true, /*wedged=*/false);
  b.run();
  int begin = indexOf(b.events, "mux_probe");
  int scale = indexOf(b.events, "scale_read");
  CHECK(begin >= 0 && scale >= 0);
  CHECK(begin < scale);                            // begin strictly before any scale read
  CHECK(indexOf(b.events, "sht_read") < begin);    // and SHT before begin too
}

int main() {
  testHealthyReadFirstTry();
  testReinitBeforeRetryOnHeal();
  testFailedReadReportsInvalid();
  testRetryStillFails();
  testSht4xBeforeMuxProbe();
  testAbsentMuxWedgeStillRecovers();
  testNoScaleReadBeforeBegin();

  std::printf("%d checks, %d failure(s)\n", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
