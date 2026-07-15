// i2c_bus.cpp — shared-I2C-bus lifecycle. See i2c_bus.h for the contract.
#include "i2c_bus.h"

#include <Wire.h>

namespace i2cbus {

namespace {

Diag gDiag;
bool gBusOk = false;
uint8_t gRuntimeRecoveries = 0;
constexpr uint8_t RUNTIME_RECOVERY_MAX = 3;

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

  // Release both lines as open-drain/inputs; external pull-ups define idle.
  pinMode(I2C_SDA, INPUT_PULLUP);
  pinMode(I2C_SCL, OUTPUT_OPEN_DRAIN);
  digitalWrite(I2C_SCL, HIGH);
  delayMicroseconds(5);

  const bool sdaLowBefore = digitalRead(I2C_SDA) == LOW;
  const bool sclLowBefore = digitalRead(I2C_SCL) == LOW;

  if (!sdaLowBefore && !sclLowBefore) {
    pinMode(I2C_SDA, INPUT);
    pinMode(I2C_SCL, INPUT);
    return true;  // bus already idle — nothing to recover
  }

  // SCL held low by a slave (or a short) is unrecoverable from the master
  // side: we cannot clock anything. Report and fail.
  if (sclLowBefore && !waitSclHigh(10000)) {
    Serial.println("[I2C] Bus recovery FAILED: SCL stuck low (slave power-cycle or wiring fix required)");
    gDiag.recoveryFailures++;
    rtcRecoveryFailures++;
    pinMode(I2C_SDA, INPUT);
    pinMode(I2C_SCL, INPUT);
    return false;
  }

  // Standard rescue: up to 9 SCL pulses lets a slave stuck mid-byte finish and
  // release SDA. Only pulse while SCL actually rises (bounded stretch wait).
  int cycles = 0;
  while (digitalRead(I2C_SDA) == LOW && cycles < 9) {
    digitalWrite(I2C_SCL, LOW);
    delayMicroseconds(5);
    digitalWrite(I2C_SCL, HIGH);   // released; pull-up raises SCL
    if (!waitSclHigh(1000)) break; // stretch/stuck — stop pulsing
    delayMicroseconds(5);
    cycles++;
  }

  // Manufacture a STOP (SDA low->high while SCL high) so any listening slave
  // sees a clean end-of-transaction before the Wire driver takes over.
  pinMode(I2C_SDA, OUTPUT_OPEN_DRAIN);
  digitalWrite(I2C_SDA, LOW);
  delayMicroseconds(5);
  digitalWrite(I2C_SCL, HIGH);
  delayMicroseconds(5);
  digitalWrite(I2C_SDA, HIGH);
  delayMicroseconds(5);

  const bool sdaHigh = digitalRead(I2C_SDA) == HIGH;
  const bool sclHigh = digitalRead(I2C_SCL) == HIGH;
  Serial.printf("[I2C] Bus recovery: %d SCL pulse(s); SDA %s, SCL %s\n",
                cycles, sdaHigh ? "high" : "STILL LOW", sclHigh ? "high" : "STILL LOW");

  // Return the pins to inputs so Wire.begin() reconfigures them cleanly.
  pinMode(I2C_SDA, INPUT);
  pinMode(I2C_SCL, INPUT);

  if (!sdaHigh || !sclHigh) {
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

  Serial.printf("[I2C] Started on SDA=%d SCL=%d at %lu Hz (reported %lu Hz)\n",
                (int)I2C_SDA, (int)I2C_SCL,
                (unsigned long)I2C_CLOCK_HZ, (unsigned long)Wire.getClock());
  gBusOk = true;
  return true;
}

bool ok() { return gBusOk; }

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
  Serial.printf("[I2C] Diag: recovery %u/%u failed this boot (%lu/%lu since power-on), init failures %u\n",
                gDiag.recoveryFailures, gDiag.recoveryAttempts,
                (unsigned long)rtcRecoveryFailures, (unsigned long)rtcRecoveryAttempts,
                gDiag.initFailures);
}

}  // namespace i2cbus
