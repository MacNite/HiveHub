"""
HiveHub sensor-based insights / alerts.

This module computes rule-based alerts from the measurement time-series that
HiveHub already stores (weight, internal hive temperature, ambient
temperature/humidity, and — when available — microphone RMS and FFT band
energy).

It is intentionally pure: it takes a list of measurement dicts (as returned
by ``measurement_row_to_dict`` in main.py) and returns a list of ``Alert``
objects. No DB access, no FastAPI imports — unit-testable in isolation.

------------------------------------------------------------------------------
Hardware assumptions
------------------------------------------------------------------------------
The current HiveHub ESP32 firmware delivers, per measurement:
    * ``scale_1_weight_kg`` / ``scale_2_weight_kg``   (HX711 + load cells)
    * ``hive_1_temp_c``     / ``hive_2_temp_c``       (DS18B20 internal probes)
    * ``ambient_temp_c``    / ``ambient_humidity_percent`` (SHT4x)
    * ``mic_left_rms_dbfs`` / ``mic_right_rms_dbfs``  (INMP441 broadband RMS)
    * ``mic_left_peak_dbfs``/ ``mic_right_peak_dbfs``
    * ``mic_left_band_*_dbfs`` / ``mic_right_band_*_dbfs``  (arduinoFFT bands)

Mic and FFT fields are optional: every detector degrades gracefully to its
weight/temperature-only rule when they are absent. The acoustic evidence
raises confidence and, in some cases, severity when both signals agree.

FFT bands (dBFS, 500 ms capture at 16 kHz, Hann window, 4096-point FFT):
    * sub_bass   50 –  150 Hz  structural vibration / low rumble
    * hum       150 –  300 Hz  normal colony hum (fundamental ~200 Hz)
    * piping    300 –  550 Hz  queen piping / tooting (pre-swarm signal)
    * stress    550 – 1500 Hz  agitated / robbing colony
    * high     1500 – 3000 Hz  harmonic overtones

Optionally, one LIS3DH / LIS2DH12 accelerometer per hive contributes
low-frequency vibration bands (mg, AC / gravity-removed, from an on-device FFT):
    * accel_{ch}_band_swarm_mg      8 –  30 Hz  pre-swarm vibration (~20 Hz)
    * accel_{ch}_band_fanning_mg   30 – 100 Hz  fanning / ventilation
    * accel_{ch}_band_activity_mg 100 – 200 Hz  general worker activity
    * accel_{ch}_rms_mg                          broadband AC RMS
all gated by accel_{ch}_ok. These cover the sub-audible band that microphones
miss; the swarm band is the headline pre-swarm predictor (see below).

------------------------------------------------------------------------------
Acoustic thresholds & literature
------------------------------------------------------------------------------
* Piping detection threshold of −45 dBFS for the 300–550 Hz band is
  conservatively derived from Seeley (2010) and Ramsey et al. (2020),
  "Acoustic detection of queen presence ...", PLOS ONE. The published
  piping fundamental is 320–480 Hz; 300–550 Hz gives margin.

* Queenless hum shift: a queenless colony shows elevated low-frequency
  noise (< 300 Hz) and a broadened, lower-pitched hum. We approximate this
  as hum_dbfs > −40 dBFS when the piping band is quiet (< −52 dBFS),
  combined with the existing temp/weight rules (MSPB arXiv 2311.10876).

* Robbing stress band: agitated bee flight and wing-beat noise concentrate
  in the 550–1500 Hz range. Threshold −38 dBFS based on the BUZZ dataset
  characterisation (Nolasco et al. 2019).

All thresholds are starting points and should be re-calibrated against your
own historical data and the public datasets (MSPB, BeeTogether, UrBAN,
NU-Hive, OSBH, BUZZ1–4, Kulkarni/Murphy).

------------------------------------------------------------------------------
Sources for the thresholds used below
------------------------------------------------------------------------------
* Project spec ("Phase 1/2/3 swarm warning", queenlessness, robbing,
  foraging, brood cycle, absconding, winter survival, harvest timing).
* Seeley, T. D. (2010). *Honeybee Democracy* - swarm preparation behaviour.
* Kulkarni & Murphy time-series benchmark - weight + in-hive temp + entrance
  traffic, PMC 11479372 (Frontiers / open access).
* MSPB multi-modal dataset, arXiv 2311.10876 - audio + temp + humidity over
  53 hives x 1 year.
* Stalidzans, E. & Berzonis, A. (2013), "Temperature changes above the
  upper hive entrance ... swarming preparation indicator".
* Meikle, W. G. et al. (2008), "Within-day variation in continuous hive
  weight data as a measure of honey bee colony activity".
* Ramsey, M. et al. (2020), "Acoustic detection of the honey bee ...
  piping signal", PLOS ONE.
* Nolasco, I. et al. (2019), "Honey bee detection ... BUZZ dataset",
  DCASE Workshop.
* Ramsey, M.-T. et al. (2020), "The prediction of swarming in honeybee
  colonies using vibrational spectra", Scientific Reports 10:9798 — a ~20 Hz
  comb vibration rises for days/weeks before swarming and is most
  discriminative at night (00:00–05:00). Basis for the accelerometer
  swarm-prediction detector.
* Bencsik, M. et al. (2011), "Identification of the honey bee swarming
  process by analysing the time course of hive vibrations", Comput. Electron.
  Agric. 76 — pre-swarm divergence of comb vibration days ahead.
* Uthoff, C., Nabhan Homsi, M. & von Bergen, M. (2023), "Acoustic and
  vibration monitoring of honeybee colonies ...", Comput. Electron. Agric.
  205:107589 — review recommending low-frequency accelerometers to capture
  the ~20 Hz swarm signal that microphones miss.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

from pydantic import BaseModel, Field

from hiveheart_fft import (
    decode_fft,
    dominant_range,
    semantic_bands,
    spectral_centroid_hz,
    total_activity,
)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

AlertSeverity = Literal["info", "watch", "warning", "critical"]
AlertCategory = Literal[
    "swarm",
    "queenless",
    "robbing",
    "foraging",
    "brood",
    "decline",
    "winter",
    "harvest",
    "acoustic",
]
ChannelRef = Literal[1, 2]


class Alert(BaseModel):
    """A single insight/alert derived from sensor data."""

    id: str = Field(..., description="Stable id, unique within one compute pass")
    category: AlertCategory
    severity: AlertSeverity
    channel: ChannelRef
    title: str
    description: str
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    source: str = Field(
        default="",
        description="Short reference to the algorithm/literature source",
    )


# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------

BROOD_NEST_TARGET_C = 35.0
BROOD_NEST_TOLERANCE_C = 1.5
IMMINENT_SWARM_DELTA_C = 1.5
IMMINENT_SWARM_SLOPE_C_PER_HOUR = 0.5

PRE_SWARM_STD_MULTIPLIER = 1.5
PRE_SWARM_BASELINE_DAYS = 7

SWARM_WEIGHT_DROP_KG = 1.5
SWARM_WEIGHT_WINDOW_MIN = 30
SWARM_DAYTIME_HOURS = (9, 17)

ROBBING_WEIGHT_LOSS_KG_PER_HOUR = 0.4
ROBBING_LATE_AFTERNOON_HOURS = (15, 19)
ROBBING_MIN_DURATION_MIN = 30

QUEENLESS_TEMP_STDDEV_C = 1.0
QUEENLESS_DAYS_WINDOW = 7
QUEENLESS_WEIGHT_STAGNANT_KG = 0.2

FORAGING_STRONG_KG_PER_DAY = 1.0
FORAGING_MODERATE_KG_PER_DAY = 0.2

BROOD_ACTIVE_STDDEV_C = 0.5
BROOD_BROODLESS_STDDEV_C = 2.0

ABSCONDING_LOOKBACK_DAYS = 14
ABSCONDING_WEIGHT_LOSS_G_PER_DAY = 100
ABSCONDING_TEMP_STDDEV_C = 1.5

WINTER_CLUSTER_DELTA_C = 2.0
WINTER_WEIGHT_LOSS_G_PER_WEEK = 300

HARVEST_FLOW_KG_PER_WEEK = 2.0
HARVEST_PLATEAU_KG_PER_WEEK = 0.3
HARVEST_PLATEAU_DAYS = 4

# ── Vibration thresholds (accelerometer, mg — see module docstring refs) ────
# Ramsey et al. (2020, Sci. Rep.) show the ~20 Hz comb-vibration band climbs for
# days/weeks before swarming, strongest at night. We compare a recent night-time
# mean of the 8–30 Hz swarm band to a longer night-time baseline and fire when it
# has risen by at least this multiple. Night = VIBRATION_NIGHT_HOURS (local-ish:
# the timestamp hour is used directly, matching the rest of this module).
VIBRATION_NIGHT_HOURS = (0, 5)
VIBRATION_RECENT_DAYS = 2
VIBRATION_BASELINE_DAYS = 10
VIBRATION_SWARM_RISE_MULT = 1.6
# A standalone watch needs a clearer rise than the corroboration boost.
VIBRATION_SWARM_STANDALONE_MULT = 2.0
# Floors so a near-still hive (tiny mg) can't produce a huge, meaningless ratio.
# The baseline night mean must be at least this, and the recent night mean must
# reach this absolute level, before the multiplier test is trusted.
VIBRATION_MIN_BASELINE_MG = 0.4
VIBRATION_MIN_RECENT_MG = 0.8

# ── Low-rate (BLE beacon) vibration thresholds ──────────────────────────────
# The HolyIot 25015 is a passive BLE beacon: it yields a per-cycle acceleration
# magnitude (accel_N_rms_mg) rather than a high-rate stream, so the 8–30 Hz FFT
# swarm band above cannot be computed from it. Instead we trend the night-time
# mean of that per-cycle magnitude — a rising baseline of in-hive movement is a
# coarse but real pre-swarm activity signal (Bencsik/Ramsey: comb excitation
# climbs for days before swarming). Thresholds are deliberately conservative and
# the alert is lower-confidence than the FFT-band detector. Recalibrate on data.
LOWRATE_SWARM_RISE_MULT = 1.8
# Absolute floors so beacon/sensor noise on a still hive can't fake a big ratio.
LOWRATE_MIN_BASELINE_MG = 2.0
LOWRATE_MIN_RECENT_MG = 4.0

# ── Acoustic thresholds (dBFS, see module docstring for literature refs) ────
# Pre-swarm: piping band energy at or above this level is a strong positive signal.
PIPING_ACTIVE_DBFS = -45.0
# Queenless hum shift: hum band is louder than this and piping is quiet.
QUEENLESS_HUM_DBFS = -40.0
QUEENLESS_PIPING_QUIET_DBFS = -52.0
# Robbing: stress band energy above this is consistent with agitated flight.
ROBBING_STRESS_DBFS = -38.0

# ── Entrance-counter (BeeCounter) thresholds ────────────────────────────────
# The BeeCounter reports per-interval inbound/outbound crossing counts
# (bee_counter_{ch}_interval_in / _interval_out), gated by bee_counter_{ch}_ok.
# All thresholds below are starting points — recalibrate against your own data.

# Swarm event (Phase 3): a swarm departure shows a massive, asymmetric OUTflow.
# The spec rule is out_count > SWARM_OUT_BASELINE_MULT x baseline AND
# out / (in + 1) > SWARM_OUT_IN_RATIO within the drop window. When this
# corroborates the weight drop, severity/confidence are raised.
SWARM_OUT_BASELINE_MULT = 3.0
SWARM_OUT_IN_RATIO = 5.0
# Minimum baseline outbound-per-interval before the multiplier test is
# meaningful (avoids a divide-by-near-zero when the hive was nearly idle).
SWARM_OUT_MIN_BASELINE = 1.0

# Queenlessness: forager (outbound) decline of >= this fraction per day,
# sustained over the queenless window (~5%/day for 7+ days per the spec).
QUEENLESS_FORAGER_DECLINE_FRAC_PER_DAY = 0.05
# Minimum mean daily outbound traffic at the start of the window; below this
# the slope fit is too noisy to trust.
QUEENLESS_FORAGER_MIN_DAILY_BASELINE = 200.0

# Robbing: an incoming spike with comparatively low outgoing. Asymmetry is
# (in - out) / max(in + out, 1), range -1..+1. >= this means inbound clearly
# dominates. Also require a minimum absolute inbound rate so a couple of
# returning foragers at dusk don't trip it.
ROBBING_IN_OUT_ASYMMETRY = 0.4
ROBBING_MIN_INBOUND_PER_HOUR = 200.0

# Absconding: declining linear trend on daily outbound traffic over the
# 14-day lookback. Same fractional slope idea as the queenless rule. When this
# third rule confirms, the alert promotes from "watch" to "warning".
ABSCONDING_FORAGER_DECLINE_FRAC_PER_DAY = 0.03

# Winter: a warm-day cleansing flight is a positive sign of cluster health.
# If any interval in the window shows outbound >= this, we note it as
# corroboration (it lowers, not raises, the concern).
WINTER_CLEANSING_FLIGHT_OUT = 50.0

# Foraging: mean outbound rate (bees/hour) over the day that counts as
# genuinely "active" foraging traffic, used to cross-check the weight-based
# foraging classifier.
FORAGING_ACTIVE_OUT_PER_HOUR = 100.0

# ── HiveHeart FFT thresholds (relative levels 0–15, NOT dBFS) ────────────────
# The beehivemonitoring.com HiveHeart reports a packed 16-band FFT of *relative*
# levels (0–15), decoded by server/hiveheart_fft.py. These are NOT calibrated
# acoustics, so every rule below compares a hive against ITS OWN rolling baseline
# rather than any absolute threshold, and stays informational/low-severity until
# the levels are validated against real field data. Never compare these values to
# the microphone dBFS bands. Recalibrate all constants once field data exists.
#
# History is required before any alert: a recent window and a longer baseline
# window, each with a minimum number of decoded spectra.
HIVEHEART_RECENT_HOURS = 24
HIVEHEART_BASELINE_DAYS = 7
HIVEHEART_MIN_RECENT_SAMPLES = 4
HIVEHEART_MIN_BASELINE_SAMPLES = 12
# Ignore near-silent spectra so decoder noise on a quiet hive can't trip a rule
# (mean total relative activity across the 16 bins, out of a 0–240 theoretical
# max).
HIVEHEART_MIN_ACTIVITY = 4.0
# A "meaningful" shift in the relative-level-weighted spectral centroid (Hz) of
# the recent window vs the baseline. Deliberately coarse (roughly one HiveHeart
# bin width) — this is a change detector, not a tuned classifier.
HIVEHEART_CENTROID_SHIFT_HZ = 120.0
# A "meaningful" rise in mean total relative activity: recent >= this multiple of
# baseline (and above the activity floor). Broad-spectrum loudening is coarse but
# real; kept at "info" severity.
HIVEHEART_ACTIVITY_RISE_MULT = 1.6


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

SeriesPoint = tuple[datetime, float]
Series = list[SeriesPoint]


def _as_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            v = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _hive_channels(measurements: Iterable[dict[str, Any]]) -> list[int]:
    """Hive indices (channels) to run detectors for. Reads the ``hives`` arrays the
    read layer attaches; falls back to the legacy two channels when none carry a
    hives[] array (e.g. raw rows passed directly in a test)."""
    found: set[int] = set()
    for m in measurements:
        for h in (m.get("hives") or []):
            idx = h.get("index")
            if isinstance(idx, int) and idx >= 1:
                found.add(idx)
    return sorted(found) if found else [1, 2]


def _extract_series(measurements: Iterable[dict[str, Any]], field: str) -> Series:
    out: Series = []
    for m in measurements:
        ts = _as_datetime(m.get("measured_at"))
        val = m.get(field)
        if ts is None or val is None:
            continue
        try:
            out.append((ts, float(val)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def _window(series: Series, end: datetime, hours: float) -> Series:
    start = end - timedelta(hours=hours)
    return [(t, v) for t, v in series if start < t <= end]


def _values(series: Series) -> list[float]:
    return [v for _, v in series]


def _safe_mean(values: list[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def _safe_stddev(values: list[float]) -> Optional[float]:
    return statistics.pstdev(values) if len(values) >= 2 else None


def _linear_slope_per_day(series: Series) -> Optional[float]:
    if len(series) < 2:
        return None
    t0 = series[0][0]
    xs = [(t - t0).total_seconds() / 86400.0 for t, _ in series]
    ys = [v for _, v in series]
    if max(xs) - min(xs) <= 0:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        return None
    return num / den


def _max_drop(
    series: Series, window_minutes: int
) -> tuple[Optional[float], Optional[datetime], Optional[datetime]]:
    if len(series) < 2:
        return (None, None, None)
    window = timedelta(minutes=window_minutes)
    best = 0.0
    best_pair: tuple[Optional[datetime], Optional[datetime]] = (None, None)
    j = 0
    for i in range(len(series)):
        ti, vi = series[i]
        if j < i + 1:
            j = i + 1
        while j < len(series) and series[j][0] - ti <= window:
            drop = vi - series[j][1]
            if drop > best:
                best = drop
                best_pair = (ti, series[j][0])
            j += 1
        j = i + 1
    return (best, best_pair[0], best_pair[1]) if best > 0 else (None, None, None)


def _is_active_season(when: datetime) -> bool:
    return 3 <= when.month <= 9


def _is_winter(when: datetime) -> bool:
    return when.month >= 10 or when.month <= 2


# ---------------------------------------------------------------------------
# Acoustic helpers
# ---------------------------------------------------------------------------

def _latest_band(measurements: list[dict[str, Any]], field: str) -> Optional[float]:
    """
    Return the most recent non-null value of a mic band field across all
    measurements.  Returns None when no measurement carries the field.
    """
    for m in reversed(measurements):
        v = m.get(field)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _mic_band_snapshot(
    measurements: list[dict[str, Any]], channel: ChannelRef
) -> dict[str, Optional[float]]:
    """
    Collect the latest value of every FFT band for the given channel.
    channel=1 -> left mic, channel=2 -> right mic.
    Returns a dict with keys: sub_bass, hum, piping, stress, high (all dBFS or None).
    """
    side = "left" if channel == 1 else "right"
    return {
        "sub_bass": _latest_band(measurements, f"mic_{side}_band_sub_bass_dbfs"),
        "hum":      _latest_band(measurements, f"mic_{side}_band_hum_dbfs"),
        "piping":   _latest_band(measurements, f"mic_{side}_band_piping_dbfs"),
        "stress":   _latest_band(measurements, f"mic_{side}_band_stress_dbfs"),
        "high":     _latest_band(measurements, f"mic_{side}_band_high_dbfs"),
    }


# ---------------------------------------------------------------------------
# HiveHeart FFT helpers (relative-level spectrum, not dBFS)
# ---------------------------------------------------------------------------

HiveHeartBins = tuple[datetime, list[int]]


def _hiveheart_bins_from_row(m: dict[str, Any], channel: int) -> Optional[list[int]]:
    """Decoded 16-bin HiveHeart spectrum for one hive from a measurement row.

    Prefers the already-decoded ``hiveheart_{ch}_fft_bins`` the read layer
    attaches; falls back to decoding the raw ``hiveheart_{ch}_fft`` array (or the
    nested ``hives[].hiveheart.fft``). Returns None when absent or malformed.
    """
    bins = m.get(f"hiveheart_{channel}_fft_bins")
    if isinstance(bins, list) and len(bins) == 16:
        return bins
    raw = m.get(f"hiveheart_{channel}_fft")
    decoded = decode_fft(raw)
    if decoded is not None:
        return decoded
    for h in (m.get("hives") or []):
        if h.get("index") != channel:
            continue
        hh = h.get("hiveheart")
        if isinstance(hh, dict):
            hb = hh.get("fft_bins")
            if isinstance(hb, list) and len(hb) == 16:
                return hb
            return decode_fft(hh.get("fft"))
    return None


def _hiveheart_fft_series(
    measurements: Iterable[dict[str, Any]], channel: int
) -> list[HiveHeartBins]:
    """Time-ordered (timestamp, 16-bin) HiveHeart spectra for one hive.

    Malformed / missing spectra are skipped so a corrupt row never derails the
    detector (mirrors the safe-omit contract of hiveheart_fft.decode_fft).
    """
    out: list[HiveHeartBins] = []
    for m in measurements:
        ts = _as_datetime(m.get("measured_at"))
        if ts is None:
            continue
        bins = _hiveheart_bins_from_row(m, channel)
        if bins is None:
            continue
        out.append((ts, bins))
    out.sort(key=lambda p: p[0])
    return out


# ---------------------------------------------------------------------------
# Accelerometer (per-hive vibration) helpers
# ---------------------------------------------------------------------------

def _accel_band_series(
    measurements: Iterable[dict[str, Any]],
    channel: ChannelRef,
    band: Literal["swarm", "fanning", "activity"],
) -> Series:
    """
    Pull a (timestamp, mg) series for one vibration band of one hive's
    accelerometer. channel 1 -> accel_1_*, channel 2 -> accel_2_*.

    Rows where the accelerometer was unreachable for that hive
    (``accel_{ch}_ok`` falsy/absent) are skipped, so a missing sensor never
    injects implicit zeros that would look like "perfectly still".
    """
    ok_field = f"accel_{channel}_ok"
    val_field = f"accel_{channel}_band_{band}_mg"
    out: Series = []
    for m in measurements:
        if not m.get(ok_field):
            continue
        ts = _as_datetime(m.get("measured_at"))
        val = m.get(val_field)
        if ts is None or val is None:
            continue
        try:
            out.append((ts, float(val)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def _accel_rms_series(
    measurements: Iterable[dict[str, Any]],
    channel: ChannelRef,
) -> Series:
    """
    Pull a (timestamp, mg) series of the per-cycle broadband AC magnitude
    (``accel_{ch}_rms_mg``) for one hive, gated by ``accel_{ch}_ok``.

    This is what a passive HolyIot 25015 BLE sensor produces — a single
    acceleration magnitude per upload cycle rather than an FFT band. Used by the
    low-rate pre-swarm detector. Identical gating to ``_accel_band_series`` so a
    missing sensor never injects implicit zeros.
    """
    ok_field = f"accel_{channel}_ok"
    val_field = f"accel_{channel}_rms_mg"
    out: Series = []
    for m in measurements:
        if not m.get(ok_field):
            continue
        ts = _as_datetime(m.get("measured_at"))
        val = m.get(val_field)
        if ts is None or val is None:
            continue
        try:
            out.append((ts, float(val)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


def _night_window(series: Series, end: datetime, days: float) -> Series:
    """
    Filter ``series`` to the last ``days`` ending at ``end`` AND to the
    night-time hours in VIBRATION_NIGHT_HOURS. Ramsey et al. (2020) found the
    swarm vibration signal is most discriminative overnight, so the detectors
    compare night-only sub-series. The timestamp hour is used directly (same
    local-time simplification the weight/robbing detectors use).
    """
    lo, hi = VIBRATION_NIGHT_HOURS
    start = end - timedelta(days=days)
    return [
        (t, v) for t, v in series
        if start < t <= end and lo <= t.hour < hi
    ]


def _vibration_swarm_rise(
    accel_swarm_series: Series, now: datetime
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Compare the recent night-time mean of the 8–30 Hz swarm band to a longer
    night-time baseline.

    Returns (recent_mean_mg, baseline_mean_mg, ratio). All three are None when
    there isn't enough night-time data, or when the levels are below the floors
    (VIBRATION_MIN_BASELINE_MG / VIBRATION_MIN_RECENT_MG) where the ratio would
    be dominated by sensor noise rather than real vibration.
    """
    if not accel_swarm_series:
        return (None, None, None)
    recent = _night_window(accel_swarm_series, now, VIBRATION_RECENT_DAYS)
    # Baseline = the nights BEFORE the recent window, out to BASELINE_DAYS.
    baseline_end = now - timedelta(days=VIBRATION_RECENT_DAYS)
    baseline = _night_window(
        accel_swarm_series,
        baseline_end,
        VIBRATION_BASELINE_DAYS - VIBRATION_RECENT_DAYS,
    )
    if len(recent) < 3 or len(baseline) < 6:
        return (None, None, None)
    recent_mean = _safe_mean(_values(recent))
    baseline_mean = _safe_mean(_values(baseline))
    if recent_mean is None or baseline_mean is None:
        return (None, None, None)
    if baseline_mean < VIBRATION_MIN_BASELINE_MG or recent_mean < VIBRATION_MIN_RECENT_MG:
        return (recent_mean, baseline_mean, None)
    ratio = recent_mean / baseline_mean
    return (recent_mean, baseline_mean, ratio)


def _lowrate_vibration_rise(
    accel_rms_series: Series, now: datetime
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Low-rate analogue of ``_vibration_swarm_rise`` for the per-cycle BLE-beacon
    magnitude (``accel_{ch}_rms_mg``). Same night-window recent-vs-baseline
    comparison, but with the LOWRATE_* floors (the per-cycle magnitude is in a
    different, larger mg range than a narrow FFT band). Returns
    (recent_mean_mg, baseline_mean_mg, ratio); ratio is None when there isn't
    enough night-time data or levels are below the noise floors.
    """
    if not accel_rms_series:
        return (None, None, None)
    recent = _night_window(accel_rms_series, now, VIBRATION_RECENT_DAYS)
    baseline_end = now - timedelta(days=VIBRATION_RECENT_DAYS)
    baseline = _night_window(
        accel_rms_series,
        baseline_end,
        VIBRATION_BASELINE_DAYS - VIBRATION_RECENT_DAYS,
    )
    if len(recent) < 3 or len(baseline) < 6:
        return (None, None, None)
    recent_mean = _safe_mean(_values(recent))
    baseline_mean = _safe_mean(_values(baseline))
    if recent_mean is None or baseline_mean is None:
        return (None, None, None)
    if baseline_mean < LOWRATE_MIN_BASELINE_MG or recent_mean < LOWRATE_MIN_RECENT_MG:
        return (recent_mean, baseline_mean, None)
    ratio = recent_mean / baseline_mean
    return (recent_mean, baseline_mean, ratio)


# ---------------------------------------------------------------------------
# Entrance-counter (BeeCounter) helpers
# ---------------------------------------------------------------------------

def _extract_counter_series(
    measurements: Iterable[dict[str, Any]],
    channel: ChannelRef,
    direction: Literal["in", "out"],
) -> Series:
    """
    Pull a (timestamp, count) series of per-interval crossing counts for one
    direction from a BeeCounter channel.

    channel 1 -> bee_counter_1_*, channel 2 -> bee_counter_2_*.

    Rows where the counter was unreachable for that hive
    (``bee_counter_{ch}_ok`` is falsy or absent) are skipped, so a missing or
    broken counter never injects implicit zeros that would look like "no
    traffic". The values are interval counts (bees since the previous poll),
    not cumulative totals.

    Two sources of the per-interval count are supported transparently:

    * **Device-reported interval** (``bee_counter_{ch}_interval_{dir}``): the
      I2C ``CMD_LATCH`` handshake path, where the counter resets its interval
      each poll. Used as-is whenever present.
    * **Differenced lifetime total** (``bee_counter_{ch}_total_{dir}``): the
      totals-only wireless/BLE path, where the device never resets and the
      interval is derived here as ``total_now - total_prev`` between
      consecutive readings (see 2026-easy-bee-counter/docs/ble-mode.md). A
      missed poll just widens the next interval; a counter reboot or uint32
      wrap (``total_now < total_prev``) is treated as ``total_now`` so the
      restart from zero is not mistaken for a huge negative delta.

    Differencing needs chronological order, so rows are sorted before the
    deltas are computed. The total baseline advances on every row that carries
    a usable total — even rows that used the device interval — so a stream that
    switches transports stays consistent across the boundary.
    """
    ok_field = f"bee_counter_{channel}_ok"
    interval_field = f"bee_counter_{channel}_interval_{direction}"
    total_field = f"bee_counter_{channel}_total_{direction}"

    def _as_float(v: Any) -> Optional[float]:
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # Collect reachable, timestamped rows in chronological order first, since
    # differencing cumulative totals depends on it.
    rows: list[tuple[datetime, dict[str, Any]]] = []
    for m in measurements:
        if not m.get(ok_field):
            continue
        ts = _as_datetime(m.get("measured_at"))
        if ts is None:
            continue
        rows.append((ts, m))
    rows.sort(key=lambda p: p[0])

    out: Series = []
    prev_total: Optional[float] = None
    for ts, m in rows:
        interval_val = _as_float(m.get(interval_field))
        cur_total = _as_float(m.get(total_field))

        # Fall back to differencing the monotonic lifetime total when the
        # device did not report a per-interval count (the BLE/totals-only path).
        if interval_val is None and cur_total is not None:
            if prev_total is None:
                interval_val = None          # no baseline yet — can't attribute
            elif cur_total >= prev_total:
                interval_val = cur_total - prev_total
            else:
                interval_val = cur_total     # reboot / wrap: count since restart

        # Advance the differencing baseline on any usable total, regardless of
        # which source supplied this row's interval.
        if cur_total is not None:
            prev_total = cur_total

        if interval_val is not None:
            out.append((ts, interval_val))

    return out


def _bucket_by_day(series: Series, end: datetime, days: int) -> Series:
    """
    Sum per-interval counts into per-day totals over the last ``days`` days
    ending at ``end``. Returns one point per day (UTC midnight key) with the
    summed count.

    Partial days at the window edges are dropped: the first and last
    calendar day usually only contain a fraction of a day's samples, and
    including them badly biases a decline-slope fit. A day is considered
    partial if it holds fewer than 60% of the median day's sample count.
    Days with no rows at all are omitted entirely (with ~10-min cadence a
    fully empty day means data loss, not zero traffic).
    """
    if not series or days <= 0:
        return []
    start = end - timedelta(days=days)
    sums: dict[datetime, float] = {}
    counts: dict[datetime, int] = {}
    for t, v in series:
        if t <= start or t > end:
            continue
        day = t.replace(hour=0, minute=0, second=0, microsecond=0)
        sums[day] = sums.get(day, 0.0) + v
        counts[day] = counts.get(day, 0) + 1
    if not sums:
        return []
    ordered = sorted(sums.keys())
    median_count = statistics.median(counts.values())
    threshold = 0.6 * median_count
    # Trim a leading and/or trailing day if it is clearly partial.
    if len(ordered) >= 3:
        if counts[ordered[0]] < threshold:
            ordered = ordered[1:]
        if ordered and counts[ordered[-1]] < threshold:
            ordered = ordered[:-1]
    return [(day, sums[day]) for day in ordered]


def _median_interval_seconds(series: Series) -> Optional[float]:
    """
    Median spacing between samples, in seconds, used to convert per-interval
    counts into a per-hour rate. Gaps are sanity-clipped to [60s, 3600s] to
    ignore deep-sleep skips and duplicate timestamps. Returns None when the
    cadence cannot be estimated.
    """
    if len(series) < 3:
        return None
    gaps = [
        (series[i][0] - series[i - 1][0]).total_seconds()
        for i in range(1, len(series))
    ]
    gaps = [g for g in gaps if 60.0 <= g <= 3600.0]
    if not gaps:
        return None
    return statistics.median(gaps)


def _per_hour_rate(series: Series) -> Optional[float]:
    """
    Mean crossings-per-hour over ``series``, derived from the mean per-interval
    count and the median inter-sample gap. Returns None when the cadence can't
    be inferred (series too short).
    """
    if not series:
        return None
    seconds = _median_interval_seconds(series)
    if seconds is None:
        return None
    mean_per_interval = _safe_mean(_values(series))
    if mean_per_interval is None:
        return None
    return mean_per_interval * (3600.0 / seconds)


def _forager_decline_frac_per_day(
    bee_out_series: Series,
    end: datetime,
    days: int,
    min_daily_baseline: float,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Fit a linear slope to daily-summed outbound traffic and express the
    decline as a fraction of the early-window baseline per day.

    Returns (decline_frac_per_day, baseline_per_day, slope_per_day):
      * decline_frac_per_day > 0 means traffic is FALLING (a decline).
      * All three are None when there isn't enough data or the baseline is
        below ``min_daily_baseline`` (too noisy to trust).
    """
    daily = _bucket_by_day(bee_out_series, end, days)
    if len(daily) < 4:
        return (None, None, None)
    # Baseline = mean of the earlier half of the window.
    half = max(2, len(daily) // 2)
    baseline = _safe_mean(_values(daily[:half]))
    slope = _linear_slope_per_day(daily)
    if baseline is None or baseline < min_daily_baseline or slope is None:
        return (None, baseline, slope)
    decline_frac = -slope / baseline  # positive when slope is negative
    return (decline_frac, baseline, slope)


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_imminent_swarm(
    hive_temp_series: Series,
    channel: ChannelRef,
    now: datetime,
) -> Optional[Alert]:
    """
    Phase 2 - imminent swarm (~10-30 min ahead) from brood-nest temperature.

    Source: project spec "Phase 2"; Stalidzans & Berzonis 2013.
    """
    last_1h = _window(hive_temp_series, now, hours=1)
    last_4h = _window(hive_temp_series, now, hours=4)
    if len(last_1h) < 3 or len(last_4h) < 5:
        return None

    baseline_cutoff = now - timedelta(hours=1)
    baseline_window = [(t, v) for t, v in last_4h if t <= baseline_cutoff]
    baseline = _safe_mean(_values(baseline_window))
    current = last_1h[-1][1]
    slope_per_hour = _linear_slope_per_day(last_1h)
    if baseline is None or slope_per_hour is None:
        return None
    slope_per_hour = slope_per_hour / 24.0

    delta = current - baseline
    above_brood_upper = current > (BROOD_NEST_TARGET_C + BROOD_NEST_TOLERANCE_C)
    if delta >= IMMINENT_SWARM_DELTA_C and slope_per_hour >= IMMINENT_SWARM_SLOPE_C_PER_HOUR and above_brood_upper:
        return Alert(
            id=f"swarm-imminent-ch{channel}",
            category="swarm",
            severity="warning",
            channel=channel,
            title=f"Imminent swarm warning (hive {channel})",
            description=(
                f"Brood-nest temperature is {current:.1f} degC, "
                f"{delta:.1f} degC above the 4h baseline and still rising "
                f"({slope_per_hour:.2f} degC/h). Swarm may issue within 30 min."
            ),
            window_start=last_4h[0][0],
            window_end=now,
            confidence=min(1.0, 0.5 + delta / 5.0),
            evidence={
                "current_c": current,
                "baseline_c": baseline,
                "delta_c": delta,
                "slope_c_per_hour": slope_per_hour,
            },
            source="project spec Phase 2; Stalidzans & Berzonis 2013",
        )
    return None


def detect_swarm_event(
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    bee_in_series: Optional[Series] = None,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Phase 3 - swarm in progress / just happened from weight signature.

    Source: project spec "Phase 3" (weight half).

    Entrance-counter corroboration (when BeeCounter data is available):
      A swarm departure produces a massive, asymmetric OUTflow. Over the
      detected weight-drop window, if the peak outbound interval exceeds
      SWARM_OUT_BASELINE_MULT x the recent baseline AND
      out / (in + 1) > SWARM_OUT_IN_RATIO, the two signals agree:
        * confidence is raised toward 1.0
        * a daytime event that was already "critical" stays critical; a
          night-time "warning" is promoted to "critical" because the
          asymmetric outflow rules out a measurement artefact.
      Source: project spec Phase 3 (counter half); Seeley 2010.
    """
    last_2h = _window(weight_series, now, hours=2)
    if len(last_2h) < 4:
        return None
    drop, t_start, t_end = _max_drop(last_2h, SWARM_WEIGHT_WINDOW_MIN)
    if drop is None or drop < SWARM_WEIGHT_DROP_KG:
        return None
    hour = now.hour
    in_daytime = SWARM_DAYTIME_HOURS[0] <= hour < SWARM_DAYTIME_HOURS[1]
    severity: AlertSeverity = "critical" if in_daytime else "warning"
    confidence = min(1.0, 0.6 + (drop - SWARM_WEIGHT_DROP_KG) * 0.1)

    evidence: dict[str, Any] = {"drop_kg": drop, "in_daytime": in_daytime}
    desc_parts = [
        f"Weight dropped {drop:.2f} kg within {SWARM_WEIGHT_WINDOW_MIN} min. "
        f"{'Daytime timing strongly suggests a swarm departure.' if in_daytime else 'Unusual time; could be a measurement artefact — investigate.'}"
    ]

    # ── Entrance-counter corroboration (asymmetric outflow) ──────────────────
    if bee_out_series and t_start is not None and t_end is not None:
        # Outbound during the drop window vs a 2h baseline ending at the drop.
        out_window = [(t, v) for t, v in bee_out_series if t_start <= t <= t_end]
        baseline_window = _window(bee_out_series, t_start, hours=2)
        peak_out = max(_values(out_window), default=None)
        baseline_out = _safe_mean(_values(baseline_window))

        in_window = []
        if bee_in_series:
            in_window = [(t, v) for t, v in bee_in_series if t_start <= t <= t_end]
        sum_out = sum(_values(out_window))
        sum_in = sum(_values(in_window))

        if peak_out is not None and baseline_out is not None:
            evidence["counter_peak_out"] = peak_out
            evidence["counter_baseline_out"] = baseline_out
            evidence["counter_sum_out"] = sum_out
            evidence["counter_sum_in"] = sum_in
            out_ratio = sum_out / (sum_in + 1.0)
            evidence["counter_out_in_ratio"] = out_ratio
            massive_outflow = (
                baseline_out >= SWARM_OUT_MIN_BASELINE
                and peak_out > SWARM_OUT_BASELINE_MULT * baseline_out
            )
            if massive_outflow and out_ratio > SWARM_OUT_IN_RATIO:
                confidence = min(1.0, confidence + 0.25)
                if severity == "warning":
                    severity = "critical"
                evidence["counter_swarm_signature"] = True
                desc_parts.append(
                    f"Entrance counter confirms a mass exodus: peak "
                    f"{peak_out:.0f} bees/interval out (baseline "
                    f"{baseline_out:.1f}), outbound/inbound ratio "
                    f"{out_ratio:.1f} — asymmetric outflow consistent with a swarm."
                )
            else:
                evidence["counter_swarm_signature"] = False

    return Alert(
        id=f"swarm-event-ch{channel}",
        category="swarm",
        severity=severity,
        channel=channel,
        title=f"Swarm event detected (hive {channel})",
        description=" ".join(desc_parts),
        window_start=t_start,
        window_end=t_end,
        confidence=confidence,
        evidence=evidence,
        source="project spec Phase 3 (weight + counter); Seeley 2010",
    )


def detect_pre_swarm_temp_instability(
    hive_temp_series: Series,
    channel: ChannelRef,
    now: datetime,
    measurements: Optional[list[dict[str, Any]]] = None,
    accel_swarm_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Phase 1 - pre-swarm watch from brood-nest temperature instability.

    Source: project spec "Phase 1"; MSPB arXiv 2311.10876.
    Rule: rolling 24h std-dev of hive temp exceeds the 7-day baseline std-dev
    by >= 50%.

    Acoustic enhancement (when FFT data is available):
      If the piping band (300–550 Hz) energy >= PIPING_ACTIVE_DBFS, the
      detector interprets this as a queen piping / tooting signal and:
        * raises base confidence by up to +0.35 (capped at 1.0)
        * adds acoustic_piping_dbfs to the evidence dict
      Source: Ramsey et al. (2020) PLOS ONE; Seeley (2010).

    Vibration enhancement (when accelerometer data is available):
      If the night-time 8–30 Hz swarm vibration band has risen by at least
      VIBRATION_SWARM_RISE_MULT vs its baseline, that independently corroborates
      swarm preparation and raises confidence by up to +0.30.
      Source: Ramsey et al. (2020, Sci. Rep.); Bencsik et al. (2011).
    """
    last_24h = _window(hive_temp_series, now, hours=24)
    last_baseline = _window(
        hive_temp_series,
        now - timedelta(hours=24),
        hours=24 * PRE_SWARM_BASELINE_DAYS,
    )
    if len(last_24h) < 6 or len(last_baseline) < 12:
        return None

    s_now = _safe_stddev(_values(last_24h))
    s_base = _safe_stddev(_values(last_baseline))
    if s_now is None or s_base is None or s_base <= 0:
        return None
    ratio = s_now / s_base
    if ratio < PRE_SWARM_STD_MULTIPLIER:
        return None

    # Base confidence from temperature signal alone
    base_confidence = min(1.0, 0.4 + (ratio - 1.5) * 0.5)
    evidence: dict[str, Any] = {
        "stddev_24h_c": s_now,
        "stddev_baseline_c": s_base,
        "ratio": ratio,
    }
    desc_parts = [
        f"24h brood-nest temperature variability ({s_now:.2f} degC) "
        f"is {(ratio - 1) * 100:.0f}% above the 7d baseline. "
        f"Inspect for queen cells in the next 24-48h."
    ]

    # ── Acoustic boost ───────────────────────────────────────────────────────
    piping_dbfs: Optional[float] = None
    if measurements:
        bands = _mic_band_snapshot(measurements, channel)
        piping_dbfs = bands.get("piping")

    if piping_dbfs is not None and piping_dbfs >= PIPING_ACTIVE_DBFS:
        # Piping is active — strong corroboration
        acoustic_boost = min(0.35, 0.15 + (piping_dbfs - PIPING_ACTIVE_DBFS) * 0.01)
        base_confidence = min(1.0, base_confidence + acoustic_boost)
        evidence["acoustic_piping_dbfs"] = piping_dbfs
        evidence["acoustic_piping_active"] = True
        desc_parts.append(
            f"Queen piping signal detected in the 300–550 Hz band "
            f"({piping_dbfs:.1f} dBFS) — acoustic corroboration of swarm preparation."
        )
    elif piping_dbfs is not None:
        evidence["acoustic_piping_dbfs"] = piping_dbfs
        evidence["acoustic_piping_active"] = False

    # ── Vibration boost (accelerometer 8–30 Hz swarm band) ───────────────────
    if accel_swarm_series:
        recent_mg, baseline_mg, ratio = _vibration_swarm_rise(accel_swarm_series, now)
        if ratio is not None:
            evidence["vibration_swarm_recent_mg"] = recent_mg
            evidence["vibration_swarm_baseline_mg"] = baseline_mg
            evidence["vibration_swarm_ratio"] = ratio
            if ratio >= VIBRATION_SWARM_RISE_MULT:
                vib_boost = min(0.30, 0.15 + (ratio - VIBRATION_SWARM_RISE_MULT) * 0.2)
                base_confidence = min(1.0, base_confidence + vib_boost)
                evidence["vibration_swarm_active"] = True
                desc_parts.append(
                    f"Night-time 8–30 Hz comb vibration has risen {ratio:.1f}× "
                    f"vs baseline ({recent_mg:.2f} vs {baseline_mg:.2f} mg) — "
                    f"accelerometer corroboration of swarm preparation."
                )
            else:
                evidence["vibration_swarm_active"] = False

    return Alert(
        id=f"swarm-watch-ch{channel}",
        category="swarm",
        severity="watch",
        channel=channel,
        title=f"Pre-swarm watch (hive {channel})",
        description=" ".join(desc_parts),
        window_start=last_baseline[0][0],
        window_end=now,
        confidence=base_confidence,
        evidence=evidence,
        source="project spec Phase 1 (temp); MSPB arXiv 2311.10876; Ramsey et al. 2020",
    )


def detect_vibration_swarm_prediction(
    accel_swarm_series: Series,
    channel: ChannelRef,
    now: datetime,
) -> Optional[Alert]:
    """
    Pre-swarm prediction from the accelerometer's 8–30 Hz vibration band.

    This is the headline use of the accelerometer and the one signal in the
    literature shown to predict swarming days-to-weeks ahead. Ramsey et al.
    (2020, Sci. Rep. 10:9798) found a ~20 Hz comb vibration that climbs before
    swarming and discriminates best at night; Bencsik et al. (2011) saw the same
    pre-swarm divergence. Microphones miss this band (most have a ~50 Hz floor),
    so it is unique to the accelerometer (Uthoff et al. 2023).

    Rule (active season only): the recent night-time mean of the swarm band has
    risen by >= VIBRATION_SWARM_STANDALONE_MULT vs the night-time baseline, with
    both above the noise floors. Fires a ``swarm`` / ``watch`` alert on its own;
    it also boosts the temperature-based pre-swarm watch when both agree.
    """
    if not _is_active_season(now):
        return None

    recent_mg, baseline_mg, ratio = _vibration_swarm_rise(accel_swarm_series, now)
    if ratio is None or ratio < VIBRATION_SWARM_STANDALONE_MULT:
        return None

    confidence = min(0.9, 0.45 + (ratio - VIBRATION_SWARM_STANDALONE_MULT) * 0.2)
    return Alert(
        id=f"swarm-vibration-ch{channel}",
        category="swarm",
        severity="watch",
        channel=channel,
        title=f"Pre-swarm vibration rising (hive {channel})",
        description=(
            f"Night-time 8–30 Hz comb vibration has risen {ratio:.1f}× over its "
            f"baseline ({recent_mg:.2f} vs {baseline_mg:.2f} mg). A rising ~20 Hz "
            f"vibration is the strongest known multi-day swarm precursor — inspect "
            f"for queen cells and prepare for swarm control."
        ),
        window_start=now - timedelta(days=VIBRATION_BASELINE_DAYS),
        window_end=now,
        confidence=confidence,
        evidence={
            "vibration_swarm_recent_mg": recent_mg,
            "vibration_swarm_baseline_mg": baseline_mg,
            "vibration_swarm_ratio": ratio,
            "night_hours": list(VIBRATION_NIGHT_HOURS),
        },
        source="Ramsey et al. 2020 (Sci. Rep. 10:9798); Bencsik et al. 2011; Uthoff et al. 2023",
    )


def detect_lowrate_accel_swarm(
    accel_rms_series: Series,
    channel: ChannelRef,
    now: datetime,
    accel_swarm_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Pre-swarm watch from a passive BLE accelerometer (HolyIot 25015).

    A passive beacon only emits periodic single-shot acceleration samples, so the
    8–30 Hz FFT swarm band used by ``detect_vibration_swarm_prediction`` cannot be
    computed. Instead this trends the night-time mean of the per-cycle broadband
    magnitude (``accel_{ch}_rms_mg``): rising in-hive movement over days is a
    coarse pre-swarm activity signal consistent with the comb-excitation increase
    Bencsik/Ramsey report before swarming.

    This is intentionally lower-confidence than the FFT-band detector. When real
    8–30 Hz band data exists for this hive (a wired/high-rate sensor), it defers
    to ``detect_vibration_swarm_prediction`` to avoid a duplicate alert. Active
    season only.
    """
    if not _is_active_season(now):
        return None

    # Defer to the FFT-band detector when proper band data is available.
    if accel_swarm_series:
        return None

    recent_mg, baseline_mg, ratio = _lowrate_vibration_rise(accel_rms_series, now)
    if ratio is None or ratio < LOWRATE_SWARM_RISE_MULT:
        return None

    confidence = min(0.7, 0.35 + (ratio - LOWRATE_SWARM_RISE_MULT) * 0.2)
    return Alert(
        id=f"swarm-ble-vibration-ch{channel}",
        category="swarm",
        severity="watch",
        channel=channel,
        title=f"Pre-swarm movement rising (hive {channel})",
        description=(
            f"Night-time in-hive movement measured by the BLE accelerometer has "
            f"risen {ratio:.1f}× over its baseline ({recent_mg:.1f} vs "
            f"{baseline_mg:.1f} mg). Rising comb excitation over several days can "
            f"precede swarming — inspect for queen cells. (Low-rate beacon signal: "
            f"coarser than a wired vibration sensor.)"
        ),
        window_start=now - timedelta(days=VIBRATION_BASELINE_DAYS),
        window_end=now,
        confidence=confidence,
        evidence={
            "lowrate_vibration_recent_mg": recent_mg,
            "lowrate_vibration_baseline_mg": baseline_mg,
            "lowrate_vibration_ratio": ratio,
            "night_hours": list(VIBRATION_NIGHT_HOURS),
            "source_sensor": "ble_holyiot_25015",
        },
        source="Bencsik et al. 2011/2015; Ramsey et al. 2020 (low-rate proxy)",
    )


def detect_queenlessness(
    hive_temp_series: Series,
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    measurements: Optional[list[dict[str, Any]]] = None,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Queenlessness - rule-based with optional acoustic + counter corroboration.

    Source: project spec "Queenlessness detection"; MSPB / BeeTogether.

    Weight/temperature rules (always applied):
      1. 7-day hive-temp std-dev > QUEENLESS_TEMP_STDDEV_C (1.0 degC)
      2. 7-day net weight change <= QUEENLESS_WEIGHT_STAGNANT_KG (0.2 kg)

    Acoustic enhancement (when FFT data is available):
      A queenless colony's hum shifts lower and broader: the hum band
      (150–300 Hz) becomes louder while the piping band (300–550 Hz) is
      quiet (no queen to pipe).  When both acoustic conditions hold:
        * confidence rises from 0.55 → up to 0.80
        * description is updated
      Source: MSPB arXiv 2311.10876; BeeTogether queenless classifiers.

    Forager-decline enhancement (when BeeCounter data is available):
      A queenless colony loses foragers without replacement. When outbound
      traffic declines by >= QUEENLESS_FORAGER_DECLINE_FRAC_PER_DAY (~5%/day)
      sustained across the window, confidence rises by +0.15 (capped at 0.90).
      This is corroborative only — it never fires the alert on its own, since
      a forager decline alone is also consistent with a spell of poor weather.
      Source: project spec queenless (forager-decline rule); MSPB.
    """
    if not _is_active_season(now):
        return None

    days = QUEENLESS_DAYS_WINDOW
    recent_temp = _window(hive_temp_series, now, hours=24 * days)
    recent_weight = _window(weight_series, now, hours=24 * days)
    if len(recent_temp) < 12 or len(recent_weight) < 12:
        return None

    stddev = _safe_stddev(_values(recent_temp))
    if stddev is None or stddev < QUEENLESS_TEMP_STDDEV_C:
        return None

    delta = recent_weight[-1][1] - recent_weight[0][1]
    if abs(delta) > QUEENLESS_WEIGHT_STAGNANT_KG:
        return None

    confidence = 0.55
    evidence: dict[str, Any] = {
        "temp_stddev_c": stddev,
        "weight_delta_kg": delta,
        "window_days": days,
    }
    desc_parts = [
        f"Over the last {days}d, hive temperature variability "
        f"({stddev:.2f} degC) suggests broodless thermoregulation, and "
        f"net weight change is only {delta:+.2f} kg during the active season. "
        f"Inspect for eggs / brood pattern."
    ]

    # ── Acoustic boost ───────────────────────────────────────────────────────
    if measurements:
        bands = _mic_band_snapshot(measurements, channel)
        hum_dbfs    = bands.get("hum")
        piping_dbfs = bands.get("piping")

        hum_elevated   = hum_dbfs    is not None and hum_dbfs    >= QUEENLESS_HUM_DBFS
        piping_quiet   = piping_dbfs is not None and piping_dbfs <  QUEENLESS_PIPING_QUIET_DBFS

        if hum_dbfs is not None:
            evidence["acoustic_hum_dbfs"] = hum_dbfs
        if piping_dbfs is not None:
            evidence["acoustic_piping_dbfs"] = piping_dbfs

        if hum_elevated and piping_quiet:
            # Both acoustic conditions match queenless signature
            confidence = min(0.80, confidence + 0.25)
            evidence["acoustic_queenless_signature"] = True
            desc_parts.append(
                f"Acoustic signature consistent with queenlessness: elevated hum band "
                f"({hum_dbfs:.1f} dBFS) with quiet piping band "
                f"({piping_dbfs:.1f} dBFS)."
            )
        elif hum_elevated or piping_quiet:
            confidence = min(0.70, confidence + 0.10)
            evidence["acoustic_queenless_signature"] = "partial"
        else:
            evidence["acoustic_queenless_signature"] = False

    # ── Forager-decline corroboration (entrance counter) ─────────────────────
    if bee_out_series:
        decline_frac, baseline, slope = _forager_decline_frac_per_day(
            bee_out_series, now, days, QUEENLESS_FORAGER_MIN_DAILY_BASELINE
        )
        if decline_frac is not None:
            evidence["forager_baseline_per_day"] = baseline
            evidence["forager_slope_per_day"] = slope
            evidence["forager_decline_frac_per_day"] = decline_frac
            if decline_frac >= QUEENLESS_FORAGER_DECLINE_FRAC_PER_DAY:
                confidence = min(0.90, confidence + 0.15)
                evidence["forager_decline_active"] = True
                desc_parts.append(
                    f"Outbound forager traffic is declining at "
                    f"{decline_frac * 100:.1f}%/day over the last {days}d "
                    f"(baseline {baseline:.0f} bees/day) — consistent with "
                    f"queen loss."
                )
            else:
                evidence["forager_decline_active"] = False

    return Alert(
        id=f"queenless-ch{channel}",
        category="queenless",
        severity="warning",
        channel=channel,
        title=f"Possible queenlessness (hive {channel})",
        description=" ".join(desc_parts),
        window_start=recent_temp[0][0],
        window_end=now,
        confidence=confidence,
        evidence=evidence,
        source="project spec queenless; MSPB arXiv 2311.10876; BeeTogether",
    )


def detect_robbing(
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    measurements: Optional[list[dict[str, Any]]] = None,
    bee_in_series: Optional[Series] = None,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Robbing detection - rapid weight loss with optional acoustic + counter
    corroboration.

    Source: project spec "Robbing detection".
    Rule: weight loss rate >= ROBBING_WEIGHT_LOSS_KG_PER_HOUR sustained for
    >= ROBBING_MIN_DURATION_MIN minutes, NOT matching a swarm signature.

    Acoustic enhancement (when FFT data is available):
      Robbing produces agitated flight noise concentrated in the stress band
      (550–1500 Hz). When stress_band_dbfs >= ROBBING_STRESS_DBFS:
        * confidence rises by +0.20 (capped at 1.0)
        * severity upgrades from "watch" → "warning" even outside the
          late-afternoon window
      Source: Nolasco et al. (2019) BUZZ dataset; project spec.

    Entrance-counter enhancement (when BeeCounter data is available):
      The canonical robbing traffic signature is an incoming spike with
      comparatively low outgoing — robber bees pour in while the home colony
      does not forage out. Over the last 2h, with asymmetry defined as
      (in - out) / max(in + out, 1):
        * when inbound rate >= ROBBING_MIN_INBOUND_PER_HOUR and asymmetry >=
          ROBBING_IN_OUT_ASYMMETRY, confidence rises by +0.20 and severity is
          upgraded watch → warning.
      Source: project spec robbing (asymmetric-traffic signature).
    """
    last_2h = _window(weight_series, now, hours=2)
    if len(last_2h) < 4:
        return None
    duration_h = (last_2h[-1][0] - last_2h[0][0]).total_seconds() / 3600.0
    if duration_h < ROBBING_MIN_DURATION_MIN / 60.0:
        return None
    delta = last_2h[0][1] - last_2h[-1][1]
    rate_kg_per_h = delta / duration_h if duration_h > 0 else 0.0
    if rate_kg_per_h < ROBBING_WEIGHT_LOSS_KG_PER_HOUR:
        return None

    # Avoid double-firing with the swarm-event detector
    drop, _, _ = _max_drop(last_2h, SWARM_WEIGHT_WINDOW_MIN)
    if drop is not None and drop >= SWARM_WEIGHT_DROP_KG:
        return None

    hour = last_2h[-1][0].hour
    in_afternoon = ROBBING_LATE_AFTERNOON_HOURS[0] <= hour < ROBBING_LATE_AFTERNOON_HOURS[1]
    severity: AlertSeverity = "warning" if in_afternoon else "watch"
    confidence = 0.5 + (0.2 if in_afternoon else 0.0)

    evidence: dict[str, Any] = {
        "rate_kg_per_h": rate_kg_per_h,
        "duration_min": duration_h * 60.0,
        "in_afternoon": in_afternoon,
    }
    desc_parts = [
        f"Sustained weight loss of {rate_kg_per_h:.2f} kg/h over "
        f"{duration_h * 60:.0f} min. "
        f"{'Late afternoon timing is consistent with dearth-period robbing.' if in_afternoon else 'Unusual time of day — investigate.'}"
    ]

    # ── Acoustic boost ───────────────────────────────────────────────────────
    if measurements:
        bands = _mic_band_snapshot(measurements, channel)
        stress_dbfs = bands.get("stress")
        if stress_dbfs is not None:
            evidence["acoustic_stress_dbfs"] = stress_dbfs
            if stress_dbfs >= ROBBING_STRESS_DBFS:
                confidence = min(1.0, confidence + 0.20)
                # Upgrade severity if acoustic confirms even outside afternoon
                if severity == "watch":
                    severity = "warning"
                evidence["acoustic_stress_active"] = True
                desc_parts.append(
                    f"Elevated agitation-band energy detected "
                    f"({stress_dbfs:.1f} dBFS in 550–1500 Hz band) — "
                    f"acoustic signature consistent with robbing activity."
                )
            else:
                evidence["acoustic_stress_active"] = False

    # ── Entrance-counter asymmetry (incoming spike, low outgoing) ────────────
    if bee_in_series and bee_out_series:
        recent_in = _window(bee_in_series, now, hours=2)
        recent_out = _window(bee_out_series, now, hours=2)
        in_rate = _per_hour_rate(recent_in)
        out_rate = _per_hour_rate(recent_out)
        if in_rate is not None and out_rate is not None:
            denom = max(in_rate + out_rate, 1.0)
            asymmetry = (in_rate - out_rate) / denom
            evidence["counter_in_per_hour"] = in_rate
            evidence["counter_out_per_hour"] = out_rate
            evidence["counter_asymmetry"] = asymmetry
            if (
                in_rate >= ROBBING_MIN_INBOUND_PER_HOUR
                and asymmetry >= ROBBING_IN_OUT_ASYMMETRY
            ):
                confidence = min(1.0, confidence + 0.20)
                if severity == "watch":
                    severity = "warning"
                evidence["counter_robbing_signature"] = True
                desc_parts.append(
                    f"Entrance counter shows asymmetric traffic: "
                    f"{in_rate:.0f} bees/h in vs {out_rate:.0f} bees/h out "
                    f"(asymmetry {asymmetry:+.2f}) — classic robbing signature."
                )
            else:
                evidence["counter_robbing_signature"] = False

    return Alert(
        id=f"robbing-ch{channel}",
        category="robbing",
        severity=severity,
        channel=channel,
        title=f"Possible robbing (hive {channel})",
        description=" ".join(desc_parts),
        window_start=last_2h[0][0],
        window_end=now,
        confidence=confidence,
        evidence=evidence,
        source="project spec robbing; Nolasco et al. 2019 BUZZ",
    )


def detect_foraging_intensity(
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Foraging intensity / nectar flow from daily weight delta.

    Source: project spec "Foraging intensity"; Meikle et al. 2008.

    Entrance-counter corroboration (when BeeCounter data is available):
      Outbound traffic is a direct proxy for forager activity and a useful
      cross-check on the weight signal:
        * strong/moderate flow + active outbound traffic -> confidence +0.10
        * strong/moderate flow + little/no traffic -> confidence -0.30
          (weight gain with no foragers leaving is suspect: calibration
          drift, rain on the lid, or someone leaning on the hive)
        * net loss + little/no traffic -> confidence +0.10
          (a stronger negative signal: not just a bad foraging day)
    """
    last_24h = _window(weight_series, now, hours=24)
    if len(last_24h) < 4:
        return None
    delta = last_24h[-1][1] - last_24h[0][1]

    if delta >= FORAGING_STRONG_KG_PER_DAY:
        level, severity = "strong", "info"
    elif delta >= FORAGING_MODERATE_KG_PER_DAY:
        level, severity = "moderate", "info"
    elif delta <= -FORAGING_MODERATE_KG_PER_DAY:
        level, severity = "negative", "watch"
    else:
        return None

    confidence = 0.8
    evidence: dict[str, Any] = {"delta_24h_kg": delta, "level": level}
    desc = (
        f"Net weight change over the last 24h: {delta:+.2f} kg. "
        f"Classified as {level} nectar flow."
    )

    # ── Entrance-counter corroboration ───────────────────────────────────────
    if bee_out_series:
        recent_out = _window(bee_out_series, now, hours=24)
        out_per_hour = _per_hour_rate(recent_out)
        if out_per_hour is not None:
            evidence["forager_out_per_hour"] = out_per_hour
            active_traffic = out_per_hour >= FORAGING_ACTIVE_OUT_PER_HOUR
            if level in ("strong", "moderate"):
                if active_traffic:
                    confidence = min(1.0, confidence + 0.10)
                    evidence["counter_corroborates"] = True
                    desc += (
                        f" Entrance counter agrees: {out_per_hour:.0f} bees/h "
                        f"outbound."
                    )
                else:
                    confidence = max(0.3, confidence - 0.30)
                    evidence["counter_corroborates"] = False
                    desc += (
                        f" But the entrance counter shows little outbound "
                        f"traffic ({out_per_hour:.0f} bees/h) — the weight gain "
                        f"may be a sensor artefact rather than a nectar flow."
                    )
            elif level == "negative":
                if not active_traffic:
                    confidence = min(1.0, confidence + 0.10)
                    evidence["counter_corroborates"] = True
                    desc += (
                        f" Outbound traffic is also low ({out_per_hour:.0f} "
                        f"bees/h), reinforcing the negative signal."
                    )
                else:
                    evidence["counter_corroborates"] = False

    return Alert(
        id=f"foraging-ch{channel}",
        category="foraging",
        severity=severity,  # type: ignore[arg-type]
        channel=channel,
        title=f"Foraging: {level} flow (hive {channel})",
        description=desc,
        window_start=last_24h[0][0],
        window_end=now,
        confidence=confidence,
        evidence=evidence,
        source="project spec foraging; Meikle et al. 2008",
    )


def detect_brood_cycle_state(
    hive_temp_series: Series,
    channel: ChannelRef,
    now: datetime,
) -> Optional[Alert]:
    """
    Brood-cycle / colony-state classifier from rolling 24h std-dev of
    hive temperature.

    Source: project spec "Brood cycle / colony state".
    """
    last_24h = _window(hive_temp_series, now, hours=24)
    if len(last_24h) < 8:
        return None
    stddev = _safe_stddev(_values(last_24h))
    mean = _safe_mean(_values(last_24h))
    if stddev is None or mean is None:
        return None

    if stddev < BROOD_ACTIVE_STDDEV_C and abs(mean - BROOD_NEST_TARGET_C) <= BROOD_NEST_TOLERANCE_C:
        title = f"Active brood rearing (hive {channel})"
        desc = (
            f"24h hive temp held at {mean:.1f}+/-{stddev:.2f} degC — "
            f"consistent with active brood thermoregulation."
        )
        severity: AlertSeverity = "info"
    elif stddev > BROOD_BROODLESS_STDDEV_C:
        title = f"Broodless / weak colony indicator (hive {channel})"
        desc = (
            f"24h hive temp variability is wide (+/-{stddev:.2f} degC, "
            f"mean {mean:.1f} degC). Suggests little or no brood being thermoregulated."
        )
        severity = "watch"
    else:
        return None

    return Alert(
        id=f"brood-state-ch{channel}",
        category="brood",
        severity=severity,
        channel=channel,
        title=title,
        description=desc,
        window_start=last_24h[0][0],
        window_end=now,
        confidence=0.7,
        evidence={"mean_c": mean, "stddev_c": stddev},
        source="project spec brood cycle",
    )


def detect_absconding_trend(
    hive_temp_series: Series,
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Absconding / collapse early warning — compound declining trend.

    Source: project spec "Absconding / collapse early warning".

    Two-rule base (always applied):
      1. Weight loss >= ABSCONDING_WEIGHT_LOSS_G_PER_DAY (100 g/day) sustained
         over ABSCONDING_LOOKBACK_DAYS (14 days).
      2. Rolling 24h temperature std-dev has a positive slope (widening
         variability => failing thermoregulation).

    Third rule (when BeeCounter data is available):
      3. Outbound forager traffic declining by >=
         ABSCONDING_FORAGER_DECLINE_FRAC_PER_DAY (3%/day) over the lookback.
      When the third rule confirms, the alert is promoted from "watch" to
      "warning" and confidence rises 0.5 -> 0.75.
    """
    lookback_h = 24 * ABSCONDING_LOOKBACK_DAYS
    weight_window = _window(weight_series, now, hours=lookback_h)
    temp_window = _window(hive_temp_series, now, hours=lookback_h)
    if len(weight_window) < 14 or len(temp_window) < 14:
        return None

    weight_slope = _linear_slope_per_day(weight_window)
    if weight_slope is None:
        return None
    weight_loss_g_per_day = -weight_slope * 1000.0
    if weight_loss_g_per_day < ABSCONDING_WEIGHT_LOSS_G_PER_DAY:
        return None

    # Compute rolling 24h std-dev series and its slope
    daily_stddev: Series = []
    step = timedelta(hours=6)
    cursor = weight_window[0][0] + timedelta(hours=24)
    while cursor <= now:
        seg = _window(temp_window, cursor, hours=24)
        sd = _safe_stddev(_values(seg))
        if sd is not None:
            daily_stddev.append((cursor, sd))
        cursor += step

    if len(daily_stddev) < 4:
        return None
    stddev_slope = _linear_slope_per_day(daily_stddev)
    if stddev_slope is None or stddev_slope <= 0:
        return None

    # ── Third rule: forager (outbound) decline (entrance counter) ────────────
    severity: AlertSeverity = "watch"
    confidence = 0.5
    forager_evidence: dict[str, Any] = {}
    forager_decline_active = False

    if bee_out_series:
        decline_frac, baseline, slope = _forager_decline_frac_per_day(
            bee_out_series,
            now,
            ABSCONDING_LOOKBACK_DAYS,
            QUEENLESS_FORAGER_MIN_DAILY_BASELINE,
        )
        if decline_frac is not None:
            forager_evidence = {
                "forager_baseline_per_day": baseline,
                "forager_slope_per_day": slope,
                "forager_decline_frac_per_day": decline_frac,
            }
            if decline_frac >= ABSCONDING_FORAGER_DECLINE_FRAC_PER_DAY:
                severity = "warning"
                confidence = 0.75
                forager_decline_active = True

    forager_evidence["forager_decline_active"] = forager_decline_active

    desc = (
        f"Weight loss of {weight_loss_g_per_day:.0f} g/day sustained "
        f"over {ABSCONDING_LOOKBACK_DAYS}d, combined with widening "
        f"temperature variability (slope +{stddev_slope:.3f} degC/day)."
    )
    if forager_decline_active:
        desc += (
            f" Outbound forager traffic is also declining at "
            f"{forager_evidence['forager_decline_frac_per_day'] * 100:.1f}%/day "
            f"— three concurrent decline signals."
        )
    desc += " Inspect for queen problems, disease, or pre-absconding stress."

    return Alert(
        id=f"absconding-ch{channel}",
        category="decline",
        severity=severity,
        channel=channel,
        title=f"Absconding / collapse risk (hive {channel})",
        description=desc,
        window_start=weight_window[0][0],
        window_end=now,
        confidence=confidence,
        evidence={
            "weight_loss_g_per_day": weight_loss_g_per_day,
            "stddev_slope_c_per_day": stddev_slope,
            "current_daily_stddev_c": daily_stddev[-1][1],
            **forager_evidence,
        },
        source=(
            "project spec absconding/collapse"
            + (" (3 of 3 rules)" if forager_decline_active else " (2 of 3 rule)")
        ),
    )


def detect_winter_risk(
    hive_temp_series: Series,
    ambient_temp_series: Series,
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
    bee_out_series: Optional[Series] = None,
) -> Optional[Alert]:
    """
    Winter survival risk (Oct-Feb) from cluster temperature and weight loss.

    Source: project spec "Winter survival risk".

    Entrance-counter corroboration (when BeeCounter data is available):
      A cleansing flight on a warm winter day — any interval with outbound
      >= WINTER_CLEANSING_FLIGHT_OUT — is positive evidence that the cluster
      is alive and active. When seen, it lowers confidence in the risk alert
      by 0.15 (floor 0.3) and is noted in the description. Absence of flights
      is NOT treated as negative evidence (bees rightly stay clustered in the
      cold), so this can only soften, never strengthen, the alert.
    """
    if not _is_winter(now):
        return None

    last_7d_temp = _window(hive_temp_series, now, hours=24 * 7)
    last_7d_amb = _window(ambient_temp_series, now, hours=24 * 7)
    last_7d_weight = _window(weight_series, now, hours=24 * 7)
    if len(last_7d_temp) < 12 or len(last_7d_weight) < 4 or len(last_7d_amb) < 12:
        return None

    min_hive = min(_values(last_7d_temp))
    ambient_mean = _safe_mean(_values(last_7d_amb))
    if ambient_mean is None:
        return None
    cluster_weak = min_hive < (ambient_mean + WINTER_CLUSTER_DELTA_C)

    weight_delta_kg = last_7d_weight[-1][1] - last_7d_weight[0][1]
    weight_loss_g_per_week = -weight_delta_kg * 1000.0
    consumption_high = weight_loss_g_per_week > WINTER_WEIGHT_LOSS_G_PER_WEEK

    if not (cluster_weak or consumption_high):
        return None
    severity: AlertSeverity = "warning" if cluster_weak and consumption_high else "watch"
    confidence = 0.6

    description = (
        f"Min hive temp last 7d: {min_hive:.1f} degC vs. mean ambient "
        f"{ambient_mean:.1f} degC. Weight change: {weight_delta_kg:+.2f} kg/week."
        + (" Cluster appears weak/small." if cluster_weak else "")
        + (" Consumption is abnormally high." if consumption_high else "")
    )

    evidence: dict[str, Any] = {
        "min_hive_temp_c": min_hive,
        "mean_ambient_c": ambient_mean,
        "weight_loss_g_per_week": weight_loss_g_per_week,
        "cluster_weak": cluster_weak,
        "consumption_high": consumption_high,
    }

    # ── Cleansing-flight corroboration (entrance counter) ────────────────────
    if bee_out_series:
        last_7d_out = _window(bee_out_series, now, hours=24 * 7)
        peak_out = max(_values(last_7d_out), default=None)
        if peak_out is not None:
            evidence["counter_peak_out_7d"] = peak_out
            if peak_out >= WINTER_CLEANSING_FLIGHT_OUT:
                confidence = max(0.3, confidence - 0.15)
                evidence["cleansing_flight_seen"] = True
                description += (
                    f" However, a cleansing flight was recorded "
                    f"({peak_out:.0f} bees out in one interval) — the cluster "
                    f"is alive and active, which lowers the risk."
                )
            else:
                evidence["cleansing_flight_seen"] = False

    return Alert(
        id=f"winter-ch{channel}",
        category="winter",
        severity=severity,
        channel=channel,
        title=f"Winter survival risk (hive {channel})",
        description=description,
        window_start=last_7d_temp[0][0],
        window_end=now,
        confidence=confidence,
        evidence=evidence,
        source="project spec winter survival",
    )


def detect_harvest_window(
    weight_series: Series,
    channel: ChannelRef,
    now: datetime,
) -> Optional[Alert]:
    """
    Honey-ready / harvest timing — plateau after sustained gain.

    Source: project spec "Honey-ready / harvest timing".
    """
    last_11d = _window(weight_series, now, hours=24 * 11)
    if len(last_11d) < 24:
        return None

    win_now = _window(weight_series, now, hours=24 * 7)
    win_prev = _window(
        weight_series,
        now - timedelta(days=HARVEST_PLATEAU_DAYS),
        hours=24 * 7,
    )
    if len(win_now) < 4 or len(win_prev) < 4:
        return None
    delta_now = win_now[-1][1] - win_now[0][1]
    delta_prev = win_prev[-1][1] - win_prev[0][1]

    if delta_prev > HARVEST_FLOW_KG_PER_WEEK and delta_now < HARVEST_PLATEAU_KG_PER_WEEK:
        return Alert(
            id=f"harvest-ch{channel}",
            category="harvest",
            severity="info",
            channel=channel,
            title=f"Harvest window likely open (hive {channel})",
            description=(
                f"Weight gain has plateaued: {delta_prev:+.2f} kg/week "
                f"earlier this period vs {delta_now:+.2f} kg/week now. "
                f"The flow appears finished — supers may be ready to harvest."
            ),
            window_start=win_prev[0][0],
            window_end=now,
            confidence=0.7,
            evidence={
                "delta_prev_kg_per_week": delta_prev,
                "delta_now_kg_per_week": delta_now,
            },
            source="project spec harvest timing",
        )
    return None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def detect_hiveheart_spectrum_shift(
    fft_series: list[HiveHeartBins],
    channel: int,
    now: datetime,
) -> Optional[Alert]:
    """Informational HiveHeart FFT change detector (relative levels, not dBFS).

    Compares a recent window of decoded HiveHeart spectra against the hive's own
    longer baseline and flags a meaningful shift in either the relative-level
    spectral centroid or the total relative activity. There are no calibrated
    absolute thresholds for these packed 0–15 levels, so this stays low-severity
    ("info"): it says "this colony's in-hive sound changed", never a direct
    acoustic diagnosis, and never compares against the microphone dBFS bands.
    """
    if len(fft_series) < HIVEHEART_MIN_RECENT_SAMPLES + HIVEHEART_MIN_BASELINE_SAMPLES:
        return None
    recent_cut = now - timedelta(hours=HIVEHEART_RECENT_HOURS)
    baseline_cut = now - timedelta(days=HIVEHEART_BASELINE_DAYS)
    recent = [(t, b) for t, b in fft_series if t > recent_cut]
    baseline = [(t, b) for t, b in fft_series if baseline_cut < t <= recent_cut]
    if len(recent) < HIVEHEART_MIN_RECENT_SAMPLES or len(baseline) < HIVEHEART_MIN_BASELINE_SAMPLES:
        return None

    def _mean_of(rows, fn):
        vals = [fn(b) for _, b in rows]
        return _safe_mean([v for v in vals if v is not None])

    recent_activity = _mean_of(recent, total_activity)
    baseline_activity = _mean_of(baseline, total_activity)
    recent_centroid = _mean_of(recent, spectral_centroid_hz)
    baseline_centroid = _mean_of(baseline, spectral_centroid_hz)

    # Require the hive to be acoustically "awake" recently; a silent HiveHeart
    # carries no meaningful spectrum to compare against its baseline.
    if recent_activity is None or recent_activity < HIVEHEART_MIN_ACTIVITY:
        return None

    reasons: list[str] = []
    evidence: dict[str, Any] = {
        "recent_activity": round(recent_activity, 2),
        "baseline_activity": round(baseline_activity, 2) if baseline_activity is not None else None,
        "recent_centroid_hz": round(recent_centroid, 1) if recent_centroid is not None else None,
        "baseline_centroid_hz": round(baseline_centroid, 1) if baseline_centroid is not None else None,
        "recent_samples": len(recent),
        "baseline_samples": len(baseline),
    }

    if recent_centroid is not None and baseline_centroid is not None:
        centroid_shift = recent_centroid - baseline_centroid
        if abs(centroid_shift) >= HIVEHEART_CENTROID_SHIFT_HZ:
            direction = "up" if centroid_shift > 0 else "down"
            reasons.append(
                f"spectral centroid shifted {direction} by {abs(centroid_shift):.0f} Hz "
                f"({baseline_centroid:.0f}->{recent_centroid:.0f} Hz)"
            )
            evidence["centroid_shift_hz"] = round(centroid_shift, 1)

    if (
        baseline_activity is not None
        and baseline_activity >= HIVEHEART_MIN_ACTIVITY
        and recent_activity >= baseline_activity * HIVEHEART_ACTIVITY_RISE_MULT
    ):
        reasons.append(
            f"total relative activity rose {recent_activity / baseline_activity:.1f}x "
            f"({baseline_activity:.0f}->{recent_activity:.0f})"
        )
        evidence["activity_rise_mult"] = round(recent_activity / baseline_activity, 2)

    if not reasons:
        return None

    # Context: which frequencies dominate now, and the overlap-weighted bands.
    latest_bins = recent[-1][1]
    dom = dominant_range(latest_bins)
    if dom:
        evidence["dominant_range_hz"] = [dom["lower_hz"], dom["upper_hz"]]
    bands = semantic_bands(latest_bins)
    if bands is not None:
        evidence["semantic_bands"] = {k: round(v, 2) for k, v in bands.items()}

    return Alert(
        id=f"hiveheart-spectrum-shift-ch{channel}",
        category="acoustic",
        severity="info",
        channel=channel,
        title=f"HiveHeart spectrum shift (hive {channel})",
        description=(
            "In-hive HiveHeart sound changed vs this colony's 7-day baseline: "
            + "; ".join(reasons)
            + ". Relative levels (0-15), not calibrated dB — informational only."
        ),
        window_start=recent[0][0],
        window_end=now,
        confidence=0.4,
        evidence=evidence,
        source="HiveHeart FFT baseline comparison (relative levels; uncalibrated)",
    )


def compute_insights(
    measurements: list[dict[str, Any]],
    now: Optional[datetime] = None,
) -> list[Alert]:
    """
    Run all detectors against both scale channels and return a flat list of
    Alerts sorted by severity then time.

    ``measurements`` is the same shape as ``measurement_row_to_dict`` returns
    in main.py: a list of dicts with at least ``measured_at``,
    ``scale_1_weight_kg``, ``scale_2_weight_kg``, ``hive_1_temp_c``,
    ``hive_2_temp_c``, ``ambient_temp_c``.
    Mic / FFT band fields and BeeCounter entrance-counter fields
    (``bee_counter_{ch}_interval_in`` / ``_interval_out``, gated by
    ``bee_counter_{ch}_ok``) are optional and consumed transparently —
    every detector degrades to its weight/temperature-only rule when they
    are absent.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    ambient = _extract_series(measurements, "ambient_temp_c")
    alerts: list[Alert] = []

    # Run every detector for each hive the device reports (up to 18). The read
    # layer synthesizes flat scale_N_/hive_N_ keys for hives beyond 2, so the
    # detectors and _extract_* helpers below work unchanged per channel.
    for channel in _hive_channels(measurements):
        weight_field = f"scale_{channel}_weight_kg"
        temp_field = f"hive_{channel}_temp_c"
        weight = _extract_series(measurements, weight_field)
        hive_temp = _extract_series(measurements, temp_field)
        bee_in = _extract_counter_series(measurements, channel, "in")
        bee_out = _extract_counter_series(measurements, channel, "out")
        accel_swarm = _accel_band_series(measurements, channel, "swarm")
        accel_rms = _accel_rms_series(measurements, channel)
        hiveheart_fft = _hiveheart_fft_series(measurements, channel)

        if (
            not weight and not hive_temp and not bee_in and not bee_out
            and not accel_swarm and not accel_rms and not hiveheart_fft
        ):
            continue

        # Detectors that accept acoustic data get ``measurements`` passed in.
        # Detectors that accept entrance-counter data get bee_in / bee_out.
        # Detectors that accept vibration data get accel_swarm (8–30 Hz band).
        for detector in (
            lambda: detect_imminent_swarm(hive_temp, channel, now),
            lambda: detect_swarm_event(weight, channel, now, bee_in, bee_out),
            lambda: detect_pre_swarm_temp_instability(
                hive_temp, channel, now, measurements, accel_swarm
            ),
            lambda: detect_vibration_swarm_prediction(accel_swarm, channel, now),
            lambda: detect_lowrate_accel_swarm(
                accel_rms, channel, now, accel_swarm
            ),
            lambda: detect_queenlessness(
                hive_temp, weight, channel, now, measurements, bee_out
            ),
            lambda: detect_robbing(
                weight, channel, now, measurements, bee_in, bee_out
            ),
            lambda: detect_foraging_intensity(weight, channel, now, bee_out),
            lambda: detect_brood_cycle_state(hive_temp, channel, now),
            lambda: detect_absconding_trend(
                hive_temp, weight, channel, now, bee_out
            ),
            lambda: detect_winter_risk(
                hive_temp, ambient, weight, channel, now, bee_out
            ),
            lambda: detect_harvest_window(weight, channel, now),
            lambda: detect_hiveheart_spectrum_shift(hiveheart_fft, channel, now),
        ):
            try:
                alert = detector()
            except Exception:
                alert = None
            if alert is not None:
                alerts.append(alert)

    severity_rank = {"critical": 4, "warning": 3, "watch": 2, "info": 1}
    alerts.sort(
        key=lambda a: (
            -severity_rank.get(a.severity, 0),
            a.window_end or datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    return alerts


# ---------------------------------------------------------------------------
# Convenience dataclass for the HTTP summary endpoint
# ---------------------------------------------------------------------------

@dataclass
class InsightsSummary:
    device_id: str
    computed_at: datetime
    alert_count: int
    highest_severity: Optional[AlertSeverity]
    highest_alert: Optional[Alert]
    categories: list[AlertCategory]


def summarize(device_id: str, alerts: list[Alert], computed_at: datetime) -> InsightsSummary:
    severity_rank = {"critical": 4, "warning": 3, "watch": 2, "info": 1}
    highest = max(alerts, key=lambda a: severity_rank.get(a.severity, 0), default=None)
    categories = sorted({a.category for a in alerts})
    return InsightsSummary(
        device_id=device_id,
        computed_at=computed_at,
        alert_count=len(alerts),
        highest_severity=highest.severity if highest else None,
        highest_alert=highest,
        categories=categories,
    )