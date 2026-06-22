# Per-hive accelerometer (LIS3DH / LIS2DH12) — vibration monitoring

> **Superseded.** The wired LIS3DH/LIS2DH12 accelerometer described here is no
> longer wired into the firmware's measurement cycle. The register-level driver
> stays in the tree (`firmware/src/accel.cpp`, behind `ENABLE_LIS3DH_ACCEL`) for
> custom builds, but stock firmware now gets in-hive vibration from a paired
> **BLE sensor**: a **HiveInside ESP32-C6** supplies full FFT bands, while a
> passive **HolyIot 25015 / RuuviTag** beacon (which cannot produce FFT bands)
> feeds a separate **low-rate** pre-swarm detector. See
> [holyiot-ble-sensor.md](holyiot-ble-sensor.md). This document is kept for the
> vibration science and the FFT-band insight, which apply to any high-rate source.

HiveScale can carry **one MEMS accelerometer per hive** on the shared I2C bus to
measure low-frequency comb/wall vibration. The feature is optional and compiled
out unless `ENABLE_LIS3DH_ACCEL` is set in `secrets.h`.

- **Prototype part:** LIS3DH (the purple GY‑LIS3DH breakout).
- **Final BOM part:** LIS2DH12TR — ST's pin‑, register‑ and address‑compatible
  successor, so the same firmware drives it unchanged.

This document explains **why** the accelerometer was added, **what** it
measures, **how** it is wired and configured, and **how** the server evaluates
the data. For pin‑by‑pin wiring see [wiring.md](wiring.md); for the full insight
catalogue see [insights.md](insights.md).

---

## Why an accelerometer in addition to the microphones?

HiveScale already has two INMP441 microphones (50–3000 Hz FFT bands). The
accelerometer is not a duplicate — it covers the **sub‑audible band the
microphones miss**, which the literature identifies as the single most useful
swarm‑prediction signal.

The review *Uthoff, Nabhan Homsi & von Bergen (2023), "Acoustic and vibration
monitoring of honeybee colonies for beekeeping‑relevant aspects of presence of
queen bee and swarming", Computers and Electronics in Agriculture 205:107589*
surveys the field and concludes (emphasis added):

> "Additionally, [Ramsey et al. (2020)] found that one of the frequencies that
> is potentially important for predicting swarming is at about **20 Hz and
> cannot be recorded by most microphones**. Thus, **including low‑frequency
> accelerometers** into the setup may be a great way of **maximising the data
> quality**."

Key findings that motivate this module:

| Finding | Source (via the review) | Consequence for HiveScale |
|---|---|---|
| A ~**20 Hz** comb vibration is the strongest known **multi‑day swarm predictor**; an alarm fired in >90 % of swarms and never on non‑swarming hives | Ramsey et al. (2020), *Sci. Rep.* 10:9798 | Dedicated **8–30 Hz "swarm" band** + a rising‑trend detector |
| The 20 Hz signal is **not captured by most microphones** (≈50 Hz floor) | Ramsey et al. (2020); review §4.5 | Accelerometer, not a mic, is the right sensor for this band |
| Pre‑swarm vibration **diverges 5–10 h to ~11 days before** the event | Bencsik et al. (2011) | Trend/baseline comparison over days, not an instantaneous threshold |
| Discrimination is **strongest at night (00:00–05:00)** | Ramsey et al. (2020); Woods (1959) | The detector compares **night‑only** sub‑series |
| Accelerometers were mounted on the **inner hive wall** or **perpendicular to the comb** in a brood frame | Bencsik et al. (2011); Ramsey et al. (2020) | Mounting guidance below |

---

## What it measures

Each upload cycle, for each hive, the firmware configures the sensor, captures a
short burst of 3‑axis samples, computes the **vector magnitude**
`a = √(x²+y²+z²)`, removes the DC component (gravity + mounting bias) and runs an
on‑device FFT. It reports the AC energy in three bands plus broadband stats, all
in **milli‑g (mg)**:

| Field (`accel_{1,2}_…`) | Band | What it reflects |
|---|---|---|
| `band_swarm_mg` | **8 – 30 Hz** | Ramsey ~20 Hz **pre‑swarm** vibration (headline) |
| `band_fanning_mg` | 30 – 100 Hz | Fanning / ventilation, low worker activity |
| `band_activity_mg` | 100 – 200 Hz | General in‑comb worker activity / buzz fundamentals |
| `rms_mg` | broadband AC | Overall vibration level (gravity removed) |
| `peak_mg` | broadband AC | Largest single‑sample deviation |

Diagnostics: `accel_{1,2}_ok` (sensor present & read), `sample_rate_hz`,
`sample_count`, `range_g`.

> The upper edge (200 Hz) is the Nyquist limit at the default 400 Hz ODR. The
> queen‑piping range (300–550 Hz) is deliberately **left to the microphone**'s
> `piping` band; the accelerometer concentrates on the low frequencies where it
> adds new information.

`slot 1 → hive 1 → accel_1_*` (address `0x18`),
`slot 2 → hive 2 → accel_2_*` (address `0x19`) — mirroring the dual load cells,
stereo mics and two BeeCounters.

---

## Wiring (short version)

I2C, 3.3 V. **CS must be tied to 3.3 V** (CS low selects SPI); **SDO/SA0 sets the
address** (GND → `0x18`, 3.3 V → `0x19`). INT1/INT2/ADC pins are unused.

```text
Accelerometer 1 (hive 1): VCC->3.3V GND->GND SCL->GPIO22 SDA->GPIO21 CS->3.3V SDO->GND   (0x18)
Accelerometer 2 (hive 2): VCC->3.3V GND->GND SCL->GPIO22 SDA->GPIO21 CS->3.3V SDO->3.3V  (0x19)
```

Full pin table and the LIS2DH12 notes are in [wiring.md](wiring.md#lis3dh--lis2dh12-accelerometers-per-hive-vibration).

> **Mounting matters.** Couple the sensor firmly to the hive body or a brood
> frame so substrate‑borne vibration transfers into it — a board on flying leads
> mostly measures cable sway. Follow the literature: inner hive wall, or
> perpendicular to the comb in a brood frame.

---

## Configuration (`secrets.h`)

```cpp
#define ENABLE_LIS3DH_ACCEL 1
#define LIS3DH_ADDR_SLOT_1  0x18   // hive 1 (SDO/SA0 -> GND)
#define LIS3DH_ADDR_SLOT_2  0x19   // hive 2 (SDO/SA0 -> VCC)
#define LIS3DH_ODR_HZ       400    // 10/25/50/100/200/400 (others fall back to 400)
#define LIS3DH_SAMPLE_COUNT 256    // power of two; 256 @ 400 Hz ≈ 0.64 s, 1.56 Hz/bin
#define LIS3DH_RANGE_G      2      // 2/4/8/16 g; 2 g maximises sensitivity
```

Defaults live in `firmware/include/config.h`. The capture adds roughly
`LIS3DH_SAMPLE_COUNT / ODR` seconds per hive (~0.64 s each at the defaults) to
the wake time. If you only care about the swarm band you can drop the ODR to
100 Hz (Nyquist 50 Hz) to save power.

### How the firmware drives the chip

Register‑level over `Wire` (no external library), so it is identical for the
LIS3DH and LIS2DH12:

1. Read `WHO_AM_I` (expect `0x33`); a mismatch ⇒ `accel_N_ok=false`, fields null.
2. `CTRL_REG1` ← ODR + XYZ enabled (normal mode). `CTRL_REG4` ← BDU + full‑scale
   + high‑resolution (12‑bit).
3. Poll `STATUS.ZYXDA` and read `OUT_X..OUT_Z` for `LIS3DH_SAMPLE_COUNT` samples.
4. Vector magnitude → remove DC → RMS/peak → Hann‑windowed FFT → per‑band RMS.

Registers are re‑written every cycle, so it recovers cleanly after a deep‑sleep
power cut. Implementation: `firmware/src/accel.cpp`, `firmware/include/accel.h`.

---

## Server storage

The 18 `accel_*` fields (9 per hive) are accepted by `POST /api/v1/measurements`,
stored in dedicated columns on the `measurements` table, and returned by the
measurement read APIs. Fresh deployments get the columns from `init_db()`;
existing ones can apply
[`server/migrations/007_accelerometer_vibration.sql`](../server/migrations/007_accelerometer_vibration.sql)
(idempotent; also backfills from `raw_json`).

A missing/`!ok` accelerometer leaves the fields **null** rather than `0`, so the
insight detectors never mistake "no sensor" for "perfectly still hive".

---

## Auto‑evaluation (insights)

The vibration data feeds two detectors in `server/insights.py` (see
[insights.md](insights.md) for the full catalogue):

1. **Pre‑swarm vibration rising** (`detect_vibration_swarm_prediction`) — the
   headline. In the active season it compares the **recent night‑time** mean of
   the 8–30 Hz band to a longer night‑time baseline and fires a `swarm` /
   `watch` alert when it has risen ≥ `VIBRATION_SWARM_STANDALONE_MULT` (2.0×),
   with noise floors to avoid false positives on a near‑still hive. Source:
   Ramsey et al. (2020); Bencsik et al. (2011).
2. **Vibration boost to the temperature pre‑swarm watch**
   (`detect_pre_swarm_temp_instability`) — when the same night‑time rise is
   present (≥ `VIBRATION_SWARM_RISE_MULT`, 1.6×) it raises the confidence of the
   existing temperature‑based watch by up to +0.30 and notes the corroboration,
   exactly like the microphone "piping" boost.

Both degrade gracefully: with no accelerometer (or off‑season) they simply don't
contribute, and every other detector is unchanged. Behavioural tests live in
[`test-data/test_accel_rules.py`](../test-data/test_accel_rules.py).

Thresholds (`VIBRATION_*` in `insights.py`) are conservative starting points —
recalibrate them against your own baseline once you have a season of data.

---

## Sources

- Uthoff, C., Nabhan Homsi, M. & von Bergen, M. (2023). *Acoustic and vibration
  monitoring of honeybee colonies …* Computers and Electronics in Agriculture
  205:107589.
- Ramsey, M.‑T. et al. (2020). *The prediction of swarming in honeybee colonies
  using vibrational spectra.* Scientific Reports 10:9798.
- Bencsik, M. et al. (2011). *Identification of the honey bee swarming process by
  analysing the time course of hive vibrations.* Computers and Electronics in
  Agriculture 76.

A broader TL;DR of the literature is in
[insights-sources-tldr.md](insights-sources-tldr.md).
