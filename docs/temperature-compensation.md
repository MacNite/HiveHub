# Load-cell temperature compensation

HX711 strain-gauge load cells drift with temperature. Both the bridge resistance
and the mechanical structure of the cell change as it warms and cools, so an
**unchanged physical load** is reported as a slowly varying weight. On a beehive
this drift looks like nectar income through the warm afternoon and like
consumption overnight — a daily sawtooth riding on top of the real signal.

HiveHub corrects for this in the **backend**, not on the device.

## Why the backend, not the firmware

Every measurement the firmware sends already carries everything needed to correct
the reading after the fact:

- the raw load-cell weight (`scale_1_weight_kg` / `scale_2_weight_kg`) and the raw
  ADC counts (`scale_1_raw` / `scale_2_raw`);
- an ambient temperature from the SHT4x (`ambient_temp_c`), plus two DS18B20 hive
  probes (`hive_1_temp_c`, `hive_2_temp_c`).

Because the raw values are stored untouched, the backend can:

- **re-tune or disable** the correction at any time without losing data;
- **fit the coefficient from the device's own logged history** instead of a
  datasheet guess;
- **apply it retroactively** to data already collected.

Doing it on-device would bake a fixed guess into the transmitted weight forever
and add code to the deep-sleep path for no benefit. The only case for on-device
compensation is a deployment that reads weights straight off the device with no
backend — which is not how HiveHub is used.

## The model

A first-order (linear) correction around a reference temperature, applied to an
**EMA-smoothed** temperature:

```
ema_temp_c    = ema(temp_c)            # exponential moving average, per device
compensated_kg = raw_kg - coeff_kg_per_c * (ema_temp_c - ref_temp_c)
```

- `coeff_kg_per_c` — drift of the **reported** weight per °C, in kg/°C. Positive
  means the scale reads heavier as it warms; the term is subtracted to remove it.
  Expressed in kg/°C (not ppm/°C of span) so it applies directly to the stored
  kilogram value.
- `ref_temp_c` — the temperature at which the correction is zero, i.e. the
  temperature the compensated reading is normalized to.

### Temperature smoothing (EMA)

Before the correction is applied, the temperature channel is run through an
**exponential moving average** (`ema_temperatures` in `server/tempcomp.py`),
computed per device over its time-ordered measurements:

```
ema_t = alpha * temp_t + (1 - alpha) * ema_(t-1)
```

The ambient sensor reacts faster than the load cell's body, so on fast swings
(sunrise, direct sun on the enclosure) the raw temperature would over-correct
and inject a spike into the compensated weight. The EMA damps those transients
so the corrected series tracks the slow thermal drift instead of the sensor's
noise. `alpha` defaults to `0.3` (`DEFAULT_EMA_ALPHA`) — at a 10-minute
measurement interval that is roughly a 20-minute time constant. `alpha = 1.0`
disables smoothing (raw temperature); lower values smooth more. `None` readings
pass through and the EMA carries its last value across the gap.

The math lives in [`server/tempcomp.py`](../server/tempcomp.py) and is covered by
[`test-data/test_tempcomp.py`](../test-data/test_tempcomp.py) (pure functions, no
database needed).

## Configuration

Per-device, per-scale settings on the `device_configs` row (exposed through the
config endpoints):

| Field | Default | Meaning |
|---|---|---|
| `tempco_enabled` | `false` | Master switch. Until set, compensation is a no-op. |
| `tempco_source` | `ambient` | Temperature channel: `ambient`, `hive_1`, or `hive_2`. |
| `tempco_ref_temp_c` | `20.0` | Reference temperature (°C). |
| `scale1_tempco_kg_per_c` | `0.0` | Scale 1 coefficient (kg/°C). |
| `scale2_tempco_kg_per_c` | `0.0` | Scale 2 coefficient (kg/°C). |

Set them via `PATCH /api/v1/app/devices/{device_id}/config` (owner/admin), or let
the fit endpoint write them for you.

> **Note on the temperature source.** The SHT4x measures *ambient air*, which
> tracks the cell body closely in steady state but lags it during fast swings
> (sunrise, direct sun on the enclosure). The EMA smoothing (above) is what keeps
> those transients from corrupting the correction; a thermistor bonded to the cell
> body would be the gold standard, but the ambient sensor plus EMA is a solid,
> already-present starting point.

## What the API returns

The per-device measurement endpoints add, alongside the raw weights:

- `scale_1_weight_kg_compensated`, `scale_2_weight_kg_compensated`
- `tempco_applied` (boolean)

When compensation is disabled (or no coefficient is set), the compensated values
equal the raw weights and `tempco_applied` is `false`, so clients can always read
the compensated field unconditionally.

The raw `scale_*_weight_kg` columns are **never** modified. The insight detectors
(`server/insights.py`) currently still run on the raw weight; feeding them the
compensated series is a natural follow-up but is deliberately left out here so
alert behaviour does not change silently.

## Deriving the coefficient

The best coefficient is measured, not guessed. Capture a window where the
**physical load is constant** and the temperature swings enough to expose the
drift, then fit:

1. Leave a scale under a fixed load — an empty/unworked hive, or a known
   reference mass — across at least one clear day/night cycle. (Optionally use
   [calibration mode](calibration-mode.md) for a denser, clearly-tagged capture.)
2. Fit and apply:

   ```http
   POST /api/v1/app/devices/{device_id}/temp-compensation/fit
   {
     "scale": 1,
     "lookback_days": 3,
     "temp_source": "ambient",
     "calibration_mode_only": false,
     "apply": true
   }
   ```

   The endpoint regresses that scale's raw weight against the chosen temperature
   over the window — first running the temperature through the same EMA smoothing
   used at read time, so the coefficient is fitted in the regime it is applied in
   — and returns:

   - `coeff_kg_per_c` — the fitted slope;
   - `ref_temp_c` — the mean temperature of the window (the natural reference);
   - `r_squared` — goodness of fit in `[0, 1]`. **Low values mean temperature
     does not explain the drift well** — treat the coefficient with suspicion and
     check the load really was constant;
   - `n`, `temp_min_c`, `temp_max_c` — sample count and the temperature span
     actually covered. A fit over a narrow span extrapolates poorly.

   With `"apply": true` and a successful fit, the coefficient, reference
   temperature and source are written to the config and `tempco_enabled` is set.
   With `"apply": false` you get the numbers back without changing anything.

3. Verify: pull recent measurements and confirm the daily sawtooth in
   `scale_*_weight_kg_compensated` is reduced versus the raw series.

Repeat per scale (`"scale": 2`). Coefficients are independent because the two
cells differ.
