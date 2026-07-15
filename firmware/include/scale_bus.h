// scale_bus.h — checked, fail-closed reads for the scale registry: HX711 pin
// pairs and NAU7802 I2C channels (optionally behind a TCA9548A mux).
//
// All NAU7802s share the fixed I2C address 0x2A, so more than one is only
// reachable behind the mux. Every mux selection and NAU7802 channel switch is
// VERIFIED by register readback before any conversion is read, and a reading is
// returned as a structured ReadResult whose `ok` is true only when routing,
// channel selection, the FULL requested sample count and the stability filter
// all passed — a bus fault can therefore only produce an invalid reading, never
// a value silently attributed to the wrong hive or the wrong load cell.
//
// Raw counts convert to kg in sensors.cpp (kg = (raw - offset) / factor); the
// per-channel offset/factor calibration lives in the hive registry.
#pragma once

#include <Arduino.h>
#include "config.h"
#include "hive_config.h"

namespace scalebus {

// One scale read with full provenance. `ok` implies `raw` is a trimmed-mean of
// the complete requested sample set from the verified mux/ADC channel.
struct ReadResult {
  bool ok = false;
  long raw = 0;

  uint8_t samplesRequested   = 0;
  uint8_t samplesAcquired    = 0;
  uint8_t commErrors         = 0;
  long    spread             = 0;      // trimmed-set max-min (stability metric)
  bool    timedOut           = false;
  bool    routeFailed        = false;  // mux select/disable failed or unverified
  bool    channelSwitchFailed= false;  // NAU7802 CH1/CH2 switch unverified
  bool    railed             = false;  // sample at/near an ADC rail (open input)
  bool    unstable           = false;  // spread above NAU7802_MAX_SPREAD_COUNTS
  bool    notConfigured      = false;  // bus down, config invalid, or chip never initialized
  bool    recoveryAttempted  = false;  // one bounded chip re-init was tried
  bool    recoverySucceeded  = false;
};

// Per-boot diagnostics counters (RAM; logged once per cycle).
struct Diag {
  uint16_t muxSelectFailures      = 0;
  uint16_t muxDisableFailures     = 0;
  uint16_t muxVerifyFailures      = 0;
  uint16_t nauInitFailures        = 0;
  uint16_t nauCalCh1Failures      = 0;
  uint16_t nauCalCh2Failures      = 0;
  uint16_t nauChannelSwitchFailures = 0;
  uint16_t incompleteSampleSets   = 0;
  uint16_t timeouts               = 0;
  uint16_t commErrors             = 0;
  uint16_t recoveryAttempts       = 0;
  uint16_t recoveryFailures       = 0;
  uint16_t powerDownFailures      = 0;
};

// Detect the mux and run the deliberate per-chip NAU7802 bring-up (reset,
// power-up, LDO/gain/rate, CH2-cap policy, AFE calibration of every USED ADC
// channel — each step checked) for every chip the registry references. A chip
// is marked initialized only when everything succeeded. Requires i2cbus::ok().
void begin();

// Read one scale channel. Never blocks longer than the bounded sample timeout;
// never leaves a mux channel selected (best-effort disable-all on every path).
// On a NAU7802 transaction failure the chip is marked for reinitialization and
// ONE bounded recovery is attempted within this call.
ReadResult readChannel(const hivecfg::ScaleChannel& ch);

// Checked power-down of every registry NAU7802 ahead of deep sleep. Returns
// false when any chip could not be confirmed powered down (logged with its mux
// channel; may raise sleep current). Always finishes by disabling all mux
// channels.
bool powerDownAllForSleep();

// True once begin() verified a TCA9548A on the bus.
bool muxPresent();

// Checked mux/probe helpers for the portal's discovery scan (same verified
// routing path as measurements — discovery must not use a weaker one).
bool muxSelectChecked(uint8_t channel);
bool muxDisableChecked();
bool nauPresentOnBus();   // ACK probe at NAU7802_I2C_ADDRESS

Diag& diag();
void logDiag();  // one summary serial line per cycle

}  // namespace scalebus
