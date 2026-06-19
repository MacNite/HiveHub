"""
Behavioral tests for the BeeCounter entrance-counter rules in insights.py.

Run: python3 test_counter_rules.py
Builds synthetic measurement dicts (same shape as measurement_row_to_dict)
and asserts the new rules fire — and that everything degrades when the
counter fields are absent.
"""

from datetime import datetime, timedelta, timezone

import insights


NOW = datetime(2024, 6, 15, 16, 0, tzinfo=timezone.utc)  # active season, afternoon
WINTER_NOW = datetime(2024, 1, 15, 13, 0, tzinfo=timezone.utc)

CADENCE_MIN = 10  # firmware polls ~every 10 minutes


def _rows(n, start, step_min=CADENCE_MIN, **field_fns):
    """
    Build n rows ending at `start`+... Each field_fn is field_name -> callable(i)->value.
    i runs 0..n-1 oldest→newest. measured_at is spaced step_min apart ending at NOW-ish.
    """
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=step_min * i)
        row = {"measured_at": ts.isoformat()}
        for field, fn in field_fns.items():
            row[field] = fn(i)
        rows.append(row)
    return rows


def _const(v):
    return lambda i: v


def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}")
    if not cond:
        raise AssertionError(name)


# ---------------------------------------------------------------------------
print("\n=== 1. Swarm event: weight drop + asymmetric outflow ===")
# 2h of 10-min rows. Weight steady then steps down 2kg. Outbound spikes in the
# drop window with near-zero inbound.
start = NOW - timedelta(hours=2)
n = 13  # 0..12, 10 min apart = 2h
def weight_fn(i):
    return 50.0 if i < 8 else 48.0  # 2 kg drop after row 8
def out_fn(i):
    # baseline ~2/interval, then a massive spike of 200 at the drop
    return 200.0 if i in (8, 9) else 2.0
def in_fn(i):
    return 1.0  # almost nobody coming in
rows = _rows(n, start,
             scale_1_weight_kg=weight_fn,
             hive_1_temp_c=_const(34.5),
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_out=out_fn,
             bee_counter_1_interval_in=in_fn)
bee_in = insights._extract_counter_series(rows, 1, "in")
bee_out = insights._extract_counter_series(rows, 1, "out")
weight = insights._extract_series(rows, "scale_1_weight_kg")
a_with = insights.detect_swarm_event(weight, 1, NOW, bee_in, bee_out)
a_without = insights.detect_swarm_event(weight, 1, NOW)
check("fires with counter", a_with is not None)
check("counter swarm signature recognized", a_with.evidence.get("counter_swarm_signature") is True)
check("daytime stays critical", a_with.severity == "critical")
check("confidence boosted above base", a_with.confidence > a_without.confidence)
check("still fires (weight-only) without counter", a_without is not None)
check("weight-only has no counter signature key", "counter_swarm_signature" not in a_without.evidence)

# Night-time + asymmetric outflow should be promoted warning->critical
NIGHT = datetime(2024, 6, 15, 23, 0, tzinfo=timezone.utc)
start_n = NIGHT - timedelta(hours=2)
rows_n = _rows(n, start_n,
               scale_1_weight_kg=weight_fn,
               bee_counter_1_ok=_const(True),
               bee_counter_1_interval_out=out_fn,
               bee_counter_1_interval_in=in_fn)
w_n = insights._extract_series(rows_n, "scale_1_weight_kg")
bi_n = insights._extract_counter_series(rows_n, 1, "in")
bo_n = insights._extract_counter_series(rows_n, 1, "out")
night_with = insights.detect_swarm_event(w_n, 1, NIGHT, bi_n, bo_n)
night_without = insights.detect_swarm_event(w_n, 1, NIGHT)
check("night-time weight-only is warning", night_without.severity == "warning")
check("night-time + counter promoted to critical", night_with.severity == "critical")


# ---------------------------------------------------------------------------
print("\n=== 2. Queenlessness: forager decline as 3rd corroborator ===")
# 7 days of rows. Temp variance high, weight stagnant -> base rule fires.
# Outbound traffic declines steadily ~8%/day.
days = 7
start = NOW - timedelta(days=days)
n = days * 24 * 6  # 6 rows/hour
def qtemp(i):
    # oscillate to create stddev > 1.0
    return 34.0 + (2.0 if (i % 2 == 0) else -2.0)
def qweight(i):
    return 50.0  # flat
def qout(i):
    # start ~600/day => ~4.17/interval; decline ~8%/day across the window
    day = i / (24 * 6)
    base_per_interval = 4.17
    return max(0.0, base_per_interval * (1.0 - 0.08 * day))
rows = _rows(n, start,
             hive_1_temp_c=qtemp,
             scale_1_weight_kg=qweight,
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_out=qout)
temp = insights._extract_series(rows, "hive_1_temp_c")
weight = insights._extract_series(rows, "scale_1_weight_kg")
bee_out = insights._extract_counter_series(rows, 1, "out")
q_with = insights.detect_queenlessness(temp, weight, 1, NOW, None, bee_out)
q_without = insights.detect_queenlessness(temp, weight, 1, NOW, None, None)
check("base rule fires without counter", q_without is not None)
check("fires with counter", q_with is not None)
check("forager decline active", q_with.evidence.get("forager_decline_active") is True)
check("confidence boosted by forager decline", q_with.confidence > q_without.confidence)
check("decline frac approx 8%/day",
      0.05 <= q_with.evidence["forager_decline_frac_per_day"] <= 0.12)

# Forager decline alone must NOT fire the alert (no temp/weight signature)
def calm_temp(i):
    return 34.8  # very low variance
rows_calm = _rows(n, start,
                  hive_1_temp_c=calm_temp,
                  scale_1_weight_kg=qweight,
                  bee_counter_1_ok=_const(True),
                  bee_counter_1_interval_out=qout)
t2 = insights._extract_series(rows_calm, "hive_1_temp_c")
w2 = insights._extract_series(rows_calm, "scale_1_weight_kg")
bo2 = insights._extract_counter_series(rows_calm, 1, "out")
q_calm = insights.detect_queenlessness(t2, w2, 1, NOW, None, bo2)
check("forager decline alone does NOT fire", q_calm is None)


# ---------------------------------------------------------------------------
print("\n=== 3. Robbing: incoming spike, low outgoing ===")
# 2h, weight bleeding 0.5 kg/h, inbound >> outbound.
start = NOW - timedelta(hours=2)
n = 13
def rweight(i):
    return 50.0 - 0.5 * (i / 6.0)  # ~0.5 kg/h loss over 2h => ~1kg
def rin(i):
    return 60.0   # ~360/h inbound
def rout(i):
    return 5.0    # ~30/h outbound
rows = _rows(n, start,
             scale_1_weight_kg=rweight,
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_in=rin,
             bee_counter_1_interval_out=rout)
weight = insights._extract_series(rows, "scale_1_weight_kg")
bee_in = insights._extract_counter_series(rows, 1, "in")
bee_out = insights._extract_counter_series(rows, 1, "out")
r_with = insights.detect_robbing(weight, 1, NOW, None, bee_in, bee_out)
r_without = insights.detect_robbing(weight, 1, NOW)
check("fires with counter", r_with is not None)
check("counter robbing signature recognized", r_with.evidence.get("counter_robbing_signature") is True)
check("asymmetry positive and high", r_with.evidence["counter_asymmetry"] >= 0.4)
check("fires weight-only too", r_without is not None)
check("counter boosts confidence", r_with.confidence > r_without.confidence)


# ---------------------------------------------------------------------------
print("\n=== 4. Foraging: counter corroboration & contradiction ===")
# Strong flow + active traffic => confidence boost.
start = NOW - timedelta(hours=24)
n = 24 * 6
def fweight(i):
    return 50.0 + 1.5 * (i / (n - 1))  # +1.5 kg over 24h => strong
def fout_active(i):
    return 30.0  # ~180/h outbound, > 100 threshold
rows = _rows(n, start,
             scale_1_weight_kg=fweight,
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_out=fout_active)
weight = insights._extract_series(rows, "scale_1_weight_kg")
bee_out = insights._extract_counter_series(rows, 1, "out")
f_active = insights.detect_foraging_intensity(weight, 1, NOW, bee_out)
f_plain = insights.detect_foraging_intensity(weight, 1, NOW)
check("strong flow fires", f_active is not None and f_active.evidence["level"] == "strong")
check("active traffic corroborates", f_active.evidence.get("counter_corroborates") is True)
check("corroboration boosts confidence", f_active.confidence > f_plain.confidence)

# Strong weight gain but NO traffic => confidence dropped (suspect artefact).
def fout_dead(i):
    return 1.0  # ~6/h outbound, well below threshold
rows2 = _rows(n, start,
              scale_1_weight_kg=fweight,
              bee_counter_1_ok=_const(True),
              bee_counter_1_interval_out=fout_dead)
w2 = insights._extract_series(rows2, "scale_1_weight_kg")
bo2 = insights._extract_counter_series(rows2, 1, "out")
f_suspect = insights.detect_foraging_intensity(w2, 1, NOW, bo2)
check("weight gain w/o traffic flagged not corroborated",
      f_suspect.evidence.get("counter_corroborates") is False)
check("confidence reduced below plain", f_suspect.confidence < f_plain.confidence)


# ---------------------------------------------------------------------------
print("\n=== 5. Absconding: promote watch -> warning on 3rd rule ===")
days = 14
start = NOW - timedelta(days=days)
n = days * 24 * 4  # 4 rows/hour to keep it light
def aweight(i):
    # lose ~150 g/day over 14 days
    day = i / (24 * 4)
    return 50.0 - 0.15 * day
def atemp(i):
    # widening variance: amplitude grows with time
    day = i / (24 * 4)
    amp = 0.3 + 0.15 * day
    return 34.5 + (amp if (i % 2 == 0) else -amp)
def aout(i):
    # decline ~5%/day
    day = i / (24 * 4)
    return max(0.0, 4.17 * (1.0 - 0.05 * day))
rows = _rows(n, start,
             scale_1_weight_kg=aweight,
             hive_1_temp_c=atemp,
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_out=aout)
temp = insights._extract_series(rows, "hive_1_temp_c")
weight = insights._extract_series(rows, "scale_1_weight_kg")
bee_out = insights._extract_counter_series(rows, 1, "out")
ab_with = insights.detect_absconding_trend(temp, weight, 1, NOW, bee_out)
ab_without = insights.detect_absconding_trend(temp, weight, 1, NOW)
check("base (2-of-3) fires without counter", ab_without is not None)
check("base severity is watch", ab_without.severity == "watch")
check("fires with counter", ab_with is not None)
check("forager decline active", ab_with.evidence.get("forager_decline_active") is True)
check("3-of-3 promoted to warning", ab_with.severity == "warning")
check("3-of-3 confidence 0.75", abs(ab_with.confidence - 0.75) < 1e-9)
check("source mentions 3 of 3", "3 of 3" in ab_with.source)


# ---------------------------------------------------------------------------
print("\n=== 6. Winter: cleansing flight softens risk ===")
days = 7
start = WINTER_NOW - timedelta(days=days)
n = days * 24 * 4
def wtemp(i):
    return 8.0   # barely above ambient => cluster_weak
def wamb(i):
    return 7.0
def wweight(i):
    # lose ~400 g/week => consumption_high
    day = i / (24 * 4)
    return 30.0 - (0.4 / 7.0) * day
def wout_flight(i):
    # one warm-day cleansing flight spike
    return 80.0 if i == n - 10 else 0.0
rows = _rows(n, start,
             hive_1_temp_c=wtemp,
             ambient_temp_c=wamb,
             scale_1_weight_kg=wweight,
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_out=wout_flight)
temp = insights._extract_series(rows, "hive_1_temp_c")
amb = insights._extract_series(rows, "ambient_temp_c")
weight = insights._extract_series(rows, "scale_1_weight_kg")
bee_out = insights._extract_counter_series(rows, 1, "out")
w_with = insights.detect_winter_risk(temp, amb, weight, 1, WINTER_NOW, bee_out)
w_without = insights.detect_winter_risk(temp, amb, weight, 1, WINTER_NOW)
check("fires without counter", w_without is not None)
check("fires with counter", w_with is not None)
check("cleansing flight detected", w_with.evidence.get("cleansing_flight_seen") is True)
check("cleansing flight lowers confidence", w_with.confidence < w_without.confidence)


# ---------------------------------------------------------------------------
print("\n=== 7. Graceful degradation: ok=False rows are skipped ===")
# All counter rows marked ok=False -> series empty -> behaves like no counter.
start = NOW - timedelta(hours=2)
n = 13
rows = _rows(n, start,
             scale_1_weight_kg=weight_fn,
             bee_counter_1_ok=_const(False),
             bee_counter_1_interval_out=out_fn,
             bee_counter_1_interval_in=in_fn)
bo = insights._extract_counter_series(rows, 1, "out")
check("ok=False yields empty series", bo == [])


# ---------------------------------------------------------------------------
print("\n=== 8. compute_insights end-to-end with counters present ===")
# Use the robbing scenario through the orchestrator and confirm an alert
# with a counter signature comes out.
start = NOW - timedelta(hours=2)
n = 13
rows = _rows(n, start,
             scale_1_weight_kg=rweight,
             hive_1_temp_c=_const(34.5),
             ambient_temp_c=_const(20.0),
             bee_counter_1_ok=_const(True),
             bee_counter_1_interval_in=rin,
             bee_counter_1_interval_out=rout)
alerts = insights.compute_insights(rows, now=NOW)
robbing = [a for a in alerts if a.category == "robbing"]
check("orchestrator emits a robbing alert", len(robbing) >= 1)
check("robbing alert carries counter signature",
      robbing[0].evidence.get("counter_robbing_signature") is True)

# Channel 2 has no counter at all -> ch2 detectors must not error and must
# not carry counter signatures.
ch2 = [a for a in alerts if a.channel == 2]
check("channel 2 detectors don't crash (may be empty)", isinstance(ch2, list))


# ---------------------------------------------------------------------------
print("\n=== 9. Legacy rows (no counter fields at all) ===")
start = NOW - timedelta(hours=2)
rows = _rows(n, start, scale_1_weight_kg=rweight, hive_1_temp_c=_const(34.5))
alerts = insights.compute_insights(rows, now=NOW)
# Should still produce the weight-only robbing alert, no counter keys.
robbing = [a for a in alerts if a.category == "robbing"]
check("legacy rows still produce robbing alert", len(robbing) >= 1)
check("legacy robbing has no counter signature",
      "counter_robbing_signature" not in robbing[0].evidence)

# ---------------------------------------------------------------------------
print("\n=== 10. Totals-only (BLE) path: interval derived from lifetime totals ===")
# The wireless/BLE counter reports only monotonic total_in/out (no interval).
# _extract_counter_series must difference consecutive totals into per-interval
# counts, handle a missed poll (wider gap), and treat a reboot (total going
# backwards) as the post-reboot count rather than a huge negative delta.
start = NOW - timedelta(hours=1)

# Cumulative outbound totals: +5, +7, +0, +12 across 4 successive readings,
# i.e. per-interval deltas 5, 7, 0, 12 (the first reading has no baseline).
totals_out = [100, 105, 112, 112, 124]
def tout(i):
    return totals_out[i]
rows = _rows(len(totals_out), start,
             bee_counter_1_ok=_const(True),
             bee_counter_1_total_out=tout)
# Deliberately NO bee_counter_1_interval_out field -> totals-only path.
series = insights._extract_counter_series(rows, 1, "out")
vals = [v for _, v in series]
check("derives interval from totals", vals == [5.0, 7.0, 0.0, 12.0])
check("first reading has no baseline (dropped)", len(series) == len(totals_out) - 1)

# Reboot: totals climb, then the device restarts and totals begin again at a
# small value. The drop must be read as "count since restart", not negative.
reboot_out = [200, 205, 3, 9]   # reboot between idx1(205) and idx2(3)
def rtout(i):
    return reboot_out[i]
rows_rb = _rows(len(reboot_out), start,
                bee_counter_1_ok=_const(True),
                bee_counter_1_total_out=rtout)
series_rb = insights._extract_counter_series(rows_rb, 1, "out")
vals_rb = [v for _, v in series_rb]
check("reboot delta is post-restart total, not negative", vals_rb == [5.0, 3.0, 6.0])

# Device-reported interval still wins when present (I2C latch path unchanged),
# even if a total is also present.
rows_mixed = _rows(3, start,
                   bee_counter_1_ok=_const(True),
                   bee_counter_1_interval_out=_const(2.0),
                   bee_counter_1_total_out=lambda i: 100 + i * 99)  # totals ignored
series_mixed = insights._extract_counter_series(rows_mixed, 1, "out")
check("device interval preferred over totals", [v for _, v in series_mixed] == [2.0, 2.0, 2.0])

print("\nAll checks passed. ✅")
