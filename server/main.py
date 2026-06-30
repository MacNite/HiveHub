import hashlib
import json
import logging
import os
import re
import threading
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Literal, Any

from jose import jwt, JWTError

import psycopg
from psycopg_pool import ConnectionPool
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from insights import compute_insights, summarize
from mqtt_publisher import publisher as mqtt_publisher
from sd_import import split_new_and_duplicate
from tempcomp import (
    VALID_TEMP_SOURCES,
    TEMP_SOURCE_FIELD,
    DEFAULT_REF_TEMP_C,
    DEFAULT_TEMP_SOURCE,
    compensate_weight,
    ema_temperatures,
    fit_temp_coefficient,
)

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]
HIVEPAL_SERVICE_API_KEY = os.environ.get("HIVEPAL_SERVICE_API_KEY", "")
HIVEPAL_JWT_SECRET = os.environ.get("HIVEPAL_JWT_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Opt-in, auth-free local dashboard (single-owner self-host). When enabled it
# exposes read-only data plus firmware/calibration controls for ALL devices under
# /api/v1/local/* and serves the static dashboard at /dashboard. Keep OFF on any
# multi-tenant / internet-facing deployment; gate behind a reverse proxy / LAN.
ENABLE_LOCAL_DASHBOARD = os.environ.get("ENABLE_LOCAL_DASHBOARD", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
FIRMWARE_DIR = Path(os.environ.get("FIRMWARE_DIR", "/app/firmware"))
DB_POOL_MIN_SIZE = int(os.environ.get("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "10"))

# ── Abuse / DoS protection knobs (all overridable via environment) ───────────
# Per-client-IP request rate limit. Generous by default: a device reports only
# once every few minutes, so this never affects normal use but stops floods.
# Set RATE_LIMIT_ENABLED=false to turn it off entirely.
RATE_LIMIT_ENABLED = os.environ.get("RATE_LIMIT_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on",
)
RATE_LIMIT_DEFAULT = os.environ.get("RATE_LIMIT_DEFAULT", "120/minute")
# Maximum size of a normal (JSON) request body. A measurement is only a few KB;
# this leaves generous head-room while preventing memory/storage amplification.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(256 * 1024)))
# Firmware uploads are large by design and are capped separately, while being
# streamed to disk, inside the upload endpoint itself.
MAX_FIRMWARE_BYTES = int(os.environ.get("MAX_FIRMWARE_BYTES", str(16 * 1024 * 1024)))

# ── Insights history / alert lifecycle reconciliation ────────────────────────
# Sensor-based insights (server/insights.py) are recomputed on demand and never
# cached. To give HivePal a *history* of alerts, we additionally persist their
# lifecycle (first seen, last seen, resolved, peak severity) into the
# `insight_alerts` table. A lightweight background thread reconciles every
# device that has recent measurements on a fixed interval; the summary endpoint
# also reconciles opportunistically when it is hit. Set
# INSIGHTS_RECONCILE_ENABLED=false to disable the background thread.
INSIGHTS_RECONCILE_ENABLED = os.environ.get(
    "INSIGHTS_RECONCILE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes", "on")
INSIGHTS_RECONCILE_INTERVAL_SECONDS = int(
    os.environ.get("INSIGHTS_RECONCILE_INTERVAL_SECONDS", "900")
)
# Lookback window used for the *persisted* lifecycle. Kept fixed (independent of
# the caller-supplied lookback on the live endpoint) so history doesn't thrash
# as different clients request different windows.
INSIGHTS_HISTORY_LOOKBACK_DAYS = int(
    os.environ.get("INSIGHTS_HISTORY_LOOKBACK_DAYS", "14")
)

logger = logging.getLogger("hivescale.insights")

# Earliest year a device-supplied measurement timestamp is trusted. Anything
# older (notably the 1970 epoch a device emits when RTC and NTP both fail) is
# treated as a missing timestamp and replaced with the server clock on ingest.
MIN_PLAUSIBLE_YEAR = 2020


class MaxBodySizeMiddleware:
    """Reject requests whose body exceeds ``max_body_bytes``.

    A single valid API key would otherwise let a client POST arbitrarily large
    JSON bodies, which are parsed into memory and (for measurements) persisted
    verbatim into ``raw_json`` — a storage/memory amplification vector. Capping
    the body closes it. The firmware-upload endpoint legitimately receives
    multi-megabyte bodies, so it is exempt here and enforces its own,
    larger ``MAX_FIRMWARE_BYTES`` while streaming to disk.
    """

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _is_exempt(scope) -> bool:
        if scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        # Firmware binary uploads are large by design and capped by the endpoint.
        # Bulk SD import is authenticated and capped by MEASUREMENT_IMPORT_MAX rows.
        return path.endswith("/firmware") or path.endswith("/measurements/import")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_body_bytes <= 0 or self._is_exempt(scope):
            await self.app(scope, receive, send)
            return

        # Fast path: trust a declared Content-Length when present.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
                        await self._send_413(send)
                        return
                except ValueError:
                    pass
                break

        # Defence in depth: enforce while streaming, covering chunked uploads or
        # a client that omits/understates Content-Length.
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _send_413(send):
        body = b'{"detail":"Request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _client_ip_key(request: Request) -> str:
    """Rate-limit key: the real client IP, even behind Cloudflare / a proxy.

    Falls back to the socket peer when no proxy headers are present. These
    headers are only trustworthy when the API sits behind a proxy you control
    (the documented deployment); avoid exposing the API directly.
    """
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=_client_ip_key,
    default_limits=[RATE_LIMIT_DEFAULT] if RATE_LIMIT_ENABLED else [],
    enabled=RATE_LIMIT_ENABLED,
    headers_enabled=True,
)

app = FastAPI(
    title="HiveHub API",
    description="HTTP endpoint for ESP32-based dual hive scales.",
    version="0.3.2",
)

# Rate limiting (slowapi): keyed on the real client IP, emits standard RateLimit
# headers, and returns HTTP 429 when the limit is exceeded.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware order: the last one added runs first. The body-size guard is added
# before the rate limiter so the limiter (outermost) rejects floods before any
# body is read.
app.add_middleware(MaxBodySizeMiddleware, max_body_bytes=MAX_BODY_BYTES)
if RATE_LIMIT_ENABLED:
    app.add_middleware(SlowAPIMiddleware)

# Compress responses (added last → outermost, so it wraps every handler). The
# measurement JSON is large and highly repetitive — the same ~30–140 keys per
# row — so gzip typically shrinks it ~8–10×, which is the single biggest win for
# dashboard/app load time over the wire. minimum_size avoids compressing tiny
# bodies where the CPU/header overhead would not pay off. Applies to every JSON
# response, so the HivePal app API (/api/v1/app/*) benefits too, not just the
# local dashboard.
app.add_middleware(GZipMiddleware, minimum_size=1024)


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
    rssi_dbm: Optional[int] = None
    firmware_version: Optional[str] = None


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
    ble_1_rssi_dbm:         Optional[int]   = None
    # HiveInside C6 reports its running firmware version over GATT ("fw"); kept
    # in raw_json (declared so extra="ignore" does not drop it).
    ble_1_firmware_version: Optional[str]   = None

    ble_2_humidity_percent: Optional[float] = None
    ble_2_pressure_hpa:     Optional[float] = None
    ble_2_accel_x_mg:       Optional[float] = None
    ble_2_accel_y_mg:       Optional[float] = None
    ble_2_accel_z_mg:       Optional[float] = None
    ble_2_battery_percent:  Optional[int]   = None
    ble_2_rssi_dbm:         Optional[int]   = None
    ble_2_firmware_version: Optional[str]   = None

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


class DeviceConfig(BaseModel):
    device_id: str
    send_interval_seconds: int = 600
    scale1_offset: int = 0
    scale1_factor: float = -7050.0
    scale2_offset: int = 0
    scale2_factor: float = -7050.0
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


class DeviceChannelsUpdateIn(BaseModel):
    scale_1_display_name: Optional[str] = None
    scale_2_display_name: Optional[str] = None


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


def require_api_key(x_api_key: str = Header(default="")) -> str:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def require_hivepal_service_key(x_hivepal_service_key: str = Header(default="")):
    if not HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=500, detail="HIVEPAL_SERVICE_API_KEY is not configured")
    if x_hivepal_service_key != HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid HivePal service key")


def require_local_dashboard():
    """Gate the auth-free local dashboard API behind ENABLE_LOCAL_DASHBOARD.

    Returns 404 (not 403) when disabled so the endpoints are indistinguishable
    from non-existent routes on a default / multi-tenant deployment.
    """
    if not ENABLE_LOCAL_DASHBOARD:
        raise HTTPException(status_code=404, detail="Not Found")


def require_user_id(authorization: str = Header(default="")) -> str:
    if not HIVEPAL_JWT_SECRET:
        raise HTTPException(status_code=500, detail="HIVEPAL_JWT_SECRET is not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> header required")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, HIVEPAL_JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")
    return str(user_id)


def verify_device_key(device_id: str, api_key: str):
    """Register a device's API key on first contact; reject mismatches thereafter.

    This runs only for device-authenticated endpoints (config/firmware/command
    polls), so it is also where we record genuine device contact: last_seen_at
    is bumped here, never by the HivePal app reading config on the device's
    behalf (see ensure_device_config / touch_last_seen).
    """
    if len(api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devices
                SET api_key_hash = COALESCE(api_key_hash, %s),
                    last_seen_at = now()
                WHERE device_id = %s
                RETURNING api_key_hash
                """,
                (key_hash, device_id),
            )
            row = cur.fetchone()
            conn.commit()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row[0] != key_hash:
        raise HTTPException(status_code=401, detail="API key does not match this device")


class DeviceKeyGuard:
    """FastAPI dependency for device-scoped endpoints. Reads device_id from the
    path and X-API-Key from the header, then delegates to verify_device_key."""
    def __call__(self, device_id: str, x_api_key: str = Header(default="")):
        verify_device_key(device_id, x_api_key)

require_device_key = DeviceKeyGuard()


db_pool = ConnectionPool(
    DATABASE_URL,
    min_size=DB_POOL_MIN_SIZE,
    max_size=DB_POOL_MAX_SIZE,
    open=False,
)


def get_conn():
    return db_pool.connection()


def hash_claim_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    claim_code_hash TEXT,
                    claimed_at TIMESTAMPTZ,
                    display_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ,
                    last_firmware_version TEXT
                );

                CREATE TABLE IF NOT EXISTS device_members (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (device_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS device_channels (
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    channel_number INTEGER NOT NULL CHECK (channel_number IN (1, 2)),
                    name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (device_id, channel_number)
                );

                CREATE TABLE IF NOT EXISTS measurements (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    measured_at TIMESTAMPTZ NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    scale_1_weight_kg DOUBLE PRECISION,
                    scale_2_weight_kg DOUBLE PRECISION,
                    hive_1_temp_c DOUBLE PRECISION,
                    hive_2_temp_c DOUBLE PRECISION,
                    hive_1_humidity_percent DOUBLE PRECISION,
                    hive_2_humidity_percent DOUBLE PRECISION,
                    ambient_temp_c DOUBLE PRECISION,
                    ambient_humidity_percent DOUBLE PRECISION,
                    battery_voltage DOUBLE PRECISION,
                    battery_soc_percent DOUBLE PRECISION,
                    battery_alert BOOLEAN,
                    battery_monitor_ok BOOLEAN,
                    solar_monitor_ok BOOLEAN,
                    solar_bus_voltage_v DOUBLE PRECISION,
                    solar_shunt_voltage_mv DOUBLE PRECISION,
                    solar_load_voltage_v DOUBLE PRECISION,
                    solar_current_ma DOUBLE PRECISION,
                    solar_power_mw DOUBLE PRECISION,
                    network_transport TEXT,
                    cellular_ok BOOLEAN,
                    cellular_csq INTEGER,
                    calibration_mode BOOLEAN,
                    boot_count BIGINT,
                    time_source TEXT,
                    rssi_dbm INTEGER,
                    firmware_version TEXT,
                    config_version INTEGER,
                    sd_ok BOOLEAN,
                    rtc_ok BOOLEAN,
                    sht_ok BOOLEAN,
                    scale_1_raw BIGINT,
                    scale_2_raw BIGINT,
                    -- INMP441 stereo microphone columns
                    mic_ok                   BOOLEAN,
                    mic_sample_rate_hz       INTEGER,
                    mic_sample_frames        INTEGER,
                    mic_left_ok              BOOLEAN,
                    mic_left_rms_dbfs        DOUBLE PRECISION,
                    mic_left_peak_dbfs       DOUBLE PRECISION,
                    mic_left_rms_normalized  DOUBLE PRECISION,
                    mic_right_ok             BOOLEAN,
                    mic_right_rms_dbfs       DOUBLE PRECISION,
                    mic_right_peak_dbfs      DOUBLE PRECISION,
                    mic_right_rms_normalized DOUBLE PRECISION,
                    -- INMP441 FFT frequency band energy columns (dBFS)
                    mic_left_band_sub_bass_dbfs  DOUBLE PRECISION,
                    mic_left_band_hum_dbfs       DOUBLE PRECISION,
                    mic_left_band_piping_dbfs    DOUBLE PRECISION,
                    mic_left_band_stress_dbfs    DOUBLE PRECISION,
                    mic_left_band_high_dbfs      DOUBLE PRECISION,
                    mic_right_band_sub_bass_dbfs DOUBLE PRECISION,
                    mic_right_band_hum_dbfs      DOUBLE PRECISION,
                    mic_right_band_piping_dbfs   DOUBLE PRECISION,
                    mic_right_band_stress_dbfs   DOUBLE PRECISION,
                    mic_right_band_high_dbfs     DOUBLE PRECISION,
                    -- BeeCounter entrance counter columns (per hive)
                    bee_counter_1_ok                BOOLEAN,
                    bee_counter_1_protocol_version  INTEGER,
                    bee_counter_1_status_flags      INTEGER,
                    bee_counter_1_uptime_s          INTEGER,
                    bee_counter_1_num_gates         INTEGER,
                    bee_counter_1_gates_healthy     INTEGER,
                    bee_counter_1_total_in          BIGINT,
                    bee_counter_1_total_out         BIGINT,
                    bee_counter_1_interval_in       BIGINT,
                    bee_counter_1_interval_out      BIGINT,
                    bee_counter_1_glitch_count      INTEGER,
                    bee_counter_1_busy_retries      INTEGER,
                    bee_counter_1_read_attempts     INTEGER,
                    bee_counter_1_latch_succeeded   BOOLEAN,
                    bee_counter_2_ok                BOOLEAN,
                    bee_counter_2_protocol_version  INTEGER,
                    bee_counter_2_status_flags      INTEGER,
                    bee_counter_2_uptime_s          INTEGER,
                    bee_counter_2_num_gates         INTEGER,
                    bee_counter_2_gates_healthy     INTEGER,
                    bee_counter_2_total_in          BIGINT,
                    bee_counter_2_total_out         BIGINT,
                    bee_counter_2_interval_in       BIGINT,
                    bee_counter_2_interval_out      BIGINT,
                    bee_counter_2_glitch_count      INTEGER,
                    bee_counter_2_busy_retries      INTEGER,
                    bee_counter_2_read_attempts     INTEGER,
                    bee_counter_2_latch_succeeded   BOOLEAN,
                    -- LIS3DH/LIS2DH12 per-hive vibration columns (mg)
                    accel_1_ok                      BOOLEAN,
                    accel_1_sample_rate_hz          INTEGER,
                    accel_1_sample_count            INTEGER,
                    accel_1_range_g                 INTEGER,
                    accel_1_rms_mg                  DOUBLE PRECISION,
                    accel_1_peak_mg                 DOUBLE PRECISION,
                    accel_1_band_swarm_mg           DOUBLE PRECISION,
                    accel_1_band_fanning_mg         DOUBLE PRECISION,
                    accel_1_band_activity_mg        DOUBLE PRECISION,
                    accel_2_ok                      BOOLEAN,
                    accel_2_sample_rate_hz          INTEGER,
                    accel_2_sample_count            INTEGER,
                    accel_2_range_g                 INTEGER,
                    accel_2_rms_mg                  DOUBLE PRECISION,
                    accel_2_peak_mg                 DOUBLE PRECISION,
                    accel_2_band_swarm_mg           DOUBLE PRECISION,
                    accel_2_band_fanning_mg         DOUBLE PRECISION,
                    accel_2_band_activity_mg        DOUBLE PRECISION,
                    -- HolyIot 25015 in-hive BLE sensor columns (per hive)
                    ble_1_humidity_percent          DOUBLE PRECISION,
                    ble_1_pressure_hpa              DOUBLE PRECISION,
                    ble_2_humidity_percent          DOUBLE PRECISION,
                    ble_2_pressure_hpa              DOUBLE PRECISION,
                    raw_json JSONB NOT NULL
                );

                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_soc_percent DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_alert BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_monitor_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_monitor_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_bus_voltage_v DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_shunt_voltage_mv DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_load_voltage_v DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_current_ma DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_power_mw DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS network_transport TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS cellular_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS cellular_csq INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS calibration_mode BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS boot_count BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS time_source TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS firmware_version TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS config_version INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sd_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS rtc_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sht_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS scale_1_raw BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS scale_2_raw BIGINT;
                -- mic columns (idempotent for existing deployments)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_ok                   BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_sample_rate_hz       INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_sample_frames        INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_ok              BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_rms_dbfs        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_peak_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_rms_normalized  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_ok             BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_rms_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_peak_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_rms_normalized DOUBLE PRECISION;
                -- fft band columns (idempotent)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_sub_bass_dbfs  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_hum_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_piping_dbfs    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_stress_dbfs    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_high_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_sub_bass_dbfs DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_hum_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_piping_dbfs   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_stress_dbfs   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_high_dbfs     DOUBLE PRECISION;

                -- bee counter columns (idempotent for existing deployments)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_protocol_version  INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_status_flags      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_uptime_s          INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_num_gates         INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_gates_healthy     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_total_in          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_total_out         BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_interval_in       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_interval_out      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_glitch_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_busy_retries      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_read_attempts     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_latch_succeeded   BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_protocol_version  INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_status_flags      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_uptime_s          INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_num_gates         INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_gates_healthy     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_total_in          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_total_out         BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_interval_in       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_interval_out      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_glitch_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_busy_retries      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_read_attempts     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_latch_succeeded   BOOLEAN;

                -- accelerometer (per-hive vibration) columns (idempotent)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_sample_rate_hz    INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_sample_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_range_g           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_rms_mg            DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_peak_mg           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_swarm_mg     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_fanning_mg   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_activity_mg  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_sample_rate_hz    INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_sample_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_range_g           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_rms_mg            DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_peak_mg           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_swarm_mg     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_fanning_mg   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_activity_mg  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_1_humidity_percent    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_1_pressure_hpa        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_2_humidity_percent    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_2_pressure_hpa        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hive_1_humidity_percent   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hive_2_humidity_percent   DOUBLE PRECISION;

                -- beehivemonitoring.com GATT sensors (HiveHeart / HiveScale), idempotent
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_frequency_hz     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_energy           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_peak             INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_battery_v        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_frequency_hz     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_energy           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_peak             INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_battery_v        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_weight_kg        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_raw_weight       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_temp_c           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_humidity_percent DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_pressure_hpa     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_battery_v        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_weight_kg        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_raw_weight       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_temp_c           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_humidity_percent DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_pressure_hpa     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_battery_v        DOUBLE PRECISION;

                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS api_key_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS display_name TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_firmware_version TEXT;
                -- Accept-to-apply OTA gate: the ESP32 self-update only proceeds
                -- once the device owner approves a specific version for THIS device
                -- in HivePal. check_firmware returns update=true only when
                -- approved_firmware_version equals the latest available version, so
                -- a fielded device never auto-flashes an unapproved build.
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS approved_firmware_version TEXT;

                -- Board/architecture the device last reported on its OTA check
                -- (?board=esp32 / esp32-c6). Lets the HivePal status/approve flow
                -- resolve the latest release for THIS device's board instead of
                -- approving a version that only exists for the other architecture.
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_board TEXT;

                CREATE INDEX IF NOT EXISTS idx_measurements_device_time
                    ON measurements (device_id, measured_at DESC);

                -- ── Per-hive readings (normalized child of measurements) ──────
                -- One row per hive per measurement cycle, so a single ESP32 can
                -- carry up to 18 hives without a per-hive column explosion. The
                -- legacy measurements.scale_1/2_* / hive_1/2_* columns stay
                -- populated for hives 1–2 (historical continuity + the existing
                -- column-based read/insights/temp-comp paths); this table is the
                -- source of truth for ALL hives the read path exposes.
                CREATE TABLE IF NOT EXISTS hive_readings (
                    id                BIGSERIAL PRIMARY KEY,
                    measurement_id    BIGINT NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
                    device_id         TEXT NOT NULL,
                    measured_at       TIMESTAMPTZ NOT NULL,
                    hive_index        SMALLINT NOT NULL,          -- 1..18
                    name              TEXT,
                    weight_kg         DOUBLE PRECISION,
                    raw_weight        BIGINT,
                    scale_source      TEXT,                       -- hx711 | nau7802 | ...
                    temp_c            DOUBLE PRECISION,
                    temp_source       TEXT,                       -- ds18b20 | ble | hiveheart
                    humidity_percent  DOUBLE PRECISION,
                    accel_ok                BOOLEAN,
                    accel_sample_count      INTEGER,
                    accel_range_g           INTEGER,
                    accel_rms_mg            DOUBLE PRECISION,
                    accel_peak_mg           DOUBLE PRECISION,
                    accel_band_swarm_mg     DOUBLE PRECISION,
                    accel_band_fanning_mg   DOUBLE PRECISION,
                    accel_band_activity_mg  DOUBLE PRECISION,
                    ble_present       BOOLEAN,
                    ble_sensor_type   TEXT,
                    ble_humidity_percent DOUBLE PRECISION,
                    ble_pressure_hpa  DOUBLE PRECISION,
                    ble_battery_percent INTEGER,
                    ble_rssi_dbm      INTEGER,
                    bee_counter_ok           BOOLEAN,
                    bee_counter_total_in     BIGINT,
                    bee_counter_total_out    BIGINT,
                    bee_counter_interval_in  BIGINT,
                    bee_counter_interval_out BIGINT,
                    raw_json          JSONB,
                    UNIQUE (measurement_id, hive_index)
                );
                CREATE INDEX IF NOT EXISTS idx_hive_readings_device_hive_time
                    ON hive_readings (device_id, hive_index, measured_at DESC);
                CREATE INDEX IF NOT EXISTS idx_hive_readings_measurement
                    ON hive_readings (measurement_id);

                CREATE TABLE IF NOT EXISTS device_configs (
                    device_id TEXT PRIMARY KEY,
                    send_interval_seconds INTEGER NOT NULL DEFAULT 600,
                    scale1_offset BIGINT NOT NULL DEFAULT 0,
                    scale1_factor DOUBLE PRECISION NOT NULL DEFAULT -7050.0,
                    scale2_offset BIGINT NOT NULL DEFAULT 0,
                    scale2_factor DOUBLE PRECISION NOT NULL DEFAULT -7050.0,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- Load-cell temperature compensation (see server/tempcomp.py)
                    tempco_enabled BOOLEAN NOT NULL DEFAULT false,
                    tempco_source TEXT NOT NULL DEFAULT 'ambient',
                    tempco_ref_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0,
                    scale1_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    scale2_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0
                );

                -- Temperature-compensation columns (idempotent for existing deployments)
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_enabled BOOLEAN NOT NULL DEFAULT false;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_source TEXT NOT NULL DEFAULT 'ambient';
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_ref_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale1_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale2_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;
                -- Per-hive calibration / temp-comp maps for hives beyond 1–2 (JSON
                -- keyed by hive_index). scale1/2_* columns stay for hives 1–2.
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_by_hive JSONB;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale_offsets_by_hive JSONB;

                CREATE TABLE IF NOT EXISTS firmware_releases (
                    id BIGSERIAL PRIMARY KEY,
                    version TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS target TEXT NOT NULL DEFAULT 'hivescale';
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS crc32 BIGINT;
                -- Owner scoping: a release uploaded from HivePal is private to the
                -- uploading device's owner (owner_user_id = that HivePal user id).
                -- A NULL owner_user_id is a global / "official" release (e.g. pushed
                -- via the master-key POST /api/v1/firmware/releases) that any device
                -- may fall back to. See latest_release_for_owner / check_firmware.
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS owner_user_id TEXT;

                -- Board/architecture a hivescale image was built for (esp32 vs
                -- esp32-c6). These two SoCs (Xtensa vs RISC-V) take incompatible
                -- images, so OTA must match on board; check_firmware filters on it.
                -- NULL for the single-architecture beecounter / hiveinside targets.
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS board TEXT;

                -- Backfill existing hivescale releases from their board-stamped
                -- filename (rename_firmware.py: hivehub_esp32_<v> /
                -- hivehub_esp32-c6_<v>; legacy builds used the hivescale_ prefix).
                -- Detection is token-based, so either prefix works. 'esp32-c6'
                -- contains 'esp32', so tag C6 first, then plain esp32 excluding
                -- the C6 names.
                UPDATE firmware_releases SET board = 'esp32-c6'
                    WHERE board IS NULL AND target = 'hivescale'
                      AND (filename ILIKE '%esp32-c6%' OR filename ILIKE '%esp32c6%'
                           OR filename ILIKE '%xiao%');
                UPDATE firmware_releases SET board = 'esp32'
                    WHERE board IS NULL AND target = 'hivescale'
                      AND filename ILIKE '%esp32%'
                      AND filename NOT ILIKE '%esp32-c6%' AND filename NOT ILIKE '%esp32c6%';

                -- A release is identified by (owner_user_id, target, board, version):
                -- each owner keeps their own release per (target, board, version),
                -- and the same version can also exist as a global release
                -- (owner_user_id NULL) and per board (one esp32 + one esp32-c6 build).
                -- NULLs are distinct in a plain unique index, so we key on
                -- COALESCE(owner_user_id, '') / COALESCE(board, '') to collapse
                -- global / boardless rows to one per (target, version). The upsert
                -- relies on this index for its ON CONFLICT inference. Drop the legacy
                -- globally-unique version constraint and the older indexes it
                -- replaced (including the pre-board (owner, target, version) one).
                ALTER TABLE firmware_releases
                    DROP CONSTRAINT IF EXISTS firmware_releases_version_key;
                DROP INDEX IF EXISTS firmware_releases_target_version_key;
                DROP INDEX IF EXISTS firmware_releases_owner_target_version_key;
                CREATE UNIQUE INDEX IF NOT EXISTS firmware_releases_owner_target_board_version_key
                    ON firmware_releases (COALESCE(owner_user_id, ''), target, COALESCE(board, ''), version);

                CREATE TABLE IF NOT EXISTS device_commands (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    claimed_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                );

                -- Persisted lifecycle of sensor-based insight alerts so HivePal
                -- can show a *history* (alerts are otherwise recomputed live and
                -- never stored). One row per distinct alert occurrence: while an
                -- alert keeps firing the same row is updated (last_seen_at bumped);
                -- when it stops firing it is resolved (resolved_at set). A later
                -- recurrence of the same detector creates a fresh row. The partial
                -- unique index guarantees at most one *active* row per detector.
                CREATE TABLE IF NOT EXISTS insight_alerts (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    alert_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    channel INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    peak_severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                    source TEXT NOT NULL DEFAULT '',
                    window_start TIMESTAMPTZ,
                    window_end TIMESTAMPTZ,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    last_seen_at TIMESTAMPTZ NOT NULL,
                    resolved_at TIMESTAMPTZ,
                    update_count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS insight_alerts_active_uniq
                    ON insight_alerts (device_id, alert_key)
                    WHERE resolved_at IS NULL;

                CREATE INDEX IF NOT EXISTS insight_alerts_device_first_seen_idx
                    ON insight_alerts (device_id, first_seen_at DESC);
                """
            )
            conn.commit()


def ensure_device_config(
    device_id: str,
    claim_code: Optional[str] = None,
    firmware_version: Optional[str] = None,
    api_key: str = "",
    touch_last_seen: bool = False,
):
    """Upsert the devices/device_configs rows for a device.

    last_seen_at is updated only when touch_last_seen is True — i.e. only for a
    genuine measurement upload. It must NOT be bumped when the HivePal app reads
    or edits config on the device's behalf (the common case here), otherwise an
    open dashboard polling config keeps a long-offline device looking "online".
    Device config/firmware polls record contact via verify_device_key instead.
    """
    claim_hash = hash_claim_code(claim_code) if claim_code else None
    key_hash = hashlib.sha256(api_key.encode()).hexdigest() if len(api_key) >= 16 else None
    # Leave last_seen_at untouched for non-device-contact calls: NULL on first
    # insert, unchanged on conflict.
    insert_last_seen = "now()" if touch_last_seen else "NULL"
    update_last_seen = "last_seen_at = now()," if touch_last_seen else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO devices (device_id, claim_code_hash, api_key_hash, last_seen_at, last_firmware_version)
                VALUES (%s, %s, %s, {insert_last_seen}, %s)
                ON CONFLICT (device_id) DO UPDATE
                    SET {update_last_seen}
                        last_firmware_version = COALESCE(EXCLUDED.last_firmware_version, devices.last_firmware_version),
                        claim_code_hash = COALESCE(devices.claim_code_hash, EXCLUDED.claim_code_hash),
                        api_key_hash = COALESCE(devices.api_key_hash, EXCLUDED.api_key_hash)
                RETURNING api_key_hash;
                """,
                (device_id, claim_hash, key_hash, firmware_version),
            )
            row = cur.fetchone()
            if key_hash and row and row[0] and row[0] != key_hash:
                raise HTTPException(status_code=401, detail="API key does not match this device")
            cur.execute(
                """
                INSERT INTO device_configs (device_id) VALUES (%s)
                ON CONFLICT (device_id) DO NOTHING;
                """,
                (device_id,),
            )
            conn.commit()


def require_device_role(user_id: str, device_id: str, allowed_roles: list[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            r = cur.fetchone()
    if not r or r[0] not in allowed_roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions for this device")


def parse_version(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int("".join(ch for ch in p if ch.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts)


@app.on_event("startup")
def startup():
    db_pool.open()
    FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    start_insight_reconciler()
    mqtt_publisher.start()


@app.on_event("shutdown")
def shutdown():
    stop_insight_reconciler()
    mqtt_publisher.stop()
    db_pool.close()


@app.get("/health")
@limiter.exempt
def health():
    return {"status": "ok"}


# Column/value mapping for a single measurement row. Shared by the device-facing
# ingest endpoint (POST /api/v1/measurements) and the app-facing bulk SD import
# (POST /api/v1/app/devices/{device_id}/measurements/import) so the two paths can
# never drift apart. The statement deliberately omits a trailing clause: callers
# append " RETURNING id;" (single insert) or ";" (executemany bulk insert).
MEASUREMENT_INSERT_SQL = """
                INSERT INTO measurements (
                    device_id, measured_at, scale_1_weight_kg, scale_2_weight_kg,
                    hive_1_temp_c, hive_2_temp_c,
                    hive_1_humidity_percent, hive_2_humidity_percent, ambient_temp_c,
                    ambient_humidity_percent, battery_voltage, battery_soc_percent,
                    battery_alert, battery_monitor_ok, solar_monitor_ok,
                    solar_bus_voltage_v, solar_shunt_voltage_mv, solar_load_voltage_v,
                    solar_current_ma, solar_power_mw, network_transport,
                    cellular_ok, cellular_csq, calibration_mode, boot_count,
                    time_source, rssi_dbm, firmware_version, config_version, sd_ok,
                    rtc_ok, sht_ok, scale_1_raw, scale_2_raw,
                    mic_ok, mic_sample_rate_hz, mic_sample_frames,
                    mic_left_ok, mic_left_rms_dbfs, mic_left_peak_dbfs, mic_left_rms_normalized,
                    mic_right_ok, mic_right_rms_dbfs, mic_right_peak_dbfs, mic_right_rms_normalized,
                    mic_left_band_sub_bass_dbfs, mic_left_band_hum_dbfs, mic_left_band_piping_dbfs,
                    mic_left_band_stress_dbfs, mic_left_band_high_dbfs,
                    mic_right_band_sub_bass_dbfs, mic_right_band_hum_dbfs, mic_right_band_piping_dbfs,
                    mic_right_band_stress_dbfs, mic_right_band_high_dbfs,
                    bee_counter_1_ok, bee_counter_1_protocol_version, bee_counter_1_status_flags,
                    bee_counter_1_uptime_s, bee_counter_1_num_gates, bee_counter_1_gates_healthy,
                    bee_counter_1_total_in, bee_counter_1_total_out,
                    bee_counter_1_interval_in, bee_counter_1_interval_out,
                    bee_counter_1_glitch_count, bee_counter_1_busy_retries,
                    bee_counter_1_read_attempts, bee_counter_1_latch_succeeded,
                    bee_counter_2_ok, bee_counter_2_protocol_version, bee_counter_2_status_flags,
                    bee_counter_2_uptime_s, bee_counter_2_num_gates, bee_counter_2_gates_healthy,
                    bee_counter_2_total_in, bee_counter_2_total_out,
                    bee_counter_2_interval_in, bee_counter_2_interval_out,
                    bee_counter_2_glitch_count, bee_counter_2_busy_retries,
                    bee_counter_2_read_attempts, bee_counter_2_latch_succeeded,
                    accel_1_ok, accel_1_sample_rate_hz, accel_1_sample_count,
                    accel_1_range_g, accel_1_rms_mg, accel_1_peak_mg,
                    accel_1_band_swarm_mg, accel_1_band_fanning_mg, accel_1_band_activity_mg,
                    accel_2_ok, accel_2_sample_rate_hz, accel_2_sample_count,
                    accel_2_range_g, accel_2_rms_mg, accel_2_peak_mg,
                    accel_2_band_swarm_mg, accel_2_band_fanning_mg, accel_2_band_activity_mg,
                    ble_1_humidity_percent, ble_1_pressure_hpa,
                    ble_2_humidity_percent, ble_2_pressure_hpa,
                    hiveheart_1_frequency_hz, hiveheart_1_energy, hiveheart_1_peak, hiveheart_1_battery_v,
                    hiveheart_2_frequency_hz, hiveheart_2_energy, hiveheart_2_peak, hiveheart_2_battery_v,
                    hivescale_1_weight_kg, hivescale_1_raw_weight, hivescale_1_temp_c,
                    hivescale_1_humidity_percent, hivescale_1_pressure_hpa, hivescale_1_battery_v,
                    hivescale_2_weight_kg, hivescale_2_raw_weight, hivescale_2_temp_c,
                    hivescale_2_humidity_percent, hivescale_2_pressure_hpa, hivescale_2_battery_v,
                    raw_json
                )
                VALUES (
                    %(device_id)s, %(measured_at)s, %(scale_1_weight_kg)s,
                    %(scale_2_weight_kg)s, %(hive_1_temp_c)s, %(hive_2_temp_c)s,
                    %(hive_1_humidity_percent)s, %(hive_2_humidity_percent)s,
                    %(ambient_temp_c)s, %(ambient_humidity_percent)s,
                    %(battery_voltage)s, %(battery_soc_percent)s,
                    %(battery_alert)s, %(battery_monitor_ok)s, %(solar_monitor_ok)s,
                    %(solar_bus_voltage_v)s, %(solar_shunt_voltage_mv)s,
                    %(solar_load_voltage_v)s, %(solar_current_ma)s,
                    %(solar_power_mw)s, %(network_transport)s, %(cellular_ok)s,
                    %(cellular_csq)s, %(calibration_mode)s, %(boot_count)s,
                    %(time_source)s, %(rssi_dbm)s, %(firmware_version)s,
                    %(config_version)s, %(sd_ok)s, %(rtc_ok)s, %(sht_ok)s,
                    %(scale_1_raw)s, %(scale_2_raw)s,
                    %(mic_ok)s, %(mic_sample_rate_hz)s, %(mic_sample_frames)s,
                    %(mic_left_ok)s, %(mic_left_rms_dbfs)s, %(mic_left_peak_dbfs)s, %(mic_left_rms_normalized)s,
                    %(mic_right_ok)s, %(mic_right_rms_dbfs)s, %(mic_right_peak_dbfs)s, %(mic_right_rms_normalized)s,
                    %(mic_left_band_sub_bass_dbfs)s, %(mic_left_band_hum_dbfs)s, %(mic_left_band_piping_dbfs)s,
                    %(mic_left_band_stress_dbfs)s, %(mic_left_band_high_dbfs)s,
                    %(mic_right_band_sub_bass_dbfs)s, %(mic_right_band_hum_dbfs)s, %(mic_right_band_piping_dbfs)s,
                    %(mic_right_band_stress_dbfs)s, %(mic_right_band_high_dbfs)s,
                    %(bee_counter_1_ok)s, %(bee_counter_1_protocol_version)s, %(bee_counter_1_status_flags)s,
                    %(bee_counter_1_uptime_s)s, %(bee_counter_1_num_gates)s, %(bee_counter_1_gates_healthy)s,
                    %(bee_counter_1_total_in)s, %(bee_counter_1_total_out)s,
                    %(bee_counter_1_interval_in)s, %(bee_counter_1_interval_out)s,
                    %(bee_counter_1_glitch_count)s, %(bee_counter_1_busy_retries)s,
                    %(bee_counter_1_read_attempts)s, %(bee_counter_1_latch_succeeded)s,
                    %(bee_counter_2_ok)s, %(bee_counter_2_protocol_version)s, %(bee_counter_2_status_flags)s,
                    %(bee_counter_2_uptime_s)s, %(bee_counter_2_num_gates)s, %(bee_counter_2_gates_healthy)s,
                    %(bee_counter_2_total_in)s, %(bee_counter_2_total_out)s,
                    %(bee_counter_2_interval_in)s, %(bee_counter_2_interval_out)s,
                    %(bee_counter_2_glitch_count)s, %(bee_counter_2_busy_retries)s,
                    %(bee_counter_2_read_attempts)s, %(bee_counter_2_latch_succeeded)s,
                    %(accel_1_ok)s, %(accel_1_sample_rate_hz)s, %(accel_1_sample_count)s,
                    %(accel_1_range_g)s, %(accel_1_rms_mg)s, %(accel_1_peak_mg)s,
                    %(accel_1_band_swarm_mg)s, %(accel_1_band_fanning_mg)s, %(accel_1_band_activity_mg)s,
                    %(accel_2_ok)s, %(accel_2_sample_rate_hz)s, %(accel_2_sample_count)s,
                    %(accel_2_range_g)s, %(accel_2_rms_mg)s, %(accel_2_peak_mg)s,
                    %(accel_2_band_swarm_mg)s, %(accel_2_band_fanning_mg)s, %(accel_2_band_activity_mg)s,
                    %(ble_1_humidity_percent)s, %(ble_1_pressure_hpa)s,
                    %(ble_2_humidity_percent)s, %(ble_2_pressure_hpa)s,
                    %(hiveheart_1_frequency_hz)s, %(hiveheart_1_energy)s, %(hiveheart_1_peak)s, %(hiveheart_1_battery_v)s,
                    %(hiveheart_2_frequency_hz)s, %(hiveheart_2_energy)s, %(hiveheart_2_peak)s, %(hiveheart_2_battery_v)s,
                    %(hivescale_1_weight_kg)s, %(hivescale_1_raw_weight)s, %(hivescale_1_temp_c)s,
                    %(hivescale_1_humidity_percent)s, %(hivescale_1_pressure_hpa)s, %(hivescale_1_battery_v)s,
                    %(hivescale_2_weight_kg)s, %(hivescale_2_raw_weight)s, %(hivescale_2_temp_c)s,
                    %(hivescale_2_humidity_percent)s, %(hivescale_2_pressure_hpa)s, %(hivescale_2_battery_v)s,
                    %(raw_json)s
                )"""


def measurement_insert_params(payload: "MeasurementIn", measured_at: datetime) -> dict:
    """Build the named-parameter dict for ``MEASUREMENT_INSERT_SQL`` from a payload."""
    return {
        "device_id": payload.device_id,
        "measured_at": measured_at,
        "scale_1_weight_kg": payload.scale_1_weight_kg,
        "scale_2_weight_kg": payload.scale_2_weight_kg,
        "hive_1_temp_c": payload.hive_1_temp_c,
        "hive_2_temp_c": payload.hive_2_temp_c,
        "hive_1_humidity_percent": payload.hive_1_humidity_percent,
        "hive_2_humidity_percent": payload.hive_2_humidity_percent,
        "ambient_temp_c": payload.ambient_temp_c,
        "ambient_humidity_percent": payload.ambient_humidity_percent,
        "battery_voltage": payload.battery_voltage_v if payload.battery_voltage_v is not None else payload.battery_voltage,
        "battery_soc_percent": payload.battery_soc_percent,
        "battery_alert": payload.battery_alert,
        "battery_monitor_ok": payload.battery_monitor_ok,
        "solar_monitor_ok": payload.solar_monitor_ok,
        "solar_bus_voltage_v": payload.solar_bus_voltage_v,
        "solar_shunt_voltage_mv": payload.solar_shunt_voltage_mv,
        "solar_load_voltage_v": payload.solar_load_voltage_v,
        "solar_current_ma": payload.solar_current_ma,
        "solar_power_mw": payload.solar_power_mw,
        "network_transport": payload.network_transport,
        "cellular_ok": payload.cellular_ok,
        "cellular_csq": payload.cellular_csq,
        "calibration_mode": payload.calibration_mode,
        "boot_count": payload.boot_count,
        "time_source": payload.time_source,
        "rssi_dbm": payload.rssi_dbm,
        "firmware_version": payload.firmware_version,
        "config_version": payload.config_version,
        "sd_ok": payload.sd_ok,
        "rtc_ok": payload.rtc_ok,
        "sht_ok": payload.sht_ok,
        "scale_1_raw": payload.scale_1_raw,
        "scale_2_raw": payload.scale_2_raw,
        "mic_ok": payload.mic_ok,
        "mic_sample_rate_hz": payload.mic_sample_rate_hz,
        "mic_sample_frames": payload.mic_sample_frames,
        "mic_left_ok": payload.mic_left_ok,
        "mic_left_rms_dbfs": payload.mic_left_rms_dbfs,
        "mic_left_peak_dbfs": payload.mic_left_peak_dbfs,
        "mic_left_rms_normalized": payload.mic_left_rms_normalized,
        "mic_right_ok": payload.mic_right_ok,
        "mic_right_rms_dbfs": payload.mic_right_rms_dbfs,
        "mic_right_peak_dbfs": payload.mic_right_peak_dbfs,
        "mic_right_rms_normalized": payload.mic_right_rms_normalized,
        "mic_left_band_sub_bass_dbfs":  payload.mic_left_band_sub_bass_dbfs,
        "mic_left_band_hum_dbfs":       payload.mic_left_band_hum_dbfs,
        "mic_left_band_piping_dbfs":    payload.mic_left_band_piping_dbfs,
        "mic_left_band_stress_dbfs":    payload.mic_left_band_stress_dbfs,
        "mic_left_band_high_dbfs":      payload.mic_left_band_high_dbfs,
        "mic_right_band_sub_bass_dbfs": payload.mic_right_band_sub_bass_dbfs,
        "mic_right_band_hum_dbfs":      payload.mic_right_band_hum_dbfs,
        "mic_right_band_piping_dbfs":   payload.mic_right_band_piping_dbfs,
        "mic_right_band_stress_dbfs":   payload.mic_right_band_stress_dbfs,
        "mic_right_band_high_dbfs":     payload.mic_right_band_high_dbfs,
        "bee_counter_1_ok":               payload.bee_counter_1_ok,
        "bee_counter_1_protocol_version": payload.bee_counter_1_protocol_version,
        "bee_counter_1_status_flags":     payload.bee_counter_1_status_flags,
        "bee_counter_1_uptime_s":         payload.bee_counter_1_uptime_s,
        "bee_counter_1_num_gates":        payload.bee_counter_1_num_gates,
        "bee_counter_1_gates_healthy":    payload.bee_counter_1_gates_healthy,
        "bee_counter_1_total_in":         payload.bee_counter_1_total_in,
        "bee_counter_1_total_out":        payload.bee_counter_1_total_out,
        "bee_counter_1_interval_in":      payload.bee_counter_1_interval_in,
        "bee_counter_1_interval_out":     payload.bee_counter_1_interval_out,
        "bee_counter_1_glitch_count":     payload.bee_counter_1_glitch_count,
        "bee_counter_1_busy_retries":     payload.bee_counter_1_busy_retries,
        "bee_counter_1_read_attempts":    payload.bee_counter_1_read_attempts,
        "bee_counter_1_latch_succeeded":  payload.bee_counter_1_latch_succeeded,
        "bee_counter_2_ok":               payload.bee_counter_2_ok,
        "bee_counter_2_protocol_version": payload.bee_counter_2_protocol_version,
        "bee_counter_2_status_flags":     payload.bee_counter_2_status_flags,
        "bee_counter_2_uptime_s":         payload.bee_counter_2_uptime_s,
        "bee_counter_2_num_gates":        payload.bee_counter_2_num_gates,
        "bee_counter_2_gates_healthy":    payload.bee_counter_2_gates_healthy,
        "bee_counter_2_total_in":         payload.bee_counter_2_total_in,
        "bee_counter_2_total_out":        payload.bee_counter_2_total_out,
        "bee_counter_2_interval_in":      payload.bee_counter_2_interval_in,
        "bee_counter_2_interval_out":     payload.bee_counter_2_interval_out,
        "bee_counter_2_glitch_count":     payload.bee_counter_2_glitch_count,
        "bee_counter_2_busy_retries":     payload.bee_counter_2_busy_retries,
        "bee_counter_2_read_attempts":    payload.bee_counter_2_read_attempts,
        "bee_counter_2_latch_succeeded":  payload.bee_counter_2_latch_succeeded,
        "accel_1_ok":               payload.accel_1_ok,
        "accel_1_sample_rate_hz":   payload.accel_1_sample_rate_hz,
        "accel_1_sample_count":     payload.accel_1_sample_count,
        "accel_1_range_g":          payload.accel_1_range_g,
        "accel_1_rms_mg":           payload.accel_1_rms_mg,
        "accel_1_peak_mg":          payload.accel_1_peak_mg,
        "accel_1_band_swarm_mg":    payload.accel_1_band_swarm_mg,
        "accel_1_band_fanning_mg":  payload.accel_1_band_fanning_mg,
        "accel_1_band_activity_mg": payload.accel_1_band_activity_mg,
        "accel_2_ok":               payload.accel_2_ok,
        "accel_2_sample_rate_hz":   payload.accel_2_sample_rate_hz,
        "accel_2_sample_count":     payload.accel_2_sample_count,
        "accel_2_range_g":          payload.accel_2_range_g,
        "accel_2_rms_mg":           payload.accel_2_rms_mg,
        "accel_2_peak_mg":          payload.accel_2_peak_mg,
        "accel_2_band_swarm_mg":    payload.accel_2_band_swarm_mg,
        "accel_2_band_fanning_mg":  payload.accel_2_band_fanning_mg,
        "accel_2_band_activity_mg": payload.accel_2_band_activity_mg,
        "ble_1_humidity_percent":   payload.ble_1_humidity_percent,
        "ble_1_pressure_hpa":       payload.ble_1_pressure_hpa,
        "ble_2_humidity_percent":   payload.ble_2_humidity_percent,
        "ble_2_pressure_hpa":       payload.ble_2_pressure_hpa,
        "hiveheart_1_frequency_hz":     payload.hiveheart_1_frequency_hz,
        "hiveheart_1_energy":           payload.hiveheart_1_energy,
        "hiveheart_1_peak":             payload.hiveheart_1_peak,
        "hiveheart_1_battery_v":        payload.hiveheart_1_battery_v,
        "hiveheart_2_frequency_hz":     payload.hiveheart_2_frequency_hz,
        "hiveheart_2_energy":           payload.hiveheart_2_energy,
        "hiveheart_2_peak":             payload.hiveheart_2_peak,
        "hiveheart_2_battery_v":        payload.hiveheart_2_battery_v,
        "hivescale_1_weight_kg":        payload.hivescale_1_weight_kg,
        "hivescale_1_raw_weight":       payload.hivescale_1_raw_weight,
        "hivescale_1_temp_c":           payload.hivescale_1_temp_c,
        "hivescale_1_humidity_percent": payload.hivescale_1_humidity_percent,
        "hivescale_1_pressure_hpa":     payload.hivescale_1_pressure_hpa,
        "hivescale_1_battery_v":        payload.hivescale_1_battery_v,
        "hivescale_2_weight_kg":        payload.hivescale_2_weight_kg,
        "hivescale_2_raw_weight":       payload.hivescale_2_raw_weight,
        "hivescale_2_temp_c":           payload.hivescale_2_temp_c,
        "hivescale_2_humidity_percent": payload.hivescale_2_humidity_percent,
        "hivescale_2_pressure_hpa":     payload.hivescale_2_pressure_hpa,
        "hivescale_2_battery_v":        payload.hivescale_2_battery_v,
        "raw_json": psycopg.types.json.Jsonb(payload.model_dump(mode="json", exclude={"claim_code"})),
    }


# ── Multi-hive ingest helpers ────────────────────────────────────────────────

HIVE_READINGS_INSERT_SQL = """
    INSERT INTO hive_readings (
        measurement_id, device_id, measured_at, hive_index, name,
        weight_kg, raw_weight, scale_source, temp_c, temp_source, humidity_percent,
        accel_ok, accel_sample_count, accel_range_g, accel_rms_mg, accel_peak_mg,
        accel_band_swarm_mg, accel_band_fanning_mg, accel_band_activity_mg,
        ble_present, ble_sensor_type, ble_humidity_percent, ble_pressure_hpa,
        ble_battery_percent, ble_rssi_dbm,
        bee_counter_ok, bee_counter_total_in, bee_counter_total_out,
        bee_counter_interval_in, bee_counter_interval_out, raw_json
    ) VALUES (
        %(measurement_id)s, %(device_id)s, %(measured_at)s, %(hive_index)s, %(name)s,
        %(weight_kg)s, %(raw_weight)s, %(scale_source)s, %(temp_c)s, %(temp_source)s, %(humidity_percent)s,
        %(accel_ok)s, %(accel_sample_count)s, %(accel_range_g)s, %(accel_rms_mg)s, %(accel_peak_mg)s,
        %(accel_band_swarm_mg)s, %(accel_band_fanning_mg)s, %(accel_band_activity_mg)s,
        %(ble_present)s, %(ble_sensor_type)s, %(ble_humidity_percent)s, %(ble_pressure_hpa)s,
        %(ble_battery_percent)s, %(ble_rssi_dbm)s,
        %(bee_counter_ok)s, %(bee_counter_total_in)s, %(bee_counter_total_out)s,
        %(bee_counter_interval_in)s, %(bee_counter_interval_out)s, %(raw_json)s
    )
    ON CONFLICT (measurement_id, hive_index) DO NOTHING
"""


def _hive_reading_row_params(device_id: str, h: "HiveReadingIn",
                             measurement_id: int, measured_at: datetime) -> dict:
    a, b, c = h.accel, h.ble, h.bee_counter
    return {
        "measurement_id": measurement_id,
        "device_id": device_id,
        "measured_at": measured_at,
        "hive_index": h.index,
        "name": h.name,
        "weight_kg": h.weight_kg,
        "raw_weight": h.raw_weight,
        "scale_source": h.scale_source,
        "temp_c": h.temp_c,
        "temp_source": h.temp_source,
        "humidity_percent": h.humidity_percent,
        "accel_ok": a.ok if a else None,
        "accel_sample_count": a.sample_count if a else None,
        "accel_range_g": a.range_g if a else None,
        "accel_rms_mg": a.rms_mg if a else None,
        "accel_peak_mg": a.peak_mg if a else None,
        "accel_band_swarm_mg": a.band_swarm_mg if a else None,
        "accel_band_fanning_mg": a.band_fanning_mg if a else None,
        "accel_band_activity_mg": a.band_activity_mg if a else None,
        "ble_present": b.present if b else None,
        "ble_sensor_type": b.sensor_type if b else None,
        "ble_humidity_percent": b.humidity_percent if b else None,
        "ble_pressure_hpa": b.pressure_hpa if b else None,
        "ble_battery_percent": b.battery_percent if b else None,
        "ble_rssi_dbm": b.rssi_dbm if b else None,
        "bee_counter_ok": c.ok if c else None,
        "bee_counter_total_in": c.total_in if c else None,
        "bee_counter_total_out": c.total_out if c else None,
        "bee_counter_interval_in": c.interval_in if c else None,
        "bee_counter_interval_out": c.interval_out if c else None,
        "raw_json": psycopg.types.json.Jsonb(h.model_dump(mode="json", exclude_none=True)),
    }


def overlay_legacy_hive_columns(params: dict, payload: "MeasurementIn") -> None:
    """When a payload carries the hives[] array (new firmware), copy hives 1–2 onto
    the legacy measurements columns the column-based read / insights / temp-comp
    still use. Only fills a column the flat payload left None, so an explicit flat
    field always wins."""
    if not payload.hives:
        return
    by_index = {h.index: h for h in payload.hives}
    for n in (1, 2):
        h = by_index.get(n)
        if h is None:
            continue

        def put(col, val):
            if val is not None and params.get(col) is None:
                params[col] = val

        put(f"scale_{n}_weight_kg", h.weight_kg)
        put(f"scale_{n}_raw", h.raw_weight)
        put(f"hive_{n}_temp_c", h.temp_c)
        put(f"hive_{n}_humidity_percent", h.humidity_percent)
        if h.accel:
            put(f"accel_{n}_ok", h.accel.ok)
            put(f"accel_{n}_sample_count", h.accel.sample_count)
            put(f"accel_{n}_range_g", h.accel.range_g)
            put(f"accel_{n}_rms_mg", h.accel.rms_mg)
            put(f"accel_{n}_peak_mg", h.accel.peak_mg)
            put(f"accel_{n}_band_swarm_mg", h.accel.band_swarm_mg)
            put(f"accel_{n}_band_fanning_mg", h.accel.band_fanning_mg)
            put(f"accel_{n}_band_activity_mg", h.accel.band_activity_mg)
        if h.ble:
            put(f"ble_{n}_humidity_percent", h.ble.humidity_percent)
            put(f"ble_{n}_pressure_hpa", h.ble.pressure_hpa)
        if h.bee_counter:
            put(f"bee_counter_{n}_ok", h.bee_counter.ok)
            put(f"bee_counter_{n}_total_in", h.bee_counter.total_in)
            put(f"bee_counter_{n}_total_out", h.bee_counter.total_out)
            put(f"bee_counter_{n}_interval_in", h.bee_counter.interval_in)
            put(f"bee_counter_{n}_interval_out", h.bee_counter.interval_out)


def insert_hive_readings(cur, payload: "MeasurementIn",
                         measurement_id: int, measured_at: datetime) -> None:
    """Fan a payload's hives[] array into hive_readings rows. No-op for legacy
    (flat-field) payloads — the read path synthesizes hives 1–2 from the legacy
    columns for those."""
    if not payload.hives:
        return
    seen: set[int] = set()
    rows = []
    for h in payload.hives:
        if h.index in seen:
            continue
        seen.add(h.index)
        rows.append(_hive_reading_row_params(payload.device_id, h, measurement_id, measured_at))
    if rows:
        cur.executemany(HIVE_READINGS_INSERT_SQL, rows)


@app.post("/api/v1/measurements")
def create_measurement(payload: MeasurementIn, x_api_key: str = Header(default="")):
    if len(x_api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    now = datetime.now(timezone.utc)
    # A device whose RTC and NTP have both failed sends the 1970 epoch fallback
    # (or, more generally, a clock far from reality). Storing that verbatim
    # freezes "last data" in the dashboard even though uploads keep arriving, so
    # we fall back to the server clock for any missing or implausible timestamp.
    measured_at = payload.timestamp
    if measured_at is None or not (
        measured_at.year >= MIN_PLAUSIBLE_YEAR and measured_at <= now + timedelta(days=1)
    ):
        if measured_at is not None:
            logger.warning(
                "Ignoring implausible client timestamp %s from device %s; using server time",
                measured_at.isoformat(), payload.device_id,
            )
        measured_at = now
    ensure_device_config(
        payload.device_id, payload.claim_code, payload.firmware_version, x_api_key,
        touch_last_seen=True,
    )
    params = measurement_insert_params(payload, measured_at)
    overlay_legacy_hive_columns(params, payload)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(MEASUREMENT_INSERT_SQL + " RETURNING id;", params)
            new_id = cur.fetchone()[0]
            # Fan the hives[] array (if any) into the normalized child table.
            insert_hive_readings(cur, payload, new_id, measured_at)
            conn.commit()
    # Mirror the reading to MQTT (Home Assistant etc.) when the bridge is enabled.
    # This is purely additive and fail-soft — it never affects the stored result.
    # Pass the device's temp-compensation config so the live payload carries
    # scale_{1,2}_weight_kg_compensated alongside the raw weights; the lookup is
    # skipped entirely when the bridge is disabled.
    if mqtt_publisher.is_active():
        mqtt_tempco = load_tempco_configs([payload.device_id]).get(payload.device_id)
        mqtt_publisher.publish_measurement(
            payload.device_id,
            payload.model_dump(mode="json", exclude_none=True, exclude={"claim_code"}),
            measured_at,
            tempco=mqtt_tempco,
        )
    return {"status": "ok", "id": new_id, "measured_at": measured_at.isoformat()}


@app.post(
    "/api/v1/app/devices/{device_id}/measurements/import",
    dependencies=[Depends(require_hivepal_service_key)],
)
def import_measurements(
    device_id: str,
    payload: MeasurementImportIn,
    user_id: str = Depends(require_user_id),
):
    """Bulk-import measurements parsed from a device's SD card backup.

    Called by the HivePal web backend after a beekeeper uploads the NDJSON/TAR
    they pulled from the scale in AP mode. The device must already be claimed by
    a user with owner/admin access — we never auto-create devices from uploaded
    data, since the file's ``device_id`` is attacker-controllable and ownership
    is established through the claim-code flow.

    Re-importing the same file is a no-op: ``(device_id, measured_at)`` is treated
    as the natural key and existing rows are skipped, so duplicates inside the
    file and rows already stored are both counted and ignored.
    """
    require_device_role(user_id, device_id, ["owner", "admin"])

    # Force the path device_id onto every row so a file cannot smuggle readings
    # in under a different device the caller may not own.
    prepared: list[tuple[datetime, MeasurementIn]] = []
    for measurement in payload.measurements:
        measured_at = measurement.timestamp or datetime.now(timezone.utc)
        prepared.append(
            (measured_at, measurement.model_copy(update={"device_id": device_id}))
        )

    # Keep the first record seen for each timestamp (file duplicates are identical).
    record_by_key: dict[datetime, MeasurementIn] = {}
    for measured_at, measurement in prepared:
        record_by_key.setdefault(measured_at, measurement)

    received = len(payload.measurements)
    inserted = 0
    duplicates = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            keys = [measured_at for measured_at, _ in prepared]
            existing: set = set()
            unique_keys = list(record_by_key.keys())
            if unique_keys:
                cur.execute(
                    "SELECT measured_at FROM measurements "
                    "WHERE device_id = %s AND measured_at = ANY(%s);",
                    (device_id, unique_keys),
                )
                existing = {row[0] for row in cur.fetchall()}

            new_keys, duplicates = split_new_and_duplicate(keys, existing)
            if new_keys:
                def _params(key):
                    p = measurement_insert_params(record_by_key[key], key)
                    overlay_legacy_hive_columns(p, record_by_key[key])
                    return p

                cur.executemany(
                    MEASUREMENT_INSERT_SQL + ";",
                    [_params(key) for key in new_keys],
                )
                inserted = len(new_keys)

                # Fan any multi-hive payloads into hive_readings. executemany gives
                # no ids back, so look up the rows we just inserted by their key.
                hive_keys = [k for k in new_keys if record_by_key[k].hives]
                if hive_keys:
                    cur.execute(
                        "SELECT measured_at, id FROM measurements "
                        "WHERE device_id = %s AND measured_at = ANY(%s);",
                        (device_id, hive_keys),
                    )
                    id_by_key = {row[0]: row[1] for row in cur.fetchall()}
                    for key in hive_keys:
                        mid = id_by_key.get(key)
                        if mid is not None:
                            insert_hive_readings(cur, record_by_key[key], mid, key)
            conn.commit()

    return {
        "status": "ok",
        "device_id": device_id,
        "received": received,
        "inserted": inserted,
        "duplicates": duplicates,
    }


# ---------------------------------------------------------------------------
# Indices for measurement_row_to_dict (keep in sync with SELECT below):
#
#  0  id                        17  scale_1_raw
#  1  device_id                 18  scale_2_raw
#  2  measured_at               19  battery_soc_percent
#  3  received_at               20  battery_alert
#  4  scale_1_weight_kg         21  battery_monitor_ok
#  5  scale_2_weight_kg         22  solar_monitor_ok
#  6  hive_1_temp_c             23  solar_bus_voltage_v
#  7  hive_2_temp_c             24  solar_shunt_voltage_mv
#  8  ambient_temp_c            25  solar_load_voltage_v
#  9  ambient_humidity_percent  26  solar_current_ma
# 10  battery_voltage           27  solar_power_mw
# 11  rssi_dbm                  28  network_transport
# 12  firmware_version          29  cellular_ok
# 13  config_version            30  cellular_csq
# 14  sd_ok                     31  calibration_mode
# 15  rtc_ok                    32  boot_count
# 16  sht_ok                    33  time_source
#                               34  mic_ok
#                               35  mic_sample_rate_hz
#                               36  mic_sample_frames
#                               37  mic_left_ok
#                               38  mic_left_rms_dbfs
#                               39  mic_left_peak_dbfs
#                               40  mic_left_rms_normalized
#                               41  mic_right_ok
#                               42  mic_right_rms_dbfs
#                               43  mic_right_peak_dbfs
#                               44  mic_right_rms_normalized
#                               45  mic_left_band_sub_bass_dbfs
#                               46  mic_left_band_hum_dbfs
#                               47  mic_left_band_piping_dbfs
#                               48  mic_left_band_stress_dbfs
#                               49  mic_left_band_high_dbfs
#                               50  mic_right_band_sub_bass_dbfs
#                               51  mic_right_band_hum_dbfs
#                               52  mic_right_band_piping_dbfs
#                               53  mic_right_band_stress_dbfs
#                               54  mic_right_band_high_dbfs
#                               55  bee_counter_1_ok
#                               56  bee_counter_1_protocol_version
#                               57  bee_counter_1_status_flags
#                               58  bee_counter_1_uptime_s
#                               59  bee_counter_1_num_gates
#                               60  bee_counter_1_gates_healthy
#                               61  bee_counter_1_total_in
#                               62  bee_counter_1_total_out
#                               63  bee_counter_1_interval_in
#                               64  bee_counter_1_interval_out
#                               65  bee_counter_1_glitch_count
#                               66  bee_counter_1_busy_retries
#                               67  bee_counter_1_read_attempts
#                               68  bee_counter_1_latch_succeeded
#                               69  bee_counter_2_ok
#                               70  bee_counter_2_protocol_version
#                               71  bee_counter_2_status_flags
#                               72  bee_counter_2_uptime_s
#                               73  bee_counter_2_num_gates
#                               74  bee_counter_2_gates_healthy
#                               75  bee_counter_2_total_in
#                               76  bee_counter_2_total_out
#                               77  bee_counter_2_interval_in
#                               78  bee_counter_2_interval_out
#                               79  bee_counter_2_glitch_count
#                               80  bee_counter_2_busy_retries
#                               81  bee_counter_2_read_attempts
#                               82  bee_counter_2_latch_succeeded
#                               83  accel_1_ok
#                               84  accel_1_sample_rate_hz
#                               85  accel_1_sample_count
#                               86  accel_1_range_g
#                               87  accel_1_rms_mg
#                               88  accel_1_peak_mg
#                               89  accel_1_band_swarm_mg
#                               90  accel_1_band_fanning_mg
#                               91  accel_1_band_activity_mg
#                               92  accel_2_ok
#                               93  accel_2_sample_rate_hz
#                               94  accel_2_sample_count
#                               95  accel_2_range_g
#                               96  accel_2_rms_mg
#                               97  accel_2_peak_mg
#                               98  accel_2_band_swarm_mg
#                               99  accel_2_band_fanning_mg
#                              100  accel_2_band_activity_mg
#                              101  ble_1_humidity_percent
#                              102  ble_1_pressure_hpa
#                              103  ble_2_humidity_percent
#                              104  ble_2_pressure_hpa
#                              105  hive_1_humidity_percent
#                              106  hive_2_humidity_percent
#  (107..130 hiveheart/hivescale fields — see SELECT order below)
#                              131  ble_1_battery_percent
#                              132  ble_1_rssi_dbm
#                              133  ble_2_battery_percent
#                              134  ble_2_rssi_dbm
#                              135  ble_1_firmware_version
#                              136  ble_2_firmware_version
# ---------------------------------------------------------------------------

MEASUREMENT_SELECT_COLUMNS = """
    id, device_id, measured_at, received_at, scale_1_weight_kg,
    scale_2_weight_kg, hive_1_temp_c, hive_2_temp_c,
    ambient_temp_c, ambient_humidity_percent,
    COALESCE(battery_voltage, NULLIF(raw_json->>'battery_voltage_v', '')::double precision, NULLIF(raw_json->>'battery_voltage', '')::double precision) AS battery_voltage,
    rssi_dbm, firmware_version, config_version, sd_ok, rtc_ok, sht_ok,
    scale_1_raw, scale_2_raw,
    COALESCE(battery_soc_percent, NULLIF(raw_json->>'battery_soc_percent', '')::double precision) AS battery_soc_percent,
    COALESCE(battery_alert, NULLIF(raw_json->>'battery_alert', '')::boolean) AS battery_alert,
    COALESCE(battery_monitor_ok, NULLIF(raw_json->>'battery_monitor_ok', '')::boolean) AS battery_monitor_ok,
    COALESCE(solar_monitor_ok, NULLIF(raw_json->>'solar_monitor_ok', '')::boolean) AS solar_monitor_ok,
    COALESCE(solar_bus_voltage_v, NULLIF(raw_json->>'solar_bus_voltage_v', '')::double precision) AS solar_bus_voltage_v,
    COALESCE(solar_shunt_voltage_mv, NULLIF(raw_json->>'solar_shunt_voltage_mv', '')::double precision) AS solar_shunt_voltage_mv,
    COALESCE(solar_load_voltage_v, NULLIF(raw_json->>'solar_load_voltage_v', '')::double precision) AS solar_load_voltage_v,
    COALESCE(solar_current_ma, NULLIF(raw_json->>'solar_current_ma', '')::double precision) AS solar_current_ma,
    COALESCE(solar_power_mw, NULLIF(raw_json->>'solar_power_mw', '')::double precision) AS solar_power_mw,
    COALESCE(network_transport, raw_json->>'network_transport') AS network_transport,
    COALESCE(cellular_ok, NULLIF(raw_json->>'cellular_ok', '')::boolean) AS cellular_ok,
    COALESCE(cellular_csq, NULLIF(raw_json->>'cellular_csq', '')::integer) AS cellular_csq,
    COALESCE(calibration_mode, NULLIF(raw_json->>'calibration_mode', '')::boolean) AS calibration_mode,
    COALESCE(boot_count, NULLIF(raw_json->>'boot_count', '')::bigint) AS boot_count,
    COALESCE(time_source, raw_json->>'time_source') AS time_source,
    COALESCE(mic_ok,                   NULLIF(raw_json->>'mic_ok',                   '')::boolean)          AS mic_ok,
    COALESCE(mic_sample_rate_hz,       NULLIF(raw_json->>'mic_sample_rate_hz',       '')::integer)          AS mic_sample_rate_hz,
    COALESCE(mic_sample_frames,        NULLIF(raw_json->>'mic_sample_frames',        '')::integer)          AS mic_sample_frames,
    COALESCE(mic_left_ok,              NULLIF(raw_json->>'mic_left_ok',              '')::boolean)          AS mic_left_ok,
    COALESCE(mic_left_rms_dbfs,        NULLIF(raw_json->>'mic_left_rms_dbfs',        '')::double precision) AS mic_left_rms_dbfs,
    COALESCE(mic_left_peak_dbfs,       NULLIF(raw_json->>'mic_left_peak_dbfs',       '')::double precision) AS mic_left_peak_dbfs,
    COALESCE(mic_left_rms_normalized,  NULLIF(raw_json->>'mic_left_rms_normalized',  '')::double precision) AS mic_left_rms_normalized,
    COALESCE(mic_right_ok,             NULLIF(raw_json->>'mic_right_ok',             '')::boolean)          AS mic_right_ok,
    COALESCE(mic_right_rms_dbfs,       NULLIF(raw_json->>'mic_right_rms_dbfs',       '')::double precision) AS mic_right_rms_dbfs,
    COALESCE(mic_right_peak_dbfs,      NULLIF(raw_json->>'mic_right_peak_dbfs',      '')::double precision) AS mic_right_peak_dbfs,
    COALESCE(mic_right_rms_normalized, NULLIF(raw_json->>'mic_right_rms_normalized', '')::double precision) AS mic_right_rms_normalized,
    COALESCE(mic_left_band_sub_bass_dbfs,  NULLIF(raw_json->>'mic_left_band_sub_bass_dbfs',  '')::double precision) AS mic_left_band_sub_bass_dbfs,
    COALESCE(mic_left_band_hum_dbfs,       NULLIF(raw_json->>'mic_left_band_hum_dbfs',       '')::double precision) AS mic_left_band_hum_dbfs,
    COALESCE(mic_left_band_piping_dbfs,    NULLIF(raw_json->>'mic_left_band_piping_dbfs',    '')::double precision) AS mic_left_band_piping_dbfs,
    COALESCE(mic_left_band_stress_dbfs,    NULLIF(raw_json->>'mic_left_band_stress_dbfs',    '')::double precision) AS mic_left_band_stress_dbfs,
    COALESCE(mic_left_band_high_dbfs,      NULLIF(raw_json->>'mic_left_band_high_dbfs',      '')::double precision) AS mic_left_band_high_dbfs,
    COALESCE(mic_right_band_sub_bass_dbfs, NULLIF(raw_json->>'mic_right_band_sub_bass_dbfs', '')::double precision) AS mic_right_band_sub_bass_dbfs,
    COALESCE(mic_right_band_hum_dbfs,      NULLIF(raw_json->>'mic_right_band_hum_dbfs',      '')::double precision) AS mic_right_band_hum_dbfs,
    COALESCE(mic_right_band_piping_dbfs,   NULLIF(raw_json->>'mic_right_band_piping_dbfs',   '')::double precision) AS mic_right_band_piping_dbfs,
    COALESCE(mic_right_band_stress_dbfs,   NULLIF(raw_json->>'mic_right_band_stress_dbfs',   '')::double precision) AS mic_right_band_stress_dbfs,
    COALESCE(mic_right_band_high_dbfs,     NULLIF(raw_json->>'mic_right_band_high_dbfs',     '')::double precision) AS mic_right_band_high_dbfs,
    COALESCE(bee_counter_1_ok,                NULLIF(raw_json->>'bee_counter_1_ok',                '')::boolean) AS bee_counter_1_ok,
    COALESCE(bee_counter_1_protocol_version,  NULLIF(raw_json->>'bee_counter_1_protocol_version',  '')::integer) AS bee_counter_1_protocol_version,
    COALESCE(bee_counter_1_status_flags,      NULLIF(raw_json->>'bee_counter_1_status_flags',      '')::integer) AS bee_counter_1_status_flags,
    COALESCE(bee_counter_1_uptime_s,          NULLIF(raw_json->>'bee_counter_1_uptime_s',          '')::integer) AS bee_counter_1_uptime_s,
    COALESCE(bee_counter_1_num_gates,         NULLIF(raw_json->>'bee_counter_1_num_gates',         '')::integer) AS bee_counter_1_num_gates,
    COALESCE(bee_counter_1_gates_healthy,     NULLIF(raw_json->>'bee_counter_1_gates_healthy',     '')::integer) AS bee_counter_1_gates_healthy,
    COALESCE(bee_counter_1_total_in,          NULLIF(raw_json->>'bee_counter_1_total_in',          '')::bigint)  AS bee_counter_1_total_in,
    COALESCE(bee_counter_1_total_out,         NULLIF(raw_json->>'bee_counter_1_total_out',         '')::bigint)  AS bee_counter_1_total_out,
    COALESCE(bee_counter_1_interval_in,       NULLIF(raw_json->>'bee_counter_1_interval_in',       '')::bigint)  AS bee_counter_1_interval_in,
    COALESCE(bee_counter_1_interval_out,      NULLIF(raw_json->>'bee_counter_1_interval_out',      '')::bigint)  AS bee_counter_1_interval_out,
    COALESCE(bee_counter_1_glitch_count,      NULLIF(raw_json->>'bee_counter_1_glitch_count',      '')::integer) AS bee_counter_1_glitch_count,
    COALESCE(bee_counter_1_busy_retries,      NULLIF(raw_json->>'bee_counter_1_busy_retries',      '')::integer) AS bee_counter_1_busy_retries,
    COALESCE(bee_counter_1_read_attempts,     NULLIF(raw_json->>'bee_counter_1_read_attempts',     '')::integer) AS bee_counter_1_read_attempts,
    COALESCE(bee_counter_1_latch_succeeded,   NULLIF(raw_json->>'bee_counter_1_latch_succeeded',   '')::boolean) AS bee_counter_1_latch_succeeded,
    COALESCE(bee_counter_2_ok,                NULLIF(raw_json->>'bee_counter_2_ok',                '')::boolean) AS bee_counter_2_ok,
    COALESCE(bee_counter_2_protocol_version,  NULLIF(raw_json->>'bee_counter_2_protocol_version',  '')::integer) AS bee_counter_2_protocol_version,
    COALESCE(bee_counter_2_status_flags,      NULLIF(raw_json->>'bee_counter_2_status_flags',      '')::integer) AS bee_counter_2_status_flags,
    COALESCE(bee_counter_2_uptime_s,          NULLIF(raw_json->>'bee_counter_2_uptime_s',          '')::integer) AS bee_counter_2_uptime_s,
    COALESCE(bee_counter_2_num_gates,         NULLIF(raw_json->>'bee_counter_2_num_gates',         '')::integer) AS bee_counter_2_num_gates,
    COALESCE(bee_counter_2_gates_healthy,     NULLIF(raw_json->>'bee_counter_2_gates_healthy',     '')::integer) AS bee_counter_2_gates_healthy,
    COALESCE(bee_counter_2_total_in,          NULLIF(raw_json->>'bee_counter_2_total_in',          '')::bigint)  AS bee_counter_2_total_in,
    COALESCE(bee_counter_2_total_out,         NULLIF(raw_json->>'bee_counter_2_total_out',         '')::bigint)  AS bee_counter_2_total_out,
    COALESCE(bee_counter_2_interval_in,       NULLIF(raw_json->>'bee_counter_2_interval_in',       '')::bigint)  AS bee_counter_2_interval_in,
    COALESCE(bee_counter_2_interval_out,      NULLIF(raw_json->>'bee_counter_2_interval_out',      '')::bigint)  AS bee_counter_2_interval_out,
    COALESCE(bee_counter_2_glitch_count,      NULLIF(raw_json->>'bee_counter_2_glitch_count',      '')::integer) AS bee_counter_2_glitch_count,
    COALESCE(bee_counter_2_busy_retries,      NULLIF(raw_json->>'bee_counter_2_busy_retries',      '')::integer) AS bee_counter_2_busy_retries,
    COALESCE(bee_counter_2_read_attempts,     NULLIF(raw_json->>'bee_counter_2_read_attempts',     '')::integer) AS bee_counter_2_read_attempts,
    COALESCE(bee_counter_2_latch_succeeded,   NULLIF(raw_json->>'bee_counter_2_latch_succeeded',   '')::boolean) AS bee_counter_2_latch_succeeded,
    COALESCE(accel_1_ok,               NULLIF(raw_json->>'accel_1_ok',               '')::boolean)          AS accel_1_ok,
    COALESCE(accel_1_sample_rate_hz,   NULLIF(raw_json->>'accel_1_sample_rate_hz',   '')::integer)          AS accel_1_sample_rate_hz,
    COALESCE(accel_1_sample_count,     NULLIF(raw_json->>'accel_1_sample_count',     '')::integer)          AS accel_1_sample_count,
    COALESCE(accel_1_range_g,          NULLIF(raw_json->>'accel_1_range_g',          '')::integer)          AS accel_1_range_g,
    COALESCE(accel_1_rms_mg,           NULLIF(raw_json->>'accel_1_rms_mg',           '')::double precision) AS accel_1_rms_mg,
    COALESCE(accel_1_peak_mg,          NULLIF(raw_json->>'accel_1_peak_mg',          '')::double precision) AS accel_1_peak_mg,
    COALESCE(accel_1_band_swarm_mg,    NULLIF(raw_json->>'accel_1_band_swarm_mg',    '')::double precision) AS accel_1_band_swarm_mg,
    COALESCE(accel_1_band_fanning_mg,  NULLIF(raw_json->>'accel_1_band_fanning_mg',  '')::double precision) AS accel_1_band_fanning_mg,
    COALESCE(accel_1_band_activity_mg, NULLIF(raw_json->>'accel_1_band_activity_mg', '')::double precision) AS accel_1_band_activity_mg,
    COALESCE(accel_2_ok,               NULLIF(raw_json->>'accel_2_ok',               '')::boolean)          AS accel_2_ok,
    COALESCE(accel_2_sample_rate_hz,   NULLIF(raw_json->>'accel_2_sample_rate_hz',   '')::integer)          AS accel_2_sample_rate_hz,
    COALESCE(accel_2_sample_count,     NULLIF(raw_json->>'accel_2_sample_count',     '')::integer)          AS accel_2_sample_count,
    COALESCE(accel_2_range_g,          NULLIF(raw_json->>'accel_2_range_g',          '')::integer)          AS accel_2_range_g,
    COALESCE(accel_2_rms_mg,           NULLIF(raw_json->>'accel_2_rms_mg',           '')::double precision) AS accel_2_rms_mg,
    COALESCE(accel_2_peak_mg,          NULLIF(raw_json->>'accel_2_peak_mg',          '')::double precision) AS accel_2_peak_mg,
    COALESCE(accel_2_band_swarm_mg,    NULLIF(raw_json->>'accel_2_band_swarm_mg',    '')::double precision) AS accel_2_band_swarm_mg,
    COALESCE(accel_2_band_fanning_mg,  NULLIF(raw_json->>'accel_2_band_fanning_mg',  '')::double precision) AS accel_2_band_fanning_mg,
    COALESCE(accel_2_band_activity_mg, NULLIF(raw_json->>'accel_2_band_activity_mg', '')::double precision) AS accel_2_band_activity_mg,
    COALESCE(ble_1_humidity_percent, NULLIF(raw_json->>'ble_1_humidity_percent', '')::double precision) AS ble_1_humidity_percent,
    COALESCE(ble_1_pressure_hpa,     NULLIF(raw_json->>'ble_1_pressure_hpa',     '')::double precision) AS ble_1_pressure_hpa,
    COALESCE(ble_2_humidity_percent, NULLIF(raw_json->>'ble_2_humidity_percent', '')::double precision) AS ble_2_humidity_percent,
    COALESCE(ble_2_pressure_hpa,     NULLIF(raw_json->>'ble_2_pressure_hpa',     '')::double precision) AS ble_2_pressure_hpa,
    COALESCE(hive_1_humidity_percent, NULLIF(raw_json->>'hive_1_humidity_percent', '')::double precision) AS hive_1_humidity_percent,
    COALESCE(hive_2_humidity_percent, NULLIF(raw_json->>'hive_2_humidity_percent', '')::double precision) AS hive_2_humidity_percent,
    COALESCE(hiveheart_1_frequency_hz,     NULLIF(raw_json->>'hiveheart_1_frequency_hz',     '')::double precision) AS hiveheart_1_frequency_hz,
    COALESCE(hiveheart_1_energy,           NULLIF(raw_json->>'hiveheart_1_energy',           '')::integer)          AS hiveheart_1_energy,
    COALESCE(hiveheart_1_peak,             NULLIF(raw_json->>'hiveheart_1_peak',             '')::integer)          AS hiveheart_1_peak,
    COALESCE(hiveheart_1_battery_v,        NULLIF(raw_json->>'hiveheart_1_battery_v',        '')::double precision) AS hiveheart_1_battery_v,
    NULLIF(raw_json->>'hiveheart_1_temp_c',           '')::double precision AS hiveheart_1_temp_c,
    NULLIF(raw_json->>'hiveheart_1_humidity_percent', '')::double precision AS hiveheart_1_humidity_percent,
    COALESCE(hiveheart_2_frequency_hz,     NULLIF(raw_json->>'hiveheart_2_frequency_hz',     '')::double precision) AS hiveheart_2_frequency_hz,
    COALESCE(hiveheart_2_energy,           NULLIF(raw_json->>'hiveheart_2_energy',           '')::integer)          AS hiveheart_2_energy,
    COALESCE(hiveheart_2_peak,             NULLIF(raw_json->>'hiveheart_2_peak',             '')::integer)          AS hiveheart_2_peak,
    COALESCE(hiveheart_2_battery_v,        NULLIF(raw_json->>'hiveheart_2_battery_v',        '')::double precision) AS hiveheart_2_battery_v,
    NULLIF(raw_json->>'hiveheart_2_temp_c',           '')::double precision AS hiveheart_2_temp_c,
    NULLIF(raw_json->>'hiveheart_2_humidity_percent', '')::double precision AS hiveheart_2_humidity_percent,
    COALESCE(hivescale_1_weight_kg,        NULLIF(raw_json->>'hivescale_1_weight_kg',        '')::double precision) AS hivescale_1_weight_kg,
    COALESCE(hivescale_1_raw_weight,       NULLIF(raw_json->>'hivescale_1_raw_weight',       '')::bigint)           AS hivescale_1_raw_weight,
    COALESCE(hivescale_1_temp_c,           NULLIF(raw_json->>'hivescale_1_temp_c',           '')::double precision) AS hivescale_1_temp_c,
    COALESCE(hivescale_1_humidity_percent, NULLIF(raw_json->>'hivescale_1_humidity_percent', '')::double precision) AS hivescale_1_humidity_percent,
    COALESCE(hivescale_1_pressure_hpa,     NULLIF(raw_json->>'hivescale_1_pressure_hpa',     '')::double precision) AS hivescale_1_pressure_hpa,
    COALESCE(hivescale_1_battery_v,        NULLIF(raw_json->>'hivescale_1_battery_v',        '')::double precision) AS hivescale_1_battery_v,
    COALESCE(hivescale_2_weight_kg,        NULLIF(raw_json->>'hivescale_2_weight_kg',        '')::double precision) AS hivescale_2_weight_kg,
    COALESCE(hivescale_2_raw_weight,       NULLIF(raw_json->>'hivescale_2_raw_weight',       '')::bigint)           AS hivescale_2_raw_weight,
    COALESCE(hivescale_2_temp_c,           NULLIF(raw_json->>'hivescale_2_temp_c',           '')::double precision) AS hivescale_2_temp_c,
    COALESCE(hivescale_2_humidity_percent, NULLIF(raw_json->>'hivescale_2_humidity_percent', '')::double precision) AS hivescale_2_humidity_percent,
    COALESCE(hivescale_2_pressure_hpa,     NULLIF(raw_json->>'hivescale_2_pressure_hpa',     '')::double precision) AS hivescale_2_pressure_hpa,
    COALESCE(hivescale_2_battery_v,        NULLIF(raw_json->>'hivescale_2_battery_v',        '')::double precision) AS hivescale_2_battery_v,
    NULLIF(raw_json->>'ble_1_battery_percent', '')::integer AS ble_1_battery_percent,
    NULLIF(raw_json->>'ble_1_rssi_dbm',        '')::integer AS ble_1_rssi_dbm,
    NULLIF(raw_json->>'ble_2_battery_percent', '')::integer AS ble_2_battery_percent,
    NULLIF(raw_json->>'ble_2_rssi_dbm',        '')::integer AS ble_2_rssi_dbm,
    raw_json->>'ble_1_firmware_version' AS ble_1_firmware_version,
    raw_json->>'ble_2_firmware_version' AS ble_2_firmware_version
"""


# Slim column set for the dashboard's time-series (chart) endpoint.
#
# The dashboard charts read only the ~30 fields below from the measurements
# series (the metric cards read the separate `latest` row, which keeps the full
# column set). Every column here is a real, typed table column — crucially, none
# fall back to `raw_json`. The full MEASUREMENT_SELECT_COLUMNS wraps almost every
# field in COALESCE(col, raw_json->>'…'), which forces Postgres to read and
# de-TOAST the large raw_json blob for every row even though the typed columns
# are populated for current firmware. Selecting only typed columns here avoids
# that de-TOAST entirely, which is the dominant per-row cost on the chart query.
#
# Trade-off: rows from older deployments that stored a charted field *only* in
# raw_json (and never in its typed column) will read NULL for that field on the
# charts. Weight/temperature/humidity/battery/RSSI are first-class ingest columns
# so this affects only edge-case legacy rows; the `latest`/app paths still carry
# the raw_json fallbacks.
CHART_MEASUREMENT_COLUMNS = """
    id, device_id, measured_at,
    scale_1_weight_kg, scale_2_weight_kg,
    hive_1_temp_c, hive_2_temp_c, ambient_temp_c,
    ambient_humidity_percent, hive_1_humidity_percent, hive_2_humidity_percent,
    ble_1_pressure_hpa, ble_2_pressure_hpa,
    hivescale_1_pressure_hpa, hivescale_2_pressure_hpa,
    mic_left_rms_dbfs, mic_right_rms_dbfs, mic_left_peak_dbfs, mic_right_peak_dbfs,
    battery_soc_percent, battery_voltage, solar_power_mw, solar_current_ma,
    rssi_dbm,
    bee_counter_1_ok, bee_counter_1_total_in, bee_counter_1_total_out,
    bee_counter_1_interval_in, bee_counter_1_interval_out,
    bee_counter_2_ok, bee_counter_2_total_in, bee_counter_2_total_out,
    bee_counter_2_interval_in, bee_counter_2_interval_out
"""


def measurement_row_to_dict(r):
    return {
        "id": r[0],
        "device_id": r[1],
        "measured_at": r[2],
        "received_at": r[3],
        "scale_1_weight_kg": r[4],
        "scale_2_weight_kg": r[5],
        "hive_1_temp_c": r[6],
        "hive_2_temp_c": r[7],
        "ambient_temp_c": r[8],
        "ambient_humidity_percent": r[9],
        "battery_voltage": r[10],
        "battery_voltage_v": r[10],
        "rssi_dbm": r[11],
        "firmware_version": r[12],
        "config_version": r[13],
        "sd_ok": r[14],
        "rtc_ok": r[15],
        "sht_ok": r[16],
        "scale_1_raw": r[17],
        "scale_2_raw": r[18],
        # Temperature-compensated weights. Default to the raw weight and
        # tempco_applied=False; attach_temperature_compensation() overrides
        # these per device when a coefficient is configured and enabled.
        "scale_1_weight_kg_compensated": r[4],
        "scale_2_weight_kg_compensated": r[5],
        "tempco_applied": False,
        "battery_soc_percent": r[19],
        "battery_alert": r[20],
        "battery_monitor_ok": r[21],
        "solar_monitor_ok": r[22],
        "solar_bus_voltage_v": r[23],
        "solar_shunt_voltage_mv": r[24],
        "solar_load_voltage_v": r[25],
        "solar_current_ma": r[26],
        "solar_power_mw": r[27],
        "network_transport": r[28],
        "cellular_ok": r[29],
        "cellular_csq": r[30],
        "calibration_mode": r[31],
        "boot_count": r[32],
        "time_source": r[33],
        # mic telemetry
        "mic_ok": r[34],
        "mic_sample_rate_hz": r[35],
        "mic_sample_frames": r[36],
        "mic_left_ok": r[37],
        "mic_left_rms_dbfs": r[38],
        "mic_left_peak_dbfs": r[39],
        "mic_left_rms_normalized": r[40],
        "mic_right_ok": r[41],
        "mic_right_rms_dbfs": r[42],
        "mic_right_peak_dbfs": r[43],
        "mic_right_rms_normalized": r[44],
        # fft frequency band energy
        "mic_left_band_sub_bass_dbfs":  r[45],
        "mic_left_band_hum_dbfs":       r[46],
        "mic_left_band_piping_dbfs":    r[47],
        "mic_left_band_stress_dbfs":    r[48],
        "mic_left_band_high_dbfs":      r[49],
        "mic_right_band_sub_bass_dbfs": r[50],
        "mic_right_band_hum_dbfs":      r[51],
        "mic_right_band_piping_dbfs":   r[52],
        "mic_right_band_stress_dbfs":   r[53],
        "mic_right_band_high_dbfs":     r[54],
        # bee counter (per-hive entrance counters)
        "bee_counter_1_ok":                r[55],
        "bee_counter_1_protocol_version":  r[56],
        "bee_counter_1_status_flags":      r[57],
        "bee_counter_1_uptime_s":          r[58],
        "bee_counter_1_num_gates":         r[59],
        "bee_counter_1_gates_healthy":     r[60],
        "bee_counter_1_total_in":          r[61],
        "bee_counter_1_total_out":         r[62],
        "bee_counter_1_interval_in":       r[63],
        "bee_counter_1_interval_out":      r[64],
        "bee_counter_1_glitch_count":      r[65],
        "bee_counter_1_busy_retries":      r[66],
        "bee_counter_1_read_attempts":     r[67],
        "bee_counter_1_latch_succeeded":   r[68],
        "bee_counter_2_ok":                r[69],
        "bee_counter_2_protocol_version":  r[70],
        "bee_counter_2_status_flags":      r[71],
        "bee_counter_2_uptime_s":          r[72],
        "bee_counter_2_num_gates":         r[73],
        "bee_counter_2_gates_healthy":     r[74],
        "bee_counter_2_total_in":          r[75],
        "bee_counter_2_total_out":         r[76],
        "bee_counter_2_interval_in":       r[77],
        "bee_counter_2_interval_out":      r[78],
        "bee_counter_2_glitch_count":      r[79],
        "bee_counter_2_busy_retries":      r[80],
        "bee_counter_2_read_attempts":     r[81],
        "bee_counter_2_latch_succeeded":   r[82],
        # accelerometer (per-hive vibration, mg)
        "accel_1_ok":                r[83],
        "accel_1_sample_rate_hz":    r[84],
        "accel_1_sample_count":      r[85],
        "accel_1_range_g":           r[86],
        "accel_1_rms_mg":            r[87],
        "accel_1_peak_mg":           r[88],
        "accel_1_band_swarm_mg":     r[89],
        "accel_1_band_fanning_mg":   r[90],
        "accel_1_band_activity_mg":  r[91],
        "accel_2_ok":                r[92],
        "accel_2_sample_rate_hz":    r[93],
        "accel_2_sample_count":      r[94],
        "accel_2_range_g":           r[95],
        "accel_2_rms_mg":            r[96],
        "accel_2_peak_mg":           r[97],
        "accel_2_band_swarm_mg":     r[98],
        "accel_2_band_fanning_mg":   r[99],
        "accel_2_band_activity_mg":  r[100],
        # HolyIot 25015 in-hive BLE sensor (per hive)
        "ble_1_humidity_percent":    r[101],
        "ble_1_pressure_hpa":        r[102],
        "ble_2_humidity_percent":    r[103],
        "ble_2_pressure_hpa":        r[104],
        # In-hive relative humidity (mirrors hive_N_temp_c; appended last).
        "hive_1_humidity_percent":   r[105],
        "hive_2_humidity_percent":   r[106],
        # beehivemonitoring.com GATT sensors (HiveHeart / HiveScale).
        # NOTE: hiveheart_N_temp_c / hiveheart_N_humidity_percent are SELECTed
        # (they are kept "independently visible" per the MeasurementIn comment)
        # and so MUST be mapped here — omitting them shifts every positional
        # index below them and silently mis-reads hiveheart_2_* / hivescale_*.
        "hiveheart_1_frequency_hz":     r[107],
        "hiveheart_1_energy":           r[108],
        "hiveheart_1_peak":             r[109],
        "hiveheart_1_battery_v":        r[110],
        "hiveheart_1_temp_c":           r[111],
        "hiveheart_1_humidity_percent": r[112],
        "hiveheart_2_frequency_hz":     r[113],
        "hiveheart_2_energy":           r[114],
        "hiveheart_2_peak":             r[115],
        "hiveheart_2_battery_v":        r[116],
        "hiveheart_2_temp_c":           r[117],
        "hiveheart_2_humidity_percent": r[118],
        "hivescale_1_weight_kg":        r[119],
        "hivescale_1_raw_weight":       r[120],
        "hivescale_1_temp_c":           r[121],
        "hivescale_1_humidity_percent": r[122],
        "hivescale_1_pressure_hpa":     r[123],
        "hivescale_1_battery_v":        r[124],
        "hivescale_2_weight_kg":        r[125],
        "hivescale_2_raw_weight":       r[126],
        "hivescale_2_temp_c":           r[127],
        "hivescale_2_humidity_percent": r[128],
        "hivescale_2_pressure_hpa":     r[129],
        "hivescale_2_battery_v":        r[130],
        # HolyIot 25015 / HiveInside in-hive BLE sensor battery + link RSSI.
        # Stored in raw_json on ingest; surfaced here so the app can show a
        # per-sensor charge level (HolyIot reports %, HiveInside reports % too —
        # 0% while USB-powered). Appended last in the SELECT.
        "ble_1_battery_percent":        r[131],
        "ble_1_rssi_dbm":               r[132],
        "ble_2_battery_percent":        r[133],
        "ble_2_rssi_dbm":               r[134],
        "ble_1_firmware_version":       r[135],
        "ble_2_firmware_version":       r[136],
    }


def load_tempco_configs(device_ids) -> dict:
    """Fetch the temperature-compensation config for a set of devices.

    Returns ``{device_id: (source, ref_temp_c, scale1_coeff, scale2_coeff)}``
    for devices that have compensation *enabled* with at least one non-zero
    coefficient. Devices absent from the map are left uncompensated.
    """
    ids = [d for d in {d for d in device_ids} if d]
    if not ids:
        return {}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id, tempco_source, tempco_ref_temp_c,
                           scale1_tempco_kg_per_c, scale2_tempco_kg_per_c
                    FROM device_configs
                    WHERE device_id = ANY(%s)
                      AND tempco_enabled
                      AND (scale1_tempco_kg_per_c <> 0 OR scale2_tempco_kg_per_c <> 0);
                    """,
                    (ids,),
                )
                rows = cur.fetchall()
    except psycopg.errors.UndefinedColumn:
        # The temp-compensation columns are missing — migration 006 has not been
        # applied (e.g. the process was hot-reloaded without re-running init_db).
        # Degrade to serving raw, uncompensated weights rather than 500-ing the
        # whole measurement-read endpoint, which would blank "last data" in the
        # dashboard. Reads keep working; compensation resumes once the migration
        # runs.
        logger.warning(
            "device_configs temp-compensation columns missing; "
            "serving uncompensated weights. Apply migration "
            "006_loadcell_temp_compensation.sql (or restart to run init_db)."
        )
        return {}
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def attach_temperature_compensation(measurements: list[dict]) -> list[dict]:
    """Fill in the compensated-weight fields on serialized measurement dicts.

    Looks up each device's coefficient once (a single batched query), applies an
    EMA to the temperature series (per device, in time order) to damp transient
    lag errors, then applies the first-order correction from server/tempcomp.py.
    Rows whose device has no enabled coefficient keep the defaults set in
    measurement_row_to_dict (raw weight, tempco_applied=False).
    """
    if not measurements:
        return measurements
    cfgs = load_tempco_configs(m["device_id"] for m in measurements)
    if not cfgs:
        return measurements

    # Group by device so EMA runs over each device's time-ordered sequence.
    from collections import defaultdict
    by_device: dict = defaultdict(list)
    for m in measurements:
        by_device[m["device_id"]].append(m)

    for device_id, rows in by_device.items():
        cfg = cfgs.get(device_id)
        if not cfg:
            continue
        source, ref_temp, c1, c2 = cfg
        field = TEMP_SOURCE_FIELD.get(source, "ambient_temp_c")

        rows.sort(key=lambda m: m["measured_at"])
        smoothed_temps = ema_temperatures([m.get(field) for m in rows])

        for m, temp in zip(rows, smoothed_temps):
            m["scale_1_weight_kg_compensated"] = compensate_weight(
                m["scale_1_weight_kg"], temp, ref_temp, c1
            )
            m["scale_2_weight_kg_compensated"] = compensate_weight(
                m["scale_2_weight_kg"], temp, ref_temp, c2
            )
            m["tempco_applied"] = True

    return measurements


def difference_bee_counter_intervals(measurements: list[dict]) -> list[dict]:
    """Backfill NULL per-interval bee-counter counts from the lifetime totals.

    The wireless/BLE (HiveTraffic) bee-counter path is *totals-only*: the device
    reports the monotonic lifetime ``total_in``/``total_out`` but no per-poll
    ``interval_in``/``interval_out`` — it performs no I2C ``CMD_LATCH`` handshake,
    so those interval columns are stored NULL (see
    2026-easy-bee-counter/docs/ble-mode.md and bee_counter_client's totals_only
    path). Display clients (HivePal) chart the interval fields directly, so a
    BLE-sourced counter would otherwise read as zero traffic for every row.

    Here we derive each missing interval as ``total_now - total_prev`` between
    consecutive readings of the same channel — the same differencing the insight
    engine already does in insights._extract_counter_series, applied to the read
    APIs so the chart panel matches the insight card. Rules:

    * Only NULL intervals are filled; the wired I2C path, which already reports a
      device interval, is left untouched.
    * Rows where the channel was unreachable (``bee_counter_{ch}_ok`` falsy) are
      skipped and never advance the baseline, so a dead counter can't inject a
      bogus delta.
    * A counter reboot or uint32 wrap (``total_now < total_prev``) is attributed
      as ``total_now`` so the restart from zero is not seen as a huge negative.

    The dicts are mutated in place; the caller's ordering is preserved (we sort a
    shallow copy by ``measured_at`` only to compute the deltas).
    """
    ordered = sorted(
        (m for m in measurements if m.get("measured_at") is not None),
        key=lambda m: m["measured_at"],
    )
    for ch in (1, 2):
        ok_key = f"bee_counter_{ch}_ok"
        for direction in ("in", "out"):
            interval_key = f"bee_counter_{ch}_interval_{direction}"
            total_key = f"bee_counter_{ch}_total_{direction}"
            prev_total = None
            for m in ordered:
                if not m.get(ok_key):
                    continue
                cur_total = m.get(total_key)
                if m.get(interval_key) is None and cur_total is not None:
                    if prev_total is None:
                        pass  # no baseline yet — can't attribute the first reading
                    elif cur_total >= prev_total:
                        m[interval_key] = cur_total - prev_total
                    else:
                        m[interval_key] = cur_total  # reboot / wrap
                if cur_total is not None:
                    prev_total = cur_total
    return measurements


HIVE_READINGS_SELECT = """
    SELECT measurement_id, hive_index, name, weight_kg, raw_weight, scale_source,
           temp_c, temp_source, humidity_percent,
           accel_ok, accel_sample_count, accel_range_g, accel_rms_mg, accel_peak_mg,
           accel_band_swarm_mg, accel_band_fanning_mg, accel_band_activity_mg,
           ble_present, ble_sensor_type, ble_humidity_percent, ble_pressure_hpa,
           ble_battery_percent, ble_rssi_dbm,
           bee_counter_ok, bee_counter_total_in, bee_counter_total_out,
           bee_counter_interval_in, bee_counter_interval_out, raw_json
    FROM hive_readings WHERE measurement_id = ANY(%s)
    ORDER BY measurement_id, hive_index
"""


def _json_obj(v) -> dict:
    if isinstance(v, dict):
        return dict(v)
    if isinstance(v, str) and v.strip():
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _merge_nested_sensor(out: dict, key: str, values: dict) -> None:
    existing = out.get(key) if isinstance(out.get(key), dict) else {}
    merged = dict(existing)
    for k, v in values.items():
        if v is not None or k not in merged:
            merged[k] = v
    if any(v is not None for v in merged.values()):
        out[key] = merged


def _hive_reading_row_to_dict(r) -> dict:
    # Preserve the original per-hive raw_json payload so nested telemetry that
    # does not have dedicated hive_readings columns, such as HiveHeart acoustic
    # fields and HiveScale/HiveHeart RSSI, survives the read API. Column values
    # still override the canonical fields below because they are the indexed /
    # migration-friendly representation.
    out = _json_obj(r[28])
    out.update({
        "index": r[1], "name": r[2], "weight_kg": r[3], "raw_weight": r[4],
        "scale_source": r[5], "temp_c": r[6], "temp_source": r[7],
        "humidity_percent": r[8],
    })
    _merge_nested_sensor(out, "accel", {
        "ok": r[9], "sample_count": r[10], "range_g": r[11],
        "rms_mg": r[12], "peak_mg": r[13], "band_swarm_mg": r[14],
        "band_fanning_mg": r[15], "band_activity_mg": r[16],
    })
    _merge_nested_sensor(out, "ble", {
        "present": r[17], "sensor_type": r[18], "humidity_percent": r[19],
        "pressure_hpa": r[20], "battery_percent": r[21], "rssi_dbm": r[22],
    })
    _merge_nested_sensor(out, "bee_counter", {
        "ok": r[23], "total_in": r[24], "total_out": r[25],
        "interval_in": r[26], "interval_out": r[27],
    })
    return out


def _synthesize_hives_from_flat(m: dict) -> list[dict]:
    """For historical / old-firmware rows with no hive_readings, build the hives[]
    array (hives 1–2) from the legacy measurements columns so the API shape is
    uniform regardless of firmware version."""
    hives = []
    for n in (1, 2):
        accel = {
            "ok": m.get(f"accel_{n}_ok"), "rms_mg": m.get(f"accel_{n}_rms_mg"),
            "peak_mg": m.get(f"accel_{n}_peak_mg"),
            "band_swarm_mg": m.get(f"accel_{n}_band_swarm_mg"),
            "band_fanning_mg": m.get(f"accel_{n}_band_fanning_mg"),
            "band_activity_mg": m.get(f"accel_{n}_band_activity_mg"),
        }
        ble = {"humidity_percent": m.get(f"ble_{n}_humidity_percent"),
               "pressure_hpa": m.get(f"ble_{n}_pressure_hpa")}
        bc = {"ok": m.get(f"bee_counter_{n}_ok"),
              "total_in": m.get(f"bee_counter_{n}_total_in"),
              "total_out": m.get(f"bee_counter_{n}_total_out"),
              "interval_in": m.get(f"bee_counter_{n}_interval_in"),
              "interval_out": m.get(f"bee_counter_{n}_interval_out")}
        hiveheart = {"temp_c": m.get(f"hiveheart_{n}_temp_c"),
                     "humidity_percent": m.get(f"hiveheart_{n}_humidity_percent"),
                     "frequency_hz": m.get(f"hiveheart_{n}_frequency_hz"),
                     "energy": m.get(f"hiveheart_{n}_energy"),
                     "peak": m.get(f"hiveheart_{n}_peak"),
                     "battery_v": m.get(f"hiveheart_{n}_battery_v"),
                     "rssi_dbm": m.get(f"hiveheart_{n}_rssi_dbm")}
        hivescale = {"weight_kg": m.get(f"hivescale_{n}_weight_kg"),
                     "raw_weight": m.get(f"hivescale_{n}_raw_weight"),
                     "temp_c": m.get(f"hivescale_{n}_temp_c"),
                     "humidity_percent": m.get(f"hivescale_{n}_humidity_percent"),
                     "pressure_hpa": m.get(f"hivescale_{n}_pressure_hpa"),
                     "battery_v": m.get(f"hivescale_{n}_battery_v"),
                     "rssi_dbm": m.get(f"hivescale_{n}_rssi_dbm")}
        fields = [m.get(f"scale_{n}_weight_kg"), m.get(f"hive_{n}_temp_c"),
                  m.get(f"hive_{n}_humidity_percent"), m.get(f"scale_{n}_raw")]
        if not any(v is not None for v in fields + list(accel.values())
                   + list(ble.values()) + list(bc.values())
                   + list(hiveheart.values()) + list(hivescale.values())):
            continue
        hive = {
            "index": n, "weight_kg": m.get(f"scale_{n}_weight_kg"),
            "raw_weight": m.get(f"scale_{n}_raw"), "temp_c": m.get(f"hive_{n}_temp_c"),
            "humidity_percent": m.get(f"hive_{n}_humidity_percent"),
            "accel": accel, "ble": ble, "bee_counter": bc,
        }
        if any(v is not None for v in hiveheart.values()):
            hive["hiveheart"] = hiveheart
        if any(v is not None for v in hivescale.values()):
            hive["hivescale"] = hivescale
        hives.append(hive)
    return hives


def _flatten_hive_to_measurement(m: dict, h: dict) -> None:
    """Synthesize flat per-hive keys from hives[].

    The nested ``hives[]`` object is the canonical multi-hive representation, but
    several app/dashboard paths still consume column-shaped keys. Generate those
    aliases from each hive row so every configured hive (up to MAX_HIVES on the
    firmware side) exposes the same read-response shape. Existing legacy columns
    for hives 1–2 are left intact; these synthesized keys are read-time aliases
    and do not require adding unbounded database columns.
    """
    n = h.get("index")
    if not n:
        return

    def put(key: str, value) -> None:
        if value is not None:
            m[key] = value

    hh, hs = h.get("hiveheart") or {}, h.get("hivescale") or {}
    if hh:
        put(f"hiveheart_{n}_temp_c", hh.get("temp_c"))
        put(f"hiveheart_{n}_humidity_percent", hh.get("humidity_percent"))
        put(f"hiveheart_{n}_frequency_hz", hh.get("frequency_hz"))
        put(f"hiveheart_{n}_energy", hh.get("energy"))
        put(f"hiveheart_{n}_peak", hh.get("peak"))
        put(f"hiveheart_{n}_battery_v", hh.get("battery_v"))
        put(f"hiveheart_{n}_rssi_dbm", hh.get("rssi_dbm"))
        if hh.get("fft") is not None:
            m[f"hiveheart_{n}_fft"] = hh.get("fft")
    if hs:
        put(f"hivescale_{n}_weight_kg", hs.get("weight_kg"))
        put(f"hivescale_{n}_raw_weight", hs.get("raw_weight"))
        put(f"hivescale_{n}_temp_c", hs.get("temp_c"))
        put(f"hivescale_{n}_humidity_percent", hs.get("humidity_percent"))
        put(f"hivescale_{n}_pressure_hpa", hs.get("pressure_hpa"))
        put(f"hivescale_{n}_battery_v", hs.get("battery_v"))
        put(f"hivescale_{n}_rssi_dbm", hs.get("rssi_dbm"))

    a = h.get("accel") or {}
    b = h.get("ble") or {}
    c = h.get("bee_counter") or {}
    mic = h.get("mic") or {}

    # Canonical per-hive aliases. These are especially important for hives 3–18,
    # which have no fixed measurements-table columns. For hives 1–2 they are
    # harmless aliases next to the historical scale_1/2 and hive_1/2 columns.
    put(f"scale_{n}_weight_kg", h.get("weight_kg"))
    put(f"scale_{n}_raw", h.get("raw_weight"))
    put(f"hive_{n}_temp_c", h.get("temp_c"))
    put(f"hive_{n}_humidity_percent", h.get("humidity_percent"))

    # Vibration aliases from hives[n].accel. Include the full HiveInside band set
    # and metadata, not only the old two-hive subset.
    if a:
        put(f"accel_{n}_ok", a.get("ok"))
        put(f"accel_{n}_sample_rate_hz", a.get("sample_rate_hz"))
        put(f"accel_{n}_sample_count", a.get("sample_count"))
        put(f"accel_{n}_range_g", a.get("range_g"))
        put(f"accel_{n}_rms_mg", a.get("rms_mg"))
        put(f"accel_{n}_peak_mg", a.get("peak_mg"))
        put(f"accel_{n}_band_swarm_mg", a.get("band_swarm_mg"))
        put(f"accel_{n}_band_fanning_mg", a.get("band_fanning_mg"))
        put(f"accel_{n}_band_activity_mg", a.get("band_activity_mg"))

    # BLE aliases from hives[n].ble. This covers HolyIot/Ruuvi/HiveInside fields
    # for all hive numbers, including RSSI, battery and HiveInside firmware.
    if b:
        put(f"ble_{n}_present", b.get("present"))
        put(f"ble_{n}_sensor_type", b.get("sensor_type"))
        put(f"ble_{n}_humidity_percent", b.get("humidity_percent"))
        put(f"ble_{n}_pressure_hpa", b.get("pressure_hpa"))
        put(f"ble_{n}_accel_x_mg", b.get("accel_x_mg"))
        put(f"ble_{n}_accel_y_mg", b.get("accel_y_mg"))
        put(f"ble_{n}_accel_z_mg", b.get("accel_z_mg"))
        put(f"ble_{n}_battery_percent", b.get("battery_percent"))
        put(f"ble_{n}_rssi_dbm", b.get("rssi_dbm"))
        put(f"ble_{n}_firmware_version", b.get("firmware_version"))

    # HiveInside acoustic aliases from hives[n].mic. The historical flat mic
    # schema is stereo-only (mic_left/mic_right), so expose per-hive mic_N_*
    # aliases for hive 3 and beyond while also making hives 1–2 addressable by
    # number. Nested hives[n].mic remains the canonical multi-hive field.
    if mic:
        put(f"mic_{n}_ok", mic.get("ok"))
        put(f"mic_{n}_rms_dbfs", mic.get("rms_dbfs"))
        put(f"mic_{n}_peak_dbfs", mic.get("peak_dbfs"))
        put(f"mic_{n}_rms_normalized", mic.get("rms_normalized"))
        put(f"mic_{n}_band_sub_bass_dbfs", mic.get("band_sub_bass_dbfs"))
        put(f"mic_{n}_band_hum_dbfs", mic.get("band_hum_dbfs"))
        put(f"mic_{n}_band_piping_dbfs", mic.get("band_piping_dbfs"))
        put(f"mic_{n}_band_stress_dbfs", mic.get("band_stress_dbfs"))
        put(f"mic_{n}_band_high_dbfs", mic.get("band_high_dbfs"))

    if c:
        put(f"bee_counter_{n}_ok", c.get("ok"))
        put(f"bee_counter_{n}_total_in", c.get("total_in"))
        put(f"bee_counter_{n}_total_out", c.get("total_out"))
        put(f"bee_counter_{n}_interval_in", c.get("interval_in"))
        put(f"bee_counter_{n}_interval_out", c.get("interval_out"))


def attach_hive_readings(measurements: list[dict]) -> list[dict]:
    """Attach a per-hive ``hives`` array (from hive_readings, else synthesized from
    the legacy columns) to each measurement, and synthesize flat per-hive keys for
    hives beyond 2 so the existing column-based read consumers see all hives."""
    if not measurements:
        return measurements
    ids = [m["id"] for m in measurements if m.get("id") is not None]
    by_mid: dict[int, list[dict]] = {}
    if ids:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(HIVE_READINGS_SELECT, (ids,))
                for r in cur.fetchall():
                    by_mid.setdefault(r[0], []).append(_hive_reading_row_to_dict(r))
    for m in measurements:
        hive_rows = by_mid.get(m.get("id"))
        m["hives"] = hive_rows if hive_rows else _synthesize_hives_from_flat(m)
        for h in m["hives"]:
            _flatten_hive_to_measurement(m, h)
    return measurements


def serialize_measurements(rows) -> list[dict]:
    """Map raw DB rows to API dicts, attach temperature compensation and the
    per-hive readings (hives[] array + flat keys for hives 3–18)."""
    measurements = attach_temperature_compensation(
        [measurement_row_to_dict(r) for r in rows]
    )
    measurements = difference_bee_counter_intervals(measurements)
    return attach_hive_readings(measurements)


def serialize_chart_measurements(cur) -> list[dict]:
    """Map the slim CHART_MEASUREMENT_COLUMNS result set to API dicts.

    Builds dicts by column name (rather than the positional mapping used for the
    full row), seeds the compensated-weight defaults that measurement_row_to_dict
    would normally set, then runs the same compensation / bee-counter / per-hive
    transforms so the chart payload stays consistent with the full read APIs.
    """
    cols = [c.name for c in cur.description]
    measurements: list[dict] = []
    for r in cur.fetchall():
        m = dict(zip(cols, r))
        # Mirror measurement_row_to_dict: default compensated weights to the raw
        # weight so the keys always exist; attach_temperature_compensation()
        # overrides them when a coefficient is configured.
        m["scale_1_weight_kg_compensated"] = m.get("scale_1_weight_kg")
        m["scale_2_weight_kg_compensated"] = m.get("scale_2_weight_kg")
        m["tempco_applied"] = False
        measurements.append(m)
    measurements = attach_temperature_compensation(measurements)
    measurements = difference_bee_counter_intervals(measurements)
    return attach_hive_readings(measurements)


def execute_measurement_query(cur, columns, where_parts, params, limit, max_points):
    """Run a measurements query (newest first), optionally time-decimated.

    When ``max_points`` is set and the matched row count exceeds it, the rows are
    thinned to ~max_points evenly-spaced samples using a stride computed in SQL.
    The stride is derived from a cheap, index-only window pass over (id,
    measured_at); the heavy column list is then materialised only for the rows
    that survive the stride, so a wide range no longer de-TOASTs raw_json (or
    ships JSON) for thousands of rows just to draw a few hundred chart pixels.
    The newest row (rn = 1) is always kept. ``limit`` still applies as a hard cap.
    """
    where_sql = " AND ".join(where_parts)
    if max_points and max_points > 0:
        cur.execute(
            f"""
            WITH filtered AS (
                SELECT id,
                       row_number() OVER (ORDER BY measured_at DESC) AS rn,
                       count(*) OVER () AS total
                FROM measurements
                WHERE {where_sql}
            ),
            picked AS (
                SELECT id AS pid FROM filtered
                WHERE (rn - 1) % GREATEST(1, (total + %s - 1) / %s) = 0
                ORDER BY rn
                LIMIT %s
            )
            SELECT {columns}
            FROM measurements m
            JOIN picked p ON p.pid = m.id
            ORDER BY m.measured_at DESC;
            """,
            [*params, max_points, max_points, limit],
        )
    else:
        cur.execute(
            f"""
            SELECT {columns}
            FROM measurements
            WHERE {where_sql}
            ORDER BY measured_at DESC
            LIMIT %s;
            """,
            [*params, limit],
        )


def measurements_for_insights(rows) -> list[dict]:
    """Build measurement dicts for the insight engine on temperature-compensated
    weights.

    compute_insights() reads weight from ``scale_1_weight_kg`` /
    ``scale_2_weight_kg``. Those keys hold the raw load-cell weight; the
    compensated values live under the separate ``*_compensated`` keys. We run the
    same compensation as the read APIs (serialize_measurements) and then fold the
    compensated weight into the primary keys so every weight-based detector
    (swarm, robbing, foraging, absconding, winter risk, harvest window) operates
    on corrected weight without any change to the engine.

    The compensated fields default to the raw weight when compensation is
    disabled or no coefficient is set, so this is a no-op in that case. These
    dicts are throwaway inputs to the engine — the stored DB rows are untouched.
    """
    measurements = serialize_measurements(rows)
    for m in measurements:
        comp1 = m.get("scale_1_weight_kg_compensated")
        comp2 = m.get("scale_2_weight_kg_compensated")
        if comp1 is not None:
            m["scale_1_weight_kg"] = comp1
        if comp2 is not None:
            m["scale_2_weight_kg"] = comp2
    return measurements


@app.get("/api/v1/measurements/latest", dependencies=[Depends(require_api_key)])
def latest_measurements(limit: int = 50):
    limit = min(max(limit, 1), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
    return serialize_measurements(rows)


# Column list shared by every device_configs read so the device-facing and
# app-facing config endpoints can never drift apart.
DEVICE_CONFIG_SELECT_COLUMNS = """
    device_id, send_interval_seconds, scale1_offset, scale1_factor,
    scale2_offset, scale2_factor, config_version,
    tempco_enabled, tempco_source, tempco_ref_temp_c,
    scale1_tempco_kg_per_c, scale2_tempco_kg_per_c
"""


def device_config_row_to_model(r) -> DeviceConfig:
    return DeviceConfig(
        device_id=r[0], send_interval_seconds=r[1], scale1_offset=r[2],
        scale1_factor=r[3], scale2_offset=r[4], scale2_factor=r[5],
        config_version=r[6], tempco_enabled=r[7], tempco_source=r[8],
        tempco_ref_temp_c=r[9], scale1_tempco_kg_per_c=r[10],
        scale2_tempco_kg_per_c=r[11],
    )


def fetch_device_config(device_id: str) -> DeviceConfig:
    ensure_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {DEVICE_CONFIG_SELECT_COLUMNS} "
                "FROM device_configs WHERE device_id = %s;",
                (device_id,),
            )
            r = cur.fetchone()
    return device_config_row_to_model(r)


@app.get("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_device_key)])
def get_device_config(device_id: str):
    return fetch_device_config(device_id)


@app.patch("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_device_key)])
def update_device_config(device_id: str, patch: DeviceConfigUpdate):
    ensure_device_config(device_id)
    fields = patch.model_dump(exclude_unset=True)
    if not fields:
        return get_device_config(device_id)
    assignments = [f"{k} = %({k})s" for k in fields]
    assignments.append("config_version = config_version + 1")
    assignments.append("updated_at = now()")
    fields["device_id"] = device_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE device_configs SET {', '.join(assignments)} WHERE device_id = %(device_id)s;",
                fields,
            )
            conn.commit()
    return get_device_config(device_id)


def get_device_owner_id(device_id: str) -> Optional[str]:
    """Return the HivePal user id of the device's owner, or None if unclaimed.

    A device has at most one ``owner`` membership (set at claim time); admins and
    viewers are added later via sharing. Owner-scoped firmware is keyed on this id.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id FROM device_members
                WHERE device_id = %s AND role = 'owner'
                ORDER BY created_at
                LIMIT 1;
                """,
                (device_id,),
            )
            r = cur.fetchone()
    return r[0] if r else None


def latest_release_for_owner(target: str, owner_user_id: Optional[str],
                             board: Optional[str] = None):
    """Most recent active release for a target, owner-first with global fallback.

    Returns the owner's own release when one exists, otherwise the newest global
    (owner_user_id IS NULL) "official" release. ``ORDER BY (owner_user_id IS NULL)``
    sorts owner-specific rows (false) ahead of global rows (true). Returns a
    (version, filename, crc32, owner_user_id) tuple, or None when nothing matches.

    When ``board`` is given (the dual-board ``hivescale`` target), only releases
    built for that exact board match — a 30-pin ESP32 (Xtensa) is never offered an
    ESP32-C6 (RISC-V) image or vice versa. ``board=None`` (single-architecture
    sub-device targets) applies no board filter.
    """
    sql = (
        "SELECT version, filename, crc32, owner_user_id "
        "FROM firmware_releases "
        "WHERE active = true AND target = %s "
        "  AND (owner_user_id = %s OR owner_user_id IS NULL) "
    )
    params: list = [target, owner_user_id]
    if board is not None:
        sql += "  AND board = %s "
        params.append(board)
    sql += "ORDER BY (owner_user_id IS NULL), created_at DESC, id DESC LIMIT 1;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()


def record_device_board(device_id: str, board: str) -> None:
    """Persist the board/architecture a device reported on its OTA check.

    No-op when the row does not exist (unclaimed device) or the value is unchanged.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET last_board = %s "
                "WHERE device_id = %s AND last_board IS DISTINCT FROM %s;",
                (board, device_id, board),
            )
            conn.commit()


def get_device_board(device_id: str) -> Optional[str]:
    """Return the board this device last reported on its OTA check, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_board FROM devices WHERE device_id = %s;", (device_id,))
            r = cur.fetchone()
    return r[0] if r and r[0] in FIRMWARE_BOARDS else None


def get_approved_firmware_version(device_id: str) -> Optional[str]:
    """Return the firmware version the owner has approved for this device, if any."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approved_firmware_version FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            r = cur.fetchone()
    return r[0] if r and r[0] else None


def set_approved_firmware_version(device_id: str, version: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET approved_firmware_version = %s WHERE device_id = %s;",
                (version, device_id),
            )
            conn.commit()


@app.get("/api/v1/devices/{device_id}/firmware", dependencies=[Depends(require_device_key)])
def check_firmware(device_id: str, version: str = Query("0.0.0"),
                   target: str = Query("hivescale"),
                   board: str = Query("")):
    ensure_device_config(device_id)
    # Deployed devices query ?target=hivescale; accept the "hivehub" alias too so
    # newer tooling can use either name. Canonical value is "hivescale".
    target = normalize_firmware_target(target)
    no_update = {"update": False, "update_available": False}
    # Per-board OTA gating for the dual-board hivescale target: the device must
    # report which architecture it is so we never serve a cross-arch (non-bootable)
    # image. A request without a recognized board is not matched against any
    # release — safer than guessing and risking a bricked flash. (Field devices on
    # firmware that predates the ?board= param therefore stop auto-updating until
    # they are reflashed once via the AP portal; the single-architecture
    # beecounter / hiveinside relays are unaffected.)
    board = (board or "").strip().lower()
    if target == "hivescale":
        if board not in FIRMWARE_BOARDS:
            logger.info("OTA check from %s without a known board (%r); no update served",
                        device_id, board)
            return no_update
        match_board: Optional[str] = board
        # Remember the board so the HivePal status/approve flow can resolve the
        # latest release for THIS device's architecture.
        record_device_board(device_id, board)
    else:
        match_board = None
    owner_id = get_device_owner_id(device_id)
    r = latest_release_for_owner(target, owner_id, match_board)
    # NOTE: the "update" and "update_available" keys carry the same value. The
    # ESP32 firmware reads doc["update"] while older clients/docs use
    # "update_available"; we emit both so a field-name mismatch can never
    # silently disable OTA again. (no_update is defined at the top of this handler.)
    if not r:
        return no_update
    latest_version, filename = r[0], r[1]
    if parse_version(latest_version) <= parse_version(version):
        return no_update
    # Accept-to-apply gate for the ESP32 self-update: a newer hivescale build is
    # only served once the owner has approved THIS exact version for THIS device
    # in HivePal (POST /api/v1/app/devices/{id}/firmware/approve). Without an
    # approval the device keeps polling but never flashes, so publishing firmware
    # no longer auto-updates every scale. Sub-device images (beecounter /
    # hiveinside) are relayed explicitly via commands, not here, so they are
    # unaffected by this gate.
    if target == "hivescale" and get_approved_firmware_version(device_id) != latest_version:
        return no_update
    url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
    return {
        "update": True,
        "update_available": True,
        "version": latest_version,
        "url": url,
    }


# Allowed firmware targets, shared by the JSON registration endpoint and the
# multipart upload endpoint below.
FIRMWARE_TARGETS = ("hivescale", "beecounter", "hiveinside")

# Board/architecture labels for the dual-board "hivescale" target. These MUST match
# the firmware's HIVESCALE_BOARD_LABEL (config.h) and rename_firmware.py BOARD_LABELS
# so a device's ?board=... query lines up with the release it should receive.
FIRMWARE_BOARDS = ("esp32", "esp32-c6")


def board_from_filename(filename: str) -> Optional[str]:
    """Infer the board/architecture from a board-stamped firmware filename.

    rename_firmware.py names artifacts ``hivehub_esp32_<v>.bin`` and
    ``hivehub_esp32-c6_<v>.bin`` (legacy builds used the ``hivescale_`` prefix);
    detection keys off the board token, so either prefix works. 'esp32-c6'
    contains the substring 'esp32', so the C6 variants are matched first. Returns
    None when the name carries no recognizable board token.
    """
    n = (filename or "").lower()
    if "esp32-c6" in n or "esp32c6" in n or "xiao" in n:
        return "esp32-c6"
    if "esp32" in n:
        return "esp32"
    return None


def resolve_hivescale_board(target: str, declared_board: Optional[str],
                            filename: str) -> Optional[str]:
    """Determine and validate the board for a release at registration time.

    Publish guard (defence against shipping a cross-architecture image): for the
    dual-board ``hivescale`` target the board must be known AND consistent with the
    board-stamped filename. A declared board that disagrees with the filename, an
    unknown board value, or a filename with no board token are all rejected, so a
    C6 binary can never be registered as an ``esp32`` release. Non-hivescale
    (single-architecture) targets carry no board.
    """
    if target != "hivescale":
        return None
    from_name = board_from_filename(filename)
    declared = (declared_board or "").strip().lower() or None
    if declared is not None and declared not in FIRMWARE_BOARDS:
        raise HTTPException(
            status_code=400,
            detail=f"board must be one of {', '.join(FIRMWARE_BOARDS)}",
        )
    if declared is not None and from_name is not None and declared != from_name:
        raise HTTPException(
            status_code=400,
            detail=(f"board '{declared}' does not match the board in filename "
                    f"'{filename}' ('{from_name}') — refusing to publish a "
                    "possibly cross-architecture image"),
        )
    board = declared or from_name
    if board is None:
        raise HTTPException(
            status_code=400,
            detail=("cannot determine board for a hivescale release: pass board= "
                    "or name the file like hivehub_esp32_<v>.bin / "
                    "hivehub_esp32-c6_<v>.bin"),
        )
    return board

# A conservative filename pattern. Firmware filenames are referenced verbatim in
# download URLs and joined onto FIRMWARE_DIR, so we reject anything that is not a
# plain basename with a safe character set. This prevents path traversal
# (e.g. "../../etc/passwd") and surprising URL encodings.
_SAFE_FIRMWARE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def crc32_of_file(path: Path) -> int:
    """Compute CRC-32 (IEEE 802.3) of a file as an unsigned 32-bit value.

    The HiveHub uses this to verify a firmware download before flashing it or
    relaying it to a BeeCounter over I2C. Stored in a BIGINT to stay positive.
    """
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def upsert_firmware_release(version: str, filename: str, active: bool,
                            target: str, crc: int,
                            owner_user_id: Optional[str] = None,
                            board: Optional[str] = None) -> None:
    """Insert or update a firmware_releases row keyed on
    (owner_user_id, target, board, version).

    Releases are unique per (owner_user_id, target, board, version), so the same
    version can coexist across targets (hivescale / beecounter / hiveinside),
    across the two hivescale boards (esp32 / esp32-c6), and across owners.
    Re-uploading the same (owner, target, board, version) replaces it.
    ``owner_user_id=None`` registers a global / "official" release that any device
    may fall back to. ``board`` is the architecture for the dual-board hivescale
    target (None for single-architecture sub-device targets).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO firmware_releases (version, filename, active, target, crc32, owner_user_id, board)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (COALESCE(owner_user_id, ''), target, COALESCE(board, ''), version) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    active   = EXCLUDED.active,
                    crc32    = EXCLUDED.crc32;
                """,
                (version, filename, active, target, crc, owner_user_id, board),
            )
            conn.commit()


@app.post("/api/v1/firmware/releases", dependencies=[Depends(require_api_key)])
def create_firmware_release(payload: FirmwareReleaseIn):
    path = FIRMWARE_DIR / payload.filename
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Firmware file '{payload.filename}' not found in firmware directory")
    # Publish guard: a hivescale release must carry a board that matches its
    # board-stamped filename, so a C6 image can never be registered as esp32.
    board = resolve_hivescale_board(payload.target, payload.board, payload.filename)
    crc = crc32_of_file(path)
    upsert_firmware_release(
        payload.version, payload.filename, payload.active, payload.target, crc,
        board=board,
    )
    return {"status": "ok", "version": payload.version, "target": payload.target,
            "board": board, "crc32": crc}


@app.get("/firmware/{filename}")
def download_firmware(filename: str):
    path = FIRMWARE_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Firmware file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


def create_command(device_id: str, payload: DeviceCommandIn) -> dict:
    ensure_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_commands (device_id, command_type, payload)
                VALUES (%s, %s, %s)
                RETURNING id, status;
                """,
                (device_id, payload.command_type, psycopg.types.json.Jsonb(payload.payload)),
            )
            r = cur.fetchone()
            conn.commit()
    return {"id": r[0], "status": r[1]}


@app.post("/api/v1/devices/{device_id}/commands", dependencies=[Depends(require_api_key)])
def queue_command(device_id: str, payload: DeviceCommandIn):
    result = create_command(device_id, payload)
    return {"status": result["status"], "id": result["id"]}


def queue_relay_firmware_update(device_id: str, target: str,
                                command_type: str, slot: int) -> dict:
    """Queue a command telling the HiveHub to relay the active firmware for
    ``target`` to the sub-device in the given slot.

    The image URL and its CRC-32 are looked up server-side (the latest active
    release for the target) and embedded in the command payload so the HiveHub
    can verify the download before relaying it. The CRC-32 is checked end-to-end
    on the receiving device before it swaps slots, so a corrupted relay never
    bricks it. Shared by the device-authenticated and HivePal-authenticated
    command endpoints.

    The release is resolved owner-first (the relaying HiveHub's owner), falling
    back to a global release, so a sub-device only ever receives an image its
    owner published or an official build.
    """
    owner_id = get_device_owner_id(device_id)
    r = latest_release_for_owner(target, owner_id)
    if not r:
        raise HTTPException(status_code=404, detail=f"No active {target} firmware release")
    version, filename, crc32 = r[0], r[1], r[2]
    url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
    return create_command(device_id, DeviceCommandIn(
        command_type=command_type,
        payload={"slot": slot, "url": url, "version": version, "crc32": int(crc32 or 0)},
    ))


@app.post("/api/v1/devices/{device_id}/commands/update-beecounter",
          dependencies=[Depends(require_api_key)])
def queue_beecounter_update(device_id: str, slot: int = Query(1)):
    """Queue a relay of the active BeeCounter firmware to the BeeCounter at the
    given slot (1 -> 0x30, 2 -> 0x31) over I2C."""
    return queue_relay_firmware_update(device_id, "beecounter", "update_beecounter", slot)


@app.post("/api/v1/devices/{device_id}/commands/update-hiveinside",
          dependencies=[Depends(require_api_key)])
def queue_hiveinside_update(device_id: str, slot: int = Query(1)):
    """Queue a relay of the active HiveInside firmware to the HiveInside sensor
    paired in the given slot (1 -> bleSensorMac0, 2 -> bleSensorMac1) over BLE
    GATT. The HiveHub resolves the BLE MAC locally, so only slot + image URL +
    CRC-32 are sent."""
    return queue_relay_firmware_update(device_id, "hiveinside", "update_hiveinside", slot)


@app.get("/api/v1/devices/{device_id}/commands/next", dependencies=[Depends(require_device_key)])
def next_command(device_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, command_type, payload FROM device_commands
                WHERE device_id = %s AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED;
                """,
                (device_id,),
            )
            r = cur.fetchone()
            if not r:
                conn.commit()
                return {"command": False}
            cur.execute(
                "UPDATE device_commands SET status = 'claimed', claimed_at = now() WHERE id = %s;",
                (r[0],),
            )
            conn.commit()
    return {"command": True, "id": r[0], "command_type": r[1], "payload": r[2]}


def apply_command_result_to_config(device_id: str, result: dict[str, Any]):
    allowed = {
        "scale1_offset",
        "scale1_factor",
        "scale2_offset",
        "scale2_factor",
        "tempco_enabled",
        "tempco_source",
        "tempco_ref_temp_c",
        "scale1_tempco_kg_per_c",
        "scale2_tempco_kg_per_c",
    }
    fields = {k: v for k, v in result.items() if k in allowed and v is not None}
    if not fields:
        return
    assignments = [f"{k} = %({k})s" for k in fields]
    assignments.append("config_version = config_version + 1")
    assignments.append("updated_at = now()")
    fields["device_id"] = device_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE device_configs SET {', '.join(assignments)} WHERE device_id = %(device_id)s;",
                fields,
            )
            conn.commit()


@app.post("/api/v1/devices/{device_id}/commands/{command_id}/result", dependencies=[Depends(require_device_key)])
def command_result(device_id: str, command_id: int, payload: DeviceCommandResult):
    if payload.success:
        apply_command_result_to_config(device_id, payload.result)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_commands
                SET status = %s, result = %s, completed_at = now()
                WHERE id = %s AND device_id = %s;
                """,
                (
                    "done" if payload.success else "failed",
                    psycopg.types.json.Jsonb(payload.model_dump()),
                    command_id,
                    device_id,
                ),
            )
            conn.commit()
    return {"status": "ok"}


@app.post("/api/v1/app/devices/claim", dependencies=[Depends(require_hivepal_service_key)])
def claim_device(payload: ClaimDeviceIn, user_id: str = Depends(require_user_id)):
    claim_hash = hash_claim_code(payload.claim_code)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT device_id FROM devices
                WHERE claim_code_hash = %s AND claimed_at IS NULL
                LIMIT 1;
                """,
                (claim_hash,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="No unclaimed device found with that claim code")
            device_id = r[0]
            cur.execute(
                "UPDATE devices SET claimed_at = now(), display_name = %s WHERE device_id = %s;",
                (payload.display_name, device_id),
            )
            cur.execute(
                """
                INSERT INTO device_members (device_id, user_id, role)
                VALUES (%s, %s, 'owner')
                ON CONFLICT (device_id, user_id) DO UPDATE SET role = 'owner';
                """,
                (device_id, user_id),
            )
            for ch_num, ch_name in [
                (1, payload.scale_1_display_name),
                (2, payload.scale_2_display_name),
            ]:
                if ch_name:
                    cur.execute(
                        """
                        INSERT INTO device_channels (device_id, channel_number, name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (device_id, channel_number) DO UPDATE SET name = EXCLUDED.name;
                        """,
                        (device_id, ch_num, ch_name),
                    )
            conn.commit()
    return {"status": "claimed", "device_id": device_id}


@app.get("/api/v1/app/devices", dependencies=[Depends(require_hivepal_service_key)])
def list_devices(user_id: str = Depends(require_user_id)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.device_id, d.display_name, d.claimed_at, d.last_seen_at,
                       d.last_firmware_version, dm.role
                FROM devices d
                JOIN device_members dm ON dm.device_id = d.device_id
                WHERE dm.user_id = %s
                ORDER BY d.last_seen_at DESC NULLS LAST;
                """,
                (user_id,),
            )
            rows = cur.fetchall()
            device_ids = [r[0] for r in rows]
            channels: dict[str, dict] = {}
            if device_ids:
                cur.execute(
                    "SELECT device_id, channel_number, name FROM device_channels WHERE device_id = ANY(%s);",
                    (device_ids,),
                )
                for ch in cur.fetchall():
                    channels.setdefault(ch[0], {})[ch[1]] = ch[2]
    return [
        {
            "device_id": r[0],
            "display_name": r[1],
            "claimed_at": r[2],
            "last_seen_at": r[3],
            "last_firmware_version": r[4],
            "role": r[5],
            "channels": {
                "scale_1": channels.get(r[0], {}).get(1),
                "scale_2": channels.get(r[0], {}).get(2),
            },
        }
        for r in rows
    ]


@app.delete("/api/v1/app/devices/{device_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_membership(device_id: str, user_id: str = Depends(require_user_id)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Device membership not found")
            cur.execute(
                "DELETE FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            conn.commit()
    return {"status": "removed", "device_id": device_id}


def fetch_device_channels(device_id: str) -> dict:
    """Return the two scale channels' display names for a device."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_number, name FROM device_channels WHERE device_id = %s ORDER BY channel_number;",
                (device_id,),
            )
            rows = cur.fetchall()
    ch = {r[0]: r[1] for r in rows}
    return {"scale_1_display_name": ch.get(1), "scale_2_display_name": ch.get(2)}


def apply_device_channels(device_id: str, payload: DeviceChannelsUpdateIn) -> dict:
    """Upsert the provided scale-channel display names and return all of them."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            for ch_num, ch_name in [
                (1, payload.scale_1_display_name),
                (2, payload.scale_2_display_name),
            ]:
                if ch_name is not None:
                    cur.execute(
                        """
                        INSERT INTO device_channels (device_id, channel_number, name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (device_id, channel_number) DO UPDATE SET name = EXCLUDED.name;
                        """,
                        (device_id, ch_num, ch_name),
                    )
            conn.commit()
    return fetch_device_channels(device_id)


@app.get("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def get_device_channels(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return fetch_device_channels(device_id)


@app.patch("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def update_device_channels(device_id: str, payload: DeviceChannelsUpdateIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return apply_device_channels(device_id, payload)


@app.get("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def list_device_members(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, role, created_at FROM device_members WHERE device_id = %s ORDER BY created_at;",
                (device_id,),
            )
            rows = cur.fetchall()
    return [{"user_id": r[0], "role": r[1], "joined_at": r[2]} for r in rows]


@app.post("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def add_device_member(device_id: str, payload: ShareDeviceIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_members (device_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (device_id, user_id) DO UPDATE SET role = EXCLUDED.role;
                """,
                (device_id, payload.user_id, payload.role),
            )
            conn.commit()
    return {"status": "ok", "device_id": device_id, "user_id": payload.user_id, "role": payload.role}


@app.delete("/api/v1/app/devices/{device_id}/members/{member_user_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_member(device_id: str, member_user_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, member_user_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Member not found")
            if r[0] == "owner":
                raise HTTPException(status_code=400, detail="Owner access cannot be revoked here")
            cur.execute(
                "DELETE FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, member_user_id),
            )
            conn.commit()
    return {"status": "revoked", "device_id": device_id, "user_id": member_user_id}


@app.get("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def get_device_config_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return fetch_device_config(device_id)


@app.get("/api/v1/app/devices/{device_id}/measurements", dependencies=[Depends(require_hivepal_service_key)])
def list_device_measurements(
    device_id: str,
    limit: int = 200,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    max_points: Optional[int] = None,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    limit = min(max(limit, 1), 10000)
    # Optional, opt-in down-sampling for chart consumers (e.g. HivePal wide
    # ranges). Default None keeps the existing full-resolution behaviour, so this
    # is non-breaking; the full column set is preserved either way.
    if max_points is not None:
        max_points = min(max(max_points, 0), 10000)
    where_parts = ["device_id = %s"]
    params: list[Any] = [device_id]

    if start_at is not None:
        where_parts.append("measured_at >= %s")
        params.append(start_at)

    if end_at is not None:
        where_parts.append("measured_at <= %s")
        params.append(end_at)

    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_measurement_query(
                cur, MEASUREMENT_SELECT_COLUMNS, where_parts, params, limit, max_points
            )
            rows = cur.fetchall()

    return serialize_measurements(rows)


@app.get("/api/v1/app/devices/{device_id}/measurements/latest", dependencies=[Depends(require_hivepal_service_key)])
def latest_device_measurements(device_id: str, limit: int = 50, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    limit = min(max(limit, 1), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                (device_id, limit),
            )
            rows = cur.fetchall()
    return serialize_measurements(rows)


@app.patch("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def update_device_config_from_app(device_id: str, patch: AppDeviceConfigUpdate, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return update_device_config(device_id, patch)


@app.post(
    "/api/v1/app/devices/{device_id}/temp-compensation/fit",
    dependencies=[Depends(require_hivepal_service_key)],
)
def fit_temp_compensation_from_app(
    device_id: str,
    body: TempCoefficientFitIn,
    user_id: str = Depends(require_user_id),
):
    """Derive a load-cell temperature coefficient from this device's history.

    Regresses the chosen scale's *raw* weight against an EMA-smoothed temperature
    channel over the requested window (see server/tempcomp.fit_temp_coefficient
    and ema_temperatures) — the same smoothing read-time compensation applies, so
    the coefficient is fitted in the regime it is used in — and returns the fit.
    With ``apply=true`` the coefficient, reference temperature and
    temperature source are written to the device config and compensation is
    enabled — applying ``apply`` requires owner/admin, a plain fit needs only
    viewer access.
    """
    role = ["owner", "admin"] if body.apply else ["owner", "admin", "viewer"]
    require_device_role(user_id, device_id, role)
    return run_temp_compensation_fit(device_id, body)


def run_temp_compensation_fit(device_id: str, body: "TempCoefficientFitIn") -> dict:
    """Fit (and optionally apply) a load-cell temperature coefficient.

    Shared by the HivePal app endpoint and the local dashboard endpoint; callers
    are responsible for any authorization. See the app endpoint docstring above
    for the regression details.
    """
    cfg = fetch_device_config(device_id)
    source = body.temp_source or cfg.tempco_source
    temp_field = TEMP_SOURCE_FIELD[source]
    weight_field = "scale_1_weight_kg" if body.scale == 1 else "scale_2_weight_kg"

    end_at = body.end_at or datetime.now(timezone.utc)
    start_at = body.start_at or (end_at - timedelta(days=body.lookback_days))

    where = ["device_id = %s", "measured_at >= %s", "measured_at <= %s",
             f"{weight_field} IS NOT NULL", f"{temp_field} IS NOT NULL"]
    params: list[Any] = [device_id, start_at, end_at]
    if body.calibration_mode_only:
        where.append("calibration_mode IS TRUE")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {temp_field}, {weight_field} FROM measurements "
                f"WHERE {' AND '.join(where)} ORDER BY measured_at ASC;",
                params,
            )
            samples = cur.fetchall()

    # Smooth the temperature series the same way read-time compensation does, so
    # the coefficient is fitted in the regime it is applied in
    # (raw − coeff·(EMA(temp) − ref)). Rows come back ordered by measured_at ASC,
    # which is what the EMA needs.
    smoothed_temps = ema_temperatures([row[0] for row in samples])
    samples = [(t, row[1]) for t, row in zip(smoothed_temps, samples)]

    fit = fit_temp_coefficient(samples)
    fit.update(
        scale=body.scale,
        temp_source=source,
        window_start=start_at.isoformat(),
        window_end=end_at.isoformat(),
        applied=False,
    )

    if body.apply and fit["ok"]:
        coeff_field = (
            "scale1_tempco_kg_per_c" if body.scale == 1 else "scale2_tempco_kg_per_c"
        )
        patch = DeviceConfigUpdate(
            tempco_enabled=True,
            tempco_source=source,
            tempco_ref_temp_c=fit["ref_temp_c"],
            **{coeff_field: fit["coeff_kg_per_c"]},
        )
        update_device_config(device_id, patch)
        fit["applied"] = True

    return fit


async def store_firmware_upload(
    device_id: str,
    file: UploadFile,
    version: str,
    target: str,
    active: bool,
    board: str,
    owner_user_id: Optional[str],
) -> dict:
    """Validate, stream-to-disk and register an uploaded firmware binary.

    Shared by the HivePal app endpoint (owner-scoped release) and the local
    dashboard endpoint (global release, owner_user_id=None). Writes the image
    into FIRMWARE_DIR in bounded chunks, enforces the size cap, computes its
    CRC-32 and upserts the firmware_releases row.
    """
    normalized_version = version.strip()
    if not normalized_version:
        raise HTTPException(status_code=400, detail="version must not be empty")

    # Accept the "hivehub" alias for the canonical "hivescale" target, then
    # validate. Normalising here (before the filename fallback below) means a
    # HiveHub upload is stored as a hivescale release with no DB migration.
    target = normalize_firmware_target(target)
    if target not in FIRMWARE_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(f"target must be one of {', '.join(FIRMWARE_TARGETS)} "
                    f"('hivehub' is accepted as an alias for 'hivescale')"),
        )

    # Derive a safe basename. We prefer the uploaded filename but fall back to a
    # deterministic name built from target + version when it is missing or
    # unsafe, so a release always has a usable, predictable filename.
    raw_name = os.path.basename((file.filename or "").strip())
    if raw_name and _SAFE_FIRMWARE_FILENAME.match(raw_name):
        filename = raw_name
    else:
        safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized_version).strip("-") or "unversioned"
        filename = f"{target}-{safe_version}.bin"

    dest = FIRMWARE_DIR / filename
    # Resolve and confirm the destination stays inside FIRMWARE_DIR. This is a
    # second line of defence on top of the basename + regex checks above.
    firmware_root = FIRMWARE_DIR.resolve()
    if dest.resolve().parent != firmware_root:
        raise HTTPException(status_code=400, detail="Invalid firmware filename")

    # Publish guard: resolve + validate the board BEFORE writing the image to disk,
    # so a hivescale release whose declared board contradicts its filename (or which
    # carries no board token at all) is rejected and leaves nothing behind.
    release_board = resolve_hivescale_board(target, board, filename)

    FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)

    # Stream the upload to disk in bounded chunks so large images do not have to
    # be held fully in memory.
    bytes_written = 0
    too_large = False
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                # Enforce the size cap before writing so a flood of oversized
                # uploads cannot fill the disk.
                if bytes_written > MAX_FIRMWARE_BYTES:
                    too_large = True
                    break
                out.write(chunk)
    finally:
        await file.close()

    if too_large:
        # Remove the partial file so a rejected upload leaves nothing behind.
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(
            status_code=413,
            detail=f"Firmware exceeds the maximum allowed size of {MAX_FIRMWARE_BYTES} bytes",
        )

    if bytes_written == 0:
        # Don't leave an empty file behind or register a zero-byte release.
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=400, detail="Uploaded firmware file is empty")

    crc = crc32_of_file(dest)
    upsert_firmware_release(normalized_version, filename, active, target, crc,
                            owner_user_id=owner_user_id, board=release_board)

    return {
        "status": "ok",
        "version": normalized_version,
        "filename": filename,
        "target": target,
        "board": release_board,
        "active": active,
        "size_bytes": bytes_written,
        "crc32": crc,
    }


@app.post(
    "/api/v1/app/devices/{device_id}/firmware",
    dependencies=[Depends(require_hivepal_service_key)],
)
async def upload_firmware_from_app(
    device_id: str,
    file: UploadFile = File(...),
    version: str = Form(...),
    target: str = Form("hivescale"),
    active: bool = Form(True),
    board: str = Form(""),
    user_id: str = Depends(require_user_id),
):
    """Upload a firmware binary from HivePal and register it as a release.

    Unlike POST /api/v1/firmware/releases (which only registers a file that is
    already present in FIRMWARE_DIR and is authenticated with the device
    X-API-Key), this endpoint accepts the binary itself as multipart/form-data,
    writes it into FIRMWARE_DIR, computes its CRC-32 and upserts the
    firmware_releases row.

    Authorization is per-device: the caller must be owner or admin on the given
    device. The release is scoped to that device's OWNER (owner_user_id), so it is
    only offered to scales owned by the same user — not the whole fleet. Pushing a
    global / official build is still possible via the master-key
    POST /api/v1/firmware/releases (which leaves owner_user_id NULL).
    """
    require_device_role(user_id, device_id, ["owner", "admin"])
    # Scope the release to the device's owner so only that owner's scales can pick
    # it up. Fall back to the uploader (an admin acting on an owner-less device)
    # so a release always has an owner and never silently becomes global.
    owner_user_id = get_device_owner_id(device_id) or user_id
    return await store_firmware_upload(
        device_id, file, version, target, active, board, owner_user_id
    )


@app.get(
    "/api/v1/app/devices/{device_id}/firmware/status",
    dependencies=[Depends(require_hivepal_service_key)],
)
def firmware_status_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    """Report the device's firmware-update status for the HivePal setup panel.

    HivePal renders an "update available — apply" notice from this.
    ``current_version`` is the version the device last reported; ``latest_version``
    is the newest active release resolved owner-first (the owner's own build, else
    a global/official one). ``update_available`` means latest > current;
    ``pending_approval`` means an update is available but the owner has not approved
    it yet, so the device will NOT auto-flash until they do via the approve
    endpoint below. Any role may read.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_firmware_version FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    current_version = row[0]

    owner_id = get_device_owner_id(device_id)
    # Resolve the latest release for this device's reported board so a C6 device is
    # never shown an esp32-only build as "available" (and vice versa). Falls back to
    # board-agnostic when the device has not yet checked in with the board param.
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    latest_version = release[0] if release else None
    latest_is_official = bool(release) and release[3] is None
    approved_version = get_approved_firmware_version(device_id)

    update_available = bool(
        latest_version is not None
        and current_version is not None
        and parse_version(latest_version) > parse_version(current_version)
    )
    return {
        "device_id": device_id,
        "target": "hivescale",
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_is_official": latest_is_official,
        "approved_version": approved_version,
        "update_available": update_available,
        "pending_approval": update_available and approved_version != latest_version,
    }


@app.post(
    "/api/v1/app/devices/{device_id}/firmware/approve",
    dependencies=[Depends(require_hivepal_service_key)],
)
def approve_firmware_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    """Approve the latest available firmware so this device may apply it.

    The accept-to-apply step: it records the approved version for this device (so
    check_firmware starts returning update=true for it) and queues an
    ``ota_update`` command to nudge the scale to update on its next check-in rather
    than waiting for its scheduled OTA poll. Requires owner or admin.
    """
    require_device_role(user_id, device_id, ["owner", "admin"])
    owner_id = get_device_owner_id(device_id)
    # Approve the latest release built for this device's board, so we never record
    # an approval for a version that only exists for the other architecture.
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    if not release:
        raise HTTPException(
            status_code=404, detail="No firmware release available for this device"
        )
    latest_version = release[0]
    set_approved_firmware_version(device_id, latest_version)
    # Nudge the device to check now rather than waiting for its scheduled poll.
    command = create_command(
        device_id, DeviceCommandIn(command_type="ota_update", payload={})
    )
    return {
        "status": "approved",
        "device_id": device_id,
        "version": latest_version,
        "command_id": command["id"],
    }


@app.post("/api/v1/app/devices/{device_id}/calibration/start", dependencies=[Depends(require_hivepal_service_key)])
def start_calibration_mode_from_app(
    device_id: str,
    payload: Optional[AppCalibrationModeStartIn] = None,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    payload = payload or AppCalibrationModeStartIn()
    command_payload = {
        "interval_seconds": payload.interval_seconds,
        "timeout_seconds": payload.timeout_seconds,
    }
    result = create_command(
        device_id,
        DeviceCommandIn(
            command_type="start_calibration_mode",
            payload=command_payload,
        ),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "start_calibration_mode",
        "payload": command_payload,
    }


@app.post("/api/v1/app/devices/{device_id}/calibration/stop", dependencies=[Depends(require_hivepal_service_key)])
def stop_calibration_mode_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = create_command(
        device_id,
        DeviceCommandIn(
            command_type="stop_calibration_mode",
            payload={},
        ),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "stop_calibration_mode",
        "payload": {},
    }


@app.post(
    "/api/v1/app/devices/{device_id}/commands/update-hiveinside",
    dependencies=[Depends(require_hivepal_service_key)],
)
def queue_hiveinside_update_from_app(
    device_id: str,
    slot: int = Query(1),
    user_id: str = Depends(require_user_id),
):
    """App-facing trigger for a HiveInside OTA relay.

    Uploading a HiveInside binary (POST .../firmware) only *registers* the
    release; it does not start the relay. HivePal calls this endpoint to actually
    queue the ``update_hiveinside`` command for the HiveHub to pick up. The
    caller must be owner or admin on the device.
    """
    if slot not in (1, 2):
        raise HTTPException(status_code=400, detail="slot must be 1 or 2")
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = queue_relay_firmware_update(
        device_id, "hiveinside", "update_hiveinside", slot
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "update_hiveinside",
        "payload": {"slot": slot},
    }


@app.post(
    "/api/v1/app/devices/{device_id}/commands/update-beecounter",
    dependencies=[Depends(require_hivepal_service_key)],
)
def queue_beecounter_update_from_app(
    device_id: str,
    slot: int = Query(1),
    user_id: str = Depends(require_user_id),
):
    """App-facing trigger for a BeeCounter OTA relay (see the HiveInside endpoint
    above; same upload-then-queue split). The caller must be owner or admin."""
    if slot not in (1, 2):
        raise HTTPException(status_code=400, detail="slot must be 1 or 2")
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = queue_relay_firmware_update(
        device_id, "beecounter", "update_beecounter", slot
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "update_beecounter",
        "payload": {"slot": slot},
    }


# ---------------------------------------------------------------------------
# Insight alert lifecycle persistence (history)
# ---------------------------------------------------------------------------

INSIGHT_SEVERITY_RANK = {"info": 1, "watch": 2, "warning": 3, "critical": 4}


def persist_insights(device_id: str, alerts: list, computed_at: datetime) -> None:
    """
    Reconcile the freshly computed ``alerts`` for ``device_id`` against the
    persisted ``insight_alerts`` lifecycle table.

    * An alert that is already active (same ``alert_key``) has its latest
      snapshot refreshed and ``last_seen_at`` bumped to ``computed_at``.
    * A newly appearing alert is inserted as an active row.
    * An active row whose detector no longer fires is resolved
      (``resolved_at = computed_at``).

    Idempotent and safe to run concurrently with itself thanks to the partial
    unique index ``insight_alerts_active_uniq`` and ``ON CONFLICT``.
    """
    # ``alert.id`` is stable within a compute pass (e.g. "swarm-watch-ch1") and
    # is what we dedupe on. Guard against accidental duplicates in one pass.
    current = {alert.id: alert for alert in alerts}
    active_keys = list(current.keys())

    with get_conn() as conn:
        with conn.cursor() as cur:
            for key, alert in current.items():
                cur.execute(
                    """
                    INSERT INTO insight_alerts (
                        device_id, alert_key, category, channel, severity,
                        peak_severity, title, description, confidence, evidence,
                        source, window_start, window_end, first_seen_at, last_seen_at
                    ) VALUES (
                        %(device_id)s, %(alert_key)s, %(category)s, %(channel)s,
                        %(severity)s, %(severity)s, %(title)s, %(description)s,
                        %(confidence)s, %(evidence)s, %(source)s, %(window_start)s,
                        %(window_end)s, %(now)s, %(now)s
                    )
                    ON CONFLICT (device_id, alert_key) WHERE resolved_at IS NULL
                    DO UPDATE SET
                        category = EXCLUDED.category,
                        channel = EXCLUDED.channel,
                        severity = EXCLUDED.severity,
                        peak_severity = CASE
                            WHEN array_position(
                                     ARRAY['info', 'watch', 'warning', 'critical'],
                                     EXCLUDED.severity)
                               > array_position(
                                     ARRAY['info', 'watch', 'warning', 'critical'],
                                     insight_alerts.peak_severity)
                            THEN EXCLUDED.severity
                            ELSE insight_alerts.peak_severity
                        END,
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        source = EXCLUDED.source,
                        window_start = EXCLUDED.window_start,
                        window_end = EXCLUDED.window_end,
                        last_seen_at = EXCLUDED.last_seen_at,
                        update_count = insight_alerts.update_count + 1,
                        updated_at = now();
                    """,
                    {
                        "device_id": device_id,
                        "alert_key": key,
                        "category": alert.category,
                        "channel": alert.channel,
                        "severity": alert.severity,
                        "title": alert.title,
                        "description": alert.description,
                        "confidence": alert.confidence,
                        "evidence": psycopg.types.json.Jsonb(alert.evidence or {}),
                        "source": alert.source or "",
                        "window_start": alert.window_start,
                        "window_end": alert.window_end,
                        "now": computed_at,
                    },
                )

            # Resolve active alerts that are no longer firing. With an empty
            # active set, ``<> ALL(ARRAY[]::text[])`` is true for every row, so
            # all currently active alerts get resolved.
            cur.execute(
                """
                UPDATE insight_alerts
                SET resolved_at = %(now)s, updated_at = now()
                WHERE device_id = %(device_id)s
                  AND resolved_at IS NULL
                  AND alert_key <> ALL(%(active_keys)s::text[]);
                """,
                {
                    "device_id": device_id,
                    "now": computed_at,
                    "active_keys": active_keys,
                },
            )
            conn.commit()


def reconcile_device_insights(
    device_id: str, lookback_days: int = INSIGHTS_HISTORY_LOOKBACK_DAYS
) -> int:
    """Compute insights for one device over the fixed lookback and persist them.

    Returns the number of alerts currently active after reconciliation.
    """
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()
    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    persist_insights(device_id, alerts, end_at)
    return len(alerts)


def reconcile_all_devices(
    lookback_days: int = INSIGHTS_HISTORY_LOOKBACK_DAYS,
) -> None:
    """Reconcile insight history for every device with recent measurements."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT device_id
                FROM measurements
                WHERE measured_at >= now() - make_interval(days => %s);
                """,
                (lookback_days,),
            )
            device_ids = [r[0] for r in cur.fetchall()]
    for device_id in device_ids:
        try:
            reconcile_device_insights(device_id, lookback_days)
        except Exception:  # one bad device must not stop the rest
            logger.exception("insight reconcile failed for device %s", device_id)


_reconcile_stop = threading.Event()
_reconcile_thread: Optional[threading.Thread] = None


def _reconcile_loop() -> None:
    # Small initial delay so startup (DB open, migrations) settles first.
    if _reconcile_stop.wait(15):
        return
    while True:
        try:
            reconcile_all_devices()
        except Exception:
            logger.exception("insight reconcile sweep failed")
        if _reconcile_stop.wait(INSIGHTS_RECONCILE_INTERVAL_SECONDS):
            return


def start_insight_reconciler() -> None:
    global _reconcile_thread
    if not INSIGHTS_RECONCILE_ENABLED:
        logger.info("insight reconciler disabled via INSIGHTS_RECONCILE_ENABLED")
        return
    if _reconcile_thread and _reconcile_thread.is_alive():
        return
    _reconcile_stop.clear()
    _reconcile_thread = threading.Thread(
        target=_reconcile_loop, name="insight-reconciler", daemon=True
    )
    _reconcile_thread.start()


def stop_insight_reconciler() -> None:
    _reconcile_stop.set()


def insight_history_row_to_dict(row: tuple) -> dict[str, Any]:
    (
        ia_id,
        alert_key,
        category,
        channel,
        severity,
        peak_severity,
        title,
        description,
        confidence,
        evidence,
        source,
        window_start,
        window_end,
        first_seen_at,
        last_seen_at,
        resolved_at,
        update_count,
    ) = row
    return {
        "id": ia_id,
        "alert_key": alert_key,
        "category": category,
        "channel": channel,
        "severity": severity,
        "peak_severity": peak_severity,
        "title": title,
        "description": description,
        "confidence": confidence,
        "evidence": evidence or {},
        "source": source or "",
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "first_seen_at": first_seen_at.isoformat() if first_seen_at else None,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "status": "active" if resolved_at is None else "resolved",
        "update_count": update_count,
    }


@app.get(
    "/api/v1/app/devices/{device_id}/insights",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights(
    device_id: str,
    lookback_days: int = Query(14, ge=1, le=90),
    user_id: str = Depends(require_user_id),
):
    """
    Compute current sensor-based alerts/insights for a device.

    See server/insights.py for the algorithms and their literature sources.
    The detectors run over the last `lookback_days` of measurements (default
    14 days, max 90). All channels (scale 1 and scale 2) are evaluated.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()

    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    return {
        "device_id": device_id,
        "computed_at": end_at.isoformat(),
        "lookback_days": lookback_days,
        "measurement_count": len(measurements),
        "alerts": [a.model_dump() for a in alerts],
    }


@app.get(
    "/api/v1/app/devices/{device_id}/insights/summary",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights_summary(
    device_id: str,
    user_id: str = Depends(require_user_id),
):
    """
    Highest-severity summary of current alerts, suitable for dashboard
    cards. Always uses the default 14-day lookback.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=14)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()

    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    # Opportunistically keep the persisted history fresh on every summary hit,
    # in addition to the background reconciler. Never let a persistence error
    # break the read.
    try:
        persist_insights(device_id, alerts, end_at)
    except Exception:
        logger.exception("opportunistic insight persist failed for %s", device_id)
    summary = summarize(device_id, alerts, end_at)
    return {
        "device_id": summary.device_id,
        "computed_at": summary.computed_at.isoformat(),
        "alert_count": summary.alert_count,
        "highest_severity": summary.highest_severity,
        "highest_alert": (
            summary.highest_alert.model_dump() if summary.highest_alert else None
        ),
        "categories": summary.categories,
    }


@app.get(
    "/api/v1/app/devices/{device_id}/insights/history",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights_history(
    device_id: str,
    status: Literal["all", "active", "resolved"] = Query("all"),
    category: Optional[str] = Query(None),
    since: Optional[datetime] = Query(
        None, description="Only alerts last seen at or after this time (ISO 8601)"
    ),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_user_id),
):
    """
    Persisted history of sensor-based alerts for a device.

    Unlike the live ``/insights`` endpoint (which recomputes the *current*
    state on every call), this returns the stored lifecycle of every alert the
    background reconciler has observed — including resolved ones — newest
    first. See ``persist_insights`` and ``server/insights.py``.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])

    conditions = ["device_id = %(device_id)s"]
    params: dict[str, Any] = {"device_id": device_id, "limit": limit}
    if status == "active":
        conditions.append("resolved_at IS NULL")
    elif status == "resolved":
        conditions.append("resolved_at IS NOT NULL")
    if category:
        conditions.append("category = %(category)s")
        params["category"] = category
    if since is not None:
        conditions.append("last_seen_at >= %(since)s")
        params["since"] = since

    where_clause = " AND ".join(conditions)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, alert_key, category, channel, severity, peak_severity,
                       title, description, confidence, evidence, source,
                       window_start, window_end, first_seen_at, last_seen_at,
                       resolved_at, update_count
                FROM insight_alerts
                WHERE {where_clause}
                ORDER BY first_seen_at DESC, id DESC
                LIMIT %(limit)s;
                """,
                params,
            )
            rows = cur.fetchall()

    entries = [insight_history_row_to_dict(r) for r in rows]
    active_count = sum(1 for e in entries if e["status"] == "active")
    return {
        "device_id": device_id,
        "lookback_days": INSIGHTS_HISTORY_LOOKBACK_DAYS,
        "count": len(entries),
        "active_count": active_count,
        "alerts": entries,
    }


@app.get("/api/v1/time", dependencies=[Depends(require_api_key)])
def get_server_time():
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "unix": int(now.timestamp()),
        "timezone": "UTC",
    }


# ---------------------------------------------------------------------------
# Local dashboard API (auth-free, single-owner self-host)
# ---------------------------------------------------------------------------
# Everything below is gated by require_local_dashboard (404 when
# ENABLE_LOCAL_DASHBOARD is off). It mirrors the read paths of the /api/v1/app/*
# endpoints but drops the per-user JWT + device-membership checks, so the built-in
# HiveHub dashboard at /dashboard can talk to it directly with no login. It
# therefore serves EVERY device on this server — only enable it on a trusted LAN
# or behind a reverse proxy. Handlers reuse the same helpers as the app API so the
# data shapes never drift.

LOCAL_DASHBOARD_DEP = [Depends(require_local_dashboard)]


@app.get("/api/v1/local/devices", dependencies=LOCAL_DASHBOARD_DEP)
def local_list_devices():
    """List every device on this server with its scale-channel display names."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT device_id, display_name, claimed_at, last_seen_at,
                       last_firmware_version
                FROM devices
                ORDER BY last_seen_at DESC NULLS LAST;
                """
            )
            rows = cur.fetchall()
            device_ids = [r[0] for r in rows]
            channels: dict[str, dict] = {}
            if device_ids:
                cur.execute(
                    "SELECT device_id, channel_number, name FROM device_channels "
                    "WHERE device_id = ANY(%s);",
                    (device_ids,),
                )
                for ch in cur.fetchall():
                    channels.setdefault(ch[0], {})[ch[1]] = ch[2]
    return [
        {
            "device_id": r[0],
            "display_name": r[1],
            "claimed_at": r[2],
            "last_seen_at": r[3],
            "last_firmware_version": r[4],
            "channels": {
                "scale_1": channels.get(r[0], {}).get(1),
                "scale_2": channels.get(r[0], {}).get(2),
            },
        }
        for r in rows
    ]


@app.get("/api/v1/local/devices/{device_id}/measurements", dependencies=LOCAL_DASHBOARD_DEP)
def local_list_measurements(
    device_id: str,
    limit: int = 2000,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    max_points: int = 1500,
):
    """Time-series measurements for one device (newest first), for the charts.

    Returns the slim chart column set (see CHART_MEASUREMENT_COLUMNS) and, by
    default, down-samples to at most ~``max_points`` evenly-spaced rows so wide
    ranges (30d / 1y / 5y) stay fast and small instead of shipping every reading
    to draw a ~600px chart. Pass ``max_points=0`` to disable down-sampling.
    """
    limit = min(max(limit, 1), 20000)
    max_points = min(max(max_points, 0), 20000)
    where_parts = ["device_id = %s"]
    params: list[Any] = [device_id]
    if start_at is not None:
        where_parts.append("measured_at >= %s")
        params.append(start_at)
    if end_at is not None:
        where_parts.append("measured_at <= %s")
        params.append(end_at)
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_measurement_query(
                cur, CHART_MEASUREMENT_COLUMNS, where_parts, params, limit, max_points
            )
            return serialize_chart_measurements(cur)


@app.get("/api/v1/local/devices/{device_id}/measurements/latest", dependencies=LOCAL_DASHBOARD_DEP)
def local_latest_measurements(device_id: str, limit: int = 1):
    """Most recent measurement row(s) for the overview cards."""
    limit = min(max(limit, 1), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                (device_id, limit),
            )
            rows = cur.fetchall()
    return serialize_measurements(rows)


@app.get("/api/v1/local/devices/{device_id}/config", dependencies=LOCAL_DASHBOARD_DEP)
def local_get_config(device_id: str):
    """Read the device's send-interval / calibration / temp-compensation config."""
    return fetch_device_config(device_id)


@app.patch("/api/v1/local/devices/{device_id}/config", dependencies=LOCAL_DASHBOARD_DEP)
def local_update_config(device_id: str, patch: DeviceConfigUpdate):
    """Update device config (send interval, scale offsets/factors, temp comp).

    Only the provided fields change; the write bumps config_version so the device
    picks it up on its next check-in (same path as the HivePal config edit).
    """
    return update_device_config(device_id, patch)


@app.get("/api/v1/local/devices/{device_id}/channels", dependencies=LOCAL_DASHBOARD_DEP)
def local_get_channels(device_id: str):
    """Read the scale-channel (hive) display names."""
    return fetch_device_channels(device_id)


@app.patch("/api/v1/local/devices/{device_id}/channels", dependencies=LOCAL_DASHBOARD_DEP)
def local_update_channels(device_id: str, payload: DeviceChannelsUpdateIn):
    """Rename the scale channels (the hive labels shown across the dashboard)."""
    return apply_device_channels(device_id, payload)


@app.get("/api/v1/local/devices/{device_id}/insights/summary", dependencies=LOCAL_DASHBOARD_DEP)
def local_insights_summary(device_id: str):
    """Highest-severity insight summary (14-day lookback) for the overview."""
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=14)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()
    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    try:
        persist_insights(device_id, alerts, end_at)
    except Exception:
        logger.exception("opportunistic insight persist failed for %s", device_id)
    summary = summarize(device_id, alerts, end_at)
    return {
        "device_id": summary.device_id,
        "computed_at": summary.computed_at.isoformat(),
        "alert_count": summary.alert_count,
        "highest_severity": summary.highest_severity,
        "highest_alert": (
            summary.highest_alert.model_dump() if summary.highest_alert else None
        ),
        "categories": summary.categories,
    }


@app.get("/api/v1/local/devices/{device_id}/firmware/status", dependencies=LOCAL_DASHBOARD_DEP)
def local_firmware_status(device_id: str):
    """Current vs latest firmware and whether an approved update is pending."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_firmware_version FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    current_version = row[0]
    owner_id = get_device_owner_id(device_id)
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    latest_version = release[0] if release else None
    latest_is_official = bool(release) and release[3] is None
    approved_version = get_approved_firmware_version(device_id)
    update_available = bool(
        latest_version is not None
        and current_version is not None
        and parse_version(latest_version) > parse_version(current_version)
    )
    return {
        "device_id": device_id,
        "target": "hivescale",
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_is_official": latest_is_official,
        "approved_version": approved_version,
        "update_available": update_available,
        "pending_approval": update_available and approved_version != latest_version,
    }


@app.post("/api/v1/local/devices/{device_id}/firmware", dependencies=LOCAL_DASHBOARD_DEP)
async def local_upload_firmware(
    device_id: str,
    file: UploadFile = File(...),
    version: str = Form(...),
    target: str = Form("hivescale"),
    active: bool = Form(True),
    board: str = Form(""),
):
    """Upload a firmware binary and register it as a GLOBAL release.

    In single-owner local mode there is no per-user scoping, so the release is
    left owner-less (owner_user_id=None) and every device on this server can pick
    it up after it is approved.
    """
    return await store_firmware_upload(
        device_id, file, version, target, active, board, owner_user_id=None
    )


@app.post("/api/v1/local/devices/{device_id}/firmware/approve", dependencies=LOCAL_DASHBOARD_DEP)
def local_approve_firmware(device_id: str):
    """Approve the latest available firmware and nudge the device to update now."""
    owner_id = get_device_owner_id(device_id)
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    if not release:
        raise HTTPException(
            status_code=404, detail="No firmware release available for this device"
        )
    latest_version = release[0]
    set_approved_firmware_version(device_id, latest_version)
    command = create_command(
        device_id, DeviceCommandIn(command_type="ota_update", payload={})
    )
    return {
        "status": "approved",
        "device_id": device_id,
        "version": latest_version,
        "command_id": command["id"],
    }


@app.post("/api/v1/local/devices/{device_id}/calibration/start", dependencies=LOCAL_DASHBOARD_DEP)
def local_start_calibration(
    device_id: str,
    payload: Optional[AppCalibrationModeStartIn] = None,
):
    """Queue a start-calibration-mode command (denser sampling) for the device."""
    payload = payload or AppCalibrationModeStartIn()
    command_payload = {
        "interval_seconds": payload.interval_seconds,
        "timeout_seconds": payload.timeout_seconds,
    }
    result = create_command(
        device_id,
        DeviceCommandIn(command_type="start_calibration_mode", payload=command_payload),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "start_calibration_mode",
        "payload": command_payload,
    }


@app.post("/api/v1/local/devices/{device_id}/calibration/stop", dependencies=LOCAL_DASHBOARD_DEP)
def local_stop_calibration(device_id: str):
    """Queue a stop-calibration-mode command for the device."""
    result = create_command(
        device_id,
        DeviceCommandIn(command_type="stop_calibration_mode", payload={}),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "stop_calibration_mode",
        "payload": {},
    }


@app.post("/api/v1/local/devices/{device_id}/temp-compensation/fit", dependencies=LOCAL_DASHBOARD_DEP)
def local_temp_compensation_fit(device_id: str, body: TempCoefficientFitIn):
    """Fit (and optionally apply) a load-cell temperature coefficient."""
    return run_temp_compensation_fit(device_id, body)


# Serve the static dashboard at /dashboard only when local mode is enabled. The
# files live in server/dashboard/ and ship in the Docker image (Dockerfile does
# `COPY . .`). html=True makes /dashboard resolve to index.html.
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"
if ENABLE_LOCAL_DASHBOARD and DASHBOARD_DIR.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(DASHBOARD_DIR), html=True),
        name="dashboard",
    )