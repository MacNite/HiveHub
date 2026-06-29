# HiveHub Insights

**Preview - the skeleton for this exists, the final evaluation / testing / implementation is WIP**

The **Insights** panel in HivePal surfaces rule-based alerts derived from the
time-series your HiveHub device already records: weight (per channel),
internal hive temperature (per channel), and ambient temperature/humidity.

Insights are computed in [`server/insights.py`](../server/insights.py) and
exposed through `/api/v1/devices/{device_id}/insights`. The HivePal frontend
displays them in the **HiveScale → Insights** card.

This document is the authoritative reference for **what is detected**,
**how**, **when an alert is raised**, and **where the rule comes from**.

---

## Quick facts

| Property | Value |
|---|---|
| Computation source | `server/insights.py` (pure Python, no DB access) |
| Trigger | Every call to the insights endpoint; cached on the frontend for 5 minutes |
| Inputs | Weight, hive temperature, ambient temperature/humidity, FFT mic bands, BeeCounter entrance counts, and in-hive vibration (HiveInside FFT bands or a low-rate HolyIot/RuuviTag beacon magnitude) — all per channel, all optional except weight/temperature |
| Lookback | Up to 14 days, configurable via the `lookback_days` query parameter |
| Per-channel | Each detector runs independently for scale 1 and scale 2 |
| Output | A flat list of `Alert` objects, sorted by severity then time |


---

## Sources

You can find a quick tldr on the sources [here](insights-sources-tldr.md).


---

## Severities

Alerts are tagged with one of four severity levels. The frontend uses these
to colour-code the alert rows and per-hive badges on the latest-value panels.

| Severity | Meaning | Frontend colour |
|---|---|---|
| `info` | Informational classification, no action required | Blue |
| `watch` | Trend worth monitoring; inspect in the next routine visit | Yellow |
| `warning` | Something is happening now; investigate within hours | Amber |
| `critical` | Acute event; immediate attention recommended | Red |

A single detector can emit different severities depending on the inputs
(e.g. a swarm event during daylight hours is `critical`, at night it is
`warning`).

---

## Detector catalogue

Each section below describes one detector function in `insights.py`. The
heading is the alert title; the table is the rule it fires on.

### 1. Pre-swarm watch (Phase 1)

| | |
|---|---|
| Function | `detect_pre_swarm_temp_instability` |
| Category | `swarm` |
| Severity | `watch` |
| Inputs | Hive temperature, last 24 h and prior 7 days |
| Rule | 24 h std-dev of hive temperature ≥ **1.5×** the 7-day baseline std-dev |
| Confidence | 0.4 plus `(ratio − 1.5) × 0.5`, capped at 1.0 |
| Source | Project spec **Phase 1** (temperature half); MSPB arXiv 2311.10876 |

**Why it fires.** A healthy brood nest is held at 34–35 °C with very little
variation (std-dev typically < 0.5 °C). When the colony starts preparing to
swarm, thermoregulation becomes less precise hours-to-days before the actual
event, and the rolling 24 h variance widens.

**What it tells you.** Inspect for queen cells in the next 24–48 hours.

**Acoustic corroboration.** When FFT mic bands are present and the 300–550 Hz
`piping` band is active (≥ −45 dBFS), confidence is raised by up to +0.35 — a
queen piping/tooting signal is direct evidence of swarm preparation.

**Accelerometer corroboration.** When per-hive vibration is present and the
night-time **8–30 Hz swarm band** has risen ≥ **1.6×** vs its baseline,
confidence is raised by up to **+0.30**. This is the ~20 Hz pre-swarm signal of
Ramsey et al. (2020) that microphones cannot reach (see detector 11 and
[accelerometer.md](accelerometer.md)).

---

### 2. Imminent swarm (Phase 2)

| | |
|---|---|
| Function | `detect_imminent_swarm` |
| Category | `swarm` |
| Severity | `warning` |
| Inputs | Hive temperature, last 1 h vs. preceding 3 h |
| Rule | All three: <br/>• current temp − 4 h baseline ≥ **1.5 °C** <br/>• slope ≥ **0.5 °C/h** (still rising) <br/>• absolute temp > **36.5 °C** (above brood-nest tolerance) |
| Confidence | 0.5 plus `delta / 5`, capped at 1.0 |
| Source | Project spec **Phase 2**; Stalidzans & Berzonis 2013 |

**Why it fires.** In the final ~10–30 minutes before a swarm issues, the
cluster gathers near the entrance and the upper-hive temperature spikes
sharply above the normal brood-rearing band.

**What it tells you.** A swarm may leave within the next half hour. If you
are on site and want to catch the swarm, prepare a swarm box now.

---

### 3. Swarm event (Phase 3)

| | |
|---|---|
| Function | `detect_swarm_event` |
| Category | `swarm` |
| Severity | `critical` (daytime) / `warning` (otherwise) |
| Inputs | Weight, last 2 h |
| Rule | Weight drop ≥ **1.5 kg** within a window of ≤ **30 minutes** |
| Daytime window | 09:00–17:00 local |
| Confidence | 0.6 plus `(drop − 1.5) × 0.1`, capped at 1.0 |
| Source | Project spec **Phase 3** (weight half) |

**Why it fires.** When a swarm leaves, a substantial fraction of the colony
(typically 50–70 % of adult bees) departs in minutes. On a hive scale this
shows up as a near-step drop in weight.

**What it tells you.** A swarm has very likely just departed. Check the
hive, look for the cluster nearby if recovery is desired.

**Not implemented.** ~~With a bee counter the spec calls for AND-ing the
weight signature with massive asymmetric outflow.~~ **Now implemented** —
see below.

**Entrance-counter corroboration.** When BeeCounter data is present, the
weight drop is cross-checked against outbound traffic in the drop window.
If the peak outbound interval exceeds **3×** the recent baseline *and*
`out / (in + 1) > 5`, the swarm signature is confirmed: confidence is
raised (+0.25) and a night-time `warning` is promoted to `critical`
(the asymmetric outflow rules out a measurement artefact). Field:
`bee_counter_{ch}_interval_in/out`.

---

### 4. Queenlessness (rule-based fallback)

| | |
|---|---|
| Function | `detect_queenlessness` |
| Category | `queenless` |
| Severity | `warning` |
| Inputs | Hive temperature and weight, last 7 days; only fires during the active season (Mar–Sep, northern hemisphere) |
| Rule | Both: <br/>• 7-day hive-temp std-dev > **1.0 °C** <br/>• 7-day net weight change ≤ **0.2 kg** (stagnant) |
| Confidence | 0.55 (moderate without acoustic confirmation) |
| Source | Project spec **Queenlessness detection** (2-of-3 rule, no audio) |

**Why it fires.** Without a queen, brood rearing stops within days. The
nurse bees no longer thermoregulate a brood nest, so hive-temperature
variance widens. At the same time, foraging effort drops and the weight
curve stalls during what would otherwise be a productive season.

**What it tells you.** Inspect for eggs and a healthy brood pattern. If
both are missing, plan a queen introduction.

**Not implemented.** The gold-standard signal is the acoustic queenless
signature (broad-band, lower fundamental), which requires a microphone.

**Entrance-counter corroboration.** When BeeCounter data is present, a
sustained decline in outbound forager traffic of **≥ 5%/day** across the
7-day window raises confidence by +0.15 (capped at 0.90). This is
corroborative only — it never raises the alert on its own, because a
forager decline alone is also consistent with a spell of poor weather.

---

### 5. Robbing

| | |
|---|---|
| Function | `detect_robbing` |
| Category | `robbing` |
| Severity | `warning` (late afternoon) / `watch` (other times) |
| Inputs | Weight, last 2 h |
| Rule | Sustained weight loss ≥ **0.4 kg/h** over ≥ **30 min**, NOT matching the swarm-event signature |
| Late-afternoon window | 15:00–19:00 local (dearth-period robbing peak) |
| Confidence | 0.5 baseline, +0.2 if late afternoon |
| Source | Project spec **Robbing detection** (weight component) |

**Why it fires.** A weak hive being robbed loses honey rapidly — much faster
than normal foraging-day swings. The detector deliberately ignores cases
that look like a swarm departure (covered by detector 3 above) to avoid
double-firing.

**What it tells you.** Reduce entrance size, consider closing the hive
temporarily, or move the colony if robbing is sustained.

**Not implemented.** The agitated acoustic spectrum signal requires a
microphone.

**Entrance-counter corroboration.** When BeeCounter data is present, the
canonical robbing traffic signature — an incoming spike with low outgoing
— is checked over the last 2 h. With asymmetry defined as
`(in − out) / max(in + out, 1)`, an inbound rate **≥ 200 bees/h** *and*
asymmetry **≥ 0.4** raises confidence by +0.20 and upgrades severity
`watch → warning`.

---

### 6. Foraging intensity (informational)

| | |
|---|---|
| Function | `detect_foraging_intensity` |
| Category | `foraging` |
| Severity | `info` (strong/moderate flow) or `watch` (net loss) |
| Inputs | Weight, last 24 h |
| Rule | 24 h weight delta: <br/>• ≥ **+1.0 kg** → strong flow (`info`) <br/>• ≥ **+0.2 kg** → moderate flow (`info`) <br/>• ≤ **−0.2 kg** → negative (`watch`) <br/>• otherwise → no alert |
| Confidence | 0.8 |
| Source | Project spec **Foraging intensity**; Meikle et al. 2008 |

**Why it fires.** Day-to-day weight delta is the classical proxy for
nectar-flow intensity. A negative delta during the active season is worth
flagging because it indicates the colony is consuming more than it gathers.

**What it tells you.** Strong flow → consider adding a super. Negative
delta → check for dearth, disease, robbing, or queen problems depending on
context.

**Entrance-counter corroboration.** When BeeCounter data is present,
outbound traffic cross-checks the weight signal. Strong/moderate flow with
active outbound traffic (**≥ 100 bees/h**) raises confidence (+0.10); a
weight gain with little/no traffic *lowers* it (−0.30), because gain
without foragers leaving is suspect (calibration drift, rain on the lid,
or someone leaning on the hive). A net loss with low traffic reinforces the
negative signal (+0.10).

---

### 7. Brood cycle / colony state

| | |
|---|---|
| Function | `detect_brood_cycle_state` |
| Category | `brood` |
| Severity | `info` (active brood) or `watch` (broodless / weak) |
| Inputs | Hive temperature, last 24 h |
| Rule | 24 h std-dev: <br/>• < **0.5 °C** *and* mean within 34–36.5 °C → active brood rearing (`info`) <br/>• > **2.0 °C** → broodless / weak colony (`watch`) <br/>• otherwise → no alert (in transition) |
| Confidence | 0.7 |
| Source | Project spec **Brood cycle / colony state** |

**Why it fires.** Brood requires tight thermoregulation. A narrow std-dev
around the canonical 34–35 °C is a positive confirmation that brood
rearing is active; a wide std-dev usually means there is little or no
brood to thermoregulate.

**What it tells you.** Use it as a quick health check: an `info` here is
reassuring, a `watch` warrants an inspection — combined with detector 4
(queenlessness) it strongly suggests an inspection.

---

### 8. Absconding / collapse trend

| | |
|---|---|
| Function | `detect_absconding_trend` |
| Category | `decline` |
| Severity | `watch` |
| Inputs | Hive temperature and weight, last 14 days |
| Rule | Both: <br/>• Weight loss > **100 g/day** sustained over 14 days <br/>• Daily-std-dev regression slope **positive** (variance widening) |
| Confidence | 0.5 |
| Source | Project spec **Absconding / collapse early warning** (2-of-3 rule) |

**Why it fires.** A colony in slow decline — disease, queen problems,
chronic robbing, pre-absconding stress — typically shows both a sustained
weight bleed and a deteriorating thermoregulation pattern in the days to
weeks before the colony collapses or absconds.

**What it tells you.** Inspect within the next routine cycle. Look for
disease, queen status, and stressors.

**Not implemented.** ~~The third leg of the original rule is a declining
linear trend on the daily entrance traffic, which requires a counter.~~
**Now implemented.**

**Entrance-counter corroboration.** When BeeCounter data is present, a
third rule is evaluated: outbound forager traffic declining by **≥ 3%/day**
over the 14-day lookback. On a 3-of-3 match the alert auto-promotes from
`watch` to `warning` and confidence rises 0.5 → 0.75. The `source` field
reports `(3 of 3 rules)` vs `(2 of 3 rule)` so you can tell which path
fired.

---

### 9. Winter survival risk

| | |
|---|---|
| Function | `detect_winter_risk` |
| Category | `winter` |
| Severity | `warning` (both rules fire) / `watch` (one rule fires) |
| Inputs | Hive temperature, ambient temperature, weight; last 7 days; only fires Oct–Feb (northern hemisphere) |
| Rule | At least one of: <br/>• Cluster weak: min hive temp 7d < mean ambient 7d + **2.0 °C** <br/>• High consumption: weight loss > **300 g/week** sustained |
| Confidence | 0.6 |
| Source | Project spec **Winter survival risk** |

**Why it fires.** Over winter, a strong cluster keeps its core well above
ambient even on the coldest days, and consumes stored honey at a roughly
predictable rate. A cluster that fails to maintain a temperature gap, or
that consumes substantially more than expected, is at risk.

**What it tells you.** Verify food stores on the next mild day; consider
emergency feeding (fondant). A persistently cold cluster may have already
died and stopped generating heat.

**Not implemented.** ~~Cleansing-flight detection on warm winter days would
corroborate cluster health, but requires a counter.~~ **Now implemented.**

**Entrance-counter corroboration.** When BeeCounter data is present, a
cleansing flight — any interval in the last 7 days with outbound
**≥ 50 bees** — is positive evidence the cluster is alive and active. When
seen, it *lowers* confidence in the risk alert by 0.15 (floor 0.3) and is
noted in the description. Absence of flights is **not** treated as negative
evidence, since bees rightly stay clustered in the cold — so this rule can
only soften, never strengthen, the alert.

---

### 10. Harvest window

| | |
|---|---|
| Function | `detect_harvest_window` |
| Category | `harvest` |
| Severity | `info` |
| Inputs | Weight, last 11 days |
| Rule | 7-day weight delta transitions from > **2.0 kg/week** (earlier window) to < **0.3 kg/week** (current window), with the plateau lasting ≥ **4 days** |
| Confidence | 0.7 |
| Source | Project spec **Honey-ready / harvest timing** |

**Why it fires.** Honey flows have a typical shape: rapid gain during peak
bloom, then a plateau when the source stops producing. Harvesting at the
top of the plateau maximises yield and minimises stress on the colony.

**What it tells you.** The current flow appears to be finished; supers may
be ready to remove. Confirm by inspection (capped frames, water content).

---

### 11. Pre-swarm vibration rising (accelerometer)

| | |
|---|---|
| Function | `detect_vibration_swarm_prediction` |
| Category | `swarm` |
| Severity | `watch` |
| Inputs | Accelerometer 8–30 Hz swarm band (`accel_{ch}_band_swarm_mg`), night-time (00:00–05:00), active season only |
| Rule | Recent (last 2 days) night-time mean ≥ **2.0×** the night-time baseline (prior ~8 days), with both above the noise floors (baseline ≥ 0.4 mg, recent ≥ 0.8 mg) |
| Confidence | 0.45 plus `(ratio − 2.0) × 0.2`, capped at 0.9 |
| Source | Ramsey et al. (2020) *Sci. Rep.* 10:9798; Bencsik et al. (2011); Uthoff et al. (2023) |

**Why it fires.** A substrate-borne comb vibration at about **20 Hz** rises in
the days-to-weeks before a colony swarms, and is most distinct **at night**.
Ramsey et al. (2020) turned this into an alarm that fired for over 90 % of
swarms and never for hives that did not swarm. The band is below what hive
microphones capture (~50 Hz floor), so it is unique to the accelerometer — which
is exactly why the Uthoff et al. (2023) review recommends adding one.

**Why night-time and trend-based.** The signal discriminates best between
midnight and 05:00, and it is a slow build-up rather than a single threshold
crossing, so the detector compares a recent night-only mean to a longer
night-only baseline instead of looking at one reading.

**What it tells you.** The colony is likely preparing to swarm over the coming
days. Inspect for queen cells and plan swarm control. This detector also boosts
the temperature-based **Pre-swarm watch** (detector 1) when both agree.

**Degrades to nothing** when no accelerometer is fitted, outside the active
season, or when vibration levels are below the noise floor. See
[accelerometer.md](accelerometer.md) for the hardware and bands.

---

### 12. Pre-swarm movement rising (low-rate BLE accelerometer)

| | |
|---|---|
| Function | `detect_lowrate_accel_swarm` |
| Category | `swarm` |
| Severity | `watch` |
| Inputs | Per-cycle broadband magnitude `accel_{ch}_rms_mg` from a passive in-hive BLE beacon (HolyIot 25015 / RuuviTag), night-time (00:00–05:00), active season only |
| Rule | Recent night-time mean ≥ **1.8×** the baseline, with floors (baseline ≥ 2.0 mg, recent ≥ 4.0 mg). **Defers** to detector 11 when real 8–30 Hz FFT-band data exists for the hive, so the two never double-fire |
| Confidence | 0.35 plus `(ratio − 1.8) × 0.2`, capped at 0.7 |
| Source | Bencsik et al. (2011); Ramsey et al. (2020) — low-rate proxy |

**Why it fires.** A passive beacon emits only periodic single-shot acceleration
samples, so the 8–30 Hz FFT swarm band of detector 11 cannot be computed. Instead
this trends the night-time mean of the broadband AC magnitude: rising in-hive
movement over days is a coarse pre-swarm activity cue. It is deliberately
lower-confidence than the FFT-band detector. See
[holyiot-ble-sensor.md](holyiot-ble-sensor.md).

---

## Severity precedence

When multiple detectors fire for the same channel, all alerts are kept and
displayed. The per-channel pill in the latest-value panel shows the
**highest** active severity for that channel, with `critical > warning >
watch > info`. The full list is in the Insights card.

---

## Tuning thresholds

All thresholds are constants near the top of `server/insights.py`:

```python
SWARM_WEIGHT_DROP_KG          = 1.5
SWARM_WEIGHT_WINDOW_MIN       = 30
SWARM_DAYTIME_HOURS           = (9, 17)
PRE_SWARM_STD_MULTIPLIER      = 1.5
PRE_SWARM_BASELINE_DAYS       = 7
ROBBING_WEIGHT_LOSS_KG_PER_HOUR = 0.4
ROBBING_LATE_AFTERNOON_HOURS  = (15, 19)
ROBBING_MIN_DURATION_MIN      = 30
QUEENLESS_TEMP_STDDEV_C       = 1.0
QUEENLESS_DAYS_WINDOW         = 7
QUEENLESS_WEIGHT_STAGNANT_KG  = 0.2
FORAGING_STRONG_KG_PER_DAY    = 1.0
FORAGING_MODERATE_KG_PER_DAY  = 0.2
BROOD_ACTIVE_STDDEV_C         = 0.5
BROOD_BROODLESS_STDDEV_C      = 2.0
ABSCONDING_LOOKBACK_DAYS      = 14
ABSCONDING_WEIGHT_LOSS_G_PER_DAY = 100
WINTER_CLUSTER_DELTA_C        = 2.0
WINTER_WEIGHT_LOSS_G_PER_WEEK = 300
HARVEST_FLOW_KG_PER_WEEK      = 2.0
HARVEST_PLATEAU_KG_PER_WEEK   = 0.3
HARVEST_PLATEAU_DAYS          = 4

# ── Entrance-counter (BeeCounter) thresholds ──
SWARM_OUT_BASELINE_MULT                 = 3.0    # peak outbound vs baseline
SWARM_OUT_IN_RATIO                      = 5.0    # out / (in + 1) in drop window
SWARM_OUT_MIN_BASELINE                  = 1.0    # min baseline out/interval
QUEENLESS_FORAGER_DECLINE_FRAC_PER_DAY  = 0.05   # 5%/day outbound decline
QUEENLESS_FORAGER_MIN_DAILY_BASELINE    = 200.0  # min daily out to trust slope
ROBBING_IN_OUT_ASYMMETRY                = 0.4    # (in-out)/(in+out)
ROBBING_MIN_INBOUND_PER_HOUR            = 200.0
ABSCONDING_FORAGER_DECLINE_FRAC_PER_DAY = 0.03   # 3%/day outbound decline
WINTER_CLEANSING_FLIGHT_OUT             = 50.0   # bees out in one interval
FORAGING_ACTIVE_OUT_PER_HOUR            = 100.0  # "active foraging" traffic

# ── Accelerometer (per-hive vibration) thresholds ──
VIBRATION_NIGHT_HOURS           = (0, 5)   # night window (00:00–05:00)
VIBRATION_RECENT_DAYS           = 2        # "recent" night-time window
VIBRATION_BASELINE_DAYS         = 10       # total span; baseline = older nights
VIBRATION_SWARM_RISE_MULT       = 1.6      # rise that boosts the temp watch
VIBRATION_SWARM_STANDALONE_MULT = 2.0      # rise that fires a standalone watch
VIBRATION_MIN_BASELINE_MG       = 0.4      # noise floor for the baseline
VIBRATION_MIN_RECENT_MG         = 0.8      # noise floor for the recent level

# Low-rate BLE beacon proxy (HolyIot/RuuviTag; detector 12) — higher floors
LOWRATE_SWARM_RISE_MULT         = 1.8      # rise that fires the BLE-beacon watch
LOWRATE_MIN_BASELINE_MG         = 2.0      # noise floor for the baseline
LOWRATE_MIN_RECENT_MG           = 4.0      # noise floor for the recent level
```

These are starting values calibrated against the project spec and the
public literature listed below. They should be **re-tuned against your own
historical data**, especially:

- the swarm-event drop threshold (depends on hive box size and colony
  strength),
- the winter consumption rate (depends on climate and stores),
- the foraging delta thresholds (depends on regional flow strength).

Future work: expose these via `/api/v1/devices/{id}/config` so users can
tune them per device without redeploying the backend.

---

## Hardware roadmap

Several detectors in the spec call for sensors beyond the base
weight/temperature stack. Both the microphone (FFT bands) and the
entrance counter (BeeCounter) are now integrated; the table below records
which detectors each one feeds:

| Sensor | Detectors that would benefit | Status |
|---|---|---|
| Microphone | Pre-swarm (piping/tooting), queenlessness (acoustic signature), robbing (agitated spectrum) | **Integrated** (FFT bands) |
| Entrance counter (BeeCounter) | Swarm event (asymmetric outflow), robbing (incoming-spike pattern), queenlessness (forager decline), absconding (daily decline → 3-of-3), foraging (traffic cross-check), winter (cleansing flights) | **Integrated** |
| In-hive vibration (BLE: HiveInside FFT / HolyIot-RuuviTag low-rate; legacy wired LIS3DH) | Pre-swarm vibration rising (detectors 11 and 12) and the pre-swarm temperature watch boost | **Integrated** |

With the BeeCounter integrated, the swarm-event, robbing, queenlessness,
absconding, foraging and winter detectors all consume
`bee_counter_{ch}_interval_in` / `_interval_out` (gated by
`bee_counter_{ch}_ok`) when present, and fall back to their
weight/temperature/acoustic rules when the counter is absent or a hive has
no counter fitted. No detector *requires* the counter.

With in-hive vibration integrated, the pre-swarm detectors additionally consume
`accel_{ch}_band_swarm_mg` (FFT bands, detector 11) or fall back to
`accel_{ch}_rms_mg` (low-rate beacon magnitude, detector 12), gated by
`accel_{ch}_ok` — the low-frequency ~20 Hz swarm precursor the microphones cannot
record. It is optional too: no detector *requires* it. See
[accelerometer.md](accelerometer.md).

---

## Sources

- **Project spec** — local design document, reproduced in conversation history.
  Defines Phase 1/2/3 swarm warnings, queenlessness, robbing, foraging, brood
  cycle, absconding, winter survival, and harvest timing.
- **Seeley, T. D. (2010).** *Honeybee Democracy.* Princeton University Press.
  Swarm-preparation behaviour and timing.
- **Stalidzans, E. & Berzonis, A. (2013).** "Temperature changes above the
  upper hive entrance show signs of bee colony swarming preparation."
  *Agronomy Research,* 11(2). Brood-nest baseline temperatures and the
  pre-swarm rise.
- **Meikle, W. G. et al. (2008).** "Within-day variation in continuous hive
  weight data as a measure of honey bee colony activity." *Apidologie* 39(6).
  Day-night weight-delta foraging algorithm.
- **Kulkarni & Murphy** — time-series benchmark, weight + in-hive temp +
  entrance traffic. PMC 11479372 (Frontiers, open access). Recommended
  validation dataset because the sensor stack matches HiveHub most closely.
- **MSPB multi-modal dataset** — arXiv 2311.10876. Audio + temperature +
  humidity across 53 hives over 1 year; cited as validation for the
  temperature-based queenlessness fallback.
- **Ramsey, M.-T. et al. (2020).** "The prediction of swarming in honeybee
  colonies using vibrational spectra." *Scientific Reports* 10:9798. The ~20 Hz
  night-time comb vibration that predicts swarming days ahead — basis for the
  accelerometer swarm-prediction detector (11).
- **Bencsik, M. et al. (2011).** "Identification of the honey bee swarming
  process by analysing the time course of hive vibrations." *Computers and
  Electronics in Agriculture* 76. Pre-swarm vibration divergence days ahead.
- **Uthoff, C., Nabhan Homsi, M. & von Bergen, M. (2023).** "Acoustic and
  vibration monitoring of honeybee colonies …" *Computers and Electronics in
  Agriculture* 205:107589. Review recommending low-frequency accelerometers to
  capture the ~20 Hz swarm signal microphones miss.

Other public datasets useful for validation: BeeTogether, UrBAN, NU-Hive,
OSBH, BUZZ1–4.

---

## Frontend reference

The Insights card lives at:

```
apps/frontend/src/pages/hivescale/hivescale-insights-card.tsx
```

It renders the alert list, a severity summary, and an `(i)` tooltip
listing every detector with a short version of the rule and a link back to
this document. The per-hive severity pill (used in the latest-value panels)
is exported from the same file as `HiveScaleSeverityPill`.

The TanStack Query hook is `useHiveScaleInsights` in
`apps/frontend/src/api/hooks/useHiveScale.ts`. The corresponding TypeScript
types — `HiveScaleInsightSeverity`, `HiveScaleInsightCategory`,
`HiveScaleInsightAlert`, `HiveScaleInsightsResponse` — mirror the Pydantic
models in `server/insights.py`.