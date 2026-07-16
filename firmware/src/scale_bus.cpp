// scale_bus.cpp — checked HX711 + NAU7802(+TCA9548A) reads for the scale
// registry. See scale_bus.h for the fail-closed contract.
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

constexpr int CHIP_SLOTS = 9;  // mux 0..7, direct main-bus chip at 8

enum class ChipState : uint8_t {
  Unknown,
  Absent,
  Ready,
  Faulted,
};

ChipState gState[CHIP_SLOTS];
bool gNeedsReinit[CHIP_SLOTS];
bool gCh2Used[CHIP_SLOTS];
bool gCalOk[CHIP_SLOTS][2];

int chipKey(const ScaleChannel& ch) {
  return (ch.muxChannel < 0) ? 8 : (int)ch.muxChannel;
}

nauchk::TcaMux& mux() {
  static nauchk::TcaMux m(i2cbus::iface(), TCA9548A_I2C_ADDRESS);
  return m;
}

nauchk::Nau7802Checked& nau() {
  static nauchk::Nau7802Checked n(i2cbus::iface(), NAU7802_I2C_ADDRESS);
  return n;
}

bool routeTo(const ScaleChannel& ch) {
#if ENABLE_I2C_MUX
  if (ch.muxChannel >= 0) {
    if (!gMuxPresent) return false;
    if (!mux().select((uint8_t)ch.muxChannel)) {
      gDiag.muxSelectFailures++;
      return false;
    }
    return true;
  }
  if (gMuxPresent && !mux().disableAll()) {
    gDiag.muxDisableFailures++;
    mux().bestEffortClear();
    return false;
  }
  return true;
#else
  return ch.muxChannel < 0;
#endif
}

void unroute() {
#if ENABLE_I2C_MUX
  if (gMuxPresent && !mux().disableAll()) {
    gDiag.muxDisableFailures++;
    mux().bestEffortClear();
  }
#endif
}

const char* chipName(int key) {
  static char buf[16];
  if (key == 8) snprintf(buf, sizeof(buf), "main-bus");
  else snprintf(buf, sizeof(buf), "mux ch%d", key);
  return buf;
}

#if ENABLE_NAU7802
bool initChip(int key, bool allowAbsentProbe) {
  gCalOk[key][0] = gCalOk[key][1] = false;

  // ESP32-C6 Arduino-ESP32 3.x I2C-NG can enter ESP_ERR_INVALID_STATE after
  // repeated transactions to an absent device. Once begin() has observed a
  // clean NACK, do not probe that address again in the same wake cycle.
  if (gState[key] == ChipState::Absent && !allowAbsentProbe) return false;

  if (!nau().present()) {
    gState[key] = ChipState::Absent;
    gNeedsReinit[key] = false;
    Serial.printf("[SCALEBUS] NAU7802 (%s, addr 0x%02X): no ACK; suppressing further probes this boot\n",
                  chipName(key), NAU7802_I2C_ADDRESS);
    gDiag.nauInitFailures++;
    return false;
  }

  gState[key] = ChipState::Faulted;
  if (!nau().configure(gCh2Used[key])) {
    Serial.printf("[SCALEBUS] NAU7802 (%s): configuration FAILED\n", chipName(key));
    gNeedsReinit[key] = true;
    gDiag.nauInitFailures++;
    return false;
  }

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

  gState[key] = ok ? ChipState::Ready : ChipState::Faulted;
  gNeedsReinit[key] = !ok;
  if (ok) {
    Serial.printf("[SCALEBUS] NAU7802 (%s): initialized (CH2 %s)\n",
                  chipName(key), gCh2Used[key] ? "in use, input cap off"
                                               : "unused, input cap on");
  }
  return ok;
}

bool attemptRead(const ScaleChannel& ch, ReadResult& r) {
  if (!nau().selectChannel(ch.adcChannel)) {
    r.channelSwitchFailed = true;
    gDiag.nauChannelSwitchFailures++;
    return false;
  }

  nauchk::SampleResult s =
      nau().collect(NAU7802_SAMPLES, NAU7802_SETTLE_DISCARD, NAU7802_READ_TIMEOUT_MS);
  r.samplesRequested = s.requested;
  r.samplesAcquired = s.acquired;
  r.commErrors += s.commErrors;
  r.timedOut = s.timedOut;
  gDiag.commErrors += s.commErrors;
  if (s.timedOut) gDiag.timeouts++;
  if (!s.ok) {
    if (s.acquired < s.requested) gDiag.incompleteSampleSets++;
    return false;
  }

  scalemath::FilterResult f =
      scalemath::filterSamples(s.samples, s.acquired, NAU7802_MAX_SPREAD_COUNTS);
  r.raw = f.value;
  r.spread = f.spread;
  r.railed = f.railed;
  r.unstable = f.unstable;
  r.ok = f.ok;
  return true;
}
#endif

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
  if (!mux().disableAll()) {
    gDiag.muxDisableFailures++;
    mux().bestEffortClear();
    return false;
  }
#endif
  return true;
}

bool nauPresentOnBus() {
#if ENABLE_NAU7802
  // The direct chip's absence is authoritative for this boot. Avoid turning a
  // portal scan into another transaction against the known-missing address.
  if (gState[8] == ChipState::Absent) return false;
  return i2cbus::ok() && nau().present();
#else
  return false;
#endif
}

void begin() {
  for (int i = 0; i < CHIP_SLOTS; i++) {
    gState[i] = ChipState::Unknown;
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
  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802 || !ch.valid()) continue;
      if (ch.adcChannel == 2) gCh2Used[chipKey(ch)] = true;
    }
  }

  for (uint8_t h = 0; h < hivecfg::gHiveCount; h++) {
    const hivecfg::Hive& hive = hivecfg::gHives[h];
    for (uint8_t s = 0; s < hive.scaleCount; s++) {
      const ScaleChannel& ch = hive.scales[s];
      if (ch.backend != ScaleBackend::NAU7802 || !ch.valid()) continue;
      int key = chipKey(ch);
      if (gState[key] != ChipState::Unknown) continue;
      if (!routeTo(ch)) {
        Serial.printf("[SCALEBUS] NAU7802 (%s): route FAILED (hive %u) — chip unreachable\n",
                      chipName(key), hive.index);
        gState[key] = ChipState::Faulted;
        continue;
      }
      initChip(key, true);
    }
  }
  unroute();
#endif
}

ReadResult readChannel(const ScaleChannel& ch) {
  ReadResult r;
  if (!ch.valid()) { r.notConfigured = true; return r; }

#if ENABLE_HX711
  if (ch.backend == ScaleBackend::HX711) {
    HX711& hx = (ch.hxIndex == 0) ? scale1 : scale2;
    r.samplesRequested = NAU7802_SAMPLES;
    if (!hx.wait_ready_timeout(1000)) {
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
    r.raw = f.value;
    r.spread = f.spread;
    r.railed = f.railed;
    r.unstable = f.unstable;
    r.ok = f.ok;
    return r;
  }
#endif

#if ENABLE_NAU7802
  if (ch.backend == ScaleBackend::NAU7802) {
    if (!i2cbus::ok()) { r.notConfigured = true; return r; }
    if (ch.muxChannel < -1 || ch.muxChannel > 7 ||
        (ch.adcChannel != 1 && ch.adcChannel != 2)) {
      r.notConfigured = true;
      return r;
    }

    int key = chipKey(ch);
    if (gState[key] == ChipState::Absent) {
      r.notConfigured = true;
      return r;
    }

    if (!routeTo(ch)) {
      r.routeFailed = true;
      if ((provisioningActive || calibrationModeActive) && i2cbus::runtimeRecover()) {
        begin();
        if (gState[key] == ChipState::Absent || !routeTo(ch)) {
          unroute();
          r.notConfigured = gState[key] == ChipState::Absent;
          return r;
        }
        r.routeFailed = false;
      } else {
        unroute();
        return r;
      }
    }

    if (gState[key] != ChipState::Ready || gNeedsReinit[key]) {
      r.recoveryAttempted = true;
      gDiag.recoveryAttempts++;
      if (!initChip(key, false)) {
        gDiag.recoveryFailures++;
        r.notConfigured = true;
        unroute();
        return r;
      }
      r.recoverySucceeded = true;
    }

    if (!attemptRead(ch, r)) {
      gState[key] = ChipState::Faulted;
      gNeedsReinit[key] = true;
      if (!r.recoveryAttempted) {
        r.recoveryAttempted = true;
        gDiag.recoveryAttempts++;
        if (initChip(key, false)) {
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
          gState[key] = ChipState::Faulted;
          gNeedsReinit[key] = true;
        } else {
          gDiag.recoveryFailures++;
        }
      }
      unroute();
      return r;
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

      // An absent chip cannot consume conversion current and must not be probed
      // again merely to power it down.
      if (gState[key] == ChipState::Absent) continue;
      if (gState[key] != ChipState::Ready) {
        allOk = false;
        continue;
      }
      if (!routeTo(ch)) {
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
