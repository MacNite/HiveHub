// ============================================================================
// night_mode.h — deciding when to suspend a HiveTraffic counter
// ============================================================================
//
// Pure logic, free of Arduino / NimBLE / time.h, so the decision can be
// exercised on a host compiler against the cases that actually bite — midnight
// wrap, a clock that never synced, a hive still flying at 21:00 in June — none
// of which anyone will sit through on real hardware. See
// test-data/test_night_mode.cpp. bee_counter_client.cpp owns the GATT session
// and calls in here for the verdict.
//
// ---------------------------------------------------------------------------
// Why this decision lives on HiveHub
// ---------------------------------------------------------------------------
// The counter has no RTC, no NVS and no network; HiveHub has NTP and a DS3231
// good to +/-2 ppm. So HiveHub owns the schedule and the counter is told only a
// DURATION — "stop sensing for N seconds" — which it forgets on any reset. See
// HiveTraffic's docs/ble-mode.md for the device half.
//
// That split is what makes the whole feature fail safe. Every path through
// decide() that is not certain returns "count": no clock, no traffic baseline,
// a window that would swallow the day, an OTA about to run. The worst outcome
// of a bug here is a counter that keeps counting, which is what it did before
// night mode existed.
//
// ---------------------------------------------------------------------------
// Why the suspension is re-armed rather than set once
// ---------------------------------------------------------------------------
// decide() is called once per upload cycle and grants a little over one cycle
// of suspension at a time. Nothing accumulates: the counter's own clock only
// has to be right for ten minutes, HiveHub's DS3231 keeps the actual schedule,
// and a HiveHub that stops calling — dead, out of range, carried off — lets the
// counter free itself within one grant. A single "sleep until 06:00" would put
// all of that on the counter's temperature-dependent RC oscillator and give it
// no way back if the schedule turned out to be wrong.
// ============================================================================

#pragma once

#include <stdint.h>

namespace nightmode {

// Minutes in a day, and the sentinel for "no valid local time".
constexpr uint16_t MINUTES_PER_DAY = 1440;

// Bounds on a single grant. The floor keeps a short configured upload interval
// from turning into a stream of tiny suspensions the counter has to re-arm
// constantly; the ceiling matches HiveTraffic's own MAX_IDLE_SECONDS, so what
// is asked for is what gets granted and the log does not lie about it.
constexpr uint32_t MIN_GRANT_S = 120;
constexpr uint32_t MAX_GRANT_S = 3600;

// How much longer than one upload cycle each grant runs. Exactly one cycle
// would have the counter resuming — lighting all 48 emitters — in the gap
// before HiveHub reconnects, every single cycle of the night. Two cycles rides
// out one missed connection and still frees the counter promptly if HiveHub
// stops calling for good.
constexpr uint32_t GRANT_CYCLES = 2;

// Why decide() answered the way it did. Logged verbatim: "the counter kept
// counting last night" is otherwise a question with six plausible answers.
//
// CamelCase, not SCREAMING_CASE, and not by taste: Arduino's esp32-hal-gpio.h
// carries `#define DISABLED 0x00`, and the preprocessor does not care that
// these live in an enum class inside a namespace — an all-caps enumerator is
// substituted anywhere it appears, and the whole namespace fails to parse. The
// firmware build caught exactly that. Any name added here must stay out of the
// all-caps macro namespace that Arduino and ESP-IDF share. (This also matches
// the house style already used by ClaimStatus in hivehub_network.h.)
enum class Reason : uint8_t {
    Disabled,          // night mode is off for this device
    NoClock,           // no valid local time — never guess at the schedule
    InvalidWindow,     // start == end, or a value out of range
    OutsideWindow,     // daytime
    RelayPending,      // an OTA relay is queued for this counter
    TrafficUnknown,    // no previous totals to difference yet
    TrafficAboveLimit, // still busy; postpone to the next cycle
    Suspend,           // all clear: stop sensing
};

// Per-device configuration, as edited in the dashboard and delivered by
// /api/v1/devices/{id}/config.
struct Config {
    bool     enabled      = false;         // OFF unless deliberately turned on
    uint16_t start_minute = 20 * 60;       // 20:00 local
    uint16_t end_minute   = 6 * 60;        // 06:00 local
    // Crossings (in + out) in the last upload cycle above which night mode is
    // postponed to the next cycle. 0 disables the gate, entering the window on
    // the clock alone.
    uint32_t max_traffic  = 0;
};

// One hive's traffic since the previous cycle.
struct Traffic {
    bool     known      = false;   // is there a previous reading to difference?
    uint32_t crossings  = 0;       // in + out over that interval
};

struct Decision {
    bool     suspend    = false;
    uint32_t duration_s = 0;       // 0 when suspend is false
    Reason   reason     = Reason::Disabled;
};

// Is `minute` inside the window, which may wrap midnight?
//
// start == end is treated as EMPTY, not as a 24-hour window. The other reading
// would mean a single mis-set field could stop a counter for good, one hour at
// a time, and there is no legitimate configuration it would serve — a beekeeper
// who wants the counter off entirely unpairs it.
inline bool inWindow(uint16_t minute, uint16_t start, uint16_t end) {
    if (start >= MINUTES_PER_DAY || end >= MINUTES_PER_DAY) return false;
    if (start == end) return false;
    if (start < end) return minute >= start && minute < end;
    return minute >= start || minute < end;   // wraps midnight
}

// Crossings between two lifetime totals, handling a counter that restarted.
//
// The counter's totals are monotonic until it reboots, when they restart from
// zero. Attributing that as `current` — rather than as a huge negative, or as
// zero — matches what the backend does when it differences the same totals
// (server/measurements.py::difference_bee_counter_intervals), so the gate here
// and the interval charted later never disagree about how busy a hive was.
inline uint32_t crossingsBetween(uint32_t previous, uint32_t current) {
    return current >= previous ? current - previous : current;
}

// How long a single grant should run, given the configured upload interval.
inline uint32_t grantSeconds(uint32_t cycle_seconds) {
    uint64_t grant = (uint64_t)cycle_seconds * GRANT_CYCLES;
    if (grant < MIN_GRANT_S) grant = MIN_GRANT_S;
    if (grant > MAX_GRANT_S) grant = MAX_GRANT_S;
    return (uint32_t)grant;
}

// Decide what to tell one counter this cycle.
//
// `minute_of_day` is local time, or >= MINUTES_PER_DAY for "unknown". The
// checks are ordered cheapest-and-most-decisive first, and every early return
// is "keep counting".
inline Decision decide(const Config& cfg, uint16_t minute_of_day,
                       const Traffic& traffic, uint32_t cycle_seconds,
                       bool relay_pending) {
    Decision d;

    if (!cfg.enabled) {
        d.reason = Reason::Disabled;
        return d;
    }
    if (cfg.start_minute >= MINUTES_PER_DAY ||
        cfg.end_minute >= MINUTES_PER_DAY ||
        cfg.start_minute == cfg.end_minute) {
        d.reason = Reason::InvalidWindow;
        return d;
    }
    // An unsynced clock is the one input whose absence is silent: RTC missing
    // and NTP not yet reached leaves a plausible-looking 1970 that would put
    // every hive in the middle of the night forever.
    if (minute_of_day >= MINUTES_PER_DAY) {
        d.reason = Reason::NoClock;
        return d;
    }
    // Never suspend a counter we are about to stream an image to. The relay
    // needs it awake, and the standing advice is specifically to relay at night
    // because a transfer costs counted bees — so the two would collide exactly
    // when both are meant to run.
    if (relay_pending) {
        d.reason = Reason::RelayPending;
        return d;
    }
    if (!inWindow(minute_of_day, cfg.start_minute, cfg.end_minute)) {
        d.reason = Reason::OutsideWindow;
        return d;
    }
    if (cfg.max_traffic > 0) {
        // No baseline yet (first cycle after a HiveHub reboot, or the first
        // successful read of this counter): the gate exists precisely to avoid
        // shutting the beam on a hive that is still flying, and "I don't know"
        // is not "it's quiet". Costs one cycle.
        if (!traffic.known) {
            d.reason = Reason::TrafficUnknown;
            return d;
        }
        if (traffic.crossings > cfg.max_traffic) {
            d.reason = Reason::TrafficAboveLimit;
            return d;
        }
    }

    d.suspend    = true;
    d.duration_s = grantSeconds(cycle_seconds);
    d.reason     = Reason::Suspend;
    return d;
}

inline const char* reasonName(Reason r) {
    switch (r) {
    case Reason::Disabled:          return "disabled";
    case Reason::NoClock:           return "no valid local time";
    case Reason::InvalidWindow:     return "invalid window";
    case Reason::OutsideWindow:     return "outside the night window";
    case Reason::RelayPending:      return "an OTA relay is queued";
    case Reason::TrafficUnknown:    return "no traffic baseline yet";
    case Reason::TrafficAboveLimit: return "still busy";
    case Reason::Suspend:           return "inside the night window";
    }
    return "unknown";
}

}  // namespace nightmode
