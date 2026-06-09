"""Tests for load-cell temperature compensation (server/tempcomp.py).

Run: python3 -m pytest test-data/test_tempcomp.py
 or: PYTHONPATH=server python3 test-data/test_tempcomp.py   (no DB / FastAPI needed)

These cover the pure math used by the backend to correct temperature-induced
load-cell drift: the first-order correction (compensate_weight) and the
least-squares fit that derives a coefficient from logged data
(fit_temp_coefficient).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

from tempcomp import compensate_weight, fit_temp_coefficient  # noqa: E402


_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


# ── compensate_weight ────────────────────────────────────────────────────────

# A scale that reads 0.01 kg heavier per °C; at 30 °C (ref 20 °C) it over-reads
# by 0.10 kg, which the correction must remove.
check(
    "removes positive drift above ref temp",
    approx(compensate_weight(50.10, 30.0, 20.0, 0.01), 50.0),
)
check(
    "removes drift below ref temp",
    approx(compensate_weight(49.90, 10.0, 20.0, 0.01), 50.0),
)
check(
    "no change at the reference temperature",
    approx(compensate_weight(50.0, 20.0, 20.0, 0.01), 50.0),
)

# Pass-through / safety cases — the function must never raise.
check("None weight stays None", compensate_weight(None, 30.0, 20.0, 0.01) is None)
check("zero coefficient is a no-op", approx(compensate_weight(50.0, 30.0, 20.0, 0.0), 50.0))
check("None coefficient is a no-op", approx(compensate_weight(50.0, 30.0, 20.0, None), 50.0))
check("missing temperature is a no-op", approx(compensate_weight(50.0, None, 20.0, 0.01), 50.0))
check(
    "missing ref temp falls back to default 20°C",
    approx(compensate_weight(50.10, 30.0, None, 0.01), 50.0),
)


# ── fit_temp_coefficient ─────────────────────────────────────────────────────

# Perfectly linear drift: slope 0.01 kg/°C around a 50 kg load, ref 20 °C.
samples = [(float(t), 50.0 + 0.01 * (t - 20)) for t in range(10, 31)]
fit = fit_temp_coefficient(samples)
check("clean fit succeeds", fit["ok"] is True)
check("recovers the slope", approx(fit["coeff_kg_per_c"], 0.01, tol=1e-9))
check("reference temp is the window mean (20°C)", approx(fit["ref_temp_c"], 20.0, tol=1e-9))
check("perfect fit has r_squared 1.0", approx(fit["r_squared"], 1.0, tol=1e-9))
check("reports sample count", fit["n"] == len(samples))
check("reports temperature span", fit["temp_min_c"] == 10.0 and fit["temp_max_c"] == 30.0)

# Round-trip: fitting then compensating should flatten the drift back to the mean.
worst = max(
    abs(compensate_weight(w, t, fit["ref_temp_c"], fit["coeff_kg_per_c"]) - 50.0)
    for t, w in samples
)
check("fit + compensate flattens drift to the mean load", worst < 1e-9)

# Degenerate inputs.
check("constant temperature cannot be fit", fit_temp_coefficient([(20, 50), (20, 51)])["ok"] is False)
check("a single sample cannot be fit", fit_temp_coefficient([(20, 50)])["ok"] is False)
check("None values are dropped before fitting", fit_temp_coefficient([(20, None), (None, 50), (10, 49.9), (30, 50.1)])["n"] == 2)


def test_tempcomp():
    """pytest entry point — fails if any check above failed."""
    assert _failures == 0, f"{_failures} temperature-compensation check(s) failed"


if __name__ == "__main__":
    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        sys.exit(1)
    print("\nAll temperature-compensation checks passed.")
