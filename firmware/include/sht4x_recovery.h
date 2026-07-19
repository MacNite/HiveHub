// sht4x_recovery.h — the ambient-SHT4x read-with-recovery SEQUENCE, factored out
// of sensors.cpp so the exact ordering can run against a scripted mock in the
// host test suite (firmware/host_test/) exactly as it runs on the device.
//
// The ESP32-C6 Arduino-ESP32 3.x I2C-NG master can be wedged into
// ESP_ERR_INVALID_STATE by an earlier transaction (e.g. a probe of an absent
// TCA9548A). A wedged driver makes the first SHT4x getEvent() fail; recovering
// it requires a bus heal (Wire.end()/begin()) AND a full reinitialization of the
// Adafruit SHT4x object, because Wire.end()/begin() invalidates the cached I2C
// device the driver holds — calling getEvent() again without a fresh begin()
// cannot succeed. The sequence is therefore:
//
//     read → (fail) → heal → ACK-probe → begin() + restore mode → read (retry)
//
// bounded to a SINGLE retry so it can never delay deep sleep.
//
// Deliberately Arduino-free (no includes): `Ops` is a caller-supplied type that
// binds each step to the real sht4/i2cbus calls on the device, or to injected
// outcomes in the host test. The template keeps the one true copy of the control
// flow here while the hardware specifics stay in sensors.cpp.
#pragma once

namespace sht4xrec {

// What actually happened during acquire(), for tests and (optionally) callers.
struct Outcome {
  bool ok            = false;  // a valid measurement was obtained (values stored)
  bool healed        = false;  // an I2C heal was performed after the first fail
  bool acked         = false;  // the sensor ACKed after the heal
  bool reinitialized = false;  // sht4.begin() + mode restore re-run after heal
  bool retried       = false;  // the single bounded retry read was attempted
};

// `Ops` must provide:
//   bool read();          // getEvent(); true stores temp/humidity in the caller
//   bool heal();          // i2cbus::healForRead()
//   bool ack();           // i2cbus::deviceResponds(SHT4X_I2C_ADDRESS)
//   bool reinit();        // sht4.begin(&Wire) + setPrecision + setHeater
//   void onInitialFail(); // log "[SHT4x] Initial read failed"
//   void onAck(bool);     // log "[SHT4x] ACK after I2C heal: yes/no"
//   void onReinit(bool);  // log "[SHT4x] Reinitialization after I2C heal: OK/FAILED"
//   void onRetry(bool);   // log "[SHT4x] Retry read: OK/FAILED"
// The retry read is NEVER attempted before reinit() has returned true — that
// ordering is the whole point of the fix and is what the host test pins down.
template <typename Ops>
Outcome acquire(Ops& ops) {
  Outcome r;
  if (ops.read()) { r.ok = true; return r; }
  ops.onInitialFail();

  if (!ops.heal()) return r;
  r.healed = true;

  r.acked = ops.ack();
  ops.onAck(r.acked);
  if (!r.acked) return r;

  r.reinitialized = ops.reinit();
  ops.onReinit(r.reinitialized);
  if (!r.reinitialized) return r;

  r.retried = true;
  r.ok = ops.read();
  ops.onRetry(r.ok);
  return r;
}

}  // namespace sht4xrec
