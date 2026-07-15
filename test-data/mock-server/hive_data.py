"""
Realistic dummy data generator for a single (dual-channel) HiveHub device.

This module is intentionally dependency-free (standard library only) so it can
be imported both by the lightweight mock API (``app.py``) and by the seeding
script (``seed.py``) that pushes the same data into a real HiveHub backend.

----------------------------------------------------------------------------
What it models
----------------------------------------------------------------------------
One physical HiveHub unit ("dual beehive scale") with two load cells, so a
single measurement row carries both ``scale_1_*`` (Hive A) and ``scale_2_*``
(Hive B). The data spans 2025-01-01 .. 2026-05-31 and reproduces the behaviour
documented for real "scale hives":

* Seasonal weight curve - slow winter decline as stores are consumed, the
  spring nectar flow build-up, beekeeper honey harvests (sudden step drops),
  autumn sugar-syrup feeding (step ups), then the next winter/spring cycle.
* Diurnal weight swing - the classic daily saw-tooth: the colony is heaviest
  around dawn, drops to a minimum in the early afternoon as foragers are out,
  then recovers in the evening as bees return with nectar (Meikle et al. 2008,
  "Within-day variation in continuous hive weight data").
* Brood-nest thermoregulation - internal hive temperature held near ~35 C
  while brood is being reared, drifting toward ambient + cluster warmth in the
  broodless winter months.
* Ambient temperature/humidity - a temperate-climate annual sinusoid (modelled
  on a Central-European / Berlin-like site, matching the repo's default
  ``TZ=Europe/Berlin``) with a diurnal cycle and weather noise.
* Off-grid telemetry - solar harvest that tracks daylight/weather and a LiPo
  state-of-charge that charges by day and discharges overnight, plus cellular
  (SIM7080G) link quality.
* Acoustic telemetry - INMP441 broadband RMS and the 5 FFT bands used by
  ``insights.py`` (hum / piping / stress / ...), louder during active foraging.
* BeeCounter (HiveTraffic, BLE/GATT) entrance traffic - in/out counts with morning departures
  and afternoon arrivals.

A few "events" are scripted so the insights endpoints return something
interesting: a spring swarm preparation + swarm departure on Hive A (May 2025),
a robbing episode on Hive B (Aug 2025), and an ongoing swarm-watch build-up on
Hive A in late May 2026 (inside the default insights look-back window).

All randomness is seeded, so the dataset is fully reproducible.
"""

from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Device identity / metadata (shared by the mock API and the seeder)
# ---------------------------------------------------------------------------

DEVICE_ID = "hive_scale_dual_01"
CLAIM_CODE = "ABCD-1234"
DISPLAY_NAME = "Demo Apiary - Garden Stand"
FIRMWARE_VERSION = "0.6.2"
SCALE_1_DISPLAY_NAME = "Hive A (Carnica)"
SCALE_2_DISPLAY_NAME = "Hive B (Buckfast)"

# Calendar span requested for the demo: all of 2025 plus 2026 up to end of May.
SPAN_START = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
SPAN_END_EXCLUSIVE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)

CLAIMED_AT = datetime(2025, 1, 2, 9, 15, 0, tzinfo=timezone.utc)
CREATED_AT = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# Calibration factors / config (mirrors server defaults in docs/api.md).
SEND_INTERVAL_DEFAULT_SECONDS = 1800
CONFIG_VERSION = 3

_RNG_SEED = 20260601


# ---------------------------------------------------------------------------
# The exact set of keys returned by ``measurement_row_to_dict`` in
# ``server/main.py``. Building every record from this template guarantees the
# mock responses are byte-for-byte shape-compatible with the real backend.
# ---------------------------------------------------------------------------

MEASUREMENT_KEYS: tuple[str, ...] = (
    "id", "device_id", "measured_at", "received_at",
    "scale_1_weight_kg", "scale_2_weight_kg",
    "hive_1_temp_c", "hive_2_temp_c",
    "ambient_temp_c", "ambient_humidity_percent",
    "battery_voltage", "battery_voltage_v",
    "rssi_dbm", "firmware_version", "config_version",
    "sd_ok", "rtc_ok", "sht_ok",
    "scale_1_raw", "scale_2_raw",
    "battery_soc_percent", "battery_alert", "battery_monitor_ok",
    "solar_monitor_ok", "solar_bus_voltage_v", "solar_shunt_voltage_mv",
    "solar_load_voltage_v", "solar_current_ma", "solar_power_mw",
    "network_transport", "cellular_ok", "cellular_csq",
    "calibration_mode", "boot_count", "time_source",
    # INMP441 stereo microphone telemetry
    "mic_ok", "mic_sample_rate_hz", "mic_sample_frames",
    "mic_left_ok", "mic_left_rms_dbfs", "mic_left_peak_dbfs", "mic_left_rms_normalized",
    "mic_right_ok", "mic_right_rms_dbfs", "mic_right_peak_dbfs", "mic_right_rms_normalized",
    # FFT frequency band energy (dBFS)
    "mic_left_band_sub_bass_dbfs", "mic_left_band_hum_dbfs", "mic_left_band_piping_dbfs",
    "mic_left_band_stress_dbfs", "mic_left_band_high_dbfs",
    "mic_right_band_sub_bass_dbfs", "mic_right_band_hum_dbfs", "mic_right_band_piping_dbfs",
    "mic_right_band_stress_dbfs", "mic_right_band_high_dbfs",
    # BeeCounter (per hive)
    "bee_counter_1_ok", "bee_counter_1_protocol_version", "bee_counter_1_status_flags",
    "bee_counter_1_uptime_s", "bee_counter_1_num_gates", "bee_counter_1_gates_healthy",
    "bee_counter_1_total_in", "bee_counter_1_total_out",
    "bee_counter_1_interval_in", "bee_counter_1_interval_out",
    "bee_counter_1_glitch_count", "bee_counter_1_busy_retries",
    "bee_counter_1_read_attempts", "bee_counter_1_latch_succeeded",
    "bee_counter_2_ok", "bee_counter_2_protocol_version", "bee_counter_2_status_flags",
    "bee_counter_2_uptime_s", "bee_counter_2_num_gates", "bee_counter_2_gates_healthy",
    "bee_counter_2_total_in", "bee_counter_2_total_out",
    "bee_counter_2_interval_in", "bee_counter_2_interval_out",
    "bee_counter_2_glitch_count", "bee_counter_2_busy_retries",
    "bee_counter_2_read_attempts", "bee_counter_2_latch_succeeded",
)


def _blank_measurement() -> dict:
    """A measurement dict with every documented key present and null."""
    return {k: None for k in MEASUREMENT_KEYS}


# ---------------------------------------------------------------------------
# Seasonal / diurnal shape helpers
# ---------------------------------------------------------------------------

def _doy(d: date) -> int:
    return d.timetuple().tm_yday


def _season_phase(d: date) -> float:
    """0..1 phase of the year (0 ~ 1 Jan), used for sinusoidal seasonality."""
    return (_doy(d) - 1) / 365.0


def _daylight_hours(d: date) -> float:
    """Approximate daylight length for a ~52 N (Berlin-like) site."""
    return 12.2 + 4.4 * math.sin(2 * math.pi * (_doy(d) - 81) / 365.25)


def _ambient_mean_c(d: date) -> float:
    """Annual ambient temperature mean: ~0 C late Jan, ~20 C late Jul."""
    return 10.0 - 10.0 * math.cos(2 * math.pi * (_doy(d) - 20) / 365.0)


def _brood_factor(d: date) -> float:
    """0..1 brood-rearing intensity.

    A spring-to-autumn plateau (not a summer-only bell): brood rearing ramps up
    in April, holds near maximum May-September while the colony tightly
    regulates the brood nest at ~35 C, then winds down through October. ~0 in
    the broodless winter cluster.
    """
    doy = _doy(d)
    ramp_up = 1.0 / (1.0 + math.exp(-(doy - 95) / 12.0))    # ~early April
    ramp_down = 1.0 / (1.0 + math.exp((doy - 285) / 12.0))  # ~mid October
    return max(0.0, min(1.0, ramp_up * ramp_down))


def _nectar_potential(d: date) -> float:
    """Environmental nectar availability, kg/day on a perfect foraging day.

    Strong spring flow (dandelion / fruit / rape) peaking late May, a summer
    dearth, then a modest late-summer flow. Roughly zero in winter. Tuned so a
    strong colony banks ~25-30 kg of harvestable surplus across a season.
    """
    doy = _doy(d)
    spring = 1.35 * math.exp(-((doy - 140) ** 2) / (2 * 28.0 ** 2))   # ~late May peak
    summer = 0.50 * math.exp(-((doy - 205) ** 2) / (2 * 22.0 ** 2))   # late-summer flow
    return spring + summer


def _consumption(d: date) -> float:
    """Daily store consumption (kg/day): low winter cluster, higher in brood season."""
    brood = _brood_factor(d)
    winter = 0.05
    return winter + 0.24 * brood


def _bell(x: float, center: float, width: float) -> float:
    return math.exp(-((x - center) ** 2) / (2 * width ** 2))


# ---------------------------------------------------------------------------
# Per-day simulation state
# ---------------------------------------------------------------------------

class _DayState:
    __slots__ = (
        "d", "weather", "daylen", "sunrise", "sunset",
        "foraging", "base_a_start", "base_a_end", "base_b_start", "base_b_end",
        "trips_a", "trips_b",
    )


def _simulate_days(rng: random.Random) -> list[_DayState]:
    """Integrate the slow seasonal weight trend day-by-day for both hives and
    pre-compute the per-day environmental state."""
    days: list[_DayState] = []

    d = SPAN_START.date()
    last = (SPAN_END_EXCLUSIVE - timedelta(days=1)).date()

    # Weather as an auto-correlated random walk in [0,1] (1 = sunny/warm).
    weather = 0.6
    # Net stores weight (kg) carried by each colony, incl. bees.
    base_a = 20.5
    base_b = 18.5

    while d <= last:
        # --- weather (multi-day spells) -----------------------------------
        weather += rng.uniform(-0.28, 0.28)
        # Seasonal pull: sunnier in summer, duller in winter.
        weather += 0.05 * (math.sin(2 * math.pi * (_doy(d) - 110) / 365.0) - (weather - 0.5))
        weather = max(0.05, min(1.0, weather))

        daylen = _daylight_hours(d)
        sunrise = 13.0 - daylen / 2.0
        sunset = 13.0 + daylen / 2.0
        foraging = max(0.0, min(1.0, _brood_factor(d) ** 0.5 * (0.35 + 0.65 * weather)))

        st = _DayState()
        st.d = d
        st.weather = weather
        st.daylen = daylen
        st.sunrise = sunrise
        st.sunset = sunset
        st.foraging = foraging

        # --- net daily weight change --------------------------------------
        nectar = _nectar_potential(d) * foraging
        net = nectar - _consumption(d)

        st.base_a_start = base_a
        st.base_b_start = base_b

        delta_a = net + rng.uniform(-0.05, 0.05)
        # Hive B forages a touch less and stores a little less.
        delta_b = net * 0.88 + rng.uniform(-0.05, 0.05)

        # Autumn feeding (sugar syrup), several events.
        if d in (date(2025, 9, 6), date(2025, 9, 13), date(2025, 9, 20)):
            delta_a += 3.0
            delta_b += 2.8

        # --- scripted swarm departure (Hive A, late May 2025) -------------
        # The slow trend takes the step here; the intraday spike + bee-counter
        # burst are added in the per-interval loop.
        if d == date(2025, 5, 24):
            delta_a -= 1.6

        base_a = max(12.0, base_a + delta_a)
        base_b = max(11.0, base_b + delta_b)

        # --- honey harvests: supers pulled -> weight drops back to a base --
        # Expressed as a target weight so the curve always returns to a
        # realistic post-harvest level instead of drifting upward over years.
        if d == date(2025, 7, 5):           # main summer harvest
            base_a = 27.5 + rng.uniform(-0.4, 0.4)
            base_b = 24.5 + rng.uniform(-0.4, 0.4)
        if d == date(2025, 8, 2):           # smaller late harvest
            base_a = min(base_a, 26.0) + rng.uniform(-0.4, 0.4)
            base_b = min(base_b, 23.0) + rng.uniform(-0.4, 0.4)
        st.base_a_end = base_a
        st.base_b_end = base_b

        # --- daily entrance traffic (round trips) -------------------------
        st.trips_a = 8200.0 * foraging
        st.trips_b = 7200.0 * foraging

        days.append(st)
        d += timedelta(days=1)

    return days


# ---------------------------------------------------------------------------
# Scripted acoustic / behavioural events (date-window based)
# ---------------------------------------------------------------------------

def _swarm_prep_2025(d: date) -> float:
    """0..1 intensity of Hive A pre-swarm period, mid->late May 2025."""
    if date(2025, 5, 14) <= d <= date(2025, 5, 24):
        return _bell((d - date(2025, 5, 14)).days, 8.0, 4.0)
    return 0.0


def _swarm_watch_piping_2026(d: date) -> float:
    """0..1 build-up of Hive A queen-piping acoustic energy over the last week
    of the dataset (26-31 May 2026)."""
    if date(2026, 5, 26) <= d <= date(2026, 5, 31):
        return min(1.0, (d - date(2026, 5, 26)).days / 5.0)
    return 0.0


def _swarm_watch_temp_instability_2026(d: date) -> float:
    """0..1 brood-nest temperature instability for the Hive A pre-swarm watch.

    Deliberately concentrated in the final ~24-36h so the detector's
    "last-24h std-dev vs prior-7-day baseline" ratio clears its 1.5x threshold
    and the live insights endpoint surfaces a pre-swarm watch on Hive A.
    """
    if d == date(2026, 5, 31):
        return 1.0
    if d == date(2026, 5, 30):
        return 0.35
    return 0.0


def _robbing_2025(d: date) -> float:
    """0..1 intensity of a Hive B robbing episode in the August dearth."""
    if date(2025, 8, 10) <= d <= date(2025, 8, 13):
        return _bell((d - date(2025, 8, 10)).days, 1.5, 1.0)
    return 0.0


# ---------------------------------------------------------------------------
# Main generator
# ---------------------------------------------------------------------------

def generate_measurements(interval_minutes: int = 30) -> list[dict]:
    """Generate the full measurement time-series (ascending by ``measured_at``)."""
    rng = random.Random(_RNG_SEED)
    days = _simulate_days(rng)

    step = timedelta(minutes=interval_minutes)
    records: list[dict] = []
    next_id = 1

    # Monotonic cumulative counters / device housekeeping.
    total_in_a = 41200
    total_out_a = 41050
    total_in_b = 38800
    total_out_b = 38760
    read_attempts_a = 90000
    read_attempts_b = 90000
    glitch_a = 31
    glitch_b = 27
    boot_count = 142

    for st in days:
        d = st.d
        # Per-day normalisation of the entrance-traffic shape.
        n_intervals = int(round(24 * 60 / interval_minutes))
        out_w_a, in_w_a, out_w_b, in_w_b = [], [], [], []
        hours = [(i * interval_minutes) / 60.0 for i in range(n_intervals)]
        for h in hours:
            if st.sunrise <= h <= st.sunset:
                out_w_a.append(_bell(h, st.sunrise + 0.30 * st.daylen, st.daylen * 0.22))
                in_w_a.append(_bell(h, st.sunrise + 0.72 * st.daylen, st.daylen * 0.22))
                out_w_b.append(_bell(h, st.sunrise + 0.32 * st.daylen, st.daylen * 0.22))
                in_w_b.append(_bell(h, st.sunrise + 0.74 * st.daylen, st.daylen * 0.22))
            else:
                out_w_a.append(0.0); in_w_a.append(0.0)
                out_w_b.append(0.0); in_w_b.append(0.0)
        sum_out_a = sum(out_w_a) or 1.0
        sum_in_a = sum(in_w_a) or 1.0
        sum_out_b = sum(out_w_b) or 1.0
        sum_in_b = sum(in_w_b) or 1.0

        amb_mean = _ambient_mean_c(d)
        amb_amp = 3.0 + 4.0 * st.foraging          # bigger diurnal swing in summer
        amb_amp *= (0.55 + 0.45 * st.weather)      # damped on dull/rainy days
        diurnal_amp_a = (0.25 + 2.0 * st.foraging) * (0.45 + 0.55 * st.weather)
        diurnal_amp_b = diurnal_amp_a * 0.9

        prep25 = _swarm_prep_2025(d)
        piping26 = _swarm_watch_piping_2026(d)
        temp_instab26 = _swarm_watch_temp_instability_2026(d)
        rob25 = _robbing_2025(d)

        for i in range(n_intervals):
            h = hours[i]
            frac_day = (i * interval_minutes) / (24 * 60)
            ts = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(minutes=i * interval_minutes)

            # --- slow trend, linearly interpolated across the day ----------
            base_a = st.base_a_start + (st.base_a_end - st.base_a_start) * frac_day
            base_b = st.base_b_start + (st.base_b_end - st.base_b_start) * frac_day

            # --- diurnal forager-out dip (min near solar noon) -------------
            if st.sunrise <= h <= st.sunset:
                ff = math.sin(math.pi * (h - st.sunrise) / max(0.1, (st.sunset - st.sunrise))) ** 1.3
            else:
                ff = 0.0
            w_a = base_a - diurnal_amp_a * ff + rng.gauss(0, 0.015)
            w_b = base_b - diurnal_amp_b * ff + rng.gauss(0, 0.015)

            # Swarm departure spike: a sharp loss around midday on 2025-05-24.
            if d == date(2025, 5, 24) and 11.5 <= h < 12.5:
                w_a -= 1.4

            # --- ambient + brood-nest temperature --------------------------
            ambient = amb_mean + amb_amp * math.cos(2 * math.pi * (h - 15) / 24.0) + rng.gauss(0, 0.4)
            brood = _brood_factor(d)
            hive1 = brood * 35.1 + (1 - brood) * (ambient + 12.0)
            hive2 = brood * 34.7 + (1 - brood) * (ambient + 11.0)
            hive1 += rng.gauss(0, 0.25 + 1.2 * (1 - brood))
            hive2 += rng.gauss(0, 0.25 + 1.2 * (1 - brood))
            # Pre-swarm broodnest temperature instability (Stalidzans 2013).
            if prep25 > 0:
                hive1 += rng.gauss(0, 1.6 * prep25)
            if temp_instab26 > 0:
                hive1 += rng.gauss(0, 1.0 * temp_instab26)

            # --- ambient humidity (higher at night / in wet weather) -------
            humidity = 62 + 18 * math.cos(2 * math.pi * (h - 4) / 24.0)
            humidity += 22 * (1 - st.weather) - 8 * st.foraging + rng.gauss(0, 3)
            humidity = max(28.0, min(98.0, humidity))

            # --- solar harvest (tracks daylight & weather) -----------------
            if st.sunrise <= h <= st.sunset:
                solar_shape = math.sin(math.pi * (h - st.sunrise) / max(0.1, (st.sunset - st.sunrise)))
            else:
                solar_shape = 0.0
            solar_current = max(0.0, 360.0 * solar_shape * (0.35 + 0.65 * st.weather) + rng.gauss(0, 6))
            solar_bus_v = 0.0 if solar_current < 5 else 5.9 + rng.gauss(0, 0.05)
            solar_power = solar_bus_v * solar_current  # mW (V * mA)
            solar_shunt_mv = solar_current * 0.1       # 0.1 ohm shunt

            # --- battery state of charge (charge by day, drain by night) ---
            # A smooth daily cycle plus a seasonal floor (lower in dark winter).
            soc_cycle = 18 * math.sin(2 * math.pi * (h - 9) / 24.0)
            soc_seasonal = 70 + 18 * math.sin(2 * math.pi * (_doy(d) - 120) / 365.0)
            soc = max(8.0, min(100.0, soc_seasonal + soc_cycle + rng.gauss(0, 1.2)))
            batt_v = 3.45 + 0.0085 * soc + rng.gauss(0, 0.004)   # ~3.5 V empty .. ~4.3 V full
            batt_alert = soc < 15.0

            # --- cellular link (SIM7080G) ----------------------------------
            csq = int(max(5, min(28, 18 + 4 * math.sin(2 * math.pi * frac_day) + rng.gauss(0, 2))))
            rssi = -113 + 2 * csq   # standard CSQ -> dBm approximation

            # --- acoustic telemetry ----------------------------------------
            # Broadband RMS rises with daytime activity.
            activity = ff if (st.sunrise <= h <= st.sunset) else 0.0
            base_rms_l = -53 + 15 * (0.2 + 0.8 * activity) * (0.4 + 0.6 * st.foraging)
            base_rms_r = base_rms_l + rng.gauss(0, 0.6)
            mic_l_rms = base_rms_l + rng.gauss(0, 0.8)
            mic_r_rms = base_rms_r + rng.gauss(0, 0.8)
            # Hive A (left) FFT bands.
            band_l = {
                "sub_bass": mic_l_rms - 6 + rng.gauss(0, 0.6),
                "hum": mic_l_rms + 2 + rng.gauss(0, 0.6),
                "piping": mic_l_rms - 14 + rng.gauss(0, 0.6),
                "stress": mic_l_rms - 7 + rng.gauss(0, 0.6),
                "high": mic_l_rms - 12 + rng.gauss(0, 0.6),
            }
            # Queen piping rises during pre-swarm windows (Ramsey et al. 2020).
            if prep25 > 0:
                band_l["piping"] = max(band_l["piping"], -47 + 6 * prep25 + rng.gauss(0, 0.5))
            if piping26 > 0:
                band_l["piping"] = max(band_l["piping"], -46 + 6 * piping26 + rng.gauss(0, 0.4))
            # Hive B (right) FFT bands.
            band_r = {
                "sub_bass": mic_r_rms - 6 + rng.gauss(0, 0.6),
                "hum": mic_r_rms + 2 + rng.gauss(0, 0.6),
                "piping": mic_r_rms - 15 + rng.gauss(0, 0.6),
                "stress": mic_r_rms - 7 + rng.gauss(0, 0.6),
                "high": mic_r_rms - 12 + rng.gauss(0, 0.6),
            }
            # Robbing raises the 550-1500 Hz stress band on Hive B (BUZZ dataset).
            if rob25 > 0:
                band_r["stress"] = max(band_r["stress"], -40 + 6 * rob25 + rng.gauss(0, 0.5))
                w_b -= 0.9 * rob25 * ff  # robbed colony bleeds weight during the day

            # --- bee-counter entrance traffic ------------------------------
            interval_out_a = st.trips_a * (out_w_a[i] / sum_out_a)
            interval_in_a = st.trips_a * (in_w_a[i] / sum_in_a)
            interval_out_b = st.trips_b * (out_w_b[i] / sum_out_b)
            interval_in_b = st.trips_b * (in_w_b[i] / sum_in_b)
            # Swarm departure: a large one-way outflow from Hive A.
            if d == date(2025, 5, 24) and 11.5 <= h < 12.5:
                interval_out_a += 4200
            # Robbing: heavy two-way traffic at Hive B's entrance.
            if rob25 > 0:
                interval_out_b += 1500 * rob25 * activity
                interval_in_b += 1700 * rob25 * activity

            interval_out_a = int(max(0, round(interval_out_a + rng.gauss(0, 4))))
            interval_in_a = int(max(0, round(interval_in_a + rng.gauss(0, 4))))
            interval_out_b = int(max(0, round(interval_out_b + rng.gauss(0, 4))))
            interval_in_b = int(max(0, round(interval_in_b + rng.gauss(0, 4))))
            total_out_a += interval_out_a
            total_in_a += interval_in_a
            total_out_b += interval_out_b
            total_in_b += interval_in_b
            read_attempts_a += 1
            read_attempts_b += 1
            if rng.random() < 0.02:
                glitch_a += 1
            if rng.random() < 0.02:
                glitch_b += 1

            # --- assemble record (every documented key present) ------------
            m = _blank_measurement()
            m.update({
                "id": next_id,
                "device_id": DEVICE_ID,
                "measured_at": ts,
                "received_at": ts,
                "scale_1_weight_kg": round(w_a, 3),
                "scale_2_weight_kg": round(w_b, 3),
                "hive_1_temp_c": round(hive1, 2),
                "hive_2_temp_c": round(hive2, 2),
                "ambient_temp_c": round(ambient, 2),
                "ambient_humidity_percent": round(humidity, 1),
                "battery_voltage": round(batt_v, 3),
                "battery_voltage_v": round(batt_v, 3),
                "rssi_dbm": int(rssi),
                "firmware_version": FIRMWARE_VERSION,
                "config_version": CONFIG_VERSION,
                "sd_ok": True,
                "rtc_ok": True,
                "sht_ok": True,
                "scale_1_raw": int(-7050 * w_a),
                "scale_2_raw": int(-7050 * w_b),
                "battery_soc_percent": round(soc, 1),
                "battery_alert": batt_alert,
                "battery_monitor_ok": True,
                "solar_monitor_ok": True,
                "solar_bus_voltage_v": round(solar_bus_v, 3),
                "solar_shunt_voltage_mv": round(solar_shunt_mv, 2),
                "solar_load_voltage_v": round(solar_bus_v + 0.01, 3) if solar_bus_v else 0.0,
                "solar_current_ma": round(solar_current, 1),
                "solar_power_mw": round(solar_power, 1),
                "network_transport": "sim7080g",
                "cellular_ok": True,
                "cellular_csq": csq,
                "calibration_mode": False,
                "boot_count": boot_count,
                "time_source": "cellular",
                "mic_ok": True,
                "mic_sample_rate_hz": 16000,
                "mic_sample_frames": 8000,
                "mic_left_ok": True,
                "mic_left_rms_dbfs": round(mic_l_rms, 1),
                "mic_left_peak_dbfs": round(mic_l_rms + 6 + rng.uniform(0, 2), 1),
                "mic_left_rms_normalized": round(max(0.0, min(1.0, (mic_l_rms + 60) / 60.0)), 3),
                "mic_right_ok": True,
                "mic_right_rms_dbfs": round(mic_r_rms, 1),
                "mic_right_peak_dbfs": round(mic_r_rms + 6 + rng.uniform(0, 2), 1),
                "mic_right_rms_normalized": round(max(0.0, min(1.0, (mic_r_rms + 60) / 60.0)), 3),
                "mic_left_band_sub_bass_dbfs": round(band_l["sub_bass"], 1),
                "mic_left_band_hum_dbfs": round(band_l["hum"], 1),
                "mic_left_band_piping_dbfs": round(band_l["piping"], 1),
                "mic_left_band_stress_dbfs": round(band_l["stress"], 1),
                "mic_left_band_high_dbfs": round(band_l["high"], 1),
                "mic_right_band_sub_bass_dbfs": round(band_r["sub_bass"], 1),
                "mic_right_band_hum_dbfs": round(band_r["hum"], 1),
                "mic_right_band_piping_dbfs": round(band_r["piping"], 1),
                "mic_right_band_stress_dbfs": round(band_r["stress"], 1),
                "mic_right_band_high_dbfs": round(band_r["high"], 1),
                "bee_counter_1_ok": True,
                "bee_counter_1_protocol_version": 1,
                "bee_counter_1_status_flags": 0,
                "bee_counter_1_uptime_s": int((ts - CLAIMED_AT).total_seconds()),
                "bee_counter_1_num_gates": 24,
                "bee_counter_1_gates_healthy": 24,
                "bee_counter_1_total_in": total_in_a,
                "bee_counter_1_total_out": total_out_a,
                "bee_counter_1_interval_in": interval_in_a,
                "bee_counter_1_interval_out": interval_out_a,
                "bee_counter_1_glitch_count": glitch_a,
                "bee_counter_1_busy_retries": 0,
                "bee_counter_1_read_attempts": read_attempts_a,
                "bee_counter_1_latch_succeeded": True,
                "bee_counter_2_ok": True,
                "bee_counter_2_protocol_version": 1,
                "bee_counter_2_status_flags": 0,
                "bee_counter_2_uptime_s": int((ts - CLAIMED_AT).total_seconds()),
                "bee_counter_2_num_gates": 24,
                "bee_counter_2_gates_healthy": 24,
                "bee_counter_2_total_in": total_in_b,
                "bee_counter_2_total_out": total_out_b,
                "bee_counter_2_interval_in": interval_in_b,
                "bee_counter_2_interval_out": interval_out_b,
                "bee_counter_2_glitch_count": glitch_b,
                "bee_counter_2_busy_retries": 0,
                "bee_counter_2_read_attempts": read_attempts_b,
                "bee_counter_2_latch_succeeded": True,
            })
            records.append(m)
            next_id += 1

    return records


# ---------------------------------------------------------------------------
# Metadata builders (device / config / channels / members)
# ---------------------------------------------------------------------------

def device_config(send_interval_seconds: int = SEND_INTERVAL_DEFAULT_SECONDS) -> dict:
    return {
        "device_id": DEVICE_ID,
        "send_interval_seconds": send_interval_seconds,
        "scale1_offset": -124800,
        "scale1_factor": -7050.0,
        "scale2_offset": -118650,
        "scale2_factor": -7050.0,
        "config_version": CONFIG_VERSION,
    }


def device_channels() -> dict:
    return {
        "scale_1_display_name": SCALE_1_DISPLAY_NAME,
        "scale_2_display_name": SCALE_2_DISPLAY_NAME,
    }


def device_members(owner_user_id: str) -> list[dict]:
    return [
        {"user_id": owner_user_id, "role": "owner", "joined_at": CLAIMED_AT},
        {"user_id": "beekeeper-friend", "role": "viewer", "joined_at": CLAIMED_AT + timedelta(days=30)},
    ]
