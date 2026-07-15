// nau7802_checked.h — HiveHub's checked NAU7802 + TCA9548A communication layer.
//
// Why not the SparkFun library alone? Its API blurs communication errors and
// data: a NACKed 8-bit register read returns 0xFF (a legal register value), a
// failed 24-bit conversion read returns 0, available() returns true on a failed
// status read, and begin() both duplicates configuration work and hides which
// step failed. For a device whose readings become billing-grade hive weights,
// every transaction here separates STATUS from DATA, verifies readbacks, and
// reports a structured result — so a bus fault can only ever produce an
// *invalid* reading, never a wrong one silently attributed to some hive.
//
// Header-only and Arduino-free (built on i2c_iface.h) so the host test suite
// (firmware/host_test/) drives the exact production transaction code with
// injected NACKs, short reads, wrong readbacks and stuck data-ready flags.
#pragma once

#include <stdint.h>
#include <stddef.h>
#include "i2c_iface.h"

namespace nauchk {

using hivei2c::I2cBusIface;
using hivei2c::readReg;
using hivei2c::readRegs;
using hivei2c::writeReg;
using hivei2c::probe;

// ── NAU7802 register map (datasheet §10) ────────────────────────────────────
constexpr uint8_t REG_PU_CTRL = 0x00;
constexpr uint8_t REG_CTRL1   = 0x01;
constexpr uint8_t REG_CTRL2   = 0x02;
constexpr uint8_t REG_ADCO_B2 = 0x12;   // conversion result, 24-bit big-endian
constexpr uint8_t REG_ADC     = 0x15;   // ADC / chopper control
constexpr uint8_t REG_PGA     = 0x1B;
constexpr uint8_t REG_PGA_PWR = 0x1C;

// PU_CTRL bits
constexpr uint8_t PU_RR    = 1 << 0;   // register reset
constexpr uint8_t PU_PUD   = 1 << 1;   // power up digital
constexpr uint8_t PU_PUA   = 1 << 2;   // power up analog
constexpr uint8_t PU_PUR   = 1 << 3;   // power-up ready (read-only)
constexpr uint8_t PU_CS    = 1 << 4;   // cycle start (begin conversions)
constexpr uint8_t PU_CR    = 1 << 5;   // cycle ready (conversion available)
constexpr uint8_t PU_AVDDS = 1 << 7;   // AVDD source = internal LDO

// CTRL1: [2:0] gain, [5:3] VLDO
constexpr uint8_t CTRL1_GAIN_128 = 0x07;
constexpr uint8_t CTRL1_VLDO_3V3 = 0x04;  // 4.5 - 0.3*n → 3.3 V

// CTRL2: [7] CHS (0=CH1, 1=CH2), [6:4] CRS sample rate, [2] CALS, [1:0] CALMOD
constexpr uint8_t CTRL2_CHS      = 1 << 7;
constexpr uint8_t CTRL2_CRS_80SPS= 0x03 << 4;
constexpr uint8_t CTRL2_CALS     = 1 << 2;
constexpr uint8_t CTRL2_CAL_ERR  = 1 << 3;

// PGA_PWR: [7] PGA_CAP_EN — connects the on-die 330 pF decoupling cap across
// the CHANNEL 2 inputs (Vin2P/Vin2N). Correct ONLY while channel 2 is unused;
// with a second load cell wired to CH2 the cap shorts its AC behaviour and
// corrupts readings, so configure() disables it whenever CH2 carries a scale.
constexpr uint8_t PGA_PWR_CAP_EN = 1 << 7;

// Bounded waits (ms).
constexpr uint32_t POWERUP_TIMEOUT_MS   = 200;
constexpr uint32_t CAL_AFE_TIMEOUT_MS   = 1000;

// ── TCA9548A checked mux driver ─────────────────────────────────────────────
// Never proceeds on an unverified selection: every select/disable is a checked
// write FOLLOWED by a control-register readback that must show exactly the
// requested state. A failed select leaves no channel enabled (best-effort
// disable-all), so a stale previous channel can never route one hive's chip
// into another hive's reading.
class TcaMux {
 public:
  struct Diag {
    uint16_t selectFailures   = 0;
    uint16_t disableFailures  = 0;
    uint16_t verifyFailures   = 0;
  };

  TcaMux(I2cBusIface& bus, uint8_t addr) : _bus(bus), _addr(addr) {}

  bool present() { return probe(_bus, _addr); }

  // Enable exactly channel `ch` (0..7). Verified by readback.
  bool select(uint8_t ch) {
    if (ch > 7) { _diag.selectFailures++; return false; }
    const uint8_t want = (uint8_t)(1u << ch);
    if (!writeControl(want)) { _diag.selectFailures++; bestEffortClear(); return false; }
    if (!verifyControl(want)) { _diag.verifyFailures++; bestEffortClear(); return false; }
    return true;
  }

  // Disable every channel. Verified by readback.
  bool disableAll() {
    if (!writeControl(0x00)) { _diag.disableFailures++; return false; }
    if (!verifyControl(0x00)) { _diag.verifyFailures++; return false; }
    return true;
  }

  // Cleanup path: try to clear, don't escalate further on failure.
  void bestEffortClear() { (void)writeControl(0x00); }

  const Diag& diag() const { return _diag; }

 private:
  bool writeControl(uint8_t v) {
    _bus.beginTransmission(_addr);
    _bus.write(v);
    return _bus.endTransmission() == 0;
  }
  bool verifyControl(uint8_t want) {
    uint8_t got = _bus.requestFrom(_addr, 1);
    if (got != 1 || _bus.available() <= 0) {
      while (_bus.available() > 0) (void)_bus.read();
      return false;
    }
    uint8_t v = (uint8_t)_bus.read();
    return v == want;
  }

  I2cBusIface& _bus;
  uint8_t _addr;
  Diag _diag;
};

// ── Structured sample-collection result ─────────────────────────────────────
constexpr uint8_t MAX_SAMPLES = 32;

struct SampleResult {
  bool    ok         = false;   // full requested count acquired, no comm errors
  uint8_t requested  = 0;
  uint8_t acquired   = 0;
  uint8_t commErrors = 0;       // NACKs / short reads during status+data polls
  bool    timedOut   = false;
  long    samples[MAX_SAMPLES] = {0};
};

// ── Checked NAU7802 driver ──────────────────────────────────────────────────
class Nau7802Checked {
 public:
  Nau7802Checked(I2cBusIface& bus, uint8_t addr) : _bus(bus), _addr(addr) {}

  bool present() { return probe(_bus, _addr); }

  // Full deliberate bring-up, every step checked (datasheet §9.1 sequencing):
  // reset → power-up (wait PUR) → LDO 3.3 V + AVDDS → gain 128 → 80 SPS →
  // ADC reg 0x30 (CLK_CHP off) → CH2 decoupling cap per `ch2Used` → start
  // conversions. AFE calibration is separate (calibrateAfe) so the caller can
  // run and record it per ADC channel.
  bool configure(bool ch2Used) {
    if (!writeReg(_bus, _addr, REG_PU_CTRL, PU_RR)) return false;      // reset
    _bus.delayMs(1);
    if (!writeReg(_bus, _addr, REG_PU_CTRL, 0x00)) return false;       // leave reset
    if (!writeReg(_bus, _addr, REG_PU_CTRL, PU_PUD | PU_PUA)) return false;
    if (!waitBitSet(REG_PU_CTRL, PU_PUR, POWERUP_TIMEOUT_MS)) return false;

    // AVDD from internal LDO at 3.3 V, PGA gain 128.
    if (!writeReg(_bus, _addr, REG_CTRL1,
                  (uint8_t)((CTRL1_VLDO_3V3 << 3) | CTRL1_GAIN_128))) return false;
    if (!setPuCtrlBits(PU_PUD | PU_PUA | PU_AVDDS)) return false;

    // 80 SPS (CRS), channel 1 selected, no calibration bits.
    if (!writeReg(_bus, _addr, REG_CTRL2, CTRL2_CRS_80SPS)) return false;

    // Datasheet §9.1: disable the ADC chopper clock (reg 0x15 = 0x30).
    if (!writeReg(_bus, _addr, REG_ADC, 0x30)) return false;

    // CH2 input cap: enabled only while CH2 is UNUSED (see PGA_PWR_CAP_EN note).
    uint8_t pgaPwr;
    if (!readReg(_bus, _addr, REG_PGA_PWR, pgaPwr)) return false;
    if (ch2Used) pgaPwr = (uint8_t)(pgaPwr & ~PGA_PWR_CAP_EN);
    else         pgaPwr = (uint8_t)(pgaPwr | PGA_PWR_CAP_EN);
    if (!writeReg(_bus, _addr, REG_PGA_PWR, pgaPwr)) return false;

    // Start continuous conversions.
    if (!setPuCtrlBits(PU_PUD | PU_PUA | PU_AVDDS | PU_CS)) return false;
    return true;
  }

  // Internal AFE (offset) calibration for the CURRENTLY selected channel.
  // Sets CALS, waits (bounded) for it to self-clear, then checks CAL_ERR.
  bool calibrateAfe() {
    uint8_t ctrl2;
    if (!readReg(_bus, _addr, REG_CTRL2, ctrl2)) return false;
    if (!writeReg(_bus, _addr, REG_CTRL2, (uint8_t)(ctrl2 | CTRL2_CALS))) return false;
    uint32_t start = _bus.millisMs();
    for (;;) {
      if (!readReg(_bus, _addr, REG_CTRL2, ctrl2)) return false;
      if (!(ctrl2 & CTRL2_CALS)) break;
      if (_bus.millisMs() - start > CAL_AFE_TIMEOUT_MS) return false;
      _bus.delayMs(1);
    }
    return !(ctrl2 & CTRL2_CAL_ERR);
  }

  // Select ADC input channel (1 or 2), verified by readback of CTRL2. Returns
  // false — and the caller must abort the reading — when the switch cannot be
  // confirmed, so CH1 data is never reported as CH2 or vice versa.
  bool selectChannel(uint8_t channel) {
    if (channel != 1 && channel != 2) return false;
    uint8_t ctrl2;
    if (!readReg(_bus, _addr, REG_CTRL2, ctrl2)) return false;
    uint8_t want = (channel == 2) ? (uint8_t)(ctrl2 | CTRL2_CHS)
                                  : (uint8_t)(ctrl2 & ~CTRL2_CHS);
    if (!writeReg(_bus, _addr, REG_CTRL2, want)) return false;
    uint8_t check;
    if (!readReg(_bus, _addr, REG_CTRL2, check)) return false;
    return (check & CTRL2_CHS) == (want & CTRL2_CHS);
  }

  // Read the data-ready flag. Transaction status is the return value; the flag
  // lands in `ready` — a failed status read is NOT "available".
  bool dataReady(bool& ready) {
    uint8_t pu;
    if (!readReg(_bus, _addr, REG_PU_CTRL, pu)) return false;
    ready = (pu & PU_CR) != 0;
    return true;
  }

  // Read one 24-bit conversion; requires exactly 3 bytes. Sign-extends.
  bool readConversion(long& out) {
    uint8_t b[3];
    if (!readRegs(_bus, _addr, REG_ADCO_B2, b, 3)) return false;
    uint32_t v = ((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2];
    if (v & 0x800000UL) v |= 0xFF000000UL;
    out = (long)(int32_t)v;
    return true;
  }

  // Collect exactly `count` conversions after discarding `discard` (the first
  // conversions after a channel/mux switch still belong to the previous
  // input's pipeline). Bounded by `timeoutMs` overall so a silent chip can
  // never stall the cycle; bounded comm-error budget so a flapping bus fails
  // fast instead of masquerading as a slow sensor.
  SampleResult collect(uint8_t count, uint8_t discard, uint32_t timeoutMs) {
    SampleResult r;
    r.requested = count;
    if (count == 0 || count > MAX_SAMPLES) return r;

    const uint8_t maxCommErrors = 3;
    uint8_t discarded = 0;
    uint32_t start = _bus.millisMs();
    while (r.acquired < count) {
      if (_bus.millisMs() - start > timeoutMs) { r.timedOut = true; return r; }
      bool ready = false;
      if (!dataReady(ready)) {
        if (++r.commErrors >= maxCommErrors) return r;
        _bus.delayMs(1);
        continue;
      }
      if (!ready) { _bus.delayMs(1); continue; }
      long v;
      if (!readConversion(v)) {
        if (++r.commErrors >= maxCommErrors) return r;
        continue;
      }
      if (discarded < discard) { discarded++; continue; }
      r.samples[r.acquired++] = v;
    }
    // Strict full-count policy: partial sample sets are never averaged.
    r.ok = (r.acquired == count) && (r.commErrors == 0);
    return r;
  }

  // Power down for deep sleep: clear PUD/PUA (checked write). ~1 µA standby.
  bool powerDown() {
    uint8_t pu;
    if (!readReg(_bus, _addr, REG_PU_CTRL, pu)) return false;
    pu = (uint8_t)(pu & ~(PU_PUD | PU_PUA));
    return writeReg(_bus, _addr, REG_PU_CTRL, pu);
  }

 private:
  bool setPuCtrlBits(uint8_t bits) {
    uint8_t pu;
    if (!readReg(_bus, _addr, REG_PU_CTRL, pu)) return false;
    return writeReg(_bus, _addr, REG_PU_CTRL, (uint8_t)(pu | bits));
  }

  bool waitBitSet(uint8_t reg, uint8_t bit, uint32_t timeoutMs) {
    uint32_t start = _bus.millisMs();
    for (;;) {
      uint8_t v;
      if (readReg(_bus, _addr, reg, v) && (v & bit)) return true;
      if (_bus.millisMs() - start > timeoutMs) return false;
      _bus.delayMs(1);
    }
  }

  I2cBusIface& _bus;
  uint8_t _addr;
};

}  // namespace nauchk
