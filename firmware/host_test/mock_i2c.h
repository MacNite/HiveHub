// mock_i2c.h — scripted in-memory I2C bus for the host test suite. Simulates a
// TCA9548A (control register) and up to nine NAU7802 chips (one per mux
// channel + one direct), with switches to inject every failure mode the
// checked layer must survive: address NACKs, data NACKs, short reads, stuck
// conversions, wrong readbacks and railed samples.
#pragma once

#include <cstring>
#include <cstdio>
#include <vector>
#include "i2c_iface.h"

struct MockNau {
  bool present = true;
  uint8_t regs[0x20] = {0};
  long conversion = 100000;          // value returned for each conversion
  long conversionStep = 0;           // added after every read (ramps/noise)
  bool dataReady = true;             // PU_CTRL CR presented as set
  bool stuckNotReady = false;        // CR never sets (timeout path)
  bool failChannelSwitchWrite = false;   // NACK CTRL2 writes
  bool liePersistCh1 = false;        // CTRL2 readback always shows CH1 (switch "succeeds" but readback disagrees)
  bool failCalibration = false;      // CALS never clears / CAL_ERR set
  int  shortReadAfterN = -1;         // deliver a short conversion read after N good ones
  int  conversionsRead = 0;
  std::vector<long> script;          // optional explicit per-read conversion values
  size_t scriptPos = 0;

  long nextConversion() {
    long v;
    if (scriptPos < script.size()) v = script[scriptPos++];
    else { v = conversion; conversion += conversionStep; }
    conversionsRead++;
    return v;
  }
};

class MockBus : public hivei2c::I2cBusIface {
 public:
  // ── configuration ──────────────────────────────────────────────────────────
  uint8_t muxAddr = 0x70;
  uint8_t nauAddr = 0x2A;
  bool muxPresent = true;
  bool muxFailWrite = false;         // NACK all mux control writes
  bool muxFailReadback = false;      // readback returns nothing
  uint8_t muxLieReadback = 0xFF;     // 0xFF = honest; else readback returns this
  bool muxWriteIgnored = false;      // write ACKs but register does not change
  uint8_t muxControl = 0x00;         // actual control register

  MockNau direct;                    // chip on the main bus (used when muxControl==0)
  MockNau muxed[8];                  // one chip per mux channel
  bool haveDirect = false;
  bool haveMuxed[8] = {false};

  uint32_t nowMs = 0;

  // ── stats for assertions ───────────────────────────────────────────────────
  int muxWrites = 0;

  // Which NAU answers at 0x2A given the current mux state? (mirrors the real
  // shared-address hazard: with a channel open, the muxed chip answers; with
  // all closed, the direct chip does)
  MockNau* activeNau() {
    for (int c = 0; c < 8; c++)
      if ((muxControl & (1 << c)) && haveMuxed[c]) return &muxed[c];
    if (muxControl == 0 && haveDirect) return &direct;
    return nullptr;
  }

  // ── I2cBusIface ────────────────────────────────────────────────────────────
  void beginTransmission(uint8_t address) override {
    txAddr = address;
    txBuf.clear();
  }
  size_t write(uint8_t data) override { txBuf.push_back(data); return 1; }

  uint8_t endTransmission() override {
    rxBuf.clear();
    if (txAddr == muxAddr) {
      if (!muxPresent) return 2;               // address NACK
      if (txBuf.empty()) return 0;             // probe
      if (muxFailWrite) return 3;              // data NACK
      muxWrites++;
      if (!muxWriteIgnored) muxControl = txBuf[0];
      return 0;
    }
    if (txAddr == nauAddr) {
      MockNau* n = activeNau();
      if (!n || !n->present) return 2;
      if (txBuf.empty()) return 0;             // probe
      pointerReg = txBuf[0];
      if (txBuf.size() >= 2) {                 // register write
        uint8_t reg = txBuf[0], val = txBuf[1];
        if (reg == 0x02 && n->failChannelSwitchWrite) return 3;
        if (reg < sizeof(n->regs)) n->regs[reg] = val;
        // PU_CTRL semantics: reads reflect written bits + PUR follows PUD|PUA.
        if (reg == 0x00) {
          if (val & 0x06) n->regs[0] |= 0x08;  // PUR ready immediately
          else n->regs[0] &= ~0x08;
        }
        // CTRL2 CALS: complete instantly unless failing.
        if (reg == 0x02 && (val & 0x04)) {
          if (n->failCalibration) n->regs[2] = val | 0x08;      // CAL_ERR, CALS kept? clear CALS, set ERR
          else n->regs[2] = val & ~0x04;                        // CALS self-clears
          if (n->failCalibration) n->regs[2] &= ~0x04;
        }
        if (reg == 0x02 && n->liePersistCh1) n->regs[2] &= ~0x80;  // CHS never sticks
      }
      return 0;
    }
    return 2;  // nothing else on the bus
  }

  uint8_t requestFrom(uint8_t address, uint8_t count) override {
    rxBuf.clear();
    if (address == muxAddr) {
      if (!muxPresent || muxFailReadback) return 0;
      rxBuf.push_back(muxLieReadback != 0xFF ? muxLieReadback : muxControl);
      return 1;
    }
    if (address == nauAddr) {
      MockNau* n = activeNau();
      if (!n || !n->present) return 0;
      if (pointerReg == 0x12) {                          // 24-bit conversion
        if (n->shortReadAfterN >= 0 && n->conversionsRead >= n->shortReadAfterN) {
          long v = n->nextConversion();
          rxBuf.push_back((uint8_t)((v >> 16) & 0xFF));  // deliver ONE byte only
          return 1;
        }
        long v = n->nextConversion();
        rxBuf.push_back((uint8_t)((v >> 16) & 0xFF));
        rxBuf.push_back((uint8_t)((v >> 8) & 0xFF));
        rxBuf.push_back((uint8_t)(v & 0xFF));
        uint8_t deliver = count < 3 ? count : 3;
        rxBuf.resize(deliver);
        return deliver;
      }
      for (uint8_t i = 0; i < count; i++) {
        uint8_t reg = (uint8_t)(pointerReg + i);
        uint8_t v = reg < sizeof(n->regs) ? n->regs[reg] : 0;
        if (reg == 0x00) {                               // PU_CTRL with live CR
          bool ready = n->dataReady && !n->stuckNotReady;
          v = (uint8_t)((v & ~0x20) | (ready ? 0x20 : 0x00));
        }
        rxBuf.push_back(v);
      }
      return count;
    }
    return 0;
  }

  int available() override { return (int)rxBuf.size(); }
  int read() override {
    if (rxBuf.empty()) return -1;
    int v = rxBuf.front();
    rxBuf.erase(rxBuf.begin());
    return v;
  }

  uint32_t millisMs() override { return nowMs; }
  void delayMs(uint32_t ms) override { nowMs += ms; }

 private:
  uint8_t txAddr = 0;
  uint8_t pointerReg = 0;
  std::vector<uint8_t> txBuf;
  std::vector<uint8_t> rxBuf;
};
