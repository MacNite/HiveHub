// inspection.cpp — see inspection.h for what inspection mode is and why.
#include "inspection.h"

#include <esp_attr.h>   // RTC_DATA_ATTR
#include <esp_sleep.h>
#include <time.h>

#include "globals.h"

#if HAS_INSPECTION_BUTTON
#include <driver/gpio.h>
#endif

namespace inspection {
namespace {

// Guards the RTC block against garbage. RTC memory is uninitialised on a cold
// boot, and "is this hub inspecting?" is exactly the question you do not want
// answered by whatever was in SRAM at power-up.
constexpr uint32_t kMagic = 0x1E5BEC70;

RTC_DATA_ATTR uint32_t rtcMagic = 0;
RTC_DATA_ATTR uint32_t rtcActive = 0;
// Unix seconds at the start, or 0 when the clock was untrustworthy then.
RTC_DATA_ATTR uint32_t rtcStartedEpoch = 0;
// Fallback age counter for a hub with no usable clock: seconds accumulated one
// send interval at a time. Never as accurate as the epoch difference, but it
// still ends a forgotten inspection, which is the whole job of the timeout.
RTC_DATA_ATTR uint32_t rtcElapsedS = 0;

uint32_t timeoutMin = INSPECTION_DEFAULT_TIMEOUT_MINUTES;

#if HAS_INSPECTION_BUTTON
bool buttonWasDown = false;
unsigned long buttonDownMs = 0;
unsigned long lastToggleMs = 0;
// Cleared when a press is accepted, set again on release: one press, one
// toggle, however long a gloved thumb rests on the button.
bool toggleArmed = true;
#endif

bool stateValid() { return rtcMagic == kMagic; }

void store(bool on, uint32_t startedEpoch, uint32_t elapsedS) {
  rtcMagic = kMagic;
  rtcActive = on ? 1 : 0;
  rtcStartedEpoch = startedEpoch;
  rtcElapsedS = elapsedS;
}

// Wall-clock seconds, or 0 when neither the DS3231 nor the system clock holds a
// plausible time. Deliberately reuses timestampNow()'s notion of "plausible"
// rather than trusting time(nullptr) on a hub that never reached NTP: a 1970
// timestamp differenced against a 2026 one would end every inspection instantly.
uint32_t epochNow() {
  if (rtcOk) {
    DateTime now = rtc.now();
    if (now.year() >= 2024 && now.year() <= 2099) return (uint32_t)now.unixtime();
  }
  time_t sys = time(nullptr);
  if (sys > 1700000000) return (uint32_t)sys;
  return 0;
}

void persistTimeout() {
  prefs.begin("hivescale", false);
  prefs.putUInt("insp_tmo", timeoutMin);
  prefs.end();
}

void logState(const char* verb, const char* source) {
  Serial.printf("[INSPECT] %s (%s); hive readings will be flagged, hub sensors keep reporting\n",
                verb, source);
}

}  // namespace

bool active() { return stateValid() && rtcActive != 0; }

uint32_t startedAt() { return active() ? rtcStartedEpoch : 0; }

void setTimeoutMinutes(uint32_t minutes) {
  uint32_t next = minutes == 0 ? (uint32_t)INSPECTION_DEFAULT_TIMEOUT_MINUTES : minutes;
  if (next > INSPECTION_TIMEOUT_MAX_MINUTES) next = INSPECTION_TIMEOUT_MAX_MINUTES;
  if (next == timeoutMin) return;
  Serial.printf("[INSPECT] Timeout %lu -> %lu minutes\n",
                (unsigned long)timeoutMin, (unsigned long)next);
  timeoutMin = next;
  persistTimeout();
}

void setActive(bool on, const char* source) {
  if (on == active()) {
    Serial.printf("[INSPECT] Already %s (%s); nothing to do\n",
                  on ? "active" : "inactive", source);
    return;
  }
  if (on) {
    store(true, epochNow(), 0);
    logState("Inspection STARTED", source);
  } else {
    store(false, 0, 0);
    Serial.printf("[INSPECT] Inspection ENDED (%s); hive readings count again\n", source);
  }
}

void begin(uint32_t wakeCauses, uint64_t gpioWakeMask) {
  if (!stateValid()) {
    // Cold boot (or corrupted RTC): come up measuring. See the header — losing
    // an "on" costs one visible spike, losing an "off" costs a silent hive.
    store(false, 0, 0);
  }

  prefs.begin("hivescale", true);
  uint32_t stored = prefs.getUInt("insp_tmo", INSPECTION_DEFAULT_TIMEOUT_MINUTES);
  prefs.end();
  if (stored == 0 || stored > INSPECTION_TIMEOUT_MAX_MINUTES) {
    stored = INSPECTION_DEFAULT_TIMEOUT_MINUTES;
  }
  timeoutMin = stored;

  // Age the fallback counter by one send interval per wake. Only used when the
  // hub has no clock; the epoch difference wins whenever it is available.
  if (active()) {
    rtcElapsedS += (uint32_t)(sendIntervalMs / 1000UL);
    // Backfill the start time if the clock only became trustworthy later, so the
    // record the backend keeps has a real start rather than a null.
    if (rtcStartedEpoch == 0) {
      uint32_t now = epochNow();
      if (now != 0 && now > rtcElapsedS) rtcStartedEpoch = now - rtcElapsedS;
    }
  }

#if HAS_INSPECTION_BUTTON
  pinMode(INSPECTION_BUTTON_PIN, INPUT_PULLUP);

  // A press that woke us from deep sleep is a toggle, exactly like a press while
  // awake. Which pin did it has to come from the wake status, not from reading
  // the pin: a firm press is over long before this line runs on a hub that took
  // a second to boot.
  const bool wokeOnInspectionPin =
      (gpioWakeMask & (1ULL << INSPECTION_BUTTON_PIN)) != 0;
  if (wokeOnInspectionPin) {
    setActive(!active(), "button (wake)");
    lastToggleMs = millis();
    // The press that woke us is almost certainly still held. Count it as
    // consumed so loop() waits for a release before accepting the next one.
    buttonWasDown = digitalRead(INSPECTION_BUTTON_PIN) == LOW;
    buttonDownMs = millis();
    toggleArmed = false;
  }
#else
  (void)gpioWakeMask;
#endif
  (void)wakeCauses;

  if (active()) {
    Serial.printf("[INSPECT] Inspection ACTIVE (%lu s elapsed, timeout %lu min)\n",
                  (unsigned long)rtcElapsedS, (unsigned long)timeoutMin);
  }
}

void poll() {
#if HAS_INSPECTION_BUTTON
  const bool down = digitalRead(INSPECTION_BUTTON_PIN) == LOW;
  const unsigned long now = millis();

  if (!down) {
    buttonWasDown = false;
    toggleArmed = true;
    return;
  }
  if (!buttonWasDown) {
    buttonWasDown = true;
    buttonDownMs = now;
    return;
  }
  // Held. Toggle once, on the far side of the debounce window, then wait for a
  // release: a long hold means the same as a short press here, so a gloved
  // thumb resting on the button cannot flip the state over and over.
  if (!toggleArmed) return;
  if (now - buttonDownMs < INSPECTION_DEBOUNCE_MS) return;
  if (lastToggleMs != 0 && now - lastToggleMs < INSPECTION_TOGGLE_COOLDOWN_MS) return;

  toggleArmed = false;
  lastToggleMs = now;
  setActive(!active(), "button");
#endif
}

bool enforceTimeout() {
  if (!active()) return false;

  uint32_t elapsed = rtcElapsedS;
  const uint32_t now = epochNow();
  if (now != 0 && rtcStartedEpoch != 0 && now > rtcStartedEpoch) {
    elapsed = now - rtcStartedEpoch;
  }
  if (elapsed < timeoutMin * 60UL) return false;

  Serial.printf("[INSPECT] Timeout: %lu s >= %lu min; ending inspection\n",
                (unsigned long)elapsed, (unsigned long)timeoutMin);
  setActive(false, "timeout");
  return true;
}

uint64_t wakePinMask() {
#if HAS_INSPECTION_BUTTON
  return 1ULL << INSPECTION_BUTTON_PIN;
#else
  return 0;
#endif
}

void prepareWakePin() {
#if HAS_INSPECTION_BUTTON
  // Same treatment as the setup button: the C6 does not power-cycle GPIOs in
  // deep sleep, so holding the pull-up is what keeps the pin high (and the
  // button meaningful) across the sleep boundary.
  gpio_pullup_en((gpio_num_t)INSPECTION_BUTTON_PIN);
  gpio_hold_en((gpio_num_t)INSPECTION_BUTTON_PIN);
#endif
}

void releaseWakeHold() {
#if HAS_INSPECTION_BUTTON
  gpio_hold_dis((gpio_num_t)INSPECTION_BUTTON_PIN);
#endif
}

}  // namespace inspection
