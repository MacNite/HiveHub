// i2c_bus.h — lifecycle of the single shared I2C bus: GPIO-level stuck-bus
// recovery, checked Wire bring-up at the explicit I2C_CLOCK_HZ, a bounded
// runtime re-recovery path for the long-lived provisioning/calibration modes,
// a clean shutdown before deep sleep, and the per-boot diagnostics counters.
//
// The shared bus carries: DS3231 RTC, SHT4x, NAU7802 (direct or behind a
// TCA9548A), optional INA219 and MAX17048. Nothing on it may change the bus
// clock — the former BeeCounter 400 kHz OTA path is gone; the bus runs at
// I2C_CLOCK_HZ (100 kHz) from begin() to end().
#pragma once

#include <Arduino.h>
#include "config.h"
#include "i2c_iface.h"

namespace i2cbus {

// Per-boot diagnostics (RAM only — logged each cycle, never flash-persisted;
// bus-recovery attempt/failure totals additionally live in RTC memory so they
// survive deep sleep and can be inspected over a day of cycles).
struct Diag {
  uint16_t recoveryAttempts   = 0;
  uint16_t recoveryFailures   = 0;
  uint16_t initFailures       = 0;   // Wire.begin()/setClock() failures
  uint16_t readHeals          = 0;   // mid-cycle driver resets (healForRead)
};

// GPIO bit-bang recovery of a stuck bus (a slave holding SDA low mid-byte).
// Inspects SDA and SCL separately, pulses SCL only while SCL itself can rise
// (bounded clock-stretch wait), manufactures a STOP, and verifies both lines
// end high. Returns true when the bus is idle (or was already). Safe no-op on
// a healthy bus; call before Wire.begin().
bool recover();

// recover() + checked Wire.begin() + checked Wire.setClock(I2C_CLOCK_HZ), with
// results and the configured frequency logged. Returns false when the bus is
// unusable (recovery failed or Wire.begin() failed) — the caller must then
// SKIP all I2C device initialization; readings fail closed as invalid/missing.
bool begin();

// True once begin() succeeded this boot (gates every I2C consumer).
bool ok();

// Address-only ACK probe on the live bus (0-byte write, Arduino endTransmission
// convention). Unlike a register read, an absent device NACKs cleanly here and
// does NOT wedge the ESP32-C6 I2C-NG master driver. Call this before invoking a
// third-party library begin() whose presence check is a register read (SparkFun
// MAX1704x, Adafruit INA219): a missing device is then reported without ever
// issuing the transaction that trips ESP_ERR_INVALID_STATE. Returns false when
// the bus is down (never begun) or the address does not ACK.
bool deviceResponds(uint8_t addr);

// Clear a wedged I2C master driver mid-cycle, then re-init (Wire.end() +
// recover() + Wire.begin() + setClock()). On Arduino-ESP32 3.x / ESP-IDF 5.x
// the new "i2c-ng" master can be left in ESP_ERR_INVALID_STATE after a failed
// transaction (e.g. a NACKed register read to an absent chip), after which
// EVERY later transfer — even to healthy sensors — fails until the driver is
// torn down and rebuilt. A cycle read path calls this once when its read fails
// and then retries. Bounded per measurement cycle (resetReadHeals()) so a truly
// dead bus can never spin: returns false once the budget is spent or the bus
// was never up. Distinct from runtimeRecover(), which serves the long-lived
// provisioning/calibration modes with its own budget.
bool healForRead();

// Reset the per-cycle healForRead() budget. Call once at the top of a
// measurement cycle so each cycle gets a fresh, bounded number of heals.
void resetReadHeals();

// Bounded runtime recovery for provisioning/calibration mode, where the device
// stays awake instead of rebooting each cycle: ends Wire, re-runs recover() +
// begin(). At most `maxAttempts` recoveries are performed per boot (default 3);
// after that it always returns false — never an infinite retry loop.
bool runtimeRecover();

// Wire.end() + release SDA/SCL as plain inputs so the external pull-ups hold
// the lines high across deep sleep (no back-powering / leakage through the
// ESP32 pads). Wake-up re-runs begin() from setup(), so this cannot break the
// next cycle's initialization. Safe on both the classic ESP32 and the C6.
void endForSleep();

// The Wire-backed implementation of the checked-driver bus interface.
hivei2c::I2cBusIface& iface();

Diag& diag();
void logDiag();  // one summary serial line

}  // namespace i2cbus
