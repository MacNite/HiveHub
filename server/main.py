import hashlib
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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ConfigDict
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from insights import compute_insights, summarize
from sd_import import split_new_and_duplicate
from tempcomp import (
    VALID_TEMP_SOURCES,
    TEMP_SOURCE_FIELD,
    DEFAULT_REF_TEMP_C,
    DEFAULT_TEMP_SOURCE,
    compensate_weight,
    fit_temp_coefficient,
)

DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]
HIVEPAL_SERVICE_API_KEY = os.environ.get("HIVEPAL_SERVICE_API_KEY", "")
HIVEPAL_JWT_SECRET = os.environ.get("HIVEPAL_JWT_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
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
        # Firmware binary uploads are large by design and capped by the endpoint.
        return scope.get("method") == "POST" and scope.get("path", "").endswith("/firmware")

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
    title="HiveScale API",
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

    # ── Per-gate forensic arrays (one value per entrance gate) ───────────────
    # Sent only inside the measurement body and kept in raw_json (never promoted
    # to columns). Declared explicitly so extra="ignore" does not drop them, and
    # length-capped so they cannot be abused for storage amplification.
    bee_counter_1_per_gate_in:  Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_1_per_gate_out: Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_2_per_gate_in:  Optional[list[int]] = Field(default=None, max_length=64)
    bee_counter_2_per_gate_out: Optional[list[int]] = Field(default=None, max_length=64)


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


class FirmwareReleaseIn(BaseModel):
    version: str
    filename: str
    active: bool = True
    target: Literal["hivescale", "beecounter"] = "hivescale"


class DeviceCommandIn(BaseModel):
    command_type: Literal[
        "tare_scale_1",
        "tare_scale_2",
        "calibrate_scale_1",
        "calibrate_scale_2",
        "reboot",
        "reset_preferences",
        "factory_reset",
        "reset_wifi",
        "check_ota",
        "ota_update",
        "update_beecounter",
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
    """Register a device's API key on first contact; reject mismatches thereafter."""
    if len(api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devices
                SET api_key_hash = COALESCE(api_key_hash, %s)
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

                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS api_key_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS display_name TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_firmware_version TEXT;

                CREATE INDEX IF NOT EXISTS idx_measurements_device_time
                    ON measurements (device_id, measured_at DESC);

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

                CREATE TABLE IF NOT EXISTS firmware_releases (
                    id BIGSERIAL PRIMARY KEY,
                    version TEXT NOT NULL UNIQUE,
                    filename TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS target TEXT NOT NULL DEFAULT 'hivescale';
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS crc32 BIGINT;

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
):
    claim_hash = hash_claim_code(claim_code) if claim_code else None
    key_hash = hashlib.sha256(api_key.encode()).hexdigest() if len(api_key) >= 16 else None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devices (device_id, claim_code_hash, api_key_hash, last_seen_at, last_firmware_version)
                VALUES (%s, %s, %s, now(), %s)
                ON CONFLICT (device_id) DO UPDATE
                    SET last_seen_at = now(),
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


@app.on_event("shutdown")
def shutdown():
    stop_insight_reconciler()
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
                    hive_1_temp_c, hive_2_temp_c, ambient_temp_c,
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
                    raw_json
                )
                VALUES (
                    %(device_id)s, %(measured_at)s, %(scale_1_weight_kg)s,
                    %(scale_2_weight_kg)s, %(hive_1_temp_c)s, %(hive_2_temp_c)s,
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
        "raw_json": psycopg.types.json.Jsonb(payload.model_dump(mode="json", exclude={"claim_code"})),
    }


@app.post("/api/v1/measurements")
def create_measurement(payload: MeasurementIn, x_api_key: str = Header(default="")):
    if len(x_api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    measured_at = payload.timestamp or datetime.now(timezone.utc)
    ensure_device_config(payload.device_id, payload.claim_code, payload.firmware_version, x_api_key)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                MEASUREMENT_INSERT_SQL + " RETURNING id;",
                measurement_insert_params(payload, measured_at),
            )
            new_id = cur.fetchone()[0]
            conn.commit()
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
                cur.executemany(
                    MEASUREMENT_INSERT_SQL + ";",
                    [
                        measurement_insert_params(record_by_key[key], key)
                        for key in new_keys
                    ],
                )
                inserted = len(new_keys)
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
    COALESCE(bee_counter_2_latch_succeeded,   NULLIF(raw_json->>'bee_counter_2_latch_succeeded',   '')::boolean) AS bee_counter_2_latch_succeeded
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
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


def attach_temperature_compensation(measurements: list[dict]) -> list[dict]:
    """Fill in the compensated-weight fields on serialized measurement dicts.

    Looks up each device's coefficient once (a single batched query) and applies
    the first-order correction from server/tempcomp.py. Rows whose device has no
    enabled coefficient keep the defaults set in measurement_row_to_dict (raw
    weight, tempco_applied=False).
    """
    if not measurements:
        return measurements
    cfgs = load_tempco_configs(m["device_id"] for m in measurements)
    if not cfgs:
        return measurements
    for m in measurements:
        cfg = cfgs.get(m["device_id"])
        if not cfg:
            continue
        source, ref_temp, c1, c2 = cfg
        temp = m.get(TEMP_SOURCE_FIELD.get(source, "ambient_temp_c"))
        m["scale_1_weight_kg_compensated"] = compensate_weight(
            m["scale_1_weight_kg"], temp, ref_temp, c1
        )
        m["scale_2_weight_kg_compensated"] = compensate_weight(
            m["scale_2_weight_kg"], temp, ref_temp, c2
        )
        m["tempco_applied"] = True
    return measurements


def serialize_measurements(rows) -> list[dict]:
    """Map raw DB rows to API dicts and attach temperature compensation."""
    return attach_temperature_compensation([measurement_row_to_dict(r) for r in rows])


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


@app.get("/api/v1/devices/{device_id}/firmware", dependencies=[Depends(require_device_key)])
def check_firmware(device_id: str, version: str = Query("0.0.0"),
                   target: str = Query("hivescale")):
    ensure_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, filename FROM firmware_releases
                WHERE active = true AND target = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1;
                """,
                (target,),
            )
            r = cur.fetchone()
    # NOTE: the "update" and "update_available" keys carry the same value. The
    # ESP32 firmware reads doc["update"] while older clients/docs use
    # "update_available"; we emit both so a field-name mismatch can never
    # silently disable OTA again. Keep both keys if you change this.
    if not r:
        return {"update": False, "update_available": False}
    latest_version, filename = r
    if parse_version(latest_version) > parse_version(version):
        url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
        return {
            "update": True,
            "update_available": True,
            "version": latest_version,
            "url": url,
        }
    return {"update": False, "update_available": False}


# Allowed firmware targets, shared by the JSON registration endpoint and the
# multipart upload endpoint below.
FIRMWARE_TARGETS = ("hivescale", "beecounter")

# A conservative filename pattern. Firmware filenames are referenced verbatim in
# download URLs and joined onto FIRMWARE_DIR, so we reject anything that is not a
# plain basename with a safe character set. This prevents path traversal
# (e.g. "../../etc/passwd") and surprising URL encodings.
_SAFE_FIRMWARE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def crc32_of_file(path: Path) -> int:
    """Compute CRC-32 (IEEE 802.3) of a file as an unsigned 32-bit value.

    The HiveScale uses this to verify a firmware download before flashing it or
    relaying it to a BeeCounter over I2C. Stored in a BIGINT to stay positive.
    """
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def upsert_firmware_release(version: str, filename: str, active: bool,
                            target: str, crc: int) -> None:
    """Insert or update a firmware_releases row keyed on the unique version."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO firmware_releases (version, filename, active, target, crc32)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (version) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    active   = EXCLUDED.active,
                    target   = EXCLUDED.target,
                    crc32    = EXCLUDED.crc32;
                """,
                (version, filename, active, target, crc),
            )
            conn.commit()


@app.post("/api/v1/firmware/releases", dependencies=[Depends(require_api_key)])
def create_firmware_release(payload: FirmwareReleaseIn):
    path = FIRMWARE_DIR / payload.filename
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Firmware file '{payload.filename}' not found in firmware directory")
    crc = crc32_of_file(path)
    upsert_firmware_release(
        payload.version, payload.filename, payload.active, payload.target, crc
    )
    return {"status": "ok", "version": payload.version, "target": payload.target, "crc32": crc}


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


@app.post("/api/v1/devices/{device_id}/commands/update-beecounter",
          dependencies=[Depends(require_api_key)])
def queue_beecounter_update(device_id: str, slot: int = Query(1)):
    """Queue a command telling the HiveScale to relay the active BeeCounter
    firmware to the BeeCounter at the given slot (1 -> 0x30, 2 -> 0x31) over
    I2C. The image URL and its CRC-32 are looked up server-side and embedded in
    the command payload so the HiveScale can verify the download before relay."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT version, filename, crc32 FROM firmware_releases
                WHERE active = true AND target = 'beecounter'
                ORDER BY created_at DESC, id DESC
                LIMIT 1;
                """,
            )
            r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="No active beecounter firmware release")
    version, filename, crc32 = r
    url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
    return create_command(device_id, DeviceCommandIn(
        command_type="update_beecounter",
        payload={"slot": slot, "url": url, "version": version, "crc32": int(crc32 or 0)},
    ))


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


@app.get("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def get_device_channels(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_number, name FROM device_channels WHERE device_id = %s ORDER BY channel_number;",
                (device_id,),
            )
            rows = cur.fetchall()
    ch = {r[0]: r[1] for r in rows}
    return {"scale_1_display_name": ch.get(1), "scale_2_display_name": ch.get(2)}


@app.patch("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def update_device_channels(device_id: str, payload: DeviceChannelsUpdateIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
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
    return get_device_channels(device_id, user_id)


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
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    limit = min(max(limit, 1), 10000)
    where_parts = ["device_id = %s"]
    params: list[Any] = [device_id]

    if start_at is not None:
        where_parts.append("measured_at >= %s")
        params.append(start_at)

    if end_at is not None:
        where_parts.append("measured_at <= %s")
        params.append(end_at)

    params.append(limit)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE {' AND '.join(where_parts)}
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                params,
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

    Regresses the chosen scale's *raw* weight against a temperature channel over
    the requested window (see server/tempcomp.fit_temp_coefficient) and returns
    the fit. With ``apply=true`` the coefficient, reference temperature and
    temperature source are written to the device config and compensation is
    enabled — applying ``apply`` requires owner/admin, a plain fit needs only
    viewer access.
    """
    role = ["owner", "admin"] if body.apply else ["owner", "admin", "viewer"]
    require_device_role(user_id, device_id, role)

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
    user_id: str = Depends(require_user_id),
):
    """Upload a firmware binary from HivePal and register it as a release.

    Unlike POST /api/v1/firmware/releases (which only registers a file that is
    already present in FIRMWARE_DIR and is authenticated with the device
    X-API-Key), this endpoint accepts the binary itself as multipart/form-data,
    writes it into FIRMWARE_DIR, computes its CRC-32 and upserts the
    firmware_releases row.

    Authorization is per-device: the caller must be owner or admin on the given
    device. The device_id scopes who may publish firmware; the resulting release
    is global (any device of the matching target can pick it up via the normal
    firmware-check endpoint), which mirrors how releases already work.
    """
    require_device_role(user_id, device_id, ["owner", "admin"])

    normalized_version = version.strip()
    if not normalized_version:
        raise HTTPException(status_code=400, detail="version must not be empty")

    if target not in FIRMWARE_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"target must be one of {', '.join(FIRMWARE_TARGETS)}",
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
    upsert_firmware_release(normalized_version, filename, active, target, crc)

    return {
        "status": "ok",
        "version": normalized_version,
        "filename": filename,
        "target": target,
        "active": active,
        "size_bytes": bytes_written,
        "crc32": crc,
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
    measurements = [measurement_row_to_dict(r) for r in rows]
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

    measurements = [measurement_row_to_dict(r) for r in rows]
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

    measurements = [measurement_row_to_dict(r) for r in rows]
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