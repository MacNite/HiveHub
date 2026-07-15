// i2c_iface.h — minimal I2C bus interface used by the checked NAU7802/TCA9548A
// layer (nau7802_checked.h) so the exact same transaction code runs against the
// real Arduino TwoWire on the device and against a scripted mock in the host
// test suite (firmware/host_test/), where NACKs, short reads and wrong readbacks
// can be injected deterministically.
//
// Deliberately Arduino-free: only <stdint.h>/<stddef.h>, so it compiles with a
// plain host g++. The Wire-backed implementation lives in i2c_bus.cpp.
#pragma once

#include <stdint.h>
#include <stddef.h>

namespace hivei2c {

// Mirrors the subset of TwoWire the checked drivers need, plus the time source
// (so bounded waits are host-testable too). endTransmission() returns the
// Arduino convention: 0 = success, non-zero = error (1 data too long, 2 NACK on
// address, 3 NACK on data, 4 other, 5 timeout).
class I2cBusIface {
 public:
  virtual ~I2cBusIface() {}

  virtual void    beginTransmission(uint8_t address)          = 0;
  virtual size_t  write(uint8_t data)                         = 0;
  virtual uint8_t endTransmission()                           = 0;
  // Returns the number of bytes actually received (may be short on failure).
  virtual uint8_t requestFrom(uint8_t address, uint8_t count) = 0;
  virtual int     available()                                 = 0;
  virtual int     read()                                      = 0;

  virtual uint32_t millisMs()                                 = 0;
  virtual void     delayMs(uint32_t ms)                       = 0;
};

// ── Checked primitive transactions ──────────────────────────────────────────
// Every helper separates transaction status from data: a failed read NEVER
// yields a value the caller could mistake for a register content (no 0xFF /
// zero sentinels).

// Write one register byte. True only on a fully ACKed transaction.
inline bool writeReg(I2cBusIface& bus, uint8_t addr, uint8_t reg, uint8_t val) {
  bus.beginTransmission(addr);
  bus.write(reg);
  bus.write(val);
  return bus.endTransmission() == 0;
}

// Read exactly `n` register bytes starting at `reg`. True only when the pointer
// write ACKed AND exactly `n` bytes arrived; a short read drains the leftovers
// and fails.
inline bool readRegs(I2cBusIface& bus, uint8_t addr, uint8_t reg,
                     uint8_t* buf, uint8_t n) {
  bus.beginTransmission(addr);
  bus.write(reg);
  if (bus.endTransmission() != 0) return false;
  uint8_t got = bus.requestFrom(addr, n);
  if (got != n) {
    while (bus.available() > 0) (void)bus.read();
    return false;
  }
  for (uint8_t i = 0; i < n; i++) {
    if (bus.available() <= 0) return false;
    buf[i] = (uint8_t)bus.read();
  }
  return true;
}

inline bool readReg(I2cBusIface& bus, uint8_t addr, uint8_t reg, uint8_t& out) {
  return readRegs(bus, addr, reg, &out, 1);
}

// Probe for an ACK at `addr` (0-byte write).
inline bool probe(I2cBusIface& bus, uint8_t addr) {
  bus.beginTransmission(addr);
  return bus.endTransmission() == 0;
}

}  // namespace hivei2c
