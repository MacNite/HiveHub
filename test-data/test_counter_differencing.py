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

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nAll counter differencing checks passed.")
