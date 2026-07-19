// i2c_bus.cpp — shared-I2C-bus lifecycle. See i2c_bus.h for the contract.
#include "i2c_bus.h"

#include <Wire.h>

namespace i2cbus {

namespace {

Diag gDiag;
bool gBusOk = false;
uint8_t gRuntimeRecoveries = 0;
constexpr uint8_t RUNTIME_RECOVERY_MAX = 3;

// Per-measurement-cycle budget for mid-cycle driver heals (healForRead). A
// normal timer-wake cycle is a single boot, but provisioning/calibration stay
// awake across cycles, so resetReadHeals() re-arms the budget at each cycle.
uint8_t gReadHeals = 0;
constexpr uint8_t READ_HEAL_MAX = 8;

// Bus-recovery totals that survive deep sleep (cheap trend signal for a bus
// that goes bad in the field). RTC memory, not flash — no wear.
RTC_DATA_ATTR uint32_t rtcRecoveryAttempts = 0;
RTC_DATA_ATTR uint32_t rtcRecoveryFailures = 0;

// TwoWire adapter for the checked NAU7802/TCA9548A layer.
class WireBus : public hivei2c::I2cBusIface {
 public:
  void    beginTransmission(uint8_t address) override { Wire.beginTransmission(address); }
  size_t  write(uint8_t data) override { return Wire.write(data); }
  uint8_t endTransmission() override { return Wire.endTransmission(); }
  uint8_t requestFrom(uint8_t address, uint8_t count) override {
    return (uint8_t)Wire.requestFrom((int)address, (int)count);
  }
  int  available() override { return Wire.available(); }
  int  read() override { return Wire.read(); }
  uint32_t millisMs() override { return millis(); }
  void delayMs(uint32_t ms) override { delay(ms); }
};

WireBus gWireBus;

// Wait (bounded) for SCL to rise after releasing it — covers clock stretching
// and detects a hard-stuck-low SCL, which SCL pulsing cannot fix.
bool waitSclHigh(uint32_t timeoutUs) {
  uint32_t start = micros();
  while (digitalRead(I2C_SCL) == LOW) {
    if (micros() - start > timeoutUs) return false;
    delayMicroseconds(2);
  }
  return true;
}

}  // namespace

bool recover() {
  gDiag.recoveryAttempts++;
  rtcRecoveryAttempts++;

  // Release BOTH lines as inputs with internal pull-ups. This is the only safe
  // way to sample the true idle state: we never actively drive a line high, so
  // a line the hardware is already holding at 3.3 V reads high instead of being
  // masked by a push-pull HIGH we drove ourselves (the old code drove SCL HIGH
  // and could then misread a physically-high SCL as "stuck low"). Give the pads
  // a moment to settle through the pull-ups before sampling.
  pinMode(I2C_SDA, INPUT_PULLUP);
  pinMode(I2C_SCL, INPUT_PULLUP);
  delayMicroseconds(20);

  const int sdaLevel = digitalRead(I2C_SDA);
  const int sclLevel = digitalRead(I2C_SCL);

  Serial.printf(
      "[I2C] Recovery initial levels: SDA=%d SCL=%d, pins SDA=%d SCL=%d\n",
      sdaLevel,
      sclLevel,
      static_cast<int>(I2C_SDA),
      static_cast<int>(I2C_SCL)
  );

  const bool sdaLowBefore = sdaLevel == LOW;
  const bool sclLowBefore = sclLevel == LOW;

  if (!sdaLowBefore && !sclLowBefore) {
    // Bus already idle — leave both lines released (pull-ups hold them high).
    pinMode(I2C_SDA, INPUT_PULLUP);
    pinMode(I2C_SCL, INPUT_PULLUP);
    return true;  // nothing to recover
  }

  // SCL held low by a slave (or a short) is unrecoverable from the master
  // side: we cannot clock anything. Report and fail closed.
  if (sclLowBefore && !waitSclHigh(10000)) {
    Serial.println("[I2C] Bus recovery FAILED: SCL stuck low (slave power-cycle or wiring fix required)");
    gDiag.recoveryFailures++;
    rtcRecoveryFailures++;
    pinMode(I2C_SDA, INPUT_PULLUP);
    pinMode(I2C_SCL, INPUT_PULLUP);
    return false;
  }

  // Standard rescue: up to 9 SCL pulses lets a slave stuck mid-byte finish and
  // release SDA. Open-drain safe — SCL is only ever driven LOW or released to
  // its pull-up, never actively driven high. Only pulse while SCL actually
  // rises after release (bounded clock-stretch wait).
  int cycles = 0;
  while (digitalRead(I2C_SDA) == LOW && cycles < 9) {
    pinMode(I2C_SCL, OUTPUT_OPEN_DRAIN);
    digitalWrite(I2C_SCL, LOW);
    delayMicroseconds(5);

    pinMode(I2C_SCL, INPUT_PULLUP);  // release; pull-up raises SCL
    if (!waitSclHigh(1000)) break;   // stretch/stuck — stop pulsing
    delayMicroseconds(5);
    cycles++;
  }

  // Manufacture a STOP (SDA low->high while SCL high) so any listening slave
  // sees a clean end-of-transaction before the Wire driver takes over. Both
  // lines stay open-drain safe: SDA is driven low then released to its pull-up,
  // SCL is only released and waited on — neither is ever actively driven high.
  bool stopSclHigh = true;
  pinMode(I2C_SDA, OUTPUT_OPEN_DRAIN);
  digitalWrite(I2C_SDA, LOW);
  delayMicroseconds(5);

  pinMode(I2C_SCL, INPUT_PULLUP);    // release SCL; require it to truly read high
  if (!waitSclHigh(1000)) stopSclHigh = false;
  delayMicroseconds(5);

  pinMode(I2C_SDA, INPUT_PULLUP);    // release SDA -> rising edge = STOP
  delayMicroseconds(5);

  const int sdaAfter = digitalRead(I2C_SDA);
  const int sclAfter = digitalRead(I2C_SCL);
  const bool sdaHigh = sdaAfter == HIGH;
  const bool sclHigh = sclAfter == HIGH && stopSclHigh;

  Serial.printf("[I2C] Bus recovery: %d SCL pulse(s); final SDA=%d SCL=%d (SDA %s, SCL %s)\n",
                cycles, sdaAfter, sclAfter,
                sdaHigh ? "high" : "STILL LOW",
                sclHigh ? "high" : "STILL LOW");

  // Leave the pins released with pull-ups; begin() reconfigures them for Wire.
  pinMode(I2C_SDA, INPUT_PULLUP);
  pinMode(I2C_SCL, INPUT_PULLUP);

  if (!sdaHigh || !sclHigh) {
    const char* reason = (!sdaHigh && !sclHigh) ? "SDA and SCL both remained low"
                         : !sdaHigh              ? "SDA remained low"
                                                 : "SCL remained low";
    Serial.printf("[I2C] Bus recovery FAILED: %s\n", reason);
    gDiag.recoveryFailures++;
    rtcRecoveryFailures++;
    return false;
  }
  return true;
}

bool begin() {
  gBusOk = false;

  if (!recover()) {
    Serial.println("[I2C] Bus stuck; skipping I2C device initialization (readings fail closed)");
    gDiag.initFailures++;
    return false;
  }

  // Hand Wire a clean, idle bus: both lines released (inputs with pull-ups),
  // not a pin we are still driving from the recovery sequence.
  pinMode(I2C_SDA, INPUT_PULLUP);
  pinMode(I2C_SCL, INPUT_PULLUP);

  if (!Wire.begin(I2C_SDA, I2C_SCL, I2C_CLOCK_HZ)) {
    Serial.println("[I2C] Wire.begin FAILED");
    gDiag.initFailures++;
    return false;
  }

  // Explicit, checked clock (never rely on the framework default; nothing may
  // change it later — the BeeCounter 400 kHz path is gone).
  if (!Wire.setClock(I2C_CLOCK_HZ)) {
    Serial.printf("[I2C] Wire.setClock(%lu) FAILED\n", (unsigned long)I2C_CLOCK_HZ);
    gDiag.initFailures++;
    Wire.end();
    return false;
  }

  Serial.printf("[I2C] Started on SDA=GPIO%d SCL=GPIO%d, configured %lu Hz, reported %lu Hz\n",
                (int)I2C_SDA, (int)I2C_SCL,
                (unsigned long)I2C_CLOCK_HZ, (unsigned long)Wire.getClock());
  gBusOk = true;
  return true;
}

bool ok() { return gBusOk; }

bool deviceResponds(uint8_t addr) {
  if (!gBusOk) return false;
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

void resetReadHeals() { gReadHeals = 0; }

bool healForRead() {
  if (!gBusOk) return false;
  if (gReadHeals >= READ_HEAL_MAX) {
    Serial.printf("[I2C] Read-path heal budget exhausted (%u/%u); not resetting\n",
                  gReadHeals, READ_HEAL_MAX);
    return false;
  }
  gReadHeals++;
  gDiag.readHeals++;
  Serial.printf("[I2C] Read-path heal %u/%u: clearing wedged driver\n",
                gReadHeals, READ_HEAL_MAX);
  Wire.end();
  delay(2);
  // begin() re-runs recover() + Wire.begin() + setClock(); it also re-logs the
  // "[I2C] Started ..." line, which usefully marks the reset in the trace.
  return begin();
}

bool runtimeRecover() {
  if (gRuntimeRecoveries >= RUNTIME_RECOVERY_MAX) {
    Serial.printf("[I2C] Runtime recovery budget exhausted (%u/%u); not retrying\n",
                  gRuntimeRecoveries, RUNTIME_RECOVERY_MAX);
    return false;
  }
  gRuntimeRecoveries++;
  Serial.printf("[I2C] Runtime bus recovery attempt %u/%u\n",
                gRuntimeRecoveries, RUNTIME_RECOVERY_MAX);
  Wire.end();
  delay(2);
  return begin();
}

void endForSleep() {
  if (gBusOk) Wire.end();
  gBusOk = false;
  // Leave SDA/SCL floating inputs: the external pull-ups park both lines high,
  // and the ESP32 pads can neither back-power a sensor nor leak through an
  // internal pull. (Identical treatment on classic ESP32 and XIAO ESP32-C6 —
  // neither needs a GPIO hold for an externally pulled-up line.)
  pinMode(I2C_SDA, INPUT);
  pinMode(I2C_SCL, INPUT);
}

hivei2c::I2cBusIface& iface() { return gWireBus; }

Diag& diag() { return gDiag; }

void logDiag() {
  Serial.printf("[I2C] Diag: recovery %u/%u failed this boot (%lu/%lu since power-on), init failures %u, read heals %u\n",
                gDiag.recoveryFailures, gDiag.recoveryAttempts,
                (unsigned long)rtcRecoveryFailures, (unsigned long)rtcRecoveryAttempts,
                gDiag.initFailures, gDiag.readHeals);
}

}  // namespace i2cbus
