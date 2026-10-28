// ============================================================================
// Host tests for firmware/include/hiveinside_audio_header.h
// ============================================================================
//
// HiveInside 0.8.0 reports the gain it applied to a clip in DATA header byte 3
// (issue HiveInside#78). Older nodes always send 0 there, so the hub has to
// decide whether to believe the byte, and a wrong call is silent: a recording
// labelled "0 dB" that was really made at +20 dB throws off every comparison
// between clips made afterwards, and nothing in the audio says so.
//
//     g++ -std=gnu++17 -Wall -Wextra -Werror -I firmware/include
//         -o /tmp/t test-data/test_hiveinside_audio_header.cpp && /tmp/t
//
// Runs in CI on a plain host compiler — no ESP32, no node, no hive.
// ============================================================================

// The header is also compiled after Arduino.h, whose all-caps macros have
// broken a pure header before (see test_night_mode.cpp). Define the ones that
// bite, with their real values, so a collision fails here first.
#define DISABLED 0x00
#define INPUT 0x01
#define OUTPUT 0x03
#define HIGH 0x1
#define LOW 0x0

#include "hiveinside_audio_header.h"

#include <cstdint>
#include <cstdio>

using namespace hiaudio;

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

// The parse as gatt_audio.cpp's onNotify does it: byte 3 of every packet.
static HeaderGain feed(const uint8_t* headerBytes, int n) {
    HeaderGain h;
    for (int i = 0; i < n; i++) h.observe(headerBytes[i]);
    return h;
}

int main() {
    g_case = "a steady applied gain is read as signed";
    {
        const uint8_t pkts[] = {0xDF, 0xDF, 0xDF, 0xDF};  // -33 dB
        HeaderGain h = feed(pkts, 4);
        CHECK(h.seen);
        CHECK(h.gainDb == -33);
        CHECK(h.changes == 0);
    }

    g_case = "a positive gain survives the int8 read";
    {
        const uint8_t pkts[] = {33, 33, 33};
        HeaderGain h = feed(pkts, 3);
        CHECK(h.gainDb == 33 && h.changes == 0);
    }

    g_case = "a gain that changes mid-clip is counted, first value kept";
    {
        const uint8_t pkts[] = {0xDF, 0xDF, 0x10, 0xDF, 0x11};
        HeaderGain h = feed(pkts, 5);
        CHECK(h.gainDb == -33);
        CHECK(h.changes == 2);
    }

    g_case = "no packet means nothing was seen";
    {
        HeaderGain h;
        CHECK(!h.seen && h.changes == 0);
        CHECK(!appliedGainReportable(h, "0.8.0"));
    }

    g_case = "reset() clears a previous session";
    {
        const uint8_t pkts[] = {5, 6};
        HeaderGain h = feed(pkts, 2);
        h.reset();
        CHECK(!h.seen && h.gainDb == 0 && h.changes == 0);
        h.observe(7);
        CHECK(h.seen && h.gainDb == 7 && h.changes == 0);
    }

    g_case = "version parsing";
    CHECK(versionAtLeast("0.8.0", 0, 8, 0));
    CHECK(versionAtLeast("0.8.1", 0, 8, 0));
    CHECK(versionAtLeast("0.10.0", 0, 8, 0));   // numeric, not lexical
    CHECK(versionAtLeast("1.0.0", 0, 8, 0));
    CHECK(versionAtLeast("0.8", 0, 8, 0));
    CHECK(!versionAtLeast("0.7.9", 0, 8, 0));
    CHECK(!versionAtLeast("0.7.12", 0, 8, 0));
    CHECK(!versionAtLeast("0.6.2", 0, 8, 0));
    CHECK(!versionAtLeast("", 0, 8, 0));
    CHECK(!versionAtLeast(nullptr, 0, 8, 0));
    CHECK(!versionAtLeast("garbage", 0, 8, 0));
    CHECK(!versionAtLeast("0.8.", 0, 8, 0));
    CHECK(!versionAtLeast("0..8", 0, 8, 0));
    CHECK(!versionAtLeast("0.8.0.1", 0, 8, 0));
    CHECK(!versionAtLeast("v0.8.0", 0, 8, 0));

    g_case = "only a 0.8.0+ node's byte is reported";
    {
        const uint8_t pkts[] = {0, 0, 0};  // what every older node sends
        HeaderGain h = feed(pkts, 3);
        CHECK(!appliedGainReportable(h, "0.7.3"));  // would be a false "0 dB"
        CHECK(!appliedGainReportable(h, ""));        // version unknown this boot
        CHECK(appliedGainReportable(h, "0.8.0"));    // a real 0 dB from auto
    }

    g_case = "a changed gain is not reported";
    {
        const uint8_t pkts[] = {20, 21};
        CHECK(!appliedGainReportable(feed(pkts, 2), "0.8.0"));
    }

    g_case = "only what the backend accepts is reported";
    {
        const uint8_t lo[] = {(uint8_t)-20}, below[] = {(uint8_t)-21};
        const uint8_t hi[] = {40}, above[] = {41}, sentinel[] = {0x7F};
        CHECK(appliedGainReportable(feed(lo, 1), "0.8.0"));
        CHECK(!appliedGainReportable(feed(below, 1), "0.8.0"));
        CHECK(appliedGainReportable(feed(hi, 1), "0.8.0"));
        CHECK(!appliedGainReportable(feed(above, 1), "0.8.0"));
        CHECK(!appliedGainReportable(feed(sentinel, 1), "0.8.0"));
    }

    g_case = "the auto sentinel is the byte the START request carries";
    CHECK((uint8_t)GAIN_AUTO == 0x7F);

    if (g_failures) {
        std::printf("%d check(s) failed\n", g_failures);
        return 1;
    }
    std::printf("All HiveInside audio header checks passed.\n");
    return 0;
}
