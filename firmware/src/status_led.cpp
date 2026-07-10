// status_led.cpp — on-board user LED heartbeat. See status_led.h.
//
// The bodies are guarded by ENABLE_STATUS_LED rather than the whole file so the
// symbols always exist for the linker; when the feature is disabled every call
// is an empty function the compiler discards. This keeps main.cpp /
// storage_power.cpp free of feature #ifdefs at the call sites.
#include "status_led.h"

#include <Arduino.h>

#include "config.h"

#if ENABLE_STATUS_LED
// Translate a logical on/off into the pin level, honouring the active-low
// wiring of the XIAO ESP32-C6 user LED (LOW = lit).
static inline void statusLedSet(bool on) {
#if STATUS_LED_ACTIVE_LOW
  digitalWrite(STATUS_LED_GPIO, on ? LOW : HIGH);
#else
  digitalWrite(STATUS_LED_GPIO, on ? HIGH : LOW);
#endif
}
#endif

void statusLedInit() {
#if ENABLE_STATUS_LED
  pinMode(STATUS_LED_GPIO, OUTPUT);
  statusLedSet(false);  // start dark; we only pulse it on events
#endif
}

void statusLedOn() {
#if ENABLE_STATUS_LED
  statusLedSet(true);
#endif
}

void statusLedOff() {
#if ENABLE_STATUS_LED
  statusLedSet(false);
#endif
}

void statusLedBootBlink() {
#if ENABLE_STATUS_LED
  statusLedSet(true);
  delay(STATUS_LED_BOOT_MS);
  statusLedSet(false);
#endif
}

void statusLedSleepBlink() {
#if ENABLE_STATUS_LED
  for (int i = 0; i < 2; i++) {
    statusLedSet(true);
    delay(STATUS_LED_BLINK_ON_MS);
    statusLedSet(false);
    // Skip the trailing gap after the last pulse — nothing follows it before we
    // sleep, so it would just be dead wait time.
    if (i == 0) delay(STATUS_LED_BLINK_OFF_MS);
  }
#endif
}
