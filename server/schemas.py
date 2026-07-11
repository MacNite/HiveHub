"""Pydantic request/response models shared across the API routers."""

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tempcomp import DEFAULT_REF_TEMP_C, DEFAULT_TEMP_SOURCE


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
    # Board/architecture a HiveInside node reports over GATT ("esp32-c6" /
    # "nrf54lm20a"), forwarded by the HiveHub so a board-specific OTA image is
    # only ever relayed to matching silicon.
    board: Optional[str] = None


class HiveMicIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    rms_dbfs: Optional[float] = None


class HiveBeeCounterIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    ok: Optional[bool] = None
    total_in: Optional[int] = None
    total_out: Optional[int] = None
    interval_in: Optional[int] = None
    interval_out: Optional[int] = None


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
    fft: Optional[list[int]] = Field(default=None, max_length=16)


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
    cellular_ok: Optional[bool] = None
    cellular_csq: Optional[int] = None
    calibration_mode: Optional[bool] = None
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
    # One BeeCounter per hive. Up to two on the shared I2C bus, addresses
    # 0x30 / 0x31. Each block is independent — a missing unit reports
    # bee_counter_N_ok=False and the rest of its fields are null.
    #
    # The per-gate 24-byte arrays live in raw_json as bee_counter_N_per_gate_in
    # / bee_counter_N_per_gate_out — they are forensic data, not surfaced as
    # columns.
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
    bee_counter_1_busy_retries:      Optional[int]  = None
    bee_counter_1_read_attempts:     Optional[int]  = None
    bee_counter_1_latch_succeeded:   Optional[bool] = None

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
    bee_counter_2_busy_retries:      Optional[int]  = None
    bee_counter_2_read_attempts:     Optional[int]  = None
    bee_counter_2_latch_succeeded:   Optional[bool] = None

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

    # ── HolyIot 25015 in-hive BLE sensor (per hive) ──────────────────────────
    # The 25015 is a passive BLE beacon (SHT40 + LPS22HB + LIS2DH12) bridged by
    # the ESP32. Its acceleration is reported through the accel_N_* fields above
    # (no FFT bands — a beacon only emits periodic single-shot samples). Humidity
    # and pressure are promoted to columns; the raw per-axis acceleration,
    # battery and link RSSI are kept in raw_json (declared so extra="ignore"
    # does not drop them). Its temperature is delivered via hive_N_temp_c.
    ble_1_humidity_percent: Optional[float] = None
    ble_1_pressure_hpa:     Optional[float] = None
    ble_1_accel_x_mg:       Optional[float] = None
    ble_1_accel_y_mg:       Optional[float] = None
    ble_1_accel_z_mg:       Optional[float] = None
    ble_1_battery_percent:  Optional[int]   = None
    ble_1_battery_mv:       Optional[int]   = None
    ble_1_rssi_dbm:         Optional[int]   = None
    # HiveInside reports its running firmware version and board over GATT
    # ("fw"/"board"); kept in raw_json (declared so extra="ignore" does not drop
    # them). The board lets the relay pick a matching OTA image per architecture.
    ble_1_firmware_version: Optional[str]   = None
    ble_1_board:            Optional[str]   = None

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
    hiveheart_1_fft:              Optional[list[int]] = Field(default=None, max_length=16)
    hiveheart_2_frequency_hz:     Optional[float] = None
    hiveheart_2_energy:           Optional[int]   = None
    hiveheart_2_peak:             Optional[int]   = None
    hiveheart_2_battery_v:        Optional[float] = None
    hiveheart_2_rssi_dbm:         Optional[int]   = None
    hiveheart_2_temp_c:           Optional[float] = None
    hiveheart_2_humidity_percent: Optional[float] = None
    hiveheart_2_fft:              Optional[list[int]] = Field(default=None, max_length=16)

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


class HiveScaleCalibrationIn(BaseModel):
    index: int = Field(..., ge=1, le=MAX_HIVES)
    scale: int = Field(0, ge=0)
    offset: Optional[int] = None
    factor: Optional[float] = None


class DeviceConfig(BaseModel):
    device_id: str
    send_interval_seconds: int = 600
    scale1_offset: int = 0
    scale1_factor: float = -7050.0
    scale2_offset: int = 0
    scale2_factor: float = -7050.0
    # Calibration for hives 3..MAX_HIVES (hives 1–2 are the scale1/2 fields above).
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


class DeviceConfigUpdate(BaseModel):
    send_interval_seconds: Optional[int] = None
    scale1_offset: Optional[int] = None
    scale1_factor: Optional[float] = None
    scale2_offset: Optional[int] = None
    scale2_factor: Optional[float] = None
    # Upsert calibration for hives 3..MAX_HIVES; each entry updates one hive's
    # stored offset/factor (omitted fields keep their current value).
    hive_scales: Optional[list[HiveScaleCalibrationIn]] = None
    tempco_enabled: Optional[bool] = None
    tempco_source: Optional[Literal["ambient", "hive_1", "hive_2"]] = None
    tempco_ref_temp_c: Optional[float] = None
    scale1_tempco_kg_per_c: Optional[float] = None
    scale2_tempco_kg_per_c: Optional[float] = None


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
    target: Literal["hivescale", "beecounter", "hiveinside"] = "hivescale"

    @field_validator("target", mode="before")
    @classmethod
    def _alias_target(cls, v):
        # Accept "hivehub" (any case/whitespace) for the canonical "hivescale"
        # target before the Literal check runs, so a HiveHub firmware upload is
        # transparently stored as a hivescale release.
        return normalize_firmware_target(v) if isinstance(v, str) else v

    # Board/architecture this image was built for ("esp32" / "esp32-c6"). Required
    # in effect for the "hivescale" target so OTA never serves a 30-pin ESP32
    # (Xtensa) image to an ESP32-C6 (RISC-V) or vice versa; left None for the
    # single-architecture beecounter / hiveinside sub-device targets. When omitted
    # for a hivescale release it is derived from the board-stamped filename.
    board: Optional[str] = None


class DeviceCommandIn(BaseModel):
    command_type: Literal[
        "calibrate_scale_1",
        "calibrate_scale_2",
        "reboot",
        "reset_preferences",
        "factory_reset",
        "reset_wifi",
        "check_ota",
        "ota_update",
        "update_beecounter",
        "update_hiveinside",
        "start_provisioning",
        "start_calibration_mode",
        "stop_calibration_mode",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


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
    scale: Literal[1, 2]
    lookback_days: int = Field(default=3, ge=1, le=90)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    # Which temperature channel to regress against; defaults to the device's
    # current tempco_source.
    temp_source: Optional[Literal["ambient", "hive_1", "hive_2"]] = None
    # Only consider rows captured in calibration mode (stable, known load).
    calibration_mode_only: bool = False
    # Persist the fitted coefficient (and ref temp / source) to the device config
    # and enable compensation. When False, only the fit result is returned.
    apply: bool = False
