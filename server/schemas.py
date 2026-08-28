"""Pydantic request/response models shared across the API routers."""

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hiveheart_fft import FFT_RAW_LEN
from tempcomp import DEFAULT_REF_TEMP_C, DEFAULT_TEMP_SOURCE


def validate_fft_raw(v):
    """Validate a HiveHeart raw FFT array: exactly 8 integers, each 0–255.

    Shared by the nested ``HiveHeartIn.fft`` and the flat ``hiveheart_N_fft``
    fields so invalid FFT data is rejected with a useful error during normal
    ingest. ``None`` (field absent) is allowed. Returns the value unchanged.
    """
    if v is None:
        return v
    if not isinstance(v, (list, tuple)) or len(v) != FFT_RAW_LEN:
        raise ValueError(f"fft must contain exactly {FFT_RAW_LEN} integers")
    for b in v:
        if isinstance(b, bool) or not isinstance(b, int) or not (0 <= b <= 255):
            raise ValueError("fft values must be integers between 0 and 255")
    return list(v)


# Maximum hives a single device reports (mirrors MAX_HIVES in the firmware's
# config.h). Used to bound the per-hive index and the hives[] array so a payload
# cannot claim an out-of-range hive; keep in sync with the firmware if it changes.
MAX_HIVES = 18

# ── Per-hive nested models (firmware v0.20.0 "hives[]" array) ────────────────
# A single ESP32 now reports up to 18 hives, each with its own scale(s) and
# in-hive sensors, as a nested object in the "hives" array. The server fans these
# out into the hive_readings table (and mirrors hives 1–2 onto the legacy
# measurements columns). Sub-objects use extra="allow" so forensic extras (raw
# axes, firmware version, per-gate arrays) survive into hive_readings.raw_json.
class HiveAccelIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    sample_rate_hz: Optional[int] = None
    sample_count: Optional[int] = None
    range_g: Optional[int] = None
    rms_mg: Optional[float] = None
    peak_mg: Optional[float] = None
    band_swarm_mg: Optional[float] = None
    band_fanning_mg: Optional[float] = None
    band_activity_mg: Optional[float] = None


class HiveBleIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    present: Optional[bool] = None
    sensor_type: Optional[str] = None
    humidity_percent: Optional[float] = None
    pressure_hpa: Optional[float] = None
    battery_percent: Optional[int] = None
    battery_mv: Optional[int] = None
    rssi_dbm: Optional[int] = None
    firmware_version: Optional[str] = None
    # Board/architecture a HiveInside node advertises in its beacon identity
    # record (currently only "nrf54lm20a"), forwarded by the HiveHub.
    board: Optional[str] = None
    # Which physical node this is. device_name is the BLE local name the node
    # advertises — "HiveInside-8A3F" as of HiveInside 0.5.0, where the four hex
    # digits are the last two bytes of its address; mac is the paired address
    # the HiveHub scanned for, which is known even for a node that reported no
    # name (older firmware, or a HolyIot/Ruuvi beacon).
    #
    # Both ride in hive_readings.raw_json rather than a column, like the
    # HiveTraffic image version: they change only when a node is re-paired and
    # nothing charts or alerts on them. Declared here rather than left to
    # extra="allow" so the field is documented and typed at the boundary.
    device_name: Optional[str] = None
    mac: Optional[str] = None


class HiveMicIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    rms_dbfs: Optional[float] = None


class HiveBeeCounterIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    # Image version the HiveTraffic counter reports ("ver" in its measurement
    # JSON), e.g. "0.1.0". Distinct from protocol_version, which versions the
    # wire format and does not move when the firmware does. Absent from counters
    # running firmware older than the field. It rides in raw_json rather than a
    # column — reported_beecounter_version reads it from there — because it
    # changes only across an OTA and nothing charts it.
    version: Optional[str] = None
    # Which physical counter this is, on the same terms as HiveBleIn above:
    # device_name is the local name the counter reports ("name" in its
    # measurement JSON, absent on every firmware that does not send one yet),
    # mac is the paired address HiveHub dialled. The HiveHub emits both before
    # it knows whether the read succeeded, so a counter that never answered is
    # still identifiable — which is exactly when a dashboard listing two failed
    # relays needs to say which counter is which.
    device_name: Optional[str] = None
    mac: Optional[str] = None
    total_in: Optional[int] = None
    total_out: Optional[int] = None
    interval_in: Optional[int] = None
    interval_out: Optional[int] = None
    # The diagnostic fields the counter also reports — protocol_version,
    # status_flags, uptime_s, num_gates, mcps_healthy, glitch_count, idle_s,
    # banks — are deliberately NOT declared. extra="allow" carries them through
    # into hive_readings.raw_json, which is where they belong: nothing charts or
    # alerts on them, so a column each would be dead weight.
    #
    # mcps_healthy counts MCP23017 port expanders (0..3), NOT gates — each
    # healthy expander covers eight of the counter's 24. HiveHub normalizes it
    # to this key from either wire revision; rows stored before HiveTraffic wire
    # revision 3 carry it as `gates_healthy`, with the same meaning under a name
    # that invited exactly the wrong reading.
    #
    # idle_s (wire revision 4) is the night-mode countdown: non-zero means the
    # flat interval on this row is deliberate — HiveHub asked the counter to
    # stop sensing because honey bees do not fly at night — rather than a failed
    # emitter bank. Nothing charts it either, but it is the only thing that
    # distinguishes the two, so it must reach raw_json intact.
    #
    # banks (wire revision 5) is the same argument at a third of the counter:
    # a bitmask of the three emitter MOSFETs that are enabled (bit 0 = gates
    # 00..07, and so on; 7 = all three). A cleared bit means those eight gates
    # are deliberately dark, so their share of the totals is permanently flat —
    # character for character what a dead FET produces. It is emitted on every
    # reading, including the 7 of a counter nobody has narrowed, because a field
    # that appeared only when interesting would make "all banks on" and "counter
    # too old to say" the same absence.


class HiveHeartIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    present: Optional[bool] = None
    temp_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    frequency_hz: Optional[float] = None
    energy: Optional[int] = None
    peak: Optional[int] = None
    battery_v: Optional[float] = None
    rssi_dbm: Optional[int] = None
    # Raw 8-byte packed-nibble FFT (see server/hiveheart_fft.py). Kept as the
    # canonical representation; decoded to 16 relative levels (0–15) on read.
    fft: Optional[list[int]] = Field(default=None)

    @field_validator("fft")
    @classmethod
    def _check_fft(cls, v):
        return validate_fft_raw(v)


class HiveScaleIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    present: Optional[bool] = None
    weight_kg: Optional[float] = None
    raw_weight: Optional[int] = None
    temp_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    pressure_hpa: Optional[float] = None
    battery_v: Optional[float] = None
    rssi_dbm: Optional[int] = None


class HiveReadingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int = Field(..., ge=1, le=MAX_HIVES)
    name: Optional[str] = None
    weight_kg: Optional[float] = None
    raw_weight: Optional[int] = None
    scale_source: Optional[str] = None
    # False when a wired scale is configured for this hive but produced no usable
    # reading (open load-cell input rails the 24-bit ADC to full scale; a missing
    # chip reads 0). weight_kg is null in that case. None when the hive has no
    # wired scale (e.g. a BLE-only hive or a hivescale_gatt source).
    scale_ok: Optional[bool] = None
    temp_c: Optional[float] = None
    temp_source: Optional[str] = None
    humidity_percent: Optional[float] = None
    accel: Optional[HiveAccelIn] = None
    ble: Optional[HiveBleIn] = None
    mic: Optional[HiveMicIn] = None
    bee_counter: Optional[HiveBeeCounterIn] = None
    hiveheart: Optional[HiveHeartIn] = None
    hivescale: Optional[HiveScaleIn] = None

    @field_validator("temp_c")
    @classmethod
    def _drop_disconnected_hive_probe(cls, v: Optional[float]) -> Optional[float]:
        # Mirror the measurements-level guard: an enabled-but-unwired DS18B20
        # reports ~-127 C; treat any sub-range value as "no reading".
        if v is not None and v <= -40.0:
            return None
        return v


class MeasurementIn(BaseModel):
    # extra="ignore": unknown/garbage fields are dropped rather than persisted
    # into raw_json. Every telemetry field this project uses is declared below
    # (including the per-gate forensic arrays), so nothing real is lost — but a
    # client with the API key can no longer pad rows with arbitrary keys.
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(..., examples=["hive_scale_dual_01"])
    # Immutable client-generated delivery key. A retry of the same cached JSON
    # must retain it so POST /measurements can be safely idempotent.
    measurement_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    claim_code: Optional[str] = Field(default=None, min_length=4, max_length=128)
    timestamp: Optional[datetime] = None
    scale_1_weight_kg: Optional[float] = None
    scale_2_weight_kg: Optional[float] = None
    hive_1_temp_c: Optional[float] = None
    hive_2_temp_c: Optional[float] = None
    # In-hive relative humidity (currently sourced from a paired in-hive BLE
    # sensor; mirrors hive_N_temp_c). Distinct from ambient_humidity_percent,
    # which is the outside-hive SHT40 reading.
    hive_1_humidity_percent: Optional[float] = None
    hive_2_humidity_percent: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    ambient_humidity_percent: Optional[float] = None
    battery_voltage: Optional[float] = None
    battery_voltage_v: Optional[float] = None
    battery_soc_percent: Optional[float] = None
    battery_alert: Optional[bool] = None
    battery_monitor_ok: Optional[bool] = None
    solar_monitor_ok: Optional[bool] = None
    solar_bus_voltage_v: Optional[float] = None
    solar_shunt_voltage_mv: Optional[float] = None
    solar_load_voltage_v: Optional[float] = None
    solar_current_ma: Optional[float] = None
    solar_power_mw: Optional[float] = None
    network_transport: Optional[str] = None
    calibration_mode: Optional[bool] = None
    # Inspection mode: true while the hub believes a beekeeper has the hive
    # open. The hive readings in this payload are still real and still stored —
    # the flag is what lets the backend open/close a device_inspections window,
    # which is in turn what keeps the readings out of charts, insights and
    # alerts. Never persisted as a per-row column: the interval is the record.
    inspection: Optional[bool] = None
    # Unix seconds at which the hub started the inspection, when it had a
    # trustworthy clock then. Used to back-date the window's start to the press
    # rather than to the first upload that reported it, which on a 10-minute
    # send interval is up to ten minutes of un-shaded spike.
    inspection_started_at: Optional[int] = None
    boot_count: Optional[int] = None
    time_source: Optional[str] = None
    rssi_dbm: Optional[int] = None
    firmware_version: Optional[str] = None
    config_version: Optional[int] = None
    sd_ok: Optional[bool] = None
    rtc_ok: Optional[bool] = None
    sht_ok: Optional[bool] = None
    scale_1_raw: Optional[int] = None
    scale_2_raw: Optional[int] = None
    # ── INMP441 stereo microphone telemetry ──────────────────────────────────
    mic_ok: Optional[bool] = None
    mic_sample_rate_hz: Optional[int] = None
    mic_sample_frames: Optional[int] = None
    mic_left_ok: Optional[bool] = None
    mic_left_rms_dbfs: Optional[float] = None
    mic_left_peak_dbfs: Optional[float] = None
    mic_left_rms_normalized: Optional[float] = None
    mic_right_ok: Optional[bool] = None
    mic_right_rms_dbfs: Optional[float] = None
    mic_right_peak_dbfs: Optional[float] = None
    mic_right_rms_normalized: Optional[float] = None
    # ── INMP441 FFT frequency band energy (dBFS) ─────────────────────────────
    # 5 bands × 2 channels = 10 fields.  Null when firmware has no FFT support.
    mic_left_band_sub_bass_dbfs:  Optional[float] = None  #   50–150 Hz
    mic_left_band_hum_dbfs:       Optional[float] = None  #  150–300 Hz colony hum
    mic_left_band_piping_dbfs:    Optional[float] = None  #  300–550 Hz piping/tooting
    mic_left_band_stress_dbfs:    Optional[float] = None  #  550–1500 Hz agitation
    mic_left_band_high_dbfs:      Optional[float] = None  # 1500–3000 Hz
    mic_right_band_sub_bass_dbfs: Optional[float] = None
    mic_right_band_hum_dbfs:      Optional[float] = None
    mic_right_band_piping_dbfs:   Optional[float] = None
    mic_right_band_stress_dbfs:   Optional[float] = None
    mic_right_band_high_dbfs:     Optional[float] = None

    # ── BeeCounter (per-hive entrance gate counts) ───────────────────────────
    # One HiveTraffic BeeCounter per hive, read over BLE/GATT (the only
    # supported transport — the wired I2C path was removed). Current firmware
    # reports LIFETIME totals only; the interval columns are backfilled
    # server-side by differencing consecutive totals (see
    # difference_bee_counter_intervals in measurements.py). Each block is
    # independent — a paired-but-unreachable unit reports
    # bee_counter_N_ok=False and the rest of its fields null.
    #
    # interval_* stay fully live: they are what display clients chart, and the
    # BLE path backfills them by differencing consecutive totals.
    #
    # The wired-only telemetry (busy_retries, read_attempts, latch_succeeded and
    # the per-gate arrays) is NO LONGER ACCEPTED. It only ever described the I2C
    # transport — bus retries, and whether CMD_LATCH landed — and that transport
    # is gone from every firmware. A device sending them now has them ignored
    # rather than stored. The measurements columns are deliberately left in
    # place: they hold real readings from the wired era, and the read path still
    # returns them, so no history is lost.
    #
    # NOTE on `_gates_healthy` below: these are the WIRED era's columns and hold
    # real readings taken under that name. Despite it, the field always counted
    # MCP23017 port expanders (0..3), never gates. BLE counters report the same
    # value as hives[].bee_counter.mcps_healthy and never populate these, so the
    # columns are frozen rather than renamed — repurposing them would make old
    # and new rows silently incomparable.
    bee_counter_1_ok:                Optional[bool] = None
    bee_counter_1_protocol_version:  Optional[int]  = None
    bee_counter_1_status_flags:      Optional[int]  = None
    bee_counter_1_uptime_s:          Optional[int]  = None
    bee_counter_1_num_gates:         Optional[int]  = None
    bee_counter_1_gates_healthy:     Optional[int]  = None
    bee_counter_1_total_in:          Optional[int]  = None
    bee_counter_1_total_out:         Optional[int]  = None
    bee_counter_1_interval_in:       Optional[int]  = None
    bee_counter_1_interval_out:      Optional[int]  = None
    bee_counter_1_glitch_count:      Optional[int]  = None

    bee_counter_2_ok:                Optional[bool] = None
    bee_counter_2_protocol_version:  Optional[int]  = None
    bee_counter_2_status_flags:      Optional[int]  = None
    bee_counter_2_uptime_s:          Optional[int]  = None
    bee_counter_2_num_gates:         Optional[int]  = None
    bee_counter_2_gates_healthy:     Optional[int]  = None
    bee_counter_2_total_in:          Optional[int]  = None
    bee_counter_2_total_out:         Optional[int]  = None
    bee_counter_2_interval_in:       Optional[int]  = None
    bee_counter_2_interval_out:      Optional[int]  = None
    bee_counter_2_glitch_count:      Optional[int]  = None

    # ── LIS3DH / LIS2DH12 per-hive vibration (accelerometer) ─────────────────
    # One accelerometer per hive on the shared I2C bus (0x18 / 0x19). Each block
    # is independent — a missing sensor reports accel_N_ok=False and the rest of
    # its fields are null. All band/RMS values are AC (gravity removed), in mg.
    # The swarm band (8–30 Hz) carries the ~20 Hz pre-swarm vibration the mics
    # cannot reach (Ramsey et al. 2020; Uthoff et al. 2023). See accel.h.
    accel_1_ok:                Optional[bool]  = None
    accel_1_sample_rate_hz:    Optional[int]   = None
    accel_1_sample_count:      Optional[int]   = None
    accel_1_range_g:           Optional[int]   = None
    accel_1_rms_mg:            Optional[float] = None
    accel_1_peak_mg:           Optional[float] = None
    accel_1_band_swarm_mg:     Optional[float] = None  #   8–30 Hz pre-swarm
    accel_1_band_fanning_mg:   Optional[float] = None  #  30–100 Hz fanning
    accel_1_band_activity_mg:  Optional[float] = None  # 100–200 Hz activity

    accel_2_ok:                Optional[bool]  = None
    accel_2_sample_rate_hz:    Optional[int]   = None
    accel_2_sample_count:      Optional[int]   = None
    accel_2_range_g:           Optional[int]   = None
    accel_2_rms_mg:            Optional[float] = None
    accel_2_peak_mg:           Optional[float] = None
    accel_2_band_swarm_mg:     Optional[float] = None
    accel_2_band_fanning_mg:   Optional[float] = None
    accel_2_band_activity_mg:  Optional[float] = None

    # ── In-hive BLE sensor (per hive) ────────────────────────────────────────
    # A passive BLE beacon bridged by the ESP32 — a HolyIot 25015 (SHT40 +
    # LPS22HB + LIS2DH12), a RuuviTag, or a HiveInside node. Which one it is
    # rides in ble_N_sensor_type; the nested hives[] form carries the same value
    # as ble.sensor_type. Its acceleration is reported through the accel_N_*
    # fields above (no FFT bands — a beacon only emits periodic single-shot
    # samples). Humidity and pressure are promoted to columns; the raw per-axis
    # acceleration, battery and link RSSI are kept in raw_json (declared so
    # extra="ignore" does not drop them). Its temperature is delivered via
    # hive_N_temp_c.
    ble_1_sensor_type:      Optional[str]   = None
    ble_1_humidity_percent: Optional[float] = None
    ble_1_pressure_hpa:     Optional[float] = None
    ble_1_accel_x_mg:       Optional[float] = None
    ble_1_accel_y_mg:       Optional[float] = None
    ble_1_accel_z_mg:       Optional[float] = None
    ble_1_battery_percent:  Optional[int]   = None
    ble_1_battery_mv:       Optional[int]   = None
    ble_1_rssi_dbm:         Optional[int]   = None
    # HiveInside advertises its running firmware version and board in its beacon
    # identity record ("fw"/"board"); kept in raw_json (declared so extra="ignore"
    # does not drop them). The board is now always "nrf54lm20a".
    ble_1_firmware_version: Optional[str]   = None
    ble_1_board:            Optional[str]   = None

    ble_2_sensor_type:      Optional[str]   = None
    ble_2_humidity_percent: Optional[float] = None
    ble_2_pressure_hpa:     Optional[float] = None
    ble_2_accel_x_mg:       Optional[float] = None
    ble_2_accel_y_mg:       Optional[float] = None
    ble_2_accel_z_mg:       Optional[float] = None
    ble_2_battery_percent:  Optional[int]   = None
    ble_2_battery_mv:       Optional[int]   = None
    ble_2_rssi_dbm:         Optional[int]   = None
    ble_2_firmware_version: Optional[str]   = None
    ble_2_board:            Optional[str]   = None

    # ── beehivemonitoring.com GATT sensors (HiveHeart / HiveScale) ───────────
    # HiveHeart is an in-hive sensor read over GATT: its temperature/humidity feed
    # hive_N_temp_c / hive_N_humidity_percent (above) only when no higher-priority
    # wired/HolyIot source filled those, so the firmware ALSO reports the raw
    # HiveHeart readings on hiveheart_N_temp_c / hiveheart_N_humidity_percent to
    # keep them independently visible. The acoustic frequency/energy/peak and
    # battery voltage plus those temp/humidity values stay in raw_json; the raw
    # FFT bins do too. HiveScale is a wireless weight scale with its own
    # weight/raw-weight plus on-board temp/humidity/pressure/battery.
    hiveheart_1_frequency_hz:     Optional[float] = None
    hiveheart_1_energy:           Optional[int]   = None
    hiveheart_1_peak:             Optional[int]   = None
    hiveheart_1_battery_v:        Optional[float] = None
    hiveheart_1_rssi_dbm:         Optional[int]   = None
    hiveheart_1_temp_c:           Optional[float] = None
    hiveheart_1_humidity_percent: Optional[float] = None
    hiveheart_1_fft:              Optional[list[int]] = Field(default=None)
    hiveheart_2_frequency_hz:     Optional[float] = None
    hiveheart_2_energy:           Optional[int]   = None
    hiveheart_2_peak:             Optional[int]   = None
    hiveheart_2_battery_v:        Optional[float] = None
    hiveheart_2_rssi_dbm:         Optional[int]   = None
    hiveheart_2_temp_c:           Optional[float] = None
    hiveheart_2_humidity_percent: Optional[float] = None
    hiveheart_2_fft:              Optional[list[int]] = Field(default=None)

    @field_validator("hiveheart_1_fft", "hiveheart_2_fft")
    @classmethod
    def _check_hiveheart_fft(cls, v):
        return validate_fft_raw(v)

    hivescale_1_weight_kg:        Optional[float] = None
    hivescale_1_raw_weight:       Optional[int]   = None
    hivescale_1_temp_c:           Optional[float] = None
    hivescale_1_humidity_percent: Optional[float] = None
    hivescale_1_pressure_hpa:     Optional[float] = None
    hivescale_1_battery_v:        Optional[float] = None
    hivescale_1_rssi_dbm:         Optional[int]   = None
    hivescale_2_weight_kg:        Optional[float] = None
    hivescale_2_raw_weight:       Optional[int]   = None
    hivescale_2_temp_c:           Optional[float] = None
    hivescale_2_humidity_percent: Optional[float] = None
    hivescale_2_pressure_hpa:     Optional[float] = None
    hivescale_2_battery_v:        Optional[float] = None
    hivescale_2_rssi_dbm:         Optional[int]   = None

    # ── Per-gate forensic arrays (one value per entrance gate) ───────────────
    # Sent only inside the measurement body and kept in raw_json (never promoted
    # to columns). Declared explicitly so extra="ignore" does not drop them, and
    # length-capped so they cannot be abused for storage amplification.
    bee_counter_1_per_gate_in:  Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_1_per_gate_out: Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_2_per_gate_in:  Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_2_per_gate_out: Optional[list[int]] = Field(default=None, max_length=64)

    # ── Multi-hive payload (firmware v0.20.0+) ───────────────────────────────
    # New firmware sends every hive (up to 18) here instead of the fixed
    # scale_1/2_* / hive_1/2_* fields above. The server fans these into the
    # hive_readings table and mirrors hives 1–2 onto the legacy columns so the
    # existing column-based read / insights / temp-comp keep working unchanged.
    # Old firmware keeps sending the flat fields and omits this.
    hive_count: Optional[int] = None
    hives: Optional[list[HiveReadingIn]] = Field(default=None, max_length=MAX_HIVES)

    # Belt-and-suspenders for field devices running firmware that predates the
    # DS18B20 disconnect-sentinel fix: an enabled-but-unwired probe reports
    # ~-127 C (DEVICE_DISCONNECTED_C), which is far below any plausible in-hive
    # temperature. Treat sub-range values as "no reading" (None) so they are not
    # persisted and rendered as bogus -127 C in HivePal. -40 C is comfortably
    # below any real reading while still catching every Dallas error code.
    @field_validator("hive_1_temp_c", "hive_2_temp_c")
    @classmethod
    def _drop_disconnected_probe(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= -40.0:
            return None
        return v


# Max measurements accepted in a single bulk-import request. The HivePal backend
# chunks a large SD download into batches no larger than this before forwarding.
MEASUREMENT_IMPORT_MAX = 20000


class MeasurementImportIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    measurements: list[MeasurementIn] = Field(
        ..., min_length=1, max_length=MEASUREMENT_IMPORT_MAX
    )


# Per-hive scale calibration for hives 3..MAX_HIVES (hives 1–2 use the legacy
# scale1/2 columns on device_configs). The firmware bridges these into its hive
# registry over remote config and reports them back after a portal calibration.
class HiveScaleCalibration(BaseModel):
    index: int = Field(..., ge=1, le=MAX_HIVES)
    scale: int = Field(0, ge=0)
    offset: int = 0
    factor: float = -7050.0
    # Load-cell temperature coefficient for this hive, kg/°C (see tempco_* on
    # DeviceConfig for the shared enable switch, source and reference). Hives 1–2
    # keep using scale{1,2}_tempco_kg_per_c; this covers hives 3..MAX_HIVES, which
    # have no dedicated columns.
    tempco_kg_per_c: float = 0.0


class HiveScaleCalibrationIn(BaseModel):
    index: int = Field(..., ge=1, le=MAX_HIVES)
    scale: int = Field(0, ge=0)
    offset: Optional[int] = None
    factor: Optional[float] = None
    tempco_kg_per_c: Optional[float] = None


class DeviceConfig(BaseModel):
    device_id: str
    send_interval_seconds: int = 600
    scale1_offset: int = 0
    scale1_factor: float = -7050.0
    scale2_offset: int = 0
    scale2_factor: float = -7050.0
    # Calibration for hives 3..MAX_HIVES (hives 1–2 are the scale1/2 fields
    # above), including each hive's temperature coefficient.
    hive_scales: list[HiveScaleCalibration] = []
    config_version: int = 1
    # ── Load-cell temperature compensation (applied in the backend on read) ───
    # See server/tempcomp.py. Coefficients are kg/°C; the correction is
    # disabled (no-op) until tempco_enabled is set and a non-zero coefficient
    # exists. The raw weight in `measurements` is never altered.
    tempco_enabled: bool = False
    tempco_source: Literal["ambient", "hive_1", "hive_2"] = DEFAULT_TEMP_SOURCE
    tempco_ref_temp_c: float = DEFAULT_REF_TEMP_C
    scale1_tempco_kg_per_c: float = 0.0
    scale2_tempco_kg_per_c: float = 0.0
    # ── HiveTraffic night mode (see firmware/include/night_mode.h) ───────────
    # A paired bee counter's 48 IR emitters dominate its power draw by an order
    # of magnitude, and honey bees are diurnal, so HiveHub can tell the counter
    # to stop sensing overnight. Per device: every counter on one hub shares an
    # apiary and a sunset. OFF by default — an existing device is unaffected
    # until this is deliberately turned on.
    beecounter_night_mode_enabled: bool = False
    # LOCAL minutes since midnight. start > end wraps midnight, which the
    # 20:00-06:00 default does; start == end is an EMPTY window, never a 24-hour
    # one (the firmware refuses it), so a single mis-set field cannot stop a
    # counter for good.
    beecounter_night_start_minute: int = 20 * 60
    beecounter_night_end_minute: int = 6 * 60
    # Crossings (in + out) in the last upload cycle above which night mode is
    # postponed to the next cycle — the "the hive is still flying, wait" rule.
    # 0 disables the check and enters the window on the clock alone.
    beecounter_night_max_traffic: int = 0
    # POSIX TZ string, e.g. "CET-1CEST,M3.5.0,M10.5.0/3". Empty means UTC.
    #
    # Load-bearing, not cosmetic: the device clock is UTC (configTime(0, 0, …)),
    # so without this a window entered as 20:00 fires at 21:00 local in summer,
    # discarding an hour of foraging on exactly the long evenings that have most
    # of it. A POSIX string rather than an IANA name because the ESP32 has no
    # tz database — newlib parses this form directly, DST rules included.
    timezone: str = ""
    # ── HiveTraffic emitter banks (see firmware/include/bee_counter_client.h) ─
    # The counter's 48 IR emitters sit behind three IRLB8721 MOSFETs, one per
    # MCP23017, so each third of the entrance is independently switchable:
    # bank 1 = gates 00..07, bank 2 = 10..17, bank 3 = 20..27. Measured on the
    # counter's 3.3 V rail, one bank draws ~0.14 A, two ~0.22 A and three
    # ~0.30 A — about 80 mA per bank on top of a ~60 mA floor.
    #
    # All three ON by default: a counter runs its whole entrance until someone
    # deliberately narrows it. Turning one off stops eight gates being counted
    # at all, so their share of the totals goes permanently flat — which is why
    # the counter reports the mask back (wire revision 5) rather than leaving it
    # indistinguishable from a dead FET.
    #
    # Three booleans rather than one bitmask: the dashboard draws three
    # checkboxes and this object is read by humans. The firmware assembles the
    # mask it writes over BLE.
    beecounter_bank1_enabled: bool = True
    beecounter_bank2_enabled: bool = True
    beecounter_bank3_enabled: bool = True
    # ── Inspection mode (see firmware/include/inspection.h) ──────────────────
    # How long an inspection may run before the hub and the server both end it.
    # This is a safety net, not a schedule: one forgotten button press would
    # otherwise blank a hive's data indefinitely, and a blank hive looks exactly
    # like a dead sensor. An hour covers a normal full inspection of a stand;
    # raise it here for a longer session (queen rearing, a comb swap) rather
    # than working around it.
    inspection_timeout_minutes: int = 60


class DeviceConfigUpdate(BaseModel):
    send_interval_seconds: Optional[int] = None
    scale1_offset: Optional[int] = None
    scale1_factor: Optional[float] = None
    scale2_offset: Optional[int] = None
    scale2_factor: Optional[float] = None
    # Upsert calibration for hives 3..MAX_HIVES; each entry updates one hive's
    # stored offset/factor/tempco (omitted fields keep their current value).
    hive_scales: Optional[list[HiveScaleCalibrationIn]] = None
    tempco_enabled: Optional[bool] = None
    tempco_source: Optional[Literal["ambient", "hive_1", "hive_2"]] = None
    tempco_ref_temp_c: Optional[float] = None
    scale1_tempco_kg_per_c: Optional[float] = None
    scale2_tempco_kg_per_c: Optional[float] = None
    beecounter_night_mode_enabled: Optional[bool] = None
    # Bounded here as well as by the database CHECK. An out-of-range minute
    # makes the firmware refuse the whole window, which presents as "night mode
    # never happened" with nothing anywhere saying why — so the write is the
    # only place a human finds out, and it has to reject rather than clamp.
    beecounter_night_start_minute: Optional[int] = Field(default=None, ge=0, le=1439)
    beecounter_night_end_minute: Optional[int] = Field(default=None, ge=0, le=1439)
    beecounter_night_max_traffic: Optional[int] = Field(default=None, ge=0)
    timezone: Optional[str] = Field(default=None, max_length=64)
    # Emitter banks. Not validated against each other here: a PATCH carries only
    # the fields that changed, so "is the last bank being turned off?" cannot be
    # answered from this object alone — it needs the stored row. That check runs
    # in update_device_config(), which has both halves.
    beecounter_bank1_enabled: Optional[bool] = None
    beecounter_bank2_enabled: Optional[bool] = None
    beecounter_bank3_enabled: Optional[bool] = None
    # Bounded here as well as by the database CHECK, and for the same reason as
    # the night window above: the firmware clamps a nonsense value rather than
    # refusing it, so the write is where a human finds out.
    inspection_timeout_minutes: Optional[int] = Field(default=None, ge=1, le=1440)


# The project was renamed HiveScale -> HiveHub, but the canonical OTA target
# stored in firmware_releases (and queried by every already-deployed device as
# ?target=hivescale) stays "hivescale" for backward compatibility. Accept
# "hivehub" as a user-facing alias on every firmware input boundary and fold it
# onto the canonical value, so new users can upload "HiveHub" firmware without
# any database migration or breaking existing devices.
FIRMWARE_TARGET_ALIASES = {"hivehub": "hivescale"}


def normalize_firmware_target(target: str) -> str:
    """Map a user-supplied OTA target onto its canonical stored value.

    Folds the "hivehub" alias onto "hivescale" (and lower-cases / trims). Unknown
    values pass through unchanged so the usual target validation still rejects them.
    """
    t = (target or "").strip().lower()
    return FIRMWARE_TARGET_ALIASES.get(t, t)


class FirmwareReleaseIn(BaseModel):
    version: str
    filename: str
    active: bool = True
    # "beecounter" is no longer an accepted target: BeeCounter firmware was
    # delivered over the (removed) wired I2C relay, and no BLE/GATT OTA exists
    # yet — a registered beecounter image could never reach a device.
    target: Literal["hivescale", "hiveinside"] = "hivescale"

    @field_validator("target", mode="before")
    @classmethod
    def _alias_target(cls, v):
        # Accept "hivehub" (any case/whitespace) for the canonical "hivescale"
        # target before the Literal check runs, so a HiveHub firmware upload is
        # transparently stored as a hivescale release.
        return normalize_firmware_target(v) if isinstance(v, str) else v

    # Board/architecture this image was built for. Required in effect for the
    # "hivescale" target ("esp32" / "esp32-c6") so OTA never serves a 30-pin
    # ESP32 (Xtensa) image to an ESP32-C6 (RISC-V) or vice versa; for
    # "hiveinside" the only valid board is "nrf54lm20a". When omitted for a
    # hivescale release it is derived from the board-stamped filename.
    board: Optional[str] = None


class DeviceCommandIn(BaseModel):
    # "update_beecounter" is back, but it is NOT the old command: the wired I2C
    # relay it originally named was deleted, and this one streams the image to
    # the counter over BLE GATT. The payload shape matches update_hiveinside
    # (slot + url + version + crc32). Firmware built without
    # ENABLE_WIRELESS_BEECOUNTER rejects it explicitly rather than faking
    # success, so queuing it against an old or unequipped device fails visibly.
    command_type: Literal[
        "calibrate_scale_1",
        "calibrate_scale_2",
        "reboot",
        "reset_preferences",
        "factory_reset",
        "reset_wifi",
        "check_ota",
        "ota_update",
        "update_hiveinside",
        "update_beecounter",
        "start_provisioning",
        "start_calibration_mode",
        "stop_calibration_mode",
        # Inspection mode, the remote equivalent of the external button. Queued
        # like any other command, so a deep-sleeping hub picks it up on its next
        # wake — see DeviceInspection.acknowledged_at for how a caller tells
        # "requested" from "the hub is actually doing it".
        "start_inspection",
        "stop_inspection",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class InspectionStartIn(BaseModel):
    """Open an inspection from the API (HivePal's in-app button, or the dashboard).

    ``hives`` scopes the inspection to specific hive indexes; omitted (or empty)
    means the whole hub, which is what the physical button always means — a
    beekeeper is standing at the stand, not at one box.
    """
    hives: Optional[list[int]] = None
    note: Optional[str] = Field(default=None, max_length=1000)
    # Back-date the start. HivePal knows when the beekeeper actually opened the
    # hive; the request may arrive minutes later over a bad rural connection,
    # and the spike belongs inside the window either way.
    started_at: Optional[datetime] = None

    @field_validator("hives")
    @classmethod
    def _check_hives(cls, v):
        if v is None:
            return v
        for h in v:
            if not (1 <= h <= MAX_HIVES):
                raise ValueError(f"hive index {h} out of range 1..{MAX_HIVES}")
        # Sorted + de-duplicated so two requests naming the same hives in a
        # different order produce the same stored array.
        return sorted(set(v))


class InspectionStopIn(BaseModel):
    """Close the open inspection. ``ended_at`` back-dates it, like started_at."""
    note: Optional[str] = Field(default=None, max_length=1000)
    ended_at: Optional[datetime] = None


class InspectionUpdateIn(BaseModel):
    """Edit a recorded inspection — in practice, add or correct its note.

    The note is the half of this feature that pays off months later: "removed 2
    supers" next to a 40 kg step turns an alarming trace into a harvest record.
    """
    note: Optional[str] = Field(default=None, max_length=1000)


class DeviceInspection(BaseModel):
    id: int
    device_id: str
    # None = every hive on the hub.
    hives: Optional[list[int]] = None
    started_at: datetime
    ended_at: Optional[datetime] = None
    active: bool
    source: str
    end_reason: Optional[str] = None
    # For an API-started inspection: when the request was queued, and when the
    # hub actually picked the command up. A hub deep-sleeps between cycles, so
    # these are minutes apart by design and a caller showing "inspection on"
    # before acknowledged_at is showing something that has not happened yet.
    requested_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    note: Optional[str] = None
    created_by: Optional[str] = None


class DeviceInspectionStatus(BaseModel):
    """Answer to "is this hub inspecting, and does it know it yet?"."""
    device_id: str
    active: bool
    pending: bool
    inspection: Optional[DeviceInspection] = None
    timeout_minutes: int = 60


class DeviceCommandResult(BaseModel):
    success: bool
    message: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)


class ClaimDeviceIn(BaseModel):
    claim_code: str = Field(..., min_length=4, max_length=128)
    display_name: Optional[str] = None
    scale_1_display_name: Optional[str] = None
    scale_2_display_name: Optional[str] = None


class ShareDeviceIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: Literal["admin", "viewer"] = "viewer"


class DeviceVisibilityUpdateIn(BaseModel):
    """Show or hide a device in the local dashboard's hive picker.

    Hiding a retired device drops it from the top-bar picker without touching
    its stored data; it can be shown again at any time.
    """
    hidden: bool


class MeasurementDeleteIn(BaseModel):
    """Delete a device's measurements within a time range.

    Used to prune the useless boot-time spikes devices emit before they know
    their calibration. Gated by the device's claim code as a second factor on
    top of the admin session: the caller must supply the claim code the device
    was provisioned with, matched against the stored hash.
    """
    start_at: datetime
    end_at: datetime
    claim_code: str = Field(..., min_length=4, max_length=128)


class DeviceDeleteIn(BaseModel):
    """Erase a device and every row that belongs to it.

    The dashboard's ``hidden`` flag only retires a device from the hive picker;
    this is the actual delete for a device that is gone for good. Irreversible,
    so it carries the same claim-code second factor as MeasurementDeleteIn plus
    an explicit confirmation of the device_id being erased (typed by the admin),
    which makes an accidental click on the wrong row impossible.
    """
    claim_code: str = Field(..., min_length=4, max_length=128)
    confirm_device_id: str = Field(..., min_length=1, max_length=128)


class DeviceChannelsUpdateIn(BaseModel):
    # Legacy two-channel fields, kept so the HivePal app endpoints keep working.
    scale_1_display_name: Optional[str] = None
    scale_2_display_name: Optional[str] = None
    # Per-hive display names for hives 1..MAX_HIVES, keyed by the hive index as a
    # string ("1".."18"). Lets the local dashboard rename every hive a device
    # reports, not just the first two. Ignored entries outside 1..MAX_HIVES are
    # dropped by apply_device_channels().
    names: Optional[dict[str, Optional[str]]] = None


# Lightweight email check — good enough to catch typos without pulling in the
# optional email-validator dependency. Blank / whitespace-only normalizes to None
# so an account can clear its email again.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_optional_email(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    email = value.strip()
    if not email:
        return None
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("Enter a valid email address")
    return email.lower()


class DashboardSetupIn(BaseModel):
    """First-run wizard payload: creates the initial admin account."""
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    email: Optional[str] = Field(default=None, max_length=254)

    _norm_email = field_validator("email")(lambda cls, v: normalize_optional_email(v))


class DashboardLoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class DashboardCreateUserIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    role: Literal["admin", "viewer"] = "viewer"
    email: Optional[str] = Field(default=None, max_length=254)

    _norm_email = field_validator("email")(lambda cls, v: normalize_optional_email(v))


class DashboardChangePasswordIn(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


class DashboardUpdateEmailIn(BaseModel):
    """Set or clear the logged-in user's contact email (alerts destination)."""
    email: Optional[str] = Field(default=None, max_length=254)

    _norm_email = field_validator("email")(lambda cls, v: normalize_optional_email(v))


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(..., min_length=1, max_length=256)
    auth: str = Field(..., min_length=1, max_length=256)


class PushSubscriptionIn(BaseModel):
    """A browser Web Push subscription, as produced by PushManager.subscribe()."""
    endpoint: str = Field(..., min_length=1, max_length=2048)
    keys: PushSubscriptionKeys


class PushUnsubscribeIn(BaseModel):
    endpoint: str = Field(..., min_length=1, max_length=2048)


class AppDeviceConfigUpdate(DeviceConfigUpdate):
    pass


class AppCalibrationModeStartIn(BaseModel):
    interval_seconds: int = Field(default=5, ge=1, le=3600)
    timeout_seconds: int = Field(default=600, ge=1, le=86400)


class TempCoefficientFitIn(BaseModel):
    """Request to fit a load-cell temperature coefficient from stored data.

    The window should cover a period where the physical load was constant (an
    empty/unworked hive or a fixed reference mass) and the temperature swung
    enough to expose the drift — e.g. a clear day/night cycle.
    """
    # Which hive's scale to fit. Hives 1–2 regress the measurements table's
    # scale_{1,2}_weight_kg columns; hives 3..MAX_HIVES regress hive_readings.
    scale: int = Field(..., ge=1, le=MAX_HIVES)
    lookback_days: int = Field(default=3, ge=1, le=90)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    # Which temperature channel to regress against; defaults to the device's
    # current tempco_source.
    temp_source: Optional[Literal["ambient", "hive_1", "hive_2"]] = None
    # Only consider rows captured in calibration mode (stable, known load).
    calibration_mode_only: bool = False
    # Persist the fitted coefficient (and source) to the device config and enable
    # compensation. When False, only the fit result is returned.
    apply: bool = False
    # Also overwrite tempco_ref_temp_c with the fitted window's mean temperature.
    # Off by default: the reference is the temperature at which the scale reads
    # true (the one it was tared/spanned at), which the fit cannot know — the
    # window mean is returned as ``ref_temp_c`` for information either way.
    set_ref_temp: bool = False


# ── Published charts ("Publish data") ────────────────────────────────────────
# One publication = one metric, over an ordered list of hives (or devices, for
# device-level metrics), over a rolling period, plus presentation options. The
# resolved display label travels with each series so the public payload never
# has to carry a device_id. See server/publish.py for the metric registry.


class PublishedSeriesIn(BaseModel):
    """One line of a published chart: which hive it reads, and what to call it."""
    device_id: str = Field(..., min_length=1, max_length=128)
    # None for device-level metrics (ambient temperature, battery, signal …),
    # which belong to the collector rather than to a single hive.
    hive: Optional[int] = Field(default=None, ge=1, le=MAX_HIVES)
    # Public label. Defaults to the hive's dashboard name on the client side;
    # publishers can rename it so a public chart need not leak internal names.
    label: str = Field(..., min_length=1, max_length=64)


class PublishedChartOptionsIn(BaseModel):
    """Presentation-only settings for the embedded page."""
    theme: Literal["auto", "light", "dark"] = "auto"
    height: int = Field(default=320, ge=140, le=1200)
    show_legend: bool = True
    # Footer line with the timestamp of the newest reading in the payload.
    show_updated: bool = True


class PublishedChartIn(BaseModel):
    """Create a publication."""
    title: str = Field(..., min_length=1, max_length=120)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    metric: str = Field(..., min_length=1, max_length=64)
    # "line" draws the time series; "value" shows one big current-value tile per
    # series (the "publish a number, not a diagram" case).
    chart_type: Literal["line", "value"] = "line"
    # Server-side down-sampling into daily buckets, for long periods where the
    # raw cadence is noise (a month of weight reads far better as a daily max).
    aggregate: Literal["none", "daily_min", "daily_max", "daily_avg"] = "none"
    series: list[PublishedSeriesIn] = Field(..., min_length=1, max_length=MAX_HIVES)
    range_days: int = Field(default=30, ge=1, le=1825)
    options: PublishedChartOptionsIn = Field(default_factory=PublishedChartOptionsIn)


class PublishedChartUpdateIn(BaseModel):
    """Edit a publication. Only the supplied fields change.

    The token is never re-issued here: an embed already pasted into a website
    keeps working, which is the point of editing rather than re-publishing.
    """
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    subtitle: Optional[str] = Field(default=None, max_length=200)
    chart_type: Optional[Literal["line", "value"]] = None
    aggregate: Optional[Literal["none", "daily_min", "daily_max", "daily_avg"]] = None
    series: Optional[list[PublishedSeriesIn]] = Field(default=None, min_length=1, max_length=MAX_HIVES)
    range_days: Optional[int] = Field(default=None, ge=1, le=1825)
    options: Optional[PublishedChartOptionsIn] = None
    # Take a publication offline without deleting it (the embed then renders a
    # "not available" notice instead of data).
    enabled: Optional[bool] = None
