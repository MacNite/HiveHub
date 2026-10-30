#!/usr/bin/env python3
"""Bee-counter intervals are differenced per device and per counter (audit B4).

difference_bee_counter_intervals kept one previous total per hive channel, so a
result set spanning several hubs (the master-key latest endpoint) differenced
hub A's counter against hub B's: totals A=100, B=1000, A=110, B=1010 gave
intervals [None, 900, 110, 900] instead of [None, None, 10, 10]. A replaced
counter was likewise read as a reboot and credited its whole lifetime total.

Run: PYTHONPATH=server python3 test-data/test_counter_differencing.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")

from measurements import difference_bee_counter_intervals  # noqa: E402

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")
    failures += not ok


def rows(*readings):
    return [
        {"device_id": dev, "measured_at": T0 + timedelta(minutes=i), "bee_counter_1_ok": True,
         "bee_counter_1_total_in": total, **({"bee_counter_1_mac": mac} if mac else {})}
        for i, (dev, total, mac) in enumerate(readings)
    ]


mixed = difference_bee_counter_intervals(rows(("A", 100, None), ("B", 1000, None),
                                              ("A", 110, None), ("B", 1010, None)))
check("two hubs are differenced separately",
      [r.get("bee_counter_1_interval_in") for r in mixed], [None, None, 10, 10])

swapped = difference_bee_counter_intervals(rows(("A", 100, "AA"), ("A", 110, "AA"),
                                                ("A", 500000, "BB"), ("A", 500020, "BB")))
check("a replaced counter starts a new baseline",
      [r.get("bee_counter_1_interval_in") for r in swapped], [None, 10, None, 20])

reboot = difference_bee_counter_intervals(rows(("A", 100, "AA"), ("A", 5, "AA")))
check("a reboot of the same counter still counts since restart",
      [r.get("bee_counter_1_interval_in") for r in reboot], [None, 5])



# Audit F07: the same counter rebooted and counted past its old total before
# the next poll. The totals alone go 100 -> 150 and read as 50; the uptime
# (10 000 s -> 60 s) says it restarted, so all 150 are since the restart.
def timed(*readings, minutes=10):
    return [
        {"device_id": "A", "measured_at": T0 + timedelta(minutes=minutes * i),
         "bee_counter_1_ok": True, "bee_counter_1_mac": "AA",
         "bee_counter_1_total_in": total,
         **({"bee_counter_1_uptime_s": up} if up is not None else {})}
        for i, (total, up) in enumerate(readings)
    ]


def intervals(rs):
    return [r.get("bee_counter_1_interval_in") for r in difference_bee_counter_intervals(rs)]


check("uptime going backwards is a restart even when the total grew",
      intervals(timed((100, 10_000), (150, 60))), [None, 150])
# Restarted right after the previous poll, which itself was shortly after an
# earlier restart: uptime 120 -> 500 grows, but 500 s of uptime cannot span the
# 3600 s between the two samples.
check("uptime shorter than the gap between samples is a restart",
      intervals(timed((100, 120), (150, 500), minutes=60)), [None, 150])
check("an ordinary interval with growing uptime is a plain difference",
      intervals(timed((100, 10_000), (150, 10_600))), [None, 50])
check("a little clock disagreement between hub and counter is not a restart",
      intervals(timed((100, 10_000), (150, 10_540))), [None, 50])
check("rows without uptime keep the totals-only rule",
      intervals(timed((100, None), (150, None), (20, None))), [None, 50, 20])
# Hives 3-18 have no table columns: the uptime arrives through the nested
# hives[] object and its flat alias.
from measurements import _flatten_hive_to_measurement  # noqa: E402
m = {}
_flatten_hive_to_measurement(m, {"index": 5, "bee_counter": {"ok": True, "uptime_s": 42}})
check("the nested uptime is aliased for every hive", m.get("bee_counter_5_uptime_s"), 42)


# Audit F08: the derived interval reached the flat alias only, so a client
# reading the canonical nested hives[] got null for every BLE reading.
def nested_rows(*totals):
    out = []
    for i, total in enumerate(totals):
        m = {"device_id": "A", "measured_at": T0 + timedelta(minutes=10 * i),
             "hives": [{"index": n, "bee_counter": {"ok": True, "total_in": total + n,
                                                    "total_out": 2 * total,
                                                    "interval_in": None, "interval_out": None}}
                       for n in (1, 2, 3)]}
        for h in m["hives"]:
            _flatten_hive_to_measurement(m, h)
        out.append(m)
    return difference_bee_counter_intervals(out)


both = nested_rows(100, 110)
for n in (1, 2, 3):
    bc = both[1]["hives"][n - 1]["bee_counter"]
    check(f"hive {n}: nested interval_in equals the flat alias",
          (bc["interval_in"], both[1].get(f"bee_counter_{n}_interval_in")), (10, 10))
    check(f"hive {n}: nested interval_out equals the flat alias",
          (bc["interval_out"], both[1].get(f"bee_counter_{n}_interval_out")), (20, 20))
check("the first reading stays null in both shapes",
      (both[0]["hives"][0]["bee_counter"]["interval_in"], both[0].get("bee_counter_1_interval_in")),
      (None, None))
wired = {"device_id": "A", "measured_at": T0, "bee_counter_1_ok": True,
         "bee_counter_1_interval_in": 7,
         "hives": [{"index": 1, "bee_counter": {"ok": True, "interval_in": 7}}]}
difference_bee_counter_intervals([wired])
check("a device-reported (wired) interval is left as it was",
      wired["hives"][0]["bee_counter"]["interval_in"], 7)


# Audit F01: a USB maintenance session freezes the totals; the interval it
# falls in must say so instead of passing for a quiet hive.
def maint_rows(*readings):
    return [
        {"device_id": "A", "measured_at": T0 + timedelta(minutes=10 * i),
         "bee_counter_1_ok": True, "bee_counter_1_mac": "AA",
         "bee_counter_1_total_in": total, "bee_counter_1_uptime_s": 10_000 + 600 * i,
         **({"bee_counter_1_maint_s": maint} if maint is not None else {})}
        for i, (total, maint) in enumerate(readings)
    ]


mr = difference_bee_counter_intervals(maint_rows((100, 0), (100, 540), (130, 540), (130, None)))
check("the maintenance interval is marked with its seconds",
      [r.get("bee_counter_1_interval_maint_s") for r in mr], [None, 540, 0, None])
check("the count outside the session is still derived",
      [r.get("bee_counter_1_interval_in") for r in mr], [None, 0, 30, 0])
legacy = difference_bee_counter_intervals(maint_rows((100, None), (110, None)))
check("a counter without maint_s is 'cannot say', not 'no maintenance'",
      "bee_counter_1_interval_maint_s" in legacy[1], False)
upgraded = difference_bee_counter_intervals(maint_rows((100, None), (110, 0)))
check("the first v6 reading after an older one is still 'cannot say'",
      "bee_counter_1_interval_maint_s" in upgraded[1], False)
# The figure lives in RAM: after a restart all of it is new.
rb = maint_rows((100, 900), (20, 60))
rb[1]["bee_counter_1_uptime_s"] = 300
check("after a restart the whole new figure falls in the interval",
      difference_bee_counter_intervals(rb)[1].get("bee_counter_1_interval_maint_s"), 60)
m = {}
_flatten_hive_to_measurement(m, {"index": 4, "bee_counter": {"ok": True, "maint_s": 77}})
check("the nested maint_s is aliased for every hive", m.get("bee_counter_4_maint_s"), 77)
nested = maint_rows((100, 0), (100, 300))
for r in nested:
    r["hives"] = [{"index": 1, "bee_counter": {"ok": True}}]
difference_bee_counter_intervals(nested)
check("the maintenance seconds reach the nested object too",
      nested[1]["hives"][0]["bee_counter"].get("interval_maint_s"), 300)


# Audit F06: a counter the hub's read rotation skipped reports ok:false with
# polled:false. The flag must reach the flat alias the dashboard also reads,
# and the skipped row must not become a baseline or an interval.
m = {}
_flatten_hive_to_measurement(m, {"index": 6, "bee_counter": {"ok": False, "polled": False,
                                                             "mac": "AA"}})
check("the polled flag is aliased", (m.get("bee_counter_6_ok"), m.get("bee_counter_6_polled")),
      (False, False))
skipped = rows(("A", 100, "AA"), ("A", 999, "AA"), ("A", 130, "AA"))
skipped[1]["bee_counter_1_ok"] = False
skipped[1]["bee_counter_1_polled"] = False
check("a skipped row is neither a baseline nor an interval",
      [r.get("bee_counter_1_interval_in") for r in difference_bee_counter_intervals(skipped)],
      [None, None, 30])

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nAll counter differencing checks passed.")
