// scale_bus.cpp — checked HX711 + NAU7802(+TCA9548A) reads for the scale
// registry. See scale_bus.h for the fail-closed contract. The low-level
// transaction code lives in nau7802_checked.h (host-tested with a mock bus).
#include "scale_bus.h"
#include "globals.h"
#include "config.h"
#include "i2c_bus.h"
#include "nau7802_checked.h"
#include "scale_math.h"

#if ENABLE_HX711
#include <HX711.h>
#endif

namespace scalebus {

using hivecfg::ScaleBackend;
using hivecfg::ScaleChannel;

namespace {

Diag gDiag;

bool gMuxPresent = false;

// One state slot per physical NAU7802 chip: index 0..7 = mux channel, 8 =
// direct (no mux). Reset each begin() so a fresh wake re-configures everything.
constexpr int CHIP_SLOTS = 9;
bool gInited[CHIP_SLOTS];
bool gNeedsReinit[CHIP_SLOTS];
bool gCh2Used[CHIP_SLOTS];       // any registry channel uses CH2 on this chip
bool gCalOk[CHIP_SLOTS][2];      // per-ADC-channel AFE calibration succeeded

int chipKey(const ScaleChannel& ch) {
  return (ch.muxChannel < 0) ? 8 : (int)ch.muxChannel;  // validated 0..7 upstream
}

nauchk::TcaMux& mux() {
  static nauchk::TcaMux m(i2cbus::iface(), TCA9548A_I2C_ADDRESS);
  return m;
}

nauchk::Nau7802Checked& nau() {
  static nauchk::Nau7802Checked n(i2cbus::iface(), NAU7802_I2C_ADDRESS);
  return n;
}

// Route the bus to the chip that owns `ch`: enable exactly its mux channel, or
// disable every channel for a chip wired directly on the main bus. Fails
// closed: false means the route is NOT verified and no NAU7802 transaction may
// follow — otherwise a stale mux selection could attribute one hive's chip to
// another hive. On failure no channel is left enabled (best-effort clear).
bool routeTo(const ScaleChannel& ch) {
#if ENABLE_I2C_MUX
  if (ch.muxChannel >= 0) {
    if (!gMuxPresent) return false;   // mux-configured hive but no mux found
    if (!mux().select((uint8_t)ch.muxChannel)) {
      gDiag.muxSelectFailures++;
      return false;
    }
    return true;
  }
  if (gMuxPresent) {
    if (!mux().disableAll()) {
      gDiag.muxDisableFailures++;
      mux().bestEffortClear();
      return false;
    }
  }
  return true;
#else
  return ch.muxChannel < 0;   // no mux support compiled in
#endif
}

void unroute() {
#if ENABLE_I2C_MUX
  if (!gMuxPresent) return;
  if (!mux().disableAll()) {
    gDiag.muxDisableFailures++;
    mux().bestEffortClear();
  }
#endif
}

const char* chipName(int key) {
  static char buf[16];
  if (key == 8) snprintf(buf, sizeof(buf), "main-bus");
  else          snprintf(buf, sizeof(buf), "mux ch%d", key);
  return buf;
}

#if ENABLE_NAU7802
// Deliberate per-chip bring-up, every step checked. The bus must already be
// routed to the chip. Marks gInited/gCalOk and returns overall success.
bool initChip(int key) {
  gInited[key] = false;
  gCalOk[key][0] = gCalOk[key][1] = false;

  if (!nau().present()) {
    Serial.printf("[SCALEBUS] NAU7802 (%s, addr 0x%02X): no ACK\n",
                  chipName(key), NAU7802_I2C_ADDRESS);
    gDiag.nauInitFailures++;
    return false;
  }
  if (!nau().configure(gCh2Used[key])) {
    Serial.printf("[SCALEBUS] NAU7802 (%s): configuration FAILED\n", chipName(key));
    gDiag.nauInitFailures++;
    return false;
  }

  // AFE-calibrate every ADC channel the registry uses on this chip, then
  // restore CH1 as the resting default. Failures are recorded per channel.
  bool ok = true;
  if (!nau().selectChannel(1) || !nau().calibrateAfe()) {
    Serial.printf("[SCALEBUS] NAU7802 (%s): CH1 AFE calibration FAILED\n", chipName(key));
    gDiag.nauCalCh1Failures++;
    ok = false;
  } else {
    gCalOk[key][0] = true;
  }
  if (gCh2Used[key]) {
    if (!nau().selectChannel(2) || !nau().calibrateAfe()) {
      Serial.printf("[SCALEBUS] NAU7802 (%s): CH2 AFE calibration FAILED\n", chipName(key));
      gDiag.nauCalCh2Failures++;
      ok = false;
    } else {
      gCalOk[key][1] = true;
    }
    if (!nau().selectChannel(1)) {
      gDiag.nauChannelSwitchFailures++;
      ok = false;
    }
  }

  gInited[key] = ok;
  gNeedsReinit[key] = !ok;
  if (ok) Serial.printf("[SCALEBUS] NAU7802 (%s): initialized (CH2 %s)\n",
                        chipName(key), gCh2Used[key] ? "in use, input cap off"
                                                     : "unused, input cap on");
  return ok;
}

// One measurement attempt on an already-routed, initialized chip. Fills the
// sample/stability fields of `r`; returns true when `r.ok` could be set.
bool attemptRead(const ScaleChannel& ch, ReadResult& r) {
  if (!nau().selectChannel(ch.adcChannel)) {
    r.channelSwitchFailed = true;
    gDiag.nauChannelSwitchFailures++;
    return false;
  }

  // Discard enough conversions to flush the previous input's pipeline, then
  // require the FULL sample count (strict policy — partial sets are never
  // averaged into a weight).
  nauchk::SampleResult s =
      nau().collect(NAU7802_SAMPLES, NAU7802_SETTLE_DISCARD, NAU7802_READ_TIMEOUT_MS);
  r.samplesRequested = s.requested;
  r.samplesAcquired  = s.acquired;
  r.commErrors      += s.commErrors;
  r.timedOut         = s.timedOut;
  gDiag.commErrors  += s.commErrors;
  if (s.timedOut) gDiag.timeouts++;
  if (!s.ok) {
    if (s.acquired < s.requested) gDiag.incompleteSampleSets++;
    return false;
  }

  scalemath::FilterResult f =
      scalemath::filterSamples(s.samples, s.acquired, NAU7802_MAX_SPREAD_COUNTS);
  r.raw      = f.value;
  r.spread   = f.spread;
  r.railed   = f.railed;
  r.unstable = f.unstable;
  r.ok       = f.ok;
  return true;
}
#endif  // ENABLE_NAU7802

}  // namespace

bool muxPresent() { return gMuxPresent; }

bool muxSelectChecked(uint8_t channel) {
#if ENABLE_I2C_MUX
  if (!gMuxPresent) return false;
  if (!mux().select(channel)) { gDiag.muxSelectFailures++; return false; }
  return true;
#else
  (void)channel;
  return false;
#endif
}

bool muxDisableChecked() {
#if ENABLE_I2C_MUX
  if (!gMuxPresent) return true;
  if (!mux().disableAll()) { gDiag.muxDisableFailures++; mux().bestEffortClear(); return false; }
  return true;
#else
  return true;
#endif
}

bool nauPresentOnBus() {
#if ENABLE_NAU7802
  return i2cbus::ok() && nau().present();
#else
  return false;
#endif
}

void begin() {
  for (int i = 0; i < CHIP_SLOTS; i++) {
    gInited[i] = false;
    gNeedsReinit[i] = false;
    gCh2Used[i] = false;
    gCalOk[i][0] = gCalOk[i][1] = false;
  }
  gMuxPresent = false;

  if (!i2cbus::ok()) {
    Serial.println("[SCALEBUS] I2C bus unavailable; skipping scale initialization (reads fail closed)");
    return;
  }

#if ENABLE_I2C_MUX
  gMuxPresent = mux().present();
  Serial.printf("[SCALEBUS] TCA9548A (0x%02X) %s\n",
                TCA9548A_I2C_ADDRESS, gMuxPresent ? "detected" : "absent");
#endif

#if ENABLE_NAU7802
  // Pass 1: which ADC channels does the registry use on each physical chip?
  // (Needed before configuration: the CH2 input cap must be disabled on chips
  // whose CH2 carries a load cell.)
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802 || !ch.valid()) continue;
      if (ch.adcChannel == 2) gCh2Used[chipKey(ch)] = true;
    }
  }

  // Pass 2: bring up each unique chip exactly once via checked routing.
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802 || !ch.valid()) continue;
      int key = chipKey(ch);
      if (gInited[key]) continue;
      if (!routeTo(ch)) {
        Serial.printf("[SCALEBUS] NAU7802 (%s): route FAILED (hive %u) — chip unreachable\n",
                      chipName(key), hive.index);
        continue;
      }
      initChip(key);
    }
  }
  unroute();
#endif
}

ReadResult readChannel(const ScaleChannel& ch) {
  ReadResult r;

  if (!ch.valid()) {           // includes configError-flagged channels
    r.notConfigured = true;
    return r;
  }

#if ENABLE_HX711
  if (ch.backend == ScaleBackend::HX711) {
    HX711& hx = (ch.hxIndex == 0) ? scale1 : scale2;
    r.samplesRequested = NAU7802_SAMPLES;
    // 1 s is ample at the HX711's 10/80 SPS; bounds a sweep of many channels
    // when an amp is unpopulated.
    if (!hx.wait_ready_timeout(1000)) {
      Serial.printf("[SCALEBUS] HX711 #%u not ready (hive channel unread)\n", ch.hxIndex + 1);
      r.timedOut = true;
      gDiag.timeouts++;
      return r;
    }
    long samples[nauchk::MAX_SAMPLES];
    uint8_t got = 0;
    for (uint8_t i = 0; i < NAU7802_SAMPLES; i++) {
      if (!hx.wait_ready_timeout(200)) break;
      samples[got++] = hx.read();
    }
    r.samplesAcquired = got;
    if (got < NAU7802_SAMPLES) {
      gDiag.incompleteSampleSets++;
      return r;
    }
    scalemath::FilterResult f =
        scalemath::filterSamples(samples, got, NAU7802_MAX_SPREAD_COUNTS);
    r.raw = f.value; r.spread = f.spread;
    r.railed = f.railed; r.unstable = f.unstable;
    r.ok = f.ok;
    return r;
  }
#endif

#if ENABLE_NAU7802
  if (ch.backend == ScaleBackend::NAU7802) {
    if (!i2cbus::ok()) { r.notConfigured = true; return r; }
    if (ch.muxChannel < -1 || ch.muxChannel > 7 ||
        (ch.adcChannel != 1 && ch.adcChannel != 2)) {
      // Defense in depth — validateRegistry() should have flagged this already.
      r.notConfigured = true;
      return r;
    }
    int key = chipKey(ch);

    if (!routeTo(ch)) {
      r.routeFailed = true;
      // In the long-lived provisioning/calibration modes a wedged bus can be
      // recovered without a reboot; strictly bounded (see i2cbus).
      if ((provisioningActive || calibrationModeActive) && i2cbus::runtimeRecover()) {
        begin();  // re-detect mux + re-init chips after the bus came back
        if (!routeTo(ch)) { unroute(); return r; }
        r.routeFailed = false;
      } else {
        unroute();
        return r;
      }
    }

    // (Re)initialize the chip if needed — at most one bounded attempt here.
    if (!gInited[key] || gNeedsReinit[key]) {
      r.recoveryAttempted = true;
      gDiag.recoveryAttempts++;
      if (!initChip(key)) {
        gDiag.recoveryFailures++;
        r.notConfigured = true;
        unroute();
        return r;
      }
      r.recoverySucceeded = true;
    }

    if (!attemptRead(ch, r)) {
      // Transaction-level failure: mark the chip for reinitialization and try
      // ONE in-call recovery (unless this call already did a recovery).
      gNeedsReinit[key] = true;
      if (!r.recoveryAttempted) {
        r.recoveryAttempted = true;
        gDiag.recoveryAttempts++;
        if (initChip(key)) {
          r.recoverySucceeded = true;
          ReadResult retry;
          if (attemptRead(ch, retry)) {
            retry.recoveryAttempted = true;
            retry.recoverySucceeded = true;
            retry.commErrors += r.commErrors;
            unroute();
            return retry;
          }
          r.channelSwitchFailed |= retry.channelSwitchFailed;
          r.timedOut |= retry.timedOut;
          r.commErrors += retry.commErrors;
          gNeedsReinit[key] = true;
        } else {
          gDiag.recoveryFailures++;
        }
      }
      unroute();
      return r;   // invalid reading, provenance preserved
    }

    unroute();
    return r;
  }
#endif

  r.notConfigured = true;
  return r;
}

bool powerDownAllForSleep() {
  bool allOk = true;
#if ENABLE_NAU7802
  if (!i2cbus::ok()) return false;
  bool done[CHIP_SLOTS] = {false};
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802 || !ch.valid()) continue;
      int key = chipKey(ch);
      if (done[key]) continue;
      done[key] = true;
      if (!routeTo(ch)) {
        Serial.printf("[SCALEBUS] power-down: NAU7802 (%s) unreachable (route failed)\n",
                      chipName(key));
        gDiag.powerDownFailures++;
        allOk = false;
        continue;
      }
      if (!nau().powerDown()) {
        Serial.printf("[SCALEBUS] power-down: NAU7802 (%s) write FAILED — sleep current may rise\n",
                      chipName(key));
        gDiag.powerDownFailures++;
        allOk = false;
      }
    }
  }
  unroute();
#endif
  return allOk;
}

Diag& diag() { return gDiag; }

void logDiag() {
  const Diag& d = gDiag;
#if ENABLE_I2C_MUX
  const nauchk::TcaMux::Diag& md = mux().diag();
  uint16_t muxVerify = md.verifyFailures;
#else
  uint16_t muxVerify = 0;
#endif
  if (d.muxSelectFailures || d.muxDisableFailures || muxVerify ||
      d.nauInitFailures || d.nauCalCh1Failures || d.nauCalCh2Failures ||
      d.nauChannelSwitchFailures || d.incompleteSampleSets || d.timeouts ||
      d.commErrors || d.recoveryAttempts || d.powerDownFailures) {
    Serial.printf("[SCALEBUS] Diag: muxSel=%u muxDis=%u muxVerify=%u nauInit=%u cal1=%u cal2=%u "
                  "chSwitch=%u partial=%u timeout=%u commErr=%u recover=%u/%u pwrDown=%u\n",
                  d.muxSelectFailures, d.muxDisableFailures, muxVerify,
                  d.nauInitFailures, d.nauCalCh1Failures, d.nauCalCh2Failures,
                  d.nauChannelSwitchFailures, d.incompleteSampleSets, d.timeouts,
                  d.commErrors, d.recoveryFailures, d.recoveryAttempts,
                  d.powerDownFailures);
  }
}

}  // namespace scalebus
