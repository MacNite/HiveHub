"""HiveHub API entry point: app construction, middleware and router wiring.

The domain logic lives in the sibling modules:

- config.py            environment-driven configuration and shared constants
- middleware.py         body-size guard and rate-limit key helpers
- schemas.py            Pydantic request/response models
- db.py                 connection pool and schema bootstrap (init_db)
- auth.py               API-key / device-key / JWT / dashboard-session auth
- measurements.py       measurement ingest + read paths (device & master-key)
- devices.py            device registry, per-device config and channel names
- firmware.py           OTA releases, checks and binary upload/download
- commands.py           device command queue
- app_api.py            HivePal app API (/api/v1/app/*)
- insights_api.py       insight alert persistence, reconciler and endpoints
- local_dashboard.py    self-host dashboard API (/api/v1/local/*)
"""
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import app_api
import commands
import devices
import firmware
import insights_api
import local_dashboard
import measurements
from auth import require_api_key
from config import (
    ENABLE_LOCAL_DASHBOARD,
    FIRMWARE_DIR,
    MAX_BODY_BYTES,
    RATE_LIMIT_DEFAULT,
    RATE_LIMIT_ENABLED,
)
from db import db_pool, init_db
from insights_api import start_insight_reconciler, stop_insight_reconciler
from middleware import MaxBodySizeMiddleware, _client_ip_key
from mqtt_publisher import publisher as mqtt_publisher

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


@app.get("/api/v1/time", dependencies=[Depends(require_api_key)])
def get_server_time():
    now = datetime.now(timezone.utc)
    return {
        "timestamp": now.isoformat(),
        "unix": int(now.timestamp()),
        "timezone": "UTC",
    }


app.include_router(measurements.router)
app.include_router(devices.router)
app.include_router(firmware.router)
app.include_router(commands.router)
app.include_router(app_api.router)
app.include_router(insights_api.router)
app.include_router(local_dashboard.router)

# Serve the static dashboard at /dashboard only when local mode is enabled. The
# files live in server/dashboard/ and ship in the Docker image (Dockerfile does
# `COPY . .`). html=True makes /dashboard resolve to index.html.
DASHBOARD_DIR = Path(__file__).resolve().parent / "dashboard"


class DashboardStaticFiles(StaticFiles):
    """StaticFiles with explicit Cache-Control headers.

    Starlette sends only ETag/Last-Modified, so every dashboard load
    re-validates all ~9 assets with conditional requests. The HTML shell stays
    no-cache (a deploy shows up on the next load); the JS/CSS assets get a
    modest max-age so repeat loads within the hour skip the network entirely,
    falling back to ETag re-validation once it expires.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code < 400:
            if path in ("", ".", "index.html") or path.endswith(".html"):
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=3600"
        return response


if ENABLE_LOCAL_DASHBOARD and DASHBOARD_DIR.is_dir():
    app.mount(
        "/dashboard",
        DashboardStaticFiles(directory=str(DASHBOARD_DIR), html=True),
        name="dashboard",
    )
