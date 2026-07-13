"""Tests for HiveHeart FFT decoding, validation, metadata and read-path shape.

Run: python3 -m pytest test-data/test_hiveheart_fft.py
 or: PYTHONPATH=server python3 test-data/test_hiveheart_fft.py

Covers:
  * the packed-nibble decoder (server/hiveheart_fft.py),
  * the derived features (dominant range, centroid, semantic overlap bands),
  * the schema validation (exactly 8 bytes, each 0–255),
  * the read-path reconstruction for both legacy flat records and new nested
    multi-hive records (server/measurements.py).

The measurements import needs a handful of env vars (config.py reads them at
import time); dummy values are injected below so the test runs without a real
database or FastAPI runtime.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Satisfy config.py's required env vars so `import measurements` works offline.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import hiveheart_fft as f  # noqa: E402

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def approx(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ── Decoding ─────────────────────────────────────────────────────────────────
SAMPLE_RAW = [103, 244, 83, 34, 17, 17, 0, 17]
SAMPLE_BINS = [6, 7, 15, 4, 5, 3, 2, 2, 1, 1, 1, 1, 0, 0, 1, 1]

check("known sample decodes exactly", f.decode_fft(SAMPLE_RAW) == SAMPLE_BINS)
check("0x00 -> [0, 0]", f.decode_fft([0, 0, 0, 0, 0, 0, 0, 0])[:2] == [0, 0])
check("all-zero -> 16 zeros", f.decode_fft([0] * 8) == [0] * 16)
check("0xF4 -> [15, 4]", f.decode_fft([0xF4, 0, 0, 0, 0, 0, 0, 0])[:2] == [15, 4])
check("high nibble first", f.decode_fft([0x1F, 0, 0, 0, 0, 0, 0, 0])[:2] == [1, 15])

# Malformed / legacy input is safely omitted (returns None, never raises).
check("7 bytes rejected", f.decode_fft([0] * 7) is None)
check("9 bytes rejected", f.decode_fft([0] * 9) is None)
check("None rejected", f.decode_fft(None) is None)
check("non-list rejected", f.decode_fft("abcdefgh") is None)
check("byte > 255 rejected", f.decode_fft([256, 0, 0, 0, 0, 0, 0, 0]) is None)
check("byte < 0 rejected", f.decode_fft([-1, 0, 0, 0, 0, 0, 0, 0]) is None)
check("non-int byte rejected", f.decode_fft([1.5, 0, 0, 0, 0, 0, 0, 0]) is None)


# ── Frequency range metadata ─────────────────────────────────────────────────
check("exactly 16 ranges", len(f.FFT_RANGES_HZ) == 16)
check("exactly 16 labels", len(f.FFT_RANGE_LABELS) == 16)
check("fft_ranges_hz has 16 pairs", len(f.fft_ranges_hz()) == 16)

# Labels must match the supplied table exactly (en-dash separated).
EXPECTED_LABELS = [
    "0–93", "94–187", "188–281", "282–375", "376–479", "480–562",
    "563–656", "657–750", "751–844", "854–937", "938–1031", "1032–1125",
    "1126–1218", "1219–1312", "1313–1406", "1407–1500",
]
check("labels match supplied table exactly", f.FFT_RANGE_LABELS == EXPECTED_LABELS)
check("first range is 0–93 Hz", f.FFT_RANGES_HZ[0] == (0, 93))
check("last range is 1407–1500 Hz", f.FFT_RANGES_HZ[-1] == (1407, 1500))
# Known documented gap: bin 9 ends at 844, bin 10 starts at 854 (845–853 gap).
check("845–853 Hz gap preserved", f.FFT_RANGES_HZ[8][1] == 844 and f.FFT_RANGES_HZ[9][0] == 854)


# ── Derived features ─────────────────────────────────────────────────────────
check("total activity sums bins", f.total_activity(SAMPLE_BINS) == sum(SAMPLE_BINS))
check("total activity from raw array", f.total_activity(SAMPLE_RAW) == sum(SAMPLE_BINS))

dom = f.dominant_range(SAMPLE_BINS)
check("dominant range is bin 2 (188–281)", dom["index"] == 2 and dom["lower_hz"] == 188 and dom["upper_hz"] == 281)
check("dominant midpoint", approx(dom["midpoint_hz"], 234.5))

# Zero-energy spectrum: centroid and dominant are undefined -> None.
check("zero-energy centroid is None", f.spectral_centroid_hz([0] * 16) is None)
check("zero-energy dominant is None", f.dominant_range([0] * 16) is None)
check("centroid defined for sample", f.spectral_centroid_hz(SAMPLE_BINS) is not None)

# Boundary-overlap semantic aggregation. A single unit in bin 1 (94–187 Hz)
# straddles sub_bass (0–150) and hum (150–300): overlap 56 & 37 of width 93.
one_in_bin1 = [0, 1] + [0] * 14
sb = f.semantic_bands(one_in_bin1)
check("bin1 splits across sub_bass", approx(sb["sub_bass"], 56.0 / 93.0))
check("bin1 splits across hum", approx(sb["hum"], 37.0 / 93.0))
check("bin1 no piping/stress", sb["piping"] == 0.0 and sb["stress"] == 0.0)
check("overlap contributions conserve the bin value", approx(sb["sub_bass"] + sb["hum"], 1.0))
check("zero-energy semantic bands all zero (not None)", f.semantic_bands([0] * 16) == {"sub_bass": 0.0, "hum": 0.0, "piping": 0.0, "stress": 0.0})

feats = f.fft_features(SAMPLE_RAW)
check("fft_features bundles bins", feats["bins"] == SAMPLE_BINS)
check("fft_features malformed -> None", f.fft_features([1, 2, 3]) is None)


# ── Schema validation ────────────────────────────────────────────────────────
from schemas import HiveHeartIn, MeasurementIn  # noqa: E402


def _rejects(fn):
    try:
        fn()
        return False
    except Exception:
        return True


check("HiveHeartIn accepts 8 valid bytes", HiveHeartIn(fft=SAMPLE_RAW).fft == SAMPLE_RAW)
check("HiveHeartIn rejects 16 values", _rejects(lambda: HiveHeartIn(fft=[0] * 16)))
check("HiveHeartIn rejects 7 values", _rejects(lambda: HiveHeartIn(fft=[0] * 7)))
check("HiveHeartIn rejects value > 255", _rejects(lambda: HiveHeartIn(fft=[256, 0, 0, 0, 0, 0, 0, 0])))
check("HiveHeartIn rejects value < 0", _rejects(lambda: HiveHeartIn(fft=[-1, 0, 0, 0, 0, 0, 0, 0])))
check("flat hiveheart_1_fft accepts 8 bytes", MeasurementIn(device_id="d", hiveheart_1_fft=[0] * 8).hiveheart_1_fft == [0] * 8)
check("flat hiveheart_1_fft rejects 16", _rejects(lambda: MeasurementIn(device_id="d", hiveheart_1_fft=[0] * 16)))
check("flat hiveheart_2_fft rejects >255", _rejects(lambda: MeasurementIn(device_id="d", hiveheart_2_fft=[300] * 8)))


# ── Read-path reconstruction (legacy flat + nested) ──────────────────────────
import measurements as M  # noqa: E402


def _reconstruct(m):
    """Mirror attach_hive_readings' per-measurement transform (no DB)."""
    if "hives" not in m:
        m["hives"] = M._synthesize_hives_from_flat(m)
    for h in m["hives"]:
        M._flatten_hive_to_measurement(m, h)
    M._attach_hiveheart_fft_bins(m)
    return m


# Legacy flat record: the raw FFT is surfaced from raw_json by the SELECT as
# hiveheart_1_fft; the read path must synthesize the hive and decode the bins.
flat = _reconstruct({"id": None, "hiveheart_1_fft": list(SAMPLE_RAW)})
check("legacy flat exposes raw fft (nested)", flat["hives"][0]["hiveheart"]["fft"] == SAMPLE_RAW)
check("legacy flat exposes fft_bins (nested)", flat["hives"][0]["hiveheart"]["fft_bins"] == SAMPLE_BINS)
check("legacy flat exposes hiveheart_1_fft", flat["hiveheart_1_fft"] == SAMPLE_RAW)
check("legacy flat exposes hiveheart_1_fft_bins", flat["hiveheart_1_fft_bins"] == SAMPLE_BINS)

# Nested multi-hive record (new firmware). The hive already carries the raw fft;
# decoded bins and flat aliases must be generated identically.
nested = _reconstruct({"id": None, "hives": [{"index": 2, "hiveheart": {"fft": list(SAMPLE_RAW)}}]})
check("nested exposes fft_bins", nested["hives"][0]["hiveheart"]["fft_bins"] == SAMPLE_BINS)
check("nested generates flat hiveheart_2_fft alias", nested["hiveheart_2_fft"] == SAMPLE_RAW)
check("nested generates flat hiveheart_2_fft_bins alias", nested["hiveheart_2_fft_bins"] == SAMPLE_BINS)

# Old flat and new nested formats produce the same public shape for the bins.
check("flat and nested agree on decoded bins",
      flat["hives"][0]["hiveheart"]["fft_bins"] == nested["hives"][0]["hiveheart"]["fft_bins"])

# Malformed historical values must be safely omitted, not crash the endpoint.
malformed = _reconstruct({"id": None, "hiveheart_1_fft": [1, 2, 3]})
check("malformed historical fft omits fft_bins", "hiveheart_1_fft_bins" not in malformed)
check("malformed historical fft leaves raw untouched", malformed["hives"][0]["hiveheart"]["fft"] == [1, 2, 3])
check("malformed historical fft has no nested fft_bins", "fft_bins" not in malformed["hives"][0]["hiveheart"])

# Backward compatibility: a hive with no HiveHeart FFT at all stays clean.
none_hh = _reconstruct({"id": None, "hive_1_temp_c": 21.0})
check("no-fft record has no fft_bins key", "hiveheart_1_fft_bins" not in none_hh)


# ── Insights integration ─────────────────────────────────────────────────────
import insights  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

NOW = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)
LOW = [12, 10, 8, 4, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]   # energy low in the band
HIGH = [0, 0, 0, 0, 0, 0, 1, 1, 2, 4, 8, 10, 12, 8, 4, 2]  # energy high in the band


def _rows(bins_by_time):
    return [
        {"measured_at": t.isoformat(),
         "hiveheart_2_fft_bins": b,
         "hives": [{"index": 2, "hiveheart": {"fft_bins": b}}]}
        for t, b in bins_by_time
    ]


# Baseline (low-centroid) for a week, then a recent (high-centroid) 24h window.
_baseline = [(NOW - timedelta(days=7) + timedelta(hours=6 * k), LOW) for k in range(20)]
_recent = [(NOW - timedelta(hours=20) + timedelta(hours=3 * k), HIGH) for k in range(6)]
shift_rows = _rows(_baseline + _recent)

series = insights._hiveheart_fft_series(shift_rows, 2)
check("hiveheart fft series extracts all rows", len(series) == len(shift_rows))

alert = insights.detect_hiveheart_spectrum_shift(series, 2, NOW)
check("centroid shift raises an alert", alert is not None)
check("alert is acoustic/info severity", alert and alert.category == "acoustic" and alert.severity == "info")
check("alert reports centroid shift", alert and "centroid_shift_hz" in alert.evidence)

# Safety: empty and all-silent series never raise and never alert.
check("empty fft series -> no alert", insights.detect_hiveheart_spectrum_shift([], 2, NOW) is None)
silent = insights._hiveheart_fft_series(
    _rows([(NOW - timedelta(hours=h), [0] * 16) for h in range(40)]), 2)
check("silent spectrum -> no alert (zero-energy)", insights.detect_hiveheart_spectrum_shift(silent, 2, NOW) is None)

# Insufficient history -> no alert.
few = insights._hiveheart_fft_series(_rows([(NOW - timedelta(hours=h), HIGH) for h in range(3)]), 2)
check("insufficient history -> no alert", insights.detect_hiveheart_spectrum_shift(few, 2, NOW) is None)

# End-to-end: compute_insights surfaces the HiveHeart acoustic alert.
all_alerts = insights.compute_insights(shift_rows, now=NOW)
check("compute_insights includes hiveheart acoustic alert",
      any(a.category == "acoustic" and "hiveheart" in a.id for a in all_alerts))
# A stable spectrum (baseline == recent) produces no acoustic alert.
stable = _rows([(NOW - timedelta(hours=6 * k), HIGH) for k in range(28)])
stable_alerts = insights.compute_insights(stable, now=NOW)
check("stable spectrum -> no hiveheart acoustic alert",
      not any(a.category == "acoustic" and "hiveheart" in a.id for a in stable_alerts))


def test_hiveheart_fft():
    """pytest entry point — fails if any check above failed."""
    assert _failures == 0, f"{_failures} HiveHeart FFT check(s) failed"


if __name__ == "__main__":
    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        sys.exit(1)
    print("\nAll HiveHeart FFT checks passed. ✅")
