"""Environment-driven configuration and shared constants."""

import logging
import os
from pathlib import Path


# Backend server version. Bump on releases; reported in the OpenAPI metadata
# (/docs) and by GET /health so a deployment's version is visible remotely.
SERVER_VERSION = "0.3.0"

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
# Whether the rate limiter may trust CF-Connecting-IP / X-Forwarded-For for the
# client IP. Defaults to false (secure by default): trusting these headers is
# only safe when a reverse proxy in front of the API overwrites them. If the API
# is reachable directly, a client can spoof the header to dodge the limiter or
# poison another client's bucket. Set it to true once the API sits behind a
# trusted proxy / Cloudflare so per-client limits key off the real client IP.
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "false").strip().lower() in (
    "1", "true", "yes", "on",
)
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


def _env_bool(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# ── Insight alert notifications (optional) ───────────────────────────────────
# When a new insight alert fires (or an existing one escalates in severity), the
# backend can push it out over two independent channels: e-mail (SMTP) and Web
# Push (browser / installable PWA). Both are OFF until configured; the alert
# lifecycle in insight_alerts is unaffected either way. Only alerts at or above
# NOTIFY_MIN_SEVERITY are ever dispatched, so the noisy "info"/"watch" tiers can
# be kept in the dashboard without spamming phones. Recipient e-mail addresses
# are NOT configured here — they are stored per dashboard account
# (dashboard_users.email), while the credentials below are server-wide infra.
NOTIFY_MIN_SEVERITY = os.environ.get("NOTIFY_MIN_SEVERITY", "warning").strip().lower()

# ── SMTP (e-mail channel) ────────────────────────────────────────────────────
# Set SMTP_ENABLED=true and point SMTP_HOST/PORT at a relay. Use SMTP_TLS=true
# for implicit TLS (usually port 465) or SMTP_STARTTLS=true to upgrade a plain
# connection (usually port 587). SMTP_FROM defaults to SMTP_USERNAME.
SMTP_ENABLED = _env_bool("SMTP_ENABLED")
SMTP_HOST = os.environ.get("SMTP_HOST", "localhost").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_TLS = _env_bool("SMTP_TLS")  # implicit TLS (SMTPS, e.g. port 465)
SMTP_STARTTLS = _env_bool("SMTP_STARTTLS", "true")  # STARTTLS upgrade (e.g. 587)
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USERNAME
SMTP_FROM_NAME = os.environ.get("SMTP_FROM_NAME", "HiveHub").strip()
SMTP_TIMEOUT_SECONDS = int(os.environ.get("SMTP_TIMEOUT_SECONDS", "20"))

# ── Web Push (browser / PWA channel) ─────────────────────────────────────────
# Web Push needs a VAPID key pair (application server keys). Generate one with
# `python gen_vapid.py` (see server/gen_vapid.py) or the `vapid` CLI, then
# set the three vars below. VAPID_SUBJECT must be a mailto: or https: URL that
# identifies you to push services. The public key is also exposed to the browser
# via GET /api/v1/local/notifications/config so it can subscribe.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "").strip()
WEB_PUSH_ENABLED = _env_bool("WEB_PUSH_ENABLED") and bool(
    VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT
)

logger = logging.getLogger("hivescale.insights")

# Earliest year a device-supplied measurement timestamp is trusted. Anything
# older (notably the 1970 epoch a device emits when RTC and NTP both fail) is
# treated as a missing timestamp and replaced with the server clock on ingest.
MIN_PLAUSIBLE_YEAR = 2020
