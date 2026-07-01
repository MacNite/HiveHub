-- 016_dashboard_user_email.sql — add a contact email to dashboard accounts.
-- The email is where insights-based alerts (swarm, robbing, winter risk, …) will
-- be delivered once alert notifications are wired up. It is optional: accounts
-- created before this migration simply have a NULL email until the owner sets one
-- from the dashboard's "Your account" panel.
--
-- init_db() in server/main.py applies the same change idempotently; this is the
-- standalone migration for an already-running database. Safe to run repeatedly.

ALTER TABLE dashboard_users
    ADD COLUMN IF NOT EXISTS email TEXT;
