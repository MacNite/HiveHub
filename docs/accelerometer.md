# Per-hive vibration monitoring (BLE) — the ~20 Hz pre-swarm signal

> **The wired LIS3DH / LIS2DH12 accelerometer driver has been removed.** Earlier
> firmware carried an optional register-level driver (`ENABLE_LIS3DH_ACCEL`) for
> two accelerometers on the shared I2C bus at `0x18` / `0x19`. It was never part
> of the stock measurement path and is now gone entirely — there is no wired
> accelerometer support, and no `ENABLE_LIS3DH_ACCEL` flag. In-hive vibration
> comes **only from a paired BLE sensor**: a **HiveInside** node supplies full
> FFT bands, while a passive **HolyIot 25015 / RuuviTag** beacon (which cannot
> produce FFT bands) feeds a separate **low-rate** pre-swarm detector. See
> [holyiot-ble-sensor.md](holyiot-ble-sensor.md) and
> [hiveinside-ble-sensor.md](hiveinside-ble-sensor.md).

This document explains **why** vibration matters, **what** the `accel_*` fields
mean, and **how** the server evaluates them. The measurement fields and the
insight detectors are unchanged — only the source of the data moved from a wired
chip to a BLE sensor.

---

## Why measure vibration in addition to the microphones?

HiveHub can carry two INMP441 microphones (50–3000 Hz FFT bands). Vibration is
not a duplicate — it covers the **sub-audible band the microphones miss**, which
the literature identifies as the single most useful swarm-prediction signal.

The review *Uthoff, Nabhan Homsi & von Bergen (2023), "Acoustic and vibration
monitoring of honeybee colonies for beekeeping-relevant aspects of presence of
queen bee and swarming", Computers and Electronics in Agriculture 205:107589*
surveys the field and concludes (emphasis added):

> "Additionally, [Ramsey et al. (2020)] found that one of the frequencies that
> is potentially important for predicting swarming is at about **20 Hz and
> cannot be recorded by most microphones**. Thus, **including low-frequency
> accelerometers** into the setup may be a great way of **maximising the data
> quality**."

Key findings that motivate the vibration bands:

| Finding | Source (via the review) | Consequence for HiveHub |
|---|---|---|
| A ~**20 Hz** comb vibration is the strongest known **multi-day swarm predictor**; an alarm fired in >90 % of swarms and never on non-swarming hives | Ramsey et al. (2020), *Sci. Rep.* 10:9798 | Dedicated **8–30 Hz "swarm" band** + a rising-trend detector |
| The 20 Hz signal is **not captured by most microphones** (≈50 Hz floor) | Ramsey et al. (2020); review §4.5 | A vibration sensor, not a mic, is the right source for this band |
| Pre-swarm vibration **diverges 5–10 h to ~11 days before** the event | Bencsik et al. (2011) | Trend/baseline comparison over days, not an instantaneous threshold |
| Discrimination is **strongest at night (00:00–05:00)** | Ramsey et al. (2020); Woods (1959) | The detector compares **night-only** sub-series |

---

## What the `accel_*` fields mean

For each hive the paired BLE sensor reports the AC energy in three vibration
bands plus broadband stats, all in **milli-g (mg)** (a HiveInside node computes
these on-device from a high-rate capture; a HolyIot/RuuviTag beacon supplies
only a low-rate magnitude that feeds the low-rate detector, not the FFT bands):

| Field (`accel_{1,2}_…`) | Band | What it reflects |
|---|---|---|
| `band_swarm_mg` | **8 – 30 Hz** | Ramsey ~20 Hz **pre-swarm** vibration (headline) |
| `band_fanning_mg` | 30 – 100 Hz | Fanning / ventilation, low worker activity |
| `band_activity_mg` | 100 – 200 Hz | General in-comb worker activity / buzz fundamentals |
| `rms_mg` | broadband AC | Overall vibration level (gravity removed) |
| `peak_mg` | broadband AC | Largest single-sample deviation |

Diagnostics: `accel_{1,2}_ok` (sensor present & read), `sample_rate_hz`,
`sample_count`, `range_g`.

> The upper edge (200 Hz) is the Nyquist limit at a 400 Hz sampling rate. The
> queen-piping range (300–550 Hz) is deliberately **left to the microphone**'s
> `piping` band; the vibration bands concentrate on the low frequencies where
> they add new information.

> **Mounting matters.** For the substrate-borne signal to be usable, the sensor
> must be firmly coupled to the hive body or a brood frame — a sensor on flying
> leads mostly measures cable sway. Follow the literature: inner hive wall, or
> perpendicular to the comb in a brood frame.

---

## Server storage

The 18 `accel_*` fields (9 per hive) are accepted by `POST /api/v1/measurements`,
stored in dedicated columns on the `measurements` table, and returned by the
measurement read APIs. Fresh deployments get the columns from `init_db()`;
existing ones can apply
[`server/migrations/007_accelerometer_vibration.sql`](../server/migrations/007_accelerometer_vibration.sql)
(idempotent; also backfills from `raw_json`).

A missing/`!ok` sensor leaves the fields **null** rather than `0`, so the insight
detectors never mistake "no sensor" for "perfectly still hive".

---

## Auto-evaluation (insights)

The vibration data feeds two detectors in `server/insights.py` (see
[insights.md](insights.md) for the full catalogue):

1. **Pre-swarm vibration rising** (`detect_vibration_swarm_prediction`) — the
   headline. In the active season it compares the **recent night-time** mean of
   the 8–30 Hz band to a longer night-time baseline and fires a `swarm` /
   `watch` alert when it has risen ≥ `VIBRATION_SWARM_STANDALONE_MULT` (2.0×),
   with noise floors to avoid false positives on a near-still hive. Source:
   Ramsey et al. (2020); Bencsik et al. (2011).
2. **Vibration boost to the temperature pre-swarm watch**
   (`detect_pre_swarm_temp_instability`) — when the same night-time rise is
   present (≥ `VIBRATION_SWARM_RISE_MULT`, 1.6×) it raises the confidence of the
   existing temperature-based watch by up to +0.30 and notes the corroboration,
   exactly like the microphone "piping" boost.

Both degrade gracefully: with no vibration source (or off-season) they simply
don't contribute, and every other detector is unchanged. Behavioural tests live
in [`test-data/test_accel_rules.py`](../test-data/test_accel_rules.py).

Thresholds (`VIBRATION_*` in `insights.py`) are conservative starting points —
recalibrate them against your own baseline once you have a season of data.

---

## Sources

- Uthoff, C., Nabhan Homsi, M. & von Bergen, M. (2023). *Acoustic and vibration
  monitoring of honeybee colonies …* Computers and Electronics in Agriculture
  205:107589.
- Ramsey, M.-T. et al. (2020). *The prediction of swarming in honeybee colonies
  using vibrational spectra.* Scientific Reports 10:9798.
- Bencsik, M. et al. (2011). *Identification of the honey bee swarming process by
  analysing the time course of hive vibrations.* Computers and Electronics in
  Agriculture 76.

A broader TL;DR of the literature is in
[insights-sources-tldr.md](insights-sources-tldr.md).
