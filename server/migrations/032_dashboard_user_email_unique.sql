-- 032_dashboard_user_email_unique.sql — one dashboard account per e-mail.
--
-- Single sign-on (OIDC, e.g. authentik) maps a provider login to a dashboard
-- account by e-mail address, so an address may belong to at most one account.
-- Case-insensitive; accounts without an e-mail (NULL) are unaffected.
--
-- Aborts with the offending addresses if duplicates exist: fix those accounts
-- first (Account -> Alert email, or delete one), then re-run. init_db() in
-- server/db.py creates the same index on startup (and only logs, instead of
-- failing, when duplicates are present).
DO $$
DECLARE
    dupes TEXT;
BEGIN
    SELECT string_agg(e || ' (' || n || 'x)', ', ') INTO dupes
    FROM (
        SELECT lower(email) AS e, count(*) AS n FROM dashboard_users
        WHERE email IS NOT NULL AND email <> ''
        GROUP BY lower(email) HAVING count(*) > 1
    ) d;
    IF dupes IS NOT NULL THEN
        RAISE EXCEPTION 'dashboard_users has duplicate email addresses: %. Give each account its own address, then re-run this migration.', dupes;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS dashboard_users_email_lower_key
    ON dashboard_users (lower(email)) WHERE email IS NOT NULL AND email <> '';
