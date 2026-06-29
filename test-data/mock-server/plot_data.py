#!/usr/bin/env python3
"""
Optional sanity-check plot of the dummy dataset (dev tool, not part of the
container). Renders the seasonal weight cycle, the diurnal saw-tooth, hive vs
ambient temperature, and entrance traffic to a PNG.

    pip install matplotlib
    python plot_data.py --out hivescale_dummy_data.png
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import hive_data


def _daily_mean(measurements, field):
    buckets = defaultdict(list)
    for m in measurements:
        v = m.get(field)
        if v is not None:
            buckets[m["measured_at"].date()].append(v)
    days = sorted(buckets)
    return days, [sum(buckets[d]) / len(buckets[d]) for d in days]


def _daily_sum(measurements, field):
    buckets = defaultdict(float)
    for m in measurements:
        v = m.get(field)
        if v is not None:
            buckets[m["measured_at"].date()] += v
    days = sorted(buckets)
    return days, [buckets[d] for d in days]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="hivescale_dummy_data.png")
    ap.add_argument("--interval-minutes", type=int, default=30)
    args = ap.parse_args()

    m = hive_data.generate_measurements(args.interval_minutes)
    fig, axes = plt.subplots(2, 2, figsize=(16, 9))
    fig.suptitle(
        f"HiveHub mock — dummy data for {hive_data.DEVICE_ID} "
        f"({m[0]['measured_at'].date()} → {m[-1]['measured_at'].date()})",
        fontsize=14, fontweight="bold",
    )

    # (a) Seasonal weight cycle, both hives (daily mean)
    ax = axes[0][0]
    d1, w1 = _daily_mean(m, "scale_1_weight_kg")
    d2, w2 = _daily_mean(m, "scale_2_weight_kg")
    ax.plot(d1, w1, label="Hive A (scale 1)", lw=1.0)
    ax.plot(d2, w2, label="Hive B (scale 2)", lw=1.0, alpha=0.8)
    ax.set_title("Seasonal weight cycle (daily mean)")
    ax.set_ylabel("kg")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, alpha=0.3)

    # (b) Diurnal saw-tooth over one peak-flow week
    ax = axes[0][1]
    wk = [r for r in m if date(2025, 6, 9) <= r["measured_at"].date() <= date(2025, 6, 15)]
    ax.plot([r["measured_at"] for r in wk], [r["scale_1_weight_kg"] for r in wk], lw=1.0)
    ax.set_title("Diurnal saw-tooth — Hive A, 9–15 Jun 2025\n(heaviest at dawn, lightest mid-afternoon)")
    ax.set_ylabel("kg")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.grid(True, alpha=0.3)

    # (c) Brood-nest vs ambient temperature (daily mean)
    ax = axes[1][0]
    dt, at = _daily_mean(m, "ambient_temp_c")
    dh, ht = _daily_mean(m, "hive_1_temp_c")
    ax.plot(dt, at, label="ambient", lw=1.0, color="tab:gray")
    ax.plot(dh, ht, label="hive A brood nest", lw=1.0, color="tab:red")
    ax.axhline(35, color="k", ls="--", lw=0.6, alpha=0.5)
    ax.set_title("Brood-nest vs ambient temperature (daily mean)")
    ax.set_ylabel("°C")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, alpha=0.3)

    # (d) BeeCounter daily entrance traffic
    ax = axes[1][1]
    di, ti = _daily_sum(m, "bee_counter_1_interval_in")
    do, to = _daily_sum(m, "bee_counter_1_interval_out")
    ax.plot(di, ti, label="bees in/day", lw=1.0)
    ax.plot(do, to, label="bees out/day", lw=1.0, alpha=0.8)
    ax.set_title("BeeCounter daily entrance traffic — Hive A")
    ax.set_ylabel("trips/day")
    ax.legend(loc="upper left", fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.grid(True, alpha=0.3)

    for row in axes:
        for a in row:
            for lbl in a.get_xticklabels():
                lbl.set_rotation(30)
                lbl.set_ha("right")

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(args.out, dpi=110)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
