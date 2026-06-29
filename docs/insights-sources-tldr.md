# TL;DR — Key Publications on Honey Bee Swarming, Hive Sensing, and Colony Monitoring

## 1. Seeley, T. D. (2010) — *Honeybee Democracy*
**Topic:** Swarm-preparation behaviour and collective decision-making in honey bee colonies.

### Core Contribution
Thomas Seeley explains how honey bee colonies prepare for swarming and how decentralized colony intelligence governs the process. The book is foundational for understanding *behavioral* indicators of swarm preparation.

### Key Findings
- Swarming is a **deliberate colony reproduction process**, not a sudden event.
- Colonies begin preparation **days to weeks before swarm departure**.
- Observable pre-swarm behaviours include:
  - Increased worker agitation/activity
  - Queen slimming through reduced feeding
  - Queen-cell construction
  - Changes in forager behaviour
  - Worker clustering near hive entrance
- Swarm departure timing is heavily influenced by:
  - Weather
  - Colony congestion
  - Brood density
  - Nectar flow

### Relevance for Hive Monitoring
This work provides the **biological basis** for swarm-prediction systems:
- Swarming creates measurable changes in:
  - hive temperature,
  - hive mass,
  - activity patterns,
  - and entrance traffic.
- Useful for defining *behavioral labels* for sensor-based prediction models.

### Main Takeaway
Swarming is preceded by a long preparation phase with detectable colony-wide behavioural and physiological changes.

---

# 2. Stalidzans & Berzonis (2013)
**Paper:** *Temperature changes above the upper hive entrance show signs of bee colony swarming preparation.*

### Core Contribution
Demonstrates that internal hive temperature patterns change before swarming, especially near the upper entrance area.

### Key Findings
- Colonies preparing to swarm show a **temperature increase above the brood nest** several days before swarm emergence.
- The rise is:
  - gradual,
  - persistent,
  - and distinguishable from normal daily fluctuations.
- Suggested mechanism:
  - Increased worker density/activity,
  - altered ventilation behaviour,
  - and brood thermoregulation changes.

### Sensor Insights
- Temperature sensors near the hive entrance can serve as:
  - early-warning swarm indicators,
  - low-cost predictive signals.
- Absolute temperature matters less than:
  - **trend deviations from baseline**.

### Relevance for HiveHub
Highly relevant because:
- Uses the same sensing modality (temperature).
- Supports anomaly-based swarm detection models.
- Suggests swarm preparation can be detected **multiple days in advance**.

### Main Takeaway
Persistent upward temperature deviations near hive entrances are a measurable precursor to swarming.

---

# 3. Meikle et al. (2008)
**Paper:** *Within-day variation in continuous hive weight data as a measure of honey bee colony activity.*

### Core Contribution
Introduces continuous hive-weight analysis as a proxy for colony activity and foraging intensity.

### Key Findings
- Hive weight fluctuates strongly during the day:
  - morning departures reduce weight,
  - evening returns increase weight.
- Day-night weight deltas correlate with:
  - foraging activity,
  - nectar intake,
  - colony strength.
- Continuous weight monitoring provides:
  - non-invasive colony assessment,
  - high temporal resolution.

### Important Algorithmic Concept
The paper establishes the basis for:
- **day/night differential analysis**
- slope-based weight interpretation
- activity signatures from load-cell data.

### Relevance for HiveHub
Extremely relevant for:
- swarm prediction,
- foraging estimation,
- anomaly detection.

Potential swarm indicators:
- reduced foraging consistency,
- abnormal morning departures,
- sudden mass drops.

### Main Takeaway
Continuous hive-weight patterns encode colony activity and can reveal behavioural state transitions.

---

# 4. Kulkarni & Murphy — Frontiers / PMC 11479372
**Topic:** Time-series benchmark dataset for colony-state analysis.

### Core Contribution
Provides a modern multi-sensor benchmark dataset combining:
- hive weight,
- in-hive temperature,
- entrance traffic.

The dataset is particularly valuable because the sensor stack closely matches commercial hive-monitoring systems.

### Key Findings
- Multi-modal sensing significantly improves colony-state classification.
- Combining signals outperforms single-sensor approaches.
- Time-series models can detect:
  - swarming,
  - queenlessness,
  - colony stress,
  - and environmental disruptions.

### Why It Matters
This is one of the strongest validation datasets for practical ML pipelines because:
- sensor modalities align with real deployments,
- data is longitudinal,
- synchronized measurements enable sensor fusion.

### Relevance for HiveHub
Likely the best available benchmark for:
- swarm prediction,
- sensor fusion validation,
- production-like testing,
- feature engineering.

### Main Takeaway
Multi-modal time-series sensing provides significantly stronger colony-state prediction than isolated measurements.

---

# 5. MSPB Multi-Modal Dataset — arXiv:2311.10876
**Topic:** Large-scale multi-modal bee-monitoring dataset.

### Core Contribution
Introduces a large dataset collected from:
- 53 hives,
- over 1 year,
- with synchronized:
  - audio,
  - temperature,
  - humidity measurements.

### Key Findings
- Colony health states can be inferred from combined environmental and acoustic patterns.
- Audio signatures contain strong information about:
  - queenlessness,
  - stress,
  - colony instability.
- Temperature and humidity trends remain useful fallback indicators when audio quality is poor.

### Important Insight
The paper supports the idea that:
- queenless colonies show altered thermoregulation behaviour,
- temperature variability increases when colony organization weakens.

### Relevance for HiveHub
Particularly useful for:
- fallback queenlessness detection,
- validating temperature-only inference systems,
- future audio integration.

### Main Takeaway
Multi-modal long-term sensing enables robust colony-state inference, with temperature remaining a valuable low-cost fallback signal.

---

# 6. Ramsey et al. (2020) — *Scientific Reports* 10:9798
**Paper:** *The prediction of swarming in honeybee colonies using vibrational spectra.*

### Core Contribution
The first study to **accurately predict swarming days-to-weeks in advance** from in-hive **vibration**, using accelerometers coupled to the comb.

### Key Findings
- A specific **~20 Hz vibrational signal** rises in the run-up to swarming.
- The signal is **most discriminative at night (00:00–05:00)**.
- A trained alarm fired in **>90 %** of swarming events and **never** for hives that did not swarm.
- Crucially, ~20 Hz is **below the usable range of most hive microphones** (~50 Hz floor), so it is only reliably captured with a **low-frequency accelerometer**.

### Relevance for HiveHub
Direct basis for the per-hive **LIS3DH / LIS2DH12 accelerometer** and the
`detect_vibration_swarm_prediction` insight: compare a recent **night-time** mean
of the 8–30 Hz band to a longer baseline and warn on a sustained rise.

### Main Takeaway
A rising night-time ~20 Hz comb vibration is the strongest known multi-day swarm predictor — and needs an accelerometer, not a microphone.

---

# 7. Bencsik et al. (2011) — *Computers and Electronics in Agriculture* 76
**Paper:** *Identification of the honey bee swarming process by analysing the time course of hive vibrations.*

### Key Findings
- Comb **vibration patterns diverge from baseline 5–10 hours to ~11 days before** swarming.
- Established that accelerometer time-series carry a **pre-swarm** signature, not just a swarm-moment one.

### Relevance for HiveHub
Justifies a **trend/baseline** comparison over days (rather than an instantaneous threshold) for the vibration swarm detector.

---

# 8. Uthoff, Nabhan Homsi & von Bergen (2023) — *Computers and Electronics in Agriculture* 205:107589
**Paper (review):** *Acoustic and vibration monitoring of honeybee colonies for beekeeping-relevant aspects of presence of queen bee and swarming.*

### Key Findings
- Surveys acoustic + vibration methods for **queen presence** and **swarming**.
- Highlights that the predictive **~20 Hz** band "**cannot be recorded by most microphones**" and explicitly recommends **adding low-frequency accelerometers** to "maximise the data quality".
- Notes the strongest signals (queen warble, swarm vibration) appear **at night**.

### Relevance for HiveHub
The review that motivated adding the accelerometer alongside the existing
microphones, and the source for the night-time analysis window.

### Main Takeaway
To capture the best swarm-prediction signal, combine microphones with a **low-frequency accelerometer** — which HiveHub now does.

---

# Cross-Paper Synthesis

## Emerging Consensus Across the Literature

### 1. Swarming is Predictable
Swarming is not random; colonies exhibit measurable precursor signals days in advance.

### 2. Temperature is a Strong Early Indicator
Brood-area and entrance temperatures consistently change during:
- swarm preparation,
- queenlessness,
- colony stress.

### 3. Weight Data Encodes Colony Behaviour
Continuous weight monitoring captures:
- foraging intensity,
- nectar flow,
- swarm departures,
- population changes.

### 4. Multi-Modal Sensing Performs Best
Combining:
- temperature,
- weight,
- entrance traffic,
- humidity,
- audio

produces significantly more reliable colony-state detection.

### 5. Trend Analysis Beats Absolute Thresholds
Most papers emphasize:
- deviations from colony baseline,
- temporal dynamics,
- anomaly detection,
rather than static thresholds.

---

# Practical Implications for HiveHub

## Most Defensible Sensor Stack
Based on the literature:
1. Hive weight
2. Internal brood temperature
3. Entrance traffic
4. Optional humidity
5. Optional audio
6. Optional low-frequency vibration (accelerometer) — uniquely captures the ~20 Hz pre-swarm signal microphones miss

## Most Promising ML Features
- Rolling temperature deviation
- Day/night weight slope
- Forager traffic rhythm
- Cross-sensor anomalies
- Temperature variance
- Circadian pattern disruption

## Strongest Validation Sources
- Kulkarni & Murphy → best benchmark alignment
- MSPB dataset → strongest long-term multi-modal validation
- Stalidzans → strongest temperature-based swarm evidence
- Meikle → foundational weight-analysis methodology
- Seeley → behavioural/theoretical grounding