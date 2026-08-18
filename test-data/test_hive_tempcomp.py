"""Tests for per-hive temperature compensation (server/tempcomp.apply_compensation)
and the per-hive calibration map (server/devices.py).

Run: python3 -m pytest test-data/test_hive_tempcomp.py
 or: PYTHONPATH=server python3 test-data/test_hive_tempcomp.py   (no DB needed)

Hives 1–2 carry their calibration in dedicated device_configs columns; hives
3–18 carry theirs — offset, factor *and* temperature coefficient — in the
scale_offsets_by_hive JSONB map. These cover the two pure pieces that make a
hive beyond the second one compensable: the row-level correction that writes
scale_{n}_weight_kg_compensated for every configured hive, and the JSON
round-trip / merge that stores the coefficient.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# devices.py pulls in the app's config module, which requires these to exist. No
# database or network is touched — only the pure map helpers are exercised.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from tempcomp import apply_compensation  # noqa: E402
from devices import hive_scales_from_json, merge_hive_scales  # noqa: E402


_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


def row(t, temp, weights):
    """A serialized measurement dict with flat + nested per-hive weights."""
    m = {"measured_at": t, "ambient_temp_c": temp, "hives": []}
    for n, w in weights.items():
        m[f"scale_{n}_weight_kg"] = w
        m["hives"].append({"index": n, "weight_kg": w})
    return m


# ── apply_compensation ───────────────────────────────────────────────────────

# alpha=1.0 keeps the temperature unsmoothed so the expected values are exact.
rows = [
    row(1, 20.0, {1: 50.0, 3: 30.0, 7: 10.0}),
    row(2, 30.0, {1: 50.1, 3: 30.2, 7: 10.0}),
]
apply_compensation(rows, "ambient_temp_c", 20.0, {1: 0.01, 3: 0.02}, alpha=1.0)

check("at the reference temperature nothing changes",
      approx(rows[0]["scale_1_weight_kg_compensated"], 50.0)
      and approx(rows[0]["scale_3_weight_kg_compensated"], 30.0))
check("hive 3 is compensated from the per-hive coefficient",
      approx(rows[1]["scale_3_weight_kg_compensated"], 30.2 - 0.02 * 10))
check("hive 1 keeps using its own coefficient",
      approx(rows[1]["scale_1_weight_kg_compensated"], 50.1 - 0.01 * 10))
check("a hive with no coefficient is left alone",
      "scale_7_weight_kg_compensated" not in rows[1])
check("raw weights are never modified",
      rows[1]["scale_3_weight_kg"] == 30.2 and rows[1]["scale_1_weight_kg"] == 50.1)
check("tempco_applied is set on every row",
      all(m["tempco_applied"] is True for m in rows))

# The nested hives[] entries mirror the flat keys, so both read shapes agree.
h3 = next(h for h in rows[1]["hives"] if h["index"] == 3)
h7 = next(h for h in rows[1]["hives"] if h["index"] == 7)
check("hives[] entry mirrors the compensated weight",
      approx(h3["weight_kg_compensated"], rows[1]["scale_3_weight_kg_compensated"]))
check("uncompensated hives[] entry gains no compensated key",
      "weight_kg_compensated" not in h7)

# A hive that did not report this cycle must not gain a null compensated key,
# while hives 1–2 keep the historical "key always present" contract.
sparse = [row(1, 25.0, {1: 50.0})]
sparse[0]["scale_2_weight_kg"] = None
apply_compensation(sparse, "ambient_temp_c", 20.0, {2: 0.01, 4: 0.02}, alpha=1.0)
check("missing hive 3+ weight yields no compensated key",
      "scale_4_weight_kg_compensated" not in sparse[0])
check("hives 1–2 keep their compensated key even when the weight is None",
      sparse[0]["scale_2_weight_kg_compensated"] is None)

# Rows arrive newest-first from the read path; the EMA needs them oldest-first.
unordered = [row(2, 30.0, {5: 10.0}), row(1, 20.0, {5: 10.0})]
apply_compensation(unordered, "ambient_temp_c", 20.0, {5: 0.01}, alpha=1.0)
by_t = {m["measured_at"]: m for m in unordered}
check("rows are compensated in time order regardless of input order",
      approx(by_t[1]["scale_5_weight_kg_compensated"], 10.0)
      and approx(by_t[2]["scale_5_weight_kg_compensated"], 10.0 - 0.01 * 10))

# Smoothing is shared across hives: the same EMA temperature drives every hive.
smoothed = [row(1, 20.0, {3: 10.0, 4: 20.0}), row(2, 30.0, {3: 10.0, 4: 20.0})]
apply_compensation(smoothed, "ambient_temp_c", 20.0, {3: 0.01, 4: 0.01}, alpha=0.5)
check("EMA is applied once per device, not per hive",
      approx(smoothed[1]["scale_3_weight_kg_compensated"], 10.0 - 0.01 * 5)
      and approx(smoothed[1]["scale_4_weight_kg_compensated"], 20.0 - 0.01 * 5))

# ── the per-hive calibration map ─────────────────────────────────────────────

stored = {"5": {"scale": 0, "offset": 123, "factor": -7100.0, "tempco": -0.004}}
parsed = hive_scales_from_json(stored)
check("stored coefficient round-trips into hive_scales",
      len(parsed) == 1 and approx(parsed[0].tempco_kg_per_c, -0.004))
check("offset and factor still round-trip",
      parsed[0].offset == 123 and approx(parsed[0].factor, -7100.0))
check("a map entry without a coefficient defaults to zero",
      hive_scales_from_json({"6": {"offset": 1}})[0].tempco_kg_per_c == 0.0)

# Fitting a coefficient must not disturb the hive's offset/factor, and editing
# the calibration must not wipe a coefficient fitted earlier.
merged = merge_hive_scales(stored, [{"index": 5, "tempco_kg_per_c": -0.006}])
check("a coefficient-only update keeps offset and factor",
      merged["5"]["offset"] == 123 and approx(merged["5"]["factor"], -7100.0)
      and approx(merged["5"]["tempco"], -0.006))
merged = merge_hive_scales(merged, [{"index": 5, "offset": 200}])
check("an offset-only update keeps the coefficient",
      merged["5"]["offset"] == 200 and approx(merged["5"]["tempco"], -0.006))
merged = merge_hive_scales(merged, [{"index": 9, "offset": 7}])
check("a new hive entry defaults its missing fields",
      merged["9"]["offset"] == 7 and approx(merged["9"]["factor"], -7050.0)
      and "tempco" not in merged["9"])
check("untouched hives are preserved", merged["5"]["offset"] == 200)


def test_hive_tempcomp():
    """pytest entry point — fails if any check above failed."""
    assert _failures == 0, f"{_failures} per-hive compensation check(s) failed"


if __name__ == "__main__":
    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        sys.exit(1)
    print("\nAll per-hive temperature-compensation checks passed.")
