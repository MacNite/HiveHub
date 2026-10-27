-- 033_dashboard_session_version.sql — revocable dashboard sessions.
--
-- Dashboard sessions are signed JWTs. A signature alone cannot be withdrawn, so
-- a deleted account or a changed password used to leave every session issued
-- before it valid until expiry (DASHBOARD_SESSION_TTL_HOURS, 7 days by default).
-- Each token now carries the account's session_version, and every request
-- checks it against the live row: deleting the account or bumping the version
-- (done on password change) revokes the outstanding sessions immediately.
--
-- init_db() in server/db.py adds the same column on startup; this is the
-- standalone migration for an already-running database.
ALTER TABLE dashboard_users
    ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0;
