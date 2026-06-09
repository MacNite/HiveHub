"""Load-cell temperature compensation — pure, database-free logic.

HX711/load-cell readings drift with temperature: the bridge resistance and the
mechanical structure both change with ambient temperature, so an *unchanged*
physical load is reported as a slowly varying weight. On a beehive that drift
masquerades as nectar income at midday and as consumption overnight.

The firmware already ships everything needed to correct this after the fact —
every measurement carries the raw load-cell weight *and* an ambient temperature
(``ambient_temp_c`` from the SHT4x, plus the two DS18B20 hive probes). We keep
the raw values untouched in the database and apply a first-order correction on
read, using a per-device, per-scale coefficient. Because the raw data is never
mutated, the coefficient can be re-tuned (or disabled) at any time without
losing anything.

Model
-----
A first-order (linear) correction around a reference temperature::

    compensated_kg = raw_kg - coeff_kg_per_c * (temp_c - ref_temp_c)

* ``coeff_kg_per_c`` is the observed drift of the *reported* weight per °C.
  Positive means the scale reads heavier as it warms; we subtract that drift.
* ``ref_temp_c`` is the temperature at which the correction is zero — i.e. the
  temperature the compensated reading is normalized to. Picking the mean
  temperature of the calibration window keeps the correction centered and small.

The coefficient is expressed directly in **kg per °C** (not ppm/°C of span) so
it applies to the stored kilogram value without needing to know the cell's
full-scale span. Typical strain-gauge cells drift on the order of a few grams
per °C, but measuring your own from logged data (see :func:`fit_temp_coefficient`)
beats any datasheet number.

These functions are deliberately free of FastAPI/psycopg imports so they can be
unit-tested with no database or server (see test-data/test_tempcomp.py).
"""

from __future__ import annotations

from typing import Iterable, Optional


# Temperature field a coefficient may be keyed against. "ambient" (SHT4x) is the
# sensible default — it tracks the cell body closely in steady state. The hive
# probes are offered for setups where the cell sits inside the hive thermal mass.
VALID_TEMP_SOURCES = ("ambient", "hive_1", "hive_2")

# Field name in a measurement dict for each temperature source.
TEMP_SOURCE_FIELD = {
    "ambient": "ambient_temp_c",
    "hive_1": "hive_1_temp_c",
    "hive_2": "hive_2_temp_c",
}

# Defaults mirrored by the device_configs columns / DeviceConfig model.
DEFAULT_REF_TEMP_C = 20.0
DEFAULT_TEMP_SOURCE = "ambient"


def compensate_weight(
    weight_kg: Optional[float],
    temp_c: Optional[float],
    ref_temp_c: Optional[float],
    coeff_kg_per_c: Optional[float],
) -> Optional[float]:
    """Apply the first-order temperature correction to a single weight.

    Returns the raw weight unchanged whenever the correction cannot be applied
    (missing weight, missing temperature, or a zero/None coefficient). This
    makes the function safe to call unconditionally — a row without a usable
    temperature simply passes through.
    """
    if weight_kg is None:
        return None
    if not coeff_kg_per_c:  # None or 0.0 → nothing to correct
        return weight_kg
    if temp_c is None:
        return weight_kg
    ref = DEFAULT_REF_TEMP_C if ref_temp_c is None else ref_temp_c
    return weight_kg - coeff_kg_per_c * (temp_c - ref)


def fit_temp_coefficient(samples: Iterable[tuple]) -> dict:
    """Least-squares fit of weight-vs-temperature drift.

    ``samples`` is an iterable of ``(temp_c, weight_kg)`` pairs, ideally captured
    while the *physical* load is constant (an empty/unworked hive, or a fixed
    reference mass) so that any weight variation is attributable to temperature.

    Returns a dict describing the fit:

    * ``coeff_kg_per_c`` — fitted slope, ready to store as the device coefficient.
    * ``ref_temp_c``     — mean temperature of the window (the natural reference).
    * ``intercept_kg``   — weight the model predicts at 0 °C (diagnostic only).
    * ``r_squared``      — goodness of fit in ``[0, 1]``; low values mean the
                           drift is not well explained by temperature alone, so
                           the coefficient should be treated with suspicion.
    * ``n``              — number of usable samples.
    * ``temp_min_c`` / ``temp_max_c`` — span actually covered; a fit over a
                           narrow temperature range extrapolates poorly.

    ``ok`` is False (with a ``reason``) when there is not enough signal to fit
    — fewer than two points, or zero temperature variation.
    """
    pts = [
        (float(t), float(w))
        for t, w in samples
        if t is not None and w is not None
    ]
    n = len(pts)
    base = {
        "ok": False,
        "coeff_kg_per_c": 0.0,
        "ref_temp_c": DEFAULT_REF_TEMP_C,
        "intercept_kg": None,
        "r_squared": None,
        "n": n,
        "temp_min_c": None,
        "temp_max_c": None,
    }
    if n < 2:
        base["reason"] = "need at least 2 samples with both temperature and weight"
        return base

    temps = [t for t, _ in pts]
    weights = [w for _, w in pts]
    mean_t = sum(temps) / n
    mean_w = sum(weights) / n

    s_tt = sum((t - mean_t) ** 2 for t in temps)
    s_tw = sum((t - mean_t) * (w - mean_w) for t, w in pts)
    s_ww = sum((w - mean_w) ** 2 for w in weights)

    base["temp_min_c"] = min(temps)
    base["temp_max_c"] = max(temps)

    if s_tt == 0.0:
        base["reason"] = "temperature does not vary across the samples"
        return base

    slope = s_tw / s_tt
    intercept = mean_w - slope * mean_t
    r_squared = (s_tw * s_tw) / (s_tt * s_ww) if s_ww > 0 else 1.0

    base.update(
        ok=True,
        coeff_kg_per_c=slope,
        ref_temp_c=mean_t,
        intercept_kg=intercept,
        r_squared=r_squared,
    )
    return base
