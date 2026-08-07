-- HiveHub "publish data" migration
-- Adds published_charts: the admin-curated slices of hive data that are served
-- publicly (no login) under /embed/chart/{token} and
-- /api/v1/public/charts/{token}, so a chart can be embedded in a club website,
-- a blog or a shop page without exposing the dashboard itself.
--
-- Each row is one publication: a metric, the hives/devices it covers (resolved
-- to display labels at publish time so the public payload never carries a
-- device_id), a rolling period and the presentation options. `token` is an
-- unguessable random string — it IS the capability, so revoking a publication
-- is a DELETE (or enabled = false).
--
-- Safe to run multiple times. init_db() in server/db.py creates the same objects
-- with IF NOT EXISTS, so this migration is optional for fresh deployments and
-- idempotent for existing ones.

BEGIN;

CREATE TABLE IF NOT EXISTS published_charts (
    id BIGSERIAL PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    subtitle TEXT,
    metric TEXT NOT NULL,
    chart_type TEXT NOT NULL DEFAULT 'line',
    aggregate TEXT NOT NULL DEFAULT 'none',
    -- [{device_id, hive, label}, …] — the published series, in draw order.
    series JSONB NOT NULL,
    range_days INTEGER NOT NULL DEFAULT 30,
    -- Presentation only: theme, height, legend/updated-stamp visibility.
    options JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT REFERENCES dashboard_users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    view_count BIGINT NOT NULL DEFAULT 0,
    last_viewed_at TIMESTAMPTZ
);

COMMIT;
