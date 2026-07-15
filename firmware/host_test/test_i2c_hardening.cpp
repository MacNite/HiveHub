// test_i2c_hardening.cpp — host-level tests for HiveHub's checked I2C layer.
//
// Runs the EXACT production transaction/validation code (i2c_iface.h,
// nau7802_checked.h, scale_math.h, scale_topology.h) against a scripted mock
// bus (mock_i2c.h) so every failure mode is injected deterministically:
// failed mux writes, failed/wrong readbacks, a previous channel staying
// active, failed NAU channel switches, short reads, partial sample sets,
// timeouts, railed values, excessive variance, and every invalid-topology
// configuration. Build & run:
//   g++ -std=gnu++17 -I../include -o test_i2c_hardening test_i2c_hardening.cpp && ./test_i2c_hardening
#include <cstdio>
#include <cstdlib>
#include <cmath>

#include "i2c_iface.h"
#include "nau7802_checked.h"
#include "scale_math.h"
#include "scale_topology.h"
#include "mock_i2c.h"

static int gFailures = 0;
static int gChecks = 0;

#define CHECK(cond) do { \
    gChecks++; \
    if (!(cond)) { gFailures++; std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
  } while (0)

// ── TCA9548A mux hardening ───────────────────────────────────────────────────

static void testMuxChannelValidation() {
  MockBus bus;
  nauchk::TcaMux mux(bus, 0x70);
  CHECK(!mux.select(8));                    // out of range — rejected before any write
  CHECK(bus.muxWrites == 0);
  CHECK(!mux.select(255));                  // (=-1 / overflowed uint8) also rejected
  CHECK(mux.select(3));                     // valid channel works
  CHECK(bus.muxControl == (1 << 3));
  CHECK(mux.disableAll());
  CHECK(bus.muxControl == 0);
}

static void testMuxFailedWrite() {
  MockBus bus;
  bus.muxFailWrite = true;
  nauchk::TcaMux mux(bus, 0x70);
  CHECK(!mux.select(2));                    // NACKed write must fail the select
  CHECK(mux.diag().selectFailures == 1);
  bus.muxFailWrite = false;
  CHECK(mux.select(2));
  bus.muxFailWrite = true;
  CHECK(!mux.disableAll());                 // NACKed disable must be reported too
}

static void testMuxFailedReadback() {
  MockBus bus;
  bus.muxFailReadback = true;
  nauchk::TcaMux mux(bus, 0x70);
  CHECK(!mux.select(1));                    // write ACKed but unverifiable — fail
  CHECK(mux.diag().verifyFailures == 1);
}

static void testMuxPreviousChannelStaysActive() {
  // The write is ACKed but the control register never changes (glitched part,
  // hot-unplugged wiring): the readback must catch that the PREVIOUS channel is
  // still active and refuse, otherwise hive A's chip would answer hive B's read.
  MockBus bus;
  nauchk::TcaMux mux(bus, 0x70);
  CHECK(mux.select(0));
  bus.muxWriteIgnored = true;
  CHECK(!mux.select(5));                    // readback shows ch0, not ch5 — fail
  CHECK(mux.diag().verifyFailures >= 1);
  // ...and the failure path clears the control register (best-effort disable
  // was also ignored here, but a functioning part would have been cleared).
  bus.muxWriteIgnored = false;
  CHECK(mux.disableAll());
  CHECK(bus.muxControl == 0);
}

// ── NAU7802 checked driver ───────────────────────────────────────────────────

static MockBus healthyBusWithDirectNau() {
  MockBus bus;
  bus.haveDirect = true;
  bus.direct.present = true;
  return bus;
}

static void testNauConfigureAndCalibrate() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.present());
  CHECK(nau.configure(/*ch2Used=*/true));
  CHECK((bus.direct.regs[0x1C] & 0x80) == 0);       // CH2 in use -> input cap OFF
  CHECK(nau.configure(/*ch2Used=*/false));
  CHECK((bus.direct.regs[0x1C] & 0x80) != 0);       // CH2 unused -> cap ON
  CHECK(nau.calibrateAfe());
  bus.direct.failCalibration = true;
  CHECK(!nau.calibrateAfe());                       // CAL_ERR must be detected
}

static void testNauFailedChannelSwitch() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.configure(true));

  bus.direct.failChannelSwitchWrite = true;
  CHECK(!nau.selectChannel(2));                     // NACKed write — no switch

  bus.direct.failChannelSwitchWrite = false;
  bus.direct.liePersistCh1 = true;
  CHECK(!nau.selectChannel(2));                     // write "ok" but readback says CH1

  bus.direct.liePersistCh1 = false;
  CHECK(nau.selectChannel(2));                      // now verified
  CHECK(!nau.selectChannel(3));                     // invalid ADC channel rejected
}

static void testNauExactByteCount() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.configure(false));
  long v = 0;
  CHECK(nau.readConversion(v));
  CHECK(v == 100000);
  bus.direct.shortReadAfterN = 0;                   // every read now returns 1 byte
  CHECK(!nau.readConversion(v));                    // short read must FAIL, not zero
}

static void testNauPartialSampleSet() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.configure(false));
  bus.direct.shortReadAfterN = 8;                   // 8 good conversions, then short reads
  nauchk::SampleResult r = nau.collect(15, 0, 1000);
  CHECK(!r.ok);                                     // partial set is NEVER ok
  CHECK(r.acquired < r.requested);
  CHECK(r.commErrors > 0);
}

static void testNauTimeout() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.configure(false));
  bus.direct.stuckNotReady = true;                  // CR never sets
  uint32_t start = bus.nowMs;
  nauchk::SampleResult r = nau.collect(15, 4, 1000);
  CHECK(!r.ok);
  CHECK(r.timedOut);
  CHECK(r.acquired == 0);
  CHECK(bus.nowMs - start <= 1100);                 // bounded — no infinite loop
}

static void testNauAbsentChip() {
  MockBus bus;                                      // no chip anywhere
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(!nau.present());
  CHECK(!nau.configure(false));                     // every step checked — fails fast
  long v;
  CHECK(!nau.readConversion(v));
}

static void testNauPowerDownChecked() {
  MockBus bus = healthyBusWithDirectNau();
  nauchk::Nau7802Checked nau(bus, 0x2A);
  CHECK(nau.configure(false));
  CHECK(nau.powerDown());
  CHECK((bus.direct.regs[0x00] & 0x06) == 0);       // PUD/PUA cleared
  bus.direct.present = false;
  CHECK(!nau.powerDown());                          // failure is reported, not ignored
}

// ── Sample filter / validity rules ───────────────────────────────────────────

static void testFilterRails() {
  long railed[15];
  for (int i = 0; i < 15; i++) railed[i] = 100000;
  railed[7] = 8388607;                              // one sample at +rail
  scalemath::FilterResult f = scalemath::filterSamples(railed, 15, 20000);
  CHECK(f.railed);
  CHECK(!f.ok);

  long zeros[15] = {0};                             // dead-chip zeros
  f = scalemath::filterSamples(zeros, 15, 20000);
  CHECK(!f.ok);

  long negRail[15];
  for (int i = 0; i < 15; i++) negRail[i] = -8388605;
  f = scalemath::filterSamples(negRail, 15, 20000);
  CHECK(f.railed);
  CHECK(!f.ok);
}

static void testFilterVariance() {
  long noisy[15];
  for (int i = 0; i < 15; i++) noisy[i] = 100000 + (i % 2 ? 40000 : -40000);
  scalemath::FilterResult f = scalemath::filterSamples(noisy, 15, 20000);
  CHECK(f.unstable);
  CHECK(!f.ok);

  long calm[15];
  for (int i = 0; i < 15; i++) calm[i] = 100000 + (i % 3) * 20;   // tiny jitter
  f = scalemath::filterSamples(calm, 15, 20000);
  CHECK(f.ok);
  CHECK(f.value > 99900 && f.value < 100100);

  // Trimmed mean kills a single glitch that a plain mean would absorb.
  long glitch[15];
  for (int i = 0; i < 15; i++) glitch[i] = 100000;
  glitch[3] = 900000;
  f = scalemath::filterSamples(glitch, 15, 20000);
  CHECK(f.ok);
  CHECK(f.value == 100000);
}

static void testFilterTooFew() {
  long s[4] = {1, 2, 3, 4};
  scalemath::FilterResult f = scalemath::filterSamples(s, 4, 20000);
  CHECK(f.tooFew);
  CHECK(!f.ok);
}

static void testFactorPlausibility() {
  CHECK(scalemath::factorPlausible(-7050.0f));
  CHECK(scalemath::factorPlausible(7050.0f));
  CHECK(!scalemath::factorPlausible(0.0f));
  CHECK(!scalemath::factorPlausible(NAN));
  CHECK(!scalemath::factorPlausible(INFINITY));
  CHECK(!scalemath::factorPlausible(0.5f));         // 1 count = 2 kg — nonsense
  CHECK(!scalemath::factorPlausible(5.0e8f));       // beyond 24-bit usefulness
}

// ── Topology validation ──────────────────────────────────────────────────────

using scaletopo::Backend;
using scaletopo::ChanSpec;

static void testDuplicateScaleAssignment() {
  ChanSpec c[2] = {
    {1, Backend::NAU7802, 0, 2, 1},
    {2, Backend::NAU7802, 0, 2, 1},                 // same mux+adc as hive 1
  };
  uint16_t issues[2];
  scaletopo::TopologyResult r = scaletopo::validateChannels(c, 2, issues);
  CHECK(!r.ok);
  CHECK(issues[0] == scaletopo::CHAN_OK);           // first claim wins
  CHECK(issues[1] & scaletopo::CHAN_DUPLICATE);

  ChanSpec hx[2] = {
    {1, Backend::HX711, 0, -1, 1},                  // both hives on HX711 #0
    {2, Backend::HX711, 0, -1, 1},
  };
  scaletopo::validateChannels(hx, 2, issues);
  CHECK(issues[1] & scaletopo::CHAN_DUPLICATE);
}

static void testDirectPlusMuxRejected() {
  ChanSpec c[2] = {
    {1, Backend::NAU7802, 0, -1, 1},                // direct, CH1
    {2, Backend::NAU7802, 0, 3, 1},                 // muxed ch3, CH1
  };
  uint16_t issues[2];
  scaletopo::TopologyResult r = scaletopo::validateChannels(c, 2, issues);
  CHECK(!r.ok);
  CHECK(issues[0] & scaletopo::CHAN_DIRECT_MUX_CONFLICT);
  CHECK(issues[1] & scaletopo::CHAN_DIRECT_MUX_CONFLICT);
}

static void testInvalidMuxValues() {
  ChanSpec c[3] = {
    {1, Backend::NAU7802, 0, 8, 1},                 // 8: one past the last port
    {2, Backend::NAU7802, 0, -2, 1},                // -2: below "direct"
    {3, Backend::NAU7802, 0, 300, 1},               // overflowed garbage
  };
  uint16_t issues[3];
  scaletopo::TopologyResult r = scaletopo::validateChannels(c, 3, issues);
  CHECK(!r.ok);
  CHECK(issues[0] & scaletopo::CHAN_BAD_MUX);
  CHECK(issues[1] & scaletopo::CHAN_BAD_MUX);
  CHECK(issues[2] & scaletopo::CHAN_BAD_MUX);
}

static void testInvalidAdcAndHxValues() {
  ChanSpec c[3] = {
    {1, Backend::NAU7802, 0, 0, 3},                 // ADC 3 does not exist
    {2, Backend::NAU7802, 0, 0, 0},                 // ADC 0 neither
    {3, Backend::HX711,   5, -1, 1},                // HX711 index 5
  };
  uint16_t issues[3];
  scaletopo::TopologyResult r = scaletopo::validateChannels(c, 3, issues);
  CHECK(!r.ok);
  CHECK(issues[0] & scaletopo::CHAN_BAD_ADC);
  CHECK(issues[1] & scaletopo::CHAN_BAD_ADC);
  CHECK(issues[2] & scaletopo::CHAN_BAD_HX_INDEX);
}

static void testFullMuxTopologyAccepted() {
  // 8 chips × 2 channels = 16 muxed channels: the supported maximum, all valid.
  ChanSpec c[16];
  uint16_t issues[16];
  for (int i = 0; i < 16; i++) {
    c[i] = {(uint8_t)(i + 1), Backend::NAU7802, 0, (int16_t)(i / 2), (int16_t)(i % 2 + 1)};
  }
  scaletopo::TopologyResult r = scaletopo::validateChannels(c, 16, issues);
  CHECK(r.ok);
  CHECK(r.muxedNau == 16);
  CHECK(r.directNau == 0);
}

static void testHiveIndexValidation() {
  uint8_t good[3] = {1, 5, 18};
  uint8_t bad[3];
  CHECK(scaletopo::validateHiveIndices(good, 3, 18, bad));

  uint8_t dup[3] = {1, 2, 2};
  CHECK(!scaletopo::validateHiveIndices(dup, 3, 18, bad));
  CHECK(bad[2] == 1 && bad[1] == 0);

  uint8_t range[3] = {0, 19, 4};
  CHECK(!scaletopo::validateHiveIndices(range, 3, 18, bad));
  CHECK(bad[0] == 1 && bad[1] == 1 && bad[2] == 0);
}

int main() {
  testMuxChannelValidation();
  testMuxFailedWrite();
  testMuxFailedReadback();
  testMuxPreviousChannelStaysActive();
  testNauConfigureAndCalibrate();
  testNauFailedChannelSwitch();
  testNauExactByteCount();
  testNauPartialSampleSet();
  testNauTimeout();
  testNauAbsentChip();
  testNauPowerDownChecked();
  testFilterRails();
  testFilterVariance();
  testFilterTooFew();
  testFactorPlausibility();
  testDuplicateScaleAssignment();
  testDirectPlusMuxRejected();
  testInvalidMuxValues();
  testInvalidAdcAndHxValues();
  testFullMuxTopologyAccepted();
  testHiveIndexValidation();

  std::printf("%d checks, %d failure(s)\n", gChecks, gFailures);
  return gFailures ? 1 : 0;
}
