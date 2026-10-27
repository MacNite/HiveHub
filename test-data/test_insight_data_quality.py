#!/usr/bin/env python3
"""Insight detectors must respect what the data actually covers.

Regression tests for the insight findings of the 2026-10 codebase audit, each
reproducing the audit's scenario against the real engine:

I1  An inspection or calibration cuts the series. A super lifted off during an
    inspection (readings masked, weight 2 kg lower afterwards) raised a
    CRITICAL swarm alert, and readings flagged only as calibration did too.
I2  Acoustic evidence is a snapshot of one recent reading from a working mic.
    A 13-day-old stress band upgraded a current robbing alert, although the
    newest reading said the microphone had failed.
I3  Counter rates are counts per actual elapsed time. A steady 600 bees/h with
    one missed poll read as 1200 bees/h, and the first interval after an outage
    looked like a burst to the swarm rule.
I5  A minimum point count is not coverage. Eight readings over seven minutes
    were reported as "24h hive temp held at 35 degC".
I4  A change in what the counter covers (another counter, fewer emitter banks,
    a dead port expander) is not a change in the colony, and night-idle
    readings are a blind period, not zero traffic.
I6  Time-of-day rules run in the apiary's local time, and the seasons follow
    its hemisphere.

Run: PYTHONPATH=server python3 test-data/test_insight_data_quality.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

import insights  # noqa: E402

NOW = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)  # active season, daytime
FAILURES = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        FAILURES.append(name)


def rows(start, n, step_min, **fields):
    """n readings from `start`, `step_min` apart; each field is a value or f(i)."""
    out = []
    for i in range(n):
        row = {"measured_at": start + timedelta(minutes=step_min * i)}
        for key, value in fields.items():
            row[key] = value(i) if callable(value) else value
        out.append(row)
    return out


def alert_ids(measurements, now=NOW):
    return {a.id: a for a in insights.compute_insights(measurements, now=now)}


# ---------------------------------------------------------------------------
print("\n=== I1. Inspections and calibration cut the series ===")

# 2 h of steady 50 kg, a 20-minute inspection whose reading is masked (as the
# read layer leaves it), then 48 kg: the super that came off.
before = rows(NOW - timedelta(minutes=120), 9, 10, scale_1_weight_kg=50.0)
masked = [{"measured_at": NOW - timedelta(minutes=35), "scale_1_weight_kg": None,
           "inspection": True, "inspection_hives": None}]
after = rows(NOW - timedelta(minutes=25), 3, 10, scale_1_weight_kg=48.0)
super_off = before + masked + after
plain_drop = before + after  # the same drop with no inspection in between

check("a 2 kg drop with no inspection is still a swarm event",
      "swarm-event-ch1" in alert_ids(plain_drop))
found = alert_ids(super_off)
check("a super lifted off during an inspection is not a swarm", "swarm-event-ch1" not in found)
check("... nor robbing", "robbing-ch1" not in found)

# An inspection limited to hive 2 does not cut hive 1's series.
other_hive = before + [dict(masked[0], inspection_hives=[2])] + after
check("an inspection of another hive does not hide hive 1's drop",
      "swarm-event-ch1" in alert_ids(other_hive))

# The hub took no reading while the hive was open: the read layer marks the
# first reading afterwards instead.
marked = before + [dict(after[0], after_inspection=True, after_inspection_hives=None)] + after[1:]
check("an inspection with no reading inside it still cuts the series",
      "swarm-event-ch1" not in alert_ids(marked))

# Readings flagged only as calibration (test weights on and off the scale).
calibration = before + rows(NOW - timedelta(minutes=35), 3, 5, scale_1_weight_kg=lambda i: 45.0 + i,
                            calibration_mode=True) + after
check("calibration readings raise no swarm alert", "swarm-event-ch1" not in alert_ids(calibration))

# Rebasing keeps a real change inside a segment visible.
rebased = insights._rebase_across(
    [(NOW - timedelta(minutes=30), 50.0), (NOW - timedelta(minutes=20), 48.0),
     (NOW - timedelta(minutes=10), 46.0)],
    [NOW - timedelta(minutes=20)],
)
check("rebasing splices the level and keeps the later change",
      [v for _, v in rebased] == [50.0, 50.0, 48.0])

# Brood temperature right after an inspection is skipped while it settles.
temps = [(NOW - timedelta(minutes=m), 30.0) for m in (40, 20, 5)]
settled = insights._drop_settling(temps, [NOW - timedelta(minutes=40)], insights.INSPECTION_SETTLE_MIN)
check("temperature readings within the settle time are dropped",
      [t for t, _ in settled] == [NOW - timedelta(minutes=5)])


# ---------------------------------------------------------------------------
print("\n=== I2. Acoustic evidence is recent, coherent and from a working mic ===")

# Robbing by weight (0.6 kg/h over 2 h, outside the late afternoon -> watch).
morning = NOW.replace(hour=11)
robbing_rows = rows(morning - timedelta(hours=2), 13, 10, scale_1_weight_kg=lambda i: 50.0 - 0.1 * i)
stale = {"measured_at": morning - timedelta(days=13),
         "mic_1_band_stress_dbfs": insights.ROBBING_STRESS_DBFS + 5.0}
fresh = {"measured_at": morning - timedelta(minutes=30),
         "mic_1_band_stress_dbfs": insights.ROBBING_STRESS_DBFS + 5.0}
broken = {"measured_at": morning - timedelta(minutes=5), "mic_1_ok": False}

base = alert_ids(robbing_rows, morning)["robbing-ch1"]
with_stale = alert_ids([stale] + robbing_rows, morning)["robbing-ch1"]
with_fresh = alert_ids(robbing_rows[:-3] + [fresh] + robbing_rows[-3:], morning)["robbing-ch1"]
with_broken = alert_ids(robbing_rows[:-3] + [fresh] + robbing_rows[-3:] + [broken], morning)["robbing-ch1"]
check("weight-only robbing is a watch (precondition)", base.severity == "watch")
check("a 13-day-old stress band does not upgrade it", with_stale.severity == "watch")
check("... and is not reported as evidence", "acoustic_stress_dbfs" not in with_stale.evidence)
check("a recent stress band still upgrades it", with_fresh.severity == "warning")
check("a newer failed-mic reading withdraws the acoustic evidence", with_broken.severity == "watch")

# All bands of a snapshot come from the same reading.
mixed = [
    {"measured_at": NOW - timedelta(hours=2), "mic_3_band_piping_dbfs": -40.0, "mic_3_band_hum_dbfs": -35.0},
    {"measured_at": NOW - timedelta(hours=1), "mic_3_band_hum_dbfs": -30.0},
]
snap = insights._mic_band_snapshot(mixed, 3, NOW, insights.MIC_EVIDENCE_MAX_AGE_HOURS)
check("bands are not stitched together from different readings",
      snap["hum"] == -30.0 and snap["piping"] is None)


# ---------------------------------------------------------------------------
print("\n=== I3. Counter rates over the time actually counted ===")

# A steady 600 bees/h; polls at 0, 10, 20, 80, 90, 100 minutes (one outage).
minutes = [0, 10, 20, 80, 90, 100]
counts = [(NOW - timedelta(minutes=100 - m), 600.0 * (m - p) / 60.0)
          for p, m in zip([-10] + minutes[:-1], minutes)]
rate = insights._per_hour_rate(counts)
check(f"steady 600 bees/h with a missed poll reads as 600 (got {rate:.0f})", abs(rate - 600.0) < 1e-6)

rates = insights._interval_rates(counts)
check("per-interval rates are 600/h", all(abs(v - 600.0) < 1e-6 for _, v in rates))
check("the outage interval is left out of peak tests",
      NOW - timedelta(minutes=100 - 80) not in [t for t, _ in rates])

# An interval spanning midnight is split across both days.
midnight = datetime(2026, 6, 14, tzinfo=timezone.utc)
span = [(midnight - timedelta(hours=2), 0.0), (midnight + timedelta(hours=2), 400.0)]
daily = dict(insights._bucket_by_day(span, midnight + timedelta(hours=3), 2))
check("a count spanning midnight is shared by both days",
      daily.get(midnight - timedelta(days=1)) == 200.0 and daily.get(midnight) == 200.0)


# ---------------------------------------------------------------------------
print("\n=== I5. Coverage, not just a point count ===")

seven_minutes = rows(NOW - timedelta(minutes=7), 8, 1, hive_1_temp_c=35.0)
whole_day = rows(NOW - timedelta(hours=24) + timedelta(minutes=10), 144, 10, hive_1_temp_c=35.0)
check("eight readings over seven minutes are not 24 h of brood rearing",
      insights.detect_brood_cycle_state(insights._extract_series(seven_minutes, "hive_1_temp_c"), 1, NOW) is None)
day_alert = insights.detect_brood_cycle_state(
    insights._extract_series(whole_day, "hive_1_temp_c"), 1, NOW)
check("a whole day at 35 degC is", day_alert is not None and day_alert.title.startswith("Active brood"))
check("the description states the span it saw",
      day_alert is not None and "last 24h" in day_alert.description)
stopped = rows(NOW - timedelta(hours=24), 60, 10, hive_1_temp_c=35.0)  # nothing for the last 14 h
check("a day whose readings stopped half-way is not covered",
      insights.detect_brood_cycle_state(insights._extract_series(stopped, "hive_1_temp_c"), 1, NOW) is None)
check("foraging needs a covered day too",
      insights.detect_foraging_intensity(
          insights._extract_series(rows(NOW - timedelta(hours=1), 6, 10, scale_1_weight_kg=lambda i: 50 + i),
                                   "scale_1_weight_kg"), 1, NOW) is None)


# ---------------------------------------------------------------------------
print("\n=== I4. Counter coverage changes start a new comparison ===")

# 14 days of steady outbound traffic; on day 10 one of three emitter banks is
# switched off, so the counter sees two thirds of the bees from then on.
start = NOW - timedelta(days=14)
cov_rows = []
for i in range(14 * 24):
    at = start + timedelta(hours=i)
    banks = 7 if at < start + timedelta(days=10) else 3
    cov_rows.append({"measured_at": at, "bee_counter_1_ok": True, "bee_counter_1_banks": banks,
                     "bee_counter_1_interval_out": 300.0 if banks == 7 else 200.0})
out_all = insights._extract_counter_series(cov_rows, 1, "out")
check("the coverage change is found",
      insights._counter_coverage_start(cov_rows, 1) == start + timedelta(days=10))
decline_before, _, _ = insights._forager_decline_frac_per_day(out_all, NOW, 14, 0.0)
check("(precondition) across the change it looks like a forager decline",
      decline_before is not None and decline_before > 0.02)
cut = [p for p in out_all if p[0] > insights._counter_coverage_start(cov_rows, 1)]
decline_after, _, _ = insights._forager_decline_frac_per_day(cut, NOW, 14, 0.0)
check("within the current coverage there is none", decline_after is None or abs(decline_after) < 0.01)
check("a field missing on one side is not a change",
      insights._counter_coverage_start([dict(cov_rows[0], bee_counter_1_banks=None)] + cov_rows[1:3], 1) is None)

idle = [
    {"measured_at": NOW - timedelta(minutes=30), "bee_counter_1_ok": True, "bee_counter_1_total_out": 100},
    {"measured_at": NOW - timedelta(minutes=20), "bee_counter_1_ok": True, "bee_counter_1_total_out": 100,
     "bee_counter_1_idle_s": 1800},
    {"measured_at": NOW - timedelta(minutes=10), "bee_counter_1_ok": True, "bee_counter_1_total_out": 130},
]
check("night-idle readings add no zero-traffic points",
      [v for _, v in insights._extract_counter_series(idle, 1, "out")] == [30.0])
swap = [
    {"measured_at": NOW - timedelta(minutes=20), "bee_counter_1_ok": True, "bee_counter_1_total_out": 5000,
     "bee_counter_1_mac": "AA"},
    {"measured_at": NOW - timedelta(minutes=10), "bee_counter_1_ok": True, "bee_counter_1_total_out": 900000,
     "bee_counter_1_mac": "BB"},
]
check("a replaced counter is not read as one huge interval",
      insights._extract_counter_series(swap, 1, "out") == [])


# ---------------------------------------------------------------------------
print("\n=== I6. Local time and hemisphere ===")
from zoneinfo import ZoneInfo  # noqa: E402

# Robbing is a warning in the local late afternoon (15-19 h). 15:30 in Sydney
# is 04:30 UTC, which the engine used to read as early morning.
sydney = ZoneInfo("Australia/Sydney")
local_afternoon = datetime(2026, 1, 15, 15, 30, tzinfo=sydney).astimezone(timezone.utc)
rob = rows(local_afternoon - timedelta(hours=2), 13, 10, scale_1_weight_kg=lambda i: 50.0 - 0.1 * i)
utc_alert = {a.id: a for a in insights.compute_insights(rob, now=local_afternoon)}["robbing-ch1"]
local_alert = {a.id: a for a in insights.compute_insights(
    rob, now=local_afternoon, site=insights.SiteProfile(tz=sydney))}["robbing-ch1"]
check("in UTC the local afternoon reads as morning (precondition)", utc_alert.severity == "watch")
check("with the apiary's zone it is the late afternoon", local_alert.severity == "warning")

january = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
south = insights.SiteProfile(southern_hemisphere=True)
token = insights._SITE.set(south)
try:
    check("January is the active season in the southern hemisphere", insights._is_active_season(january))
    check("... and July its winter", insights._is_winter(datetime(2026, 7, 15, tzinfo=timezone.utc)))
finally:
    insights._SITE.reset(token)
check("the default stays northern", not insights._is_active_season(january))


if FAILURES:
    print(f"\n{len(FAILURES)} check(s) failed")
    sys.exit(1)
print("\nAll insight data-quality checks passed.")
