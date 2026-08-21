// ============================================================================
// Host tests for firmware/include/night_mode.h
// ============================================================================
//
// Night mode decides, once per upload cycle, whether to blind a bee counter. It
// is therefore a feature whose bugs are quiet in both directions: suspend when
// you should not and a day's foraging vanishes into a row of zeros that looks
// like bad weather; fail to suspend and nothing happens at all except a flat
// battery three weeks later. Neither shows up in a bench session, and the
// interesting inputs — midnight, an unsynced clock, a hive still flying at
// 21:00 in June — are not ones anyone will sit through on hardware.
//
//     g++ -std=gnu++17 -Wall -Wextra -Werror -I firmware/include
//         -o /tmp/t test-data/test_night_mode.cpp && /tmp/t
//
// Runs in CI on a plain host compiler — no ESP32, no counter, no hive.
// ============================================================================

// ---------------------------------------------------------------------------
// Arduino's macro soup, reproduced before the include on purpose.
// ---------------------------------------------------------------------------
// This header is built two ways: here, against a bare host compiler, and in the
// firmware, after Arduino.h has defined a few hundred all-caps macros. The
// second is far more hostile, and this test could not see it — a `Reason`
// enumerator named DISABLED collided with esp32-hal-gpio.h's
// `#define DISABLED 0x00` and broke both firmware builds while every check here
// passed.
//
// So define the ones that actually bite, with their real Arduino values, before
// including night_mode.h. Any future identifier that strays into the all-caps
// macro namespace now fails on a host compiler in seconds instead of three
// minutes into a firmware build. These are deliberately NOT #undef'd.
#define DISABLED 0x00
#define INPUT 0x01
#define OUTPUT 0x03
#define PULLUP 0x04
#define PULLDOWN 0x08
#define HIGH 0x1
#define LOW 0x0
#define ANALOG 0xC0
#define OPEN_DRAIN 0x10

#include "night_mode.h"

#include <cstdio>
#include <cstdlib>

using namespace nightmode;

static int g_failures = 0;
static const char* g_case = "";

#define CHECK(cond)                                                          \
    do {                                                                     \
        if (!(cond)) {                                                       \
            std::printf("  FAIL %s:%d  [%s]  %s\n", __FILE__, __LINE__,      \
                        g_case, #cond);                                      \
            ++g_failures;                                                    \
        }                                                                    \
    } while (0)

static constexpr uint16_t hm(int h, int m) {
    return static_cast<uint16_t>(h * 60 + m);
}

static constexpr uint32_t CYCLE_S = 600;   // HiveHub's default 10 minutes

// A configuration that is on, with the window the dashboard defaults to.
static Config enabled() {
    Config c;
    c.enabled = true;
    c.start_minute = hm(20, 0);
    c.end_minute = hm(6, 0);
    c.max_traffic = 0;
    return c;
}

static Traffic quiet(uint32_t crossings = 0) {
    Traffic t;
    t.known = true;
    t.crossings = crossings;
    return t;
}

// --------------------------------------------------------------------------

static void test_window_wraps_midnight() {
    // The whole point of the default window is that it spans midnight, which is
    // the arithmetic everyone gets wrong once.
    g_case = "a 20:00-06:00 window wraps midnight";
    const uint16_t start = hm(20, 0), end = hm(6, 0);
    CHECK(inWindow(hm(20, 0), start, end));    // inclusive at the start
    CHECK(inWindow(hm(23, 59), start, end));
    CHECK(inWindow(hm(0, 0), start, end));     // midnight itself
    CHECK(inWindow(hm(3, 30), start, end));
    CHECK(inWindow(hm(5, 59), start, end));
    CHECK(!inWindow(hm(6, 0), start, end));    // exclusive at the end
    CHECK(!inWindow(hm(12, 0), start, end));
    CHECK(!inWindow(hm(19, 59), start, end));
}

static void test_window_within_one_day() {
    g_case = "a window that does not cross midnight";
    const uint16_t start = hm(1, 0), end = hm(5, 0);
    CHECK(!inWindow(hm(0, 59), start, end));
    CHECK(inWindow(hm(1, 0), start, end));
    CHECK(inWindow(hm(4, 59), start, end));
    CHECK(!inWindow(hm(5, 0), start, end));
    CHECK(!inWindow(hm(23, 0), start, end));
}

static void test_equal_bounds_are_empty_not_all_day() {
    // Reading start == end as "all day" would let one mis-set field stop a
    // counter permanently, an hour at a time, with nothing in the data saying
    // why. There is no configuration that reading would serve.
    g_case = "start == end is an empty window, never a 24-hour one";
    CHECK(!inWindow(hm(0, 0), hm(20, 0), hm(20, 0)));
    CHECK(!inWindow(hm(20, 0), hm(20, 0), hm(20, 0)));
    CHECK(!inWindow(hm(12, 0), hm(20, 0), hm(20, 0)));

    Config c = enabled();
    c.end_minute = c.start_minute;
    const Decision d = decide(c, hm(23, 0), quiet(), CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.reason == Reason::InvalidWindow);
}

static void test_out_of_range_minutes_are_refused() {
    g_case = "a nonsense window suspends nothing";
    CHECK(!inWindow(hm(23, 0), 1440, hm(6, 0)));
    CHECK(!inWindow(hm(23, 0), hm(20, 0), 5000));

    Config c = enabled();
    c.start_minute = 1440;
    const Decision d = decide(c, hm(23, 0), quiet(), CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.reason == Reason::InvalidWindow);
}

static void test_disabled_by_default() {
    // The feature ships off. A device that has never been configured must
    // behave exactly as it did before night mode existed.
    g_case = "an unconfigured device is never suspended";
    Config c;                                  // defaults
    CHECK(!c.enabled);
    const Decision d = decide(c, hm(2, 0), quiet(), CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.duration_s == 0);
    CHECK(d.reason == Reason::Disabled);
}

static void test_suspends_inside_the_window() {
    g_case = "the ordinary night";
    const Decision d = decide(enabled(), hm(23, 30), quiet(), CYCLE_S, false);
    CHECK(d.suspend);
    CHECK(d.reason == Reason::Suspend);
    CHECK(d.duration_s == CYCLE_S * GRANT_CYCLES);
}

static void test_daytime_resumes() {
    g_case = "daytime never suspends";
    const Decision d = decide(enabled(), hm(13, 0), quiet(), CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.duration_s == 0);
    CHECK(d.reason == Reason::OutsideWindow);
}

static void test_unknown_clock_never_suspends() {
    // An RTC that lost power and an NTP sync that has not landed leave a
    // plausible-looking 1970. Guessing would park every counter in the fleet.
    g_case = "no valid local time means keep counting";
    const Decision d = decide(enabled(), MINUTES_PER_DAY, quiet(), CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.reason == Reason::NoClock);

    const Decision d2 = decide(enabled(), 60000, quiet(), CYCLE_S, false);
    CHECK(!d2.suspend);
    CHECK(d2.reason == Reason::NoClock);
}

static void test_relay_pending_wins() {
    // The relay needs the counter awake, and the standing advice is to relay at
    // night precisely because a transfer costs counted bees — so without this
    // the two features collide exactly when both are meant to run.
    g_case = "a queued OTA relay keeps the counter awake";
    const Decision d = decide(enabled(), hm(1, 0), quiet(), CYCLE_S, true);
    CHECK(!d.suspend);
    CHECK(d.reason == Reason::RelayPending);
}

static void test_traffic_gate_postpones_a_busy_hive() {
    // The user-facing rule: inside the window, but still busy -> wait. A hot
    // June evening at 20:30 is exactly this case.
    g_case = "a hive still flying postpones night mode";
    Config c = enabled();
    c.max_traffic = 100;

    const Decision busy = decide(c, hm(20, 30), quiet(350), CYCLE_S, false);
    CHECK(!busy.suspend);
    CHECK(busy.reason == Reason::TrafficAboveLimit);

    // At the threshold exactly: not "above", so it suspends. Documented here
    // because "max 100 bees" reads either way to a human.
    const Decision at_limit = decide(c, hm(20, 30), quiet(100), CYCLE_S, false);
    CHECK(at_limit.suspend);

    const Decision settled = decide(c, hm(21, 30), quiet(12), CYCLE_S, false);
    CHECK(settled.suspend);
    CHECK(settled.reason == Reason::Suspend);
}

static void test_traffic_gate_needs_a_baseline() {
    // "I have no idea how busy this hive is" is not "it is quiet". Costs one
    // cycle after a HiveHub reboot; the alternative closes the beam on a hive
    // that may still be flying.
    g_case = "no baseline postpones rather than assuming quiet";
    Config c = enabled();
    c.max_traffic = 100;
    Traffic t;                                  // known == false
    const Decision d = decide(c, hm(23, 0), t, CYCLE_S, false);
    CHECK(!d.suspend);
    CHECK(d.reason == Reason::TrafficUnknown);

    // With the gate off, a missing baseline is irrelevant — the clock decides.
    Config no_gate = enabled();
    const Decision d2 = decide(no_gate, hm(23, 0), t, CYCLE_S, false);
    CHECK(d2.suspend);
}

static void test_grant_is_bounded() {
    // Long enough to ride out one missed cycle, short enough that a HiveHub
    // that dies mid-night frees the counter promptly.
    g_case = "grants are clamped at both ends";
    CHECK(grantSeconds(600) == 1200);
    CHECK(grantSeconds(30) == MIN_GRANT_S);      // absurdly short interval
    CHECK(grantSeconds(0) == MIN_GRANT_S);
    CHECK(grantSeconds(7200) == MAX_GRANT_S);    // absurdly long interval
    CHECK(grantSeconds(0xFFFFFFFFUL) == MAX_GRANT_S);   // no overflow wrap
    CHECK(MAX_GRANT_S <= 3600);                  // matches the counter's cap
}

static void test_crossings_handle_a_counter_reboot() {
    // Same rule the backend applies when it differences the same totals, so the
    // gate here and the interval charted later never disagree.
    g_case = "a counter that restarted is not a negative interval";
    CHECK(crossingsBetween(100, 140) == 40);
    CHECK(crossingsBetween(100, 100) == 0);
    CHECK(crossingsBetween(5000, 12) == 12);     // rebooted, 12 since restart
    CHECK(crossingsBetween(0, 0) == 0);
    CHECK(crossingsBetween(UINT32_MAX, UINT32_MAX) == 0);   // saturated
}

static void test_a_whole_night_of_cycles() {
    // Walk the clock through a night at the real cadence and assert the shape
    // of the whole thing rather than one instant: quiet evening, suspended
    // overnight, counting again at 06:00 sharp.
    g_case = "one simulated night, cycle by cycle";
    Config c = enabled();
    c.max_traffic = 100;

    int suspended = 0, counting = 0;
    for (int minute = 0; minute < 1440; minute += 10) {
        // A plausible day: busy from 08:00 to 20:00, nothing outside it.
        const bool flying = minute >= hm(8, 0) && minute < hm(20, 0);
        const Decision d = decide(c, static_cast<uint16_t>(minute),
                                  quiet(flying ? 400 : 0), CYCLE_S, false);
        if (d.suspend) {
            ++suspended;
            CHECK(inWindow(static_cast<uint16_t>(minute), c.start_minute, c.end_minute));
        } else {
            ++counting;
        }
    }
    // 20:00-06:00 is 600 minutes = 60 ten-minute cycles, all of them quiet.
    CHECK(suspended == 60);
    CHECK(counting == 84);

    // And the boundaries specifically.
    CHECK(decide(c, hm(19, 50), quiet(400), CYCLE_S, false).suspend == false);
    CHECK(decide(c, hm(20, 0), quiet(0), CYCLE_S, false).suspend == true);
    CHECK(decide(c, hm(5, 50), quiet(0), CYCLE_S, false).suspend == true);
    CHECK(decide(c, hm(6, 0), quiet(0), CYCLE_S, false).suspend == false);
}

int main() {
    std::printf("night_mode tests\n");
    test_window_wraps_midnight();
    test_window_within_one_day();
    test_equal_bounds_are_empty_not_all_day();
    test_out_of_range_minutes_are_refused();
    test_disabled_by_default();
    test_suspends_inside_the_window();
    test_daytime_resumes();
    test_unknown_clock_never_suspends();
    test_relay_pending_wins();
    test_traffic_gate_postpones_a_busy_hive();
    test_traffic_gate_needs_a_baseline();
    test_grant_is_bounded();
    test_crossings_handle_a_counter_reboot();
    test_a_whole_night_of_cycles();

    if (g_failures == 0) {
        std::printf("all tests passed\n");
        return EXIT_SUCCESS;
    }
    std::printf("%d check(s) failed\n", g_failures);
    return EXIT_FAILURE;
}
