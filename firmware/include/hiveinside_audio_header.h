// ============================================================================
// hiveinside_audio_header.h — the gain a HiveInside node applied to a clip
// ============================================================================
//
// Pure logic, free of Arduino / NimBLE, so it runs on a host compiler — see
// test-data/test_hiveinside_audio_header.cpp. gatt_audio.cpp feeds it every
// DATA notification; hivehub_network.cpp asks it whether the result may be
// reported.
//
// ---------------------------------------------------------------------------
// The contract (HiveInside 0.8.0, issue HiveInside#78)
// ---------------------------------------------------------------------------
// START byte 5 is the requested gain: -20..+40 dB, or GAIN_AUTO (0x7F) for
// "choose one gain for this clip yourself". DATA header byte 3 is the gain the
// node actually applied, as a signed int8 — the same in every packet of a
// clip, the final header-only packet included.
//
// Before 0.8.0 that byte was reserved and always 0. Read from an old node it
// would say "0 dB" whatever the node really did (+20 dB for an auto request),
// so the byte is only worth reporting once the node's own firmware version
// says it means something: nodeReportsAppliedGain().
// ============================================================================

#pragma once

#include <stdint.h>

namespace hiaudio {

// START byte 5: let the node pick the gain. An older node reads it as +127 dB
// and clamps it to +20 — a louder clip, never a refused one — so the hub sends
// it without a version check.
constexpr int8_t GAIN_AUTO = 0x7F;

// What a node may legitimately report as applied. Anything outside is a
// protocol anomaly, and the backend would reject the whole finish report over
// it, so it is never forwarded.
constexpr int8_t APPLIED_GAIN_MIN_DB = -20;
constexpr int8_t APPLIED_GAIN_MAX_DB = 40;

// First HiveInside firmware whose DATA header byte 3 carries the applied gain.
constexpr int APPLIED_GAIN_SINCE_MAJOR = 0;
constexpr int APPLIED_GAIN_SINCE_MINOR = 8;
constexpr int APPLIED_GAIN_SINCE_PATCH = 0;

// DATA header byte 3, accumulated over a session.
//
// observe() runs on the NimBLE host task inside the audio ring's critical
// section, so it is a compare and a store — nothing that could log or block.
struct HeaderGain {
    int8_t   gainDb  = 0;      // first value seen
    bool     seen    = false;  // false: no DATA packet arrived
    uint32_t changes = 0;      // packets that disagreed; must stay 0

    void reset() { gainDb = 0; seen = false; changes = 0; }

    void observe(uint8_t headerByte3) {
        const int8_t g = (int8_t)headerByte3;
        if (!seen) { gainDb = g; seen = true; }
        else if (g != gainDb) changes++;
    }
};

// "major.minor.patch" -> true when it is at least major.minor.patch given.
// Missing trailing parts read as 0 ("0.8" is 0.8.0). Empty, non-numeric or
// otherwise unparsable input is false: an unknown version is not a new one.
inline bool versionAtLeast(const char* v, int major, int minor, int patch) {
    if (v == nullptr || *v == '\0') return false;
    int part[3] = {0, 0, 0};
    int idx = 0;
    bool digit = false;
    for (const char* c = v; *c; ++c) {
        if (*c >= '0' && *c <= '9') {
            if (part[idx] > 100000) return false;  // nonsense, not a version
            part[idx] = part[idx] * 10 + (*c - '0');
            digit = true;
        } else if (*c == '.' && digit && idx < 2) {
            idx++;
            digit = false;
        } else {
            return false;
        }
    }
    if (!digit) return false;  // "", "1.", "1..2"
    if (part[0] != major) return part[0] > major;
    if (part[1] != minor) return part[1] > minor;
    return part[2] >= patch;
}

// True when a node on firmware `fwVersion` (its beacon identity record) puts
// the applied gain in DATA header byte 3.
inline bool nodeReportsAppliedGain(const char* fwVersion) {
    return versionAtLeast(fwVersion, APPLIED_GAIN_SINCE_MAJOR,
                          APPLIED_GAIN_SINCE_MINOR, APPLIED_GAIN_SINCE_PATCH);
}

// Whether a session's applied gain belongs in the finish report. Only when a
// packet arrived, every packet agreed (one gain per clip is the contract; a
// clip that changed mid-way has no single answer), the node is new enough for
// the byte to mean anything, and the value is one the backend accepts.
inline bool appliedGainReportable(const HeaderGain& h, const char* fwVersion) {
    return h.seen && h.changes == 0 && nodeReportsAppliedGain(fwVersion) &&
           h.gainDb >= APPLIED_GAIN_MIN_DB && h.gainDb <= APPLIED_GAIN_MAX_DB;
}

}  // namespace hiaudio
