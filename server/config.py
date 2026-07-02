"""Environment-driven configuration and shared constants."""

import logging
import os
from pathlib import Path


DATABASE_URL = os.environ["DATABASE_URL"]
API_KEY = os.environ["API_KEY"]
HIVEPAL_SERVICE_API_KEY = os.environ.get("HIVEPAL_SERVICE_API_KEY", "")
HIVEPAL_JWT_SECRET = os.environ.get("HIVEPAL_JWT_SECRET", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
# Opt-in local dashboard (single-owner self-host). When enabled it exposes data
# plus firmware/calibration controls for ALL devices under /api/v1/local/* and
# serves the static dashboard at /dashboard. Access is gated by a username +
# password login (see the dashboard_users table): the first visit runs a setup
# wizard that creates the initial admin, and every data/control endpoint then
# requires a valid session. This makes it safe to expose to the internet, though
# putting it behind TLS / a reverse proxy is still recommended.
ENABLE_LOCAL_DASHBOARD = os.environ.get("ENABLE_LOCAL_DASHBOARD", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Dashboard login session settings. The signing secret defaults to a random value
# persisted in the dashboard_settings table (so sessions survive restarts with no
# extra config); set DASHBOARD_SESSION_SECRET to pin it across replicas. Set
# DASHBOARD_COOKIE_SECURE=true when serving over HTTPS so the cookie is only sent
# over TLS.
DASHBOARD_SESSION_COOKIE = "hivehub_dashboard_session"
DASHBOARD_SESSION_SECRET_ENV = os.environ.get("DASHBOARD_SESSION_SECRET", "").strip()
DASHBOARD_SESSION_TTL_HOURS = int(os.environ.get("DASHBOARD_SESSION_TTL_HOURS", "168"))
DASHBOARD_COOKIE_SECURE = os.environ.get("DASHBOARD_COOKIE_SECURE", "false").strip().lower() in (
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
