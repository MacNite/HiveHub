// inspection.h — inspection mode: the window while a beekeeper has the hive
// open, during which every hive-specific reading is an artefact of the
// inspection rather than a measurement of the colony.
//
// The hub keeps measuring and keeps uploading throughout — nothing is dropped
// and nothing is faked. Each upload carries `inspection: true`, and the backend
// turns the runs of flagged cycles into inspection windows that charts shade,
// insights skip and alert rules ignore. Hub-level sensors (ambient, battery,
// solar, RSSI) are unaffected and are how you know the hub stayed alive.
//
// Three things can toggle it:
//   * the external button on INSPECTION_BUTTON_PIN (C6 only) — one press
//     toggles, including a press that wakes the hub out of deep sleep;
//   * a `start_inspection` / `stop_inspection` command from the backend, which
//     is how HivePal's in-app buttons reach a hub;
//   * the timeout, which ends an inspection nobody switched off.
//
// State lives in RTC memory, so an inspection survives the deep sleeps between
// cycles. It does NOT survive a power cut or a firmware update — a hub that
// cold-boots comes up measuring, which is the safe direction to fail: worst
// case a chart shows one honest spike, rather than a hive going quiet for good
// because a reset lost the "off" press.
#pragma once

#include <Arduino.h>

#include "config.h"

namespace inspection {

// Restore the RTC-held state and act on a press that woke us. Call once from
// setup(), after loadConfigFromPrefs() (which restores the timeout) and before
// the first measurement is assembled. `gpioWakeMask` is the deep-sleep GPIO
// wake status; pass 0 when the boot was not a GPIO wake.
void begin(uint32_t wakeCauses, uint64_t gpioWakeMask);

// True while a hive inspection is in progress.
bool active();

// Unix seconds at which the current inspection started, or 0 when it is not
// active or the hub had no trustworthy clock at the time.
uint32_t startedAt();

// Turn inspection on/off from a backend command. `source` is logged.
void setActive(bool on, const char* source);

// Poll the external button. Call from loop(); no-op on boards without one.
void poll();

// End an inspection that has run past its timeout. Called once per cycle.
// Returns true if this call ended one.
bool enforceTimeout();

// Per-device timeout, delivered by /api/v1/devices/{id}/config and persisted in
// NVS so a hub that boots without WiFi still ends its inspections. Clamped to
// [1, INSPECTION_TIMEOUT_MAX_MINUTES]; 0 selects the compiled default.
//
// Why the hub enforces this at all, when the server sweeps timed-out windows
// too: the server's sweep closes the RECORD, and a hub that never heard about it
// would go on flagging its uploads, which re-opens the window on the very next
// one. The timeout has to end on the device for it to actually end.
void setTimeoutMinutes(uint32_t minutes);

// The inspection button's bit in the deep-sleep GPIO wake mask, or 0 on a board
// without one. Returned rather than armed here so storage_power.cpp can enable
// both buttons in a SINGLE esp_deep_sleep_enable_gpio_wakeup() call: whether a
// second call adds to the mask or replaces it is an IDF implementation detail,
// and a hub whose inspection button silently stopped waking it would present as
// "the button only works sometimes".
uint64_t wakePinMask();

// Pull up and hold the inspection button pin across deep sleep. Called from
// configureButtonWake(); no-op on boards without an inspection button.
void prepareWakePin();
// Release the sleep-time pin hold. Called from releaseSleepPinHolds().
void releaseWakeHold();

}  // namespace inspection
