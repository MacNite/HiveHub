// status_led.h — on-board user LED used as a lightweight status/heartbeat
// indicator. On the XIAO ESP32-C6 this is the GPIO15 LED (active-low). The whole
// feature compiles to no-ops when ENABLE_STATUS_LED is 0, so callers never need
// their own #if guards.
#pragma once

// Configure the LED pin and force the LED off. Safe to call every boot.
void statusLedInit();

// Drive the LED on / off (respecting the active-low wiring).
void statusLedOn();
void statusLedOff();

// Short single blink to signal a fresh boot/wake.
void statusLedBootBlink();

// Double blink to signal that the wake cycle is done and the device is about to
// enter deep sleep. Leaves the LED off.
void statusLedSleepBlink();
