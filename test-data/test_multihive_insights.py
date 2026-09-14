"""
Behavioral tests for two ways the insight engine used to ignore hives it was
told about, both of which failed silently and neither of which any existing
suite covered.

Run: python3 test_multihive_insights.py

1. Hive channel range. compute_insights() iterates every hive a device reports
   (up to MAX_HIVES = 18), but `Alert.channel` was annotated `Literal[1, 2]`
   from the two-scale era. Alert is a pydantic model, so constructing one for
   hive 3 raised ValidationError, and the blanket `except Exception` around each
   detector swallowed it — every detector in every category was dead for hives
   3-18, with no alert, no log line and no error anywhere to notice.

2. Acoustic band source. _mic_band_snapshot() read only the legacy stereo keys
   (mic_left_* for channel 1, mic_right_* for everything else). An in-hive BLE
   node's acoustics land in the per-hive mic_{n}_* keys the read layer
   synthesizes from hives[].mic, so a HiveInside supplied five FFT bands that no
   detector ever read — while hives 3-18 were handed hive 2's microphone.

Both are asserted end-to-end (through compute_insights) as well as at the unit
level, because the first bug was invisible at the unit level: the detector
returned a perfectly good Alert and it was thrown away afterwards.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# Same loader as the other suites here: prefer the authoritative server/ copy.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(_HERE, "..", "server"),
    os.path.join(_HERE, "mock-server"),
    _HERE,
):
    if os.path.exists(os.path.join(_candidate, "insights.py")):
        sys.path.insert(0, _candidate)
        break

import insights


# Active season, midday, so the night windows both detectors use have room.
NOW = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

# Every hive index a device can report. 1 and 2 are the historical scale
# channels; 3 and up are the ones that were silently dropped.
ALL_HIVES = list(range(1, 19))


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        raise AssertionError(name)


def _vibration_rows(channel, recent_mg=12.0, baseline_mg=5.0):
    """Night-time per-cycle vibration rising over its baseline, for one hive.

    Carries both the nested hives[] entry (which _hive_channels reads to decide
    which channels to run) and the flat accel_{n}_* aliases the read layer
    synthesizes, so the shape matches what serialize_measurements() produces.
    """
    rows = []
    recent_days = (1.8, 1.4, 1.0, 0.4)
    baseline_days = (2.4, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)
    for days_ago, mg in (
        [(d, recent_mg) for d in recent_days] + [(d, baseline_mg) for d in baseline_days]
    ):
        ts = (NOW - timedelta(days=days_ago)).replace(hour=2, minute=0)
        rows.append({
            "measured_at": ts,
            "hives": [{"index": channel, "accel": {"ok": True, "rms_mg": mg}}],
            f"accel_{channel}_ok": True,
            f"accel_{channel}_rms_mg": mg,
        })
    rows.sort(key=lambda r: r["measured_at"])
    return rows


print("=== 1. Every reported hive runs its detectors, not just 1 and 2 ===")

for hive in ALL_HIVES:
    alerts = insights.compute_insights(_vibration_rows(hive), now=NOW)
    check(f"hive {hive}: vibration alert produced",
          any(a.category == "swarm" for a in alerts))
    check(f"hive {hive}: alert carries its own channel",
          all(a.channel == hive for a in alerts))

# The regression in its most direct form: an Alert for a hive above 2 must be
# constructible at all. This is what the Literal[1, 2] annotation rejected.
for hive in (3, 9, 18):
    alert = insights.Alert(
        id=f"probe-ch{hive}", category="swarm", severity="watch", channel=hive,
        title="probe", description="probe", confidence=0.5,
    )
    check(f"Alert(channel={hive}) validates", alert.channel == hive)


print("\n=== 2. Acoustic bands come from the hive's own microphone ===")

BANDS = ("sub_bass", "hum", "piping", "stress", "high")


def _mic_row(per_hive=None, legacy=None):
    """One measurement carrying per-hive mic_{n}_* and/or legacy stereo keys."""
    row = {"measured_at": NOW - timedelta(hours=1)}
    for hive, value in (per_hive or {}).items():
        for band in BANDS:
            row[f"mic_{hive}_band_{band}_dbfs"] = value
    for side, value in (legacy or {}).items():
        for band in BANDS:
            row[f"mic_{side}_band_{band}_dbfs"] = value
    return row


# A HiveInside on any hive: its bands arrive under mic_{n}_*, and every hive
# must read its own.
per_hive_row = _mic_row(per_hive={h: -30.0 - h for h in ALL_HIVES})
for hive in ALL_HIVES:
    snap = insights._mic_band_snapshot([per_hive_row], hive)
    check(f"hive {hive}: reads its own per-hive bands",
          all(snap[b] == -30.0 - hive for b in BANDS))

# A legacy stereo-INMP441 device: left is hive 1, right is hive 2, and nothing
# else may borrow either of them.
legacy_row = _mic_row(legacy={"left": -41.0, "right": -52.0})
check("hive 1 falls back to the left channel",
      insights._mic_band_snapshot([legacy_row], 1)["piping"] == -41.0)
check("hive 2 falls back to the right channel",
      insights._mic_band_snapshot([legacy_row], 2)["piping"] == -52.0)
for hive in (3, 7, 18):
    snap = insights._mic_band_snapshot([legacy_row], hive)
    check(f"hive {hive} does not borrow the right channel",
          all(snap[b] is None for b in BANDS))

# Both shapes on one row (a device with wired mics AND a beacon): the hive's own
# reading wins over the legacy alias.
mixed_row = _mic_row(per_hive={1: -30.0}, legacy={"left": -60.0})
check("per-hive key wins over the legacy alias",
      insights._mic_band_snapshot([mixed_row], 1)["piping"] == -30.0)


print("\n=== 3. Acoustic evidence reaches a detector for any hive ===")

# Queen piping raises the confidence of the pre-swarm temperature watch. Run it
# with and without per-hive acoustics on a hive above 2 — before the fix the
# bands were invisible there, so the two were always identical.
PIPING_LOUD = insights.PIPING_ACTIVE_DBFS + 5.0


# Amplitudes chosen so the temperature rule fires (24 h stddev >= 1.5x the 7 d
# baseline) but lands well short of the 1.0 confidence cap — otherwise the
# acoustic boost has nowhere to go and the assertion below cannot fail.
_BASELINE_SWING_C = 0.4
_RECENT_SWING_C = 0.8


def _temp_instability_rows(channel, piping=None):
    """Rising 24 h brood-temperature variability, optionally with acoustics."""
    rows = []
    for i, hours in enumerate(range(24 * 7, 24, -3)):
        ts = NOW - timedelta(hours=hours)
        swing = _BASELINE_SWING_C if i % 2 else -_BASELINE_SWING_C
        rows.append({"measured_at": ts, f"hive_{channel}_temp_c": 35.0 + swing,
                     "hives": [{"index": channel}]})
    for i, hours in enumerate(range(24, 0, -1)):
        ts = NOW - timedelta(hours=hours)
        swing = _RECENT_SWING_C if i % 2 else -_RECENT_SWING_C
        rows.append({"measured_at": ts, f"hive_{channel}_temp_c": 35.0 + swing,
                     "hives": [{"index": channel}]})
    if piping is not None:
        for band in BANDS:
            rows[-1][f"mic_{channel}_band_{band}_dbfs"] = piping
    return rows


for hive in (1, 5, 18):
    temps = insights._extract_series(_temp_instability_rows(hive), f"hive_{hive}_temp_c")
    quiet = insights.detect_pre_swarm_temp_instability(
        temps, hive, NOW, _temp_instability_rows(hive), None)
    loud = insights.detect_pre_swarm_temp_instability(
        temps, hive, NOW, _temp_instability_rows(hive, piping=PIPING_LOUD), None)
    # The acoustic boost only exists on top of the temperature watch, so a hive
    # where that did not fire would make the rest of this vacuously true.
    check(f"hive {hive}: temperature watch fires (precondition)",
          quiet is not None and loud is not None)
    check(f"hive {hive}: temperature-only confidence leaves room to boost",
          quiet.confidence < 1.0)
    check(f"hive {hive}: piping is seen as acoustic evidence",
          loud.evidence.get("acoustic_piping_active") is True)
    check(f"hive {hive}: piping raises confidence over temperature alone",
          loud.confidence > quiet.confidence)


print("\nAll multi-hive insight tests passed.\n")
