-- 015_dashboard_auth.sql — username + password login for the local dashboard.
-- The local dashboard (ENABLE_LOCAL_DASHBOARD) used to be auth-free, so exposing
-- it to the internet exposed every device. It is now gated by login accounts:
-- the first visit runs a setup wizard that creates the initial admin, and every
-- data/control endpoint then requires a valid session (admin role for writes).
--
-- init_db() in server/main.py creates the same objects idempotently; this is the
-- standalone migration for an already-running database.

-- Login accounts. Passwords are stored as salted PBKDF2-HMAC-SHA256 hashes
-- (algo$iters$salt$hash), never in plaintext. Roles: admin (full control) or
-- viewer (read-only).
CREATE TABLE IF NOT EXISTS dashboard_users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- Dashboard-wide settings. Currently holds the auto-generated session signing
-- secret so logins survive restarts without requiring DASHBOARD_SESSION_SECRET
-- to be set explicitly.
CREATE TABLE IF NOT EXISTS dashboard_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
