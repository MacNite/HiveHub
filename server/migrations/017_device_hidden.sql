-- 017_device_hidden.sql — hide retired devices from the dashboard hive picker.
--
-- Adds a boolean `hidden` flag to devices. When set, the local dashboard drops
-- the device from the top-bar hive picker without deleting any of its readings;
-- it keeps ingesting data and stays fully addressable through the API. The admin
-- "Visible devices" panel toggles this flag, and it can be cleared to bring a
-- device back at any time.
--
-- init_db() in server/db.py applies the same change idempotently; this is the
-- standalone migration for an already-running database. Safe to run repeatedly.

ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false;
