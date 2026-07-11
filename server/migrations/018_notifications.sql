-- HiveHub insight alert notifications migration
-- Adds the bookkeeping the notification dispatcher needs (server/notifications.py):
--   * insight_alerts.notified_at / notified_severity — the last severity a row
--     was dispatched at, so a new alert notifies once and re-notifies only when
--     it escalates, surviving restarts without re-sending the same state.
--   * push_subscriptions — browser / PWA Web Push subscriptions opted in from
--     the local dashboard.
--
-- Recipient e-mail addresses are NOT stored here; they live per account in
-- dashboard_users.email (migration 016). SMTP / VAPID credentials are env-only.
--
-- Safe to run multiple times. init_db() in server/db.py creates the same objects
-- with IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so this migration is optional
-- for fresh deployments and idempotent for existing ones.

BEGIN;

ALTER TABLE insight_alerts
    ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
ALTER TABLE insight_alerts
    ADD COLUMN IF NOT EXISTS notified_severity TEXT;

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES dashboard_users(id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);

COMMIT;
