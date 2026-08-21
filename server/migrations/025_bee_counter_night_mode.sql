-- 025_bee_counter_night_mode.sql — HiveTraffic night mode, per device.
--
-- The counter's 48 IR emitters dominate its power draw by an order of
-- magnitude, and European honey bees are diurnal: flight needs light, and stops
-- below roughly 10 C regardless. So HiveHub can now tell a paired HiveTraffic
-- counter to stop sensing through a nightly window. The counter has no clock —
-- it is told a bounded DURATION and re-armed each cycle — so the schedule lives
-- here, alongside the send interval the device already polls for.
--
-- Per DEVICE rather than per hive: every counter on one hub is at one apiary,
-- under one sunset. A per-hive window would be a JSONB column and a second
-- editor for a distinction nobody has asked for.
--
--   beecounter_night_mode_enabled  master switch; OFF, so nothing changes for
--                                  an existing device until it is turned on
--   beecounter_night_start_minute  window start, LOCAL minutes since midnight
--   beecounter_night_end_minute    window end; start > end wraps midnight,
--                                  which the 20:00-06:00 default does
--   beecounter_night_max_traffic   crossings in the last cycle above which
--                                  night mode is postponed to the next one;
--                                  0 disables the check
--   timezone                       POSIX TZ string. Load-bearing: the device
--                                  clock is UTC, so without it "20:00" drifts
--                                  by an hour twice a year and quietly discards
--                                  an hour of foraging on long summer evenings
--
-- init_db() in server/main.py applies the same columns idempotently; this is
-- the standalone migration for an already-running database.

ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_night_mode_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_night_start_minute INTEGER NOT NULL DEFAULT 1200;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_night_end_minute INTEGER NOT NULL DEFAULT 360;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_night_max_traffic INTEGER NOT NULL DEFAULT 0;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT '';

-- Range checks rather than trust: an out-of-range minute makes the firmware
-- refuse the whole window (night_mode.h::decide returns INVALID_WINDOW), which
-- presents as "night mode never happened" with nothing saying why. Rejecting it
-- at the write is the only place a human sees the mistake.
ALTER TABLE device_configs
    DROP CONSTRAINT IF EXISTS device_configs_night_start_check;
ALTER TABLE device_configs
    ADD CONSTRAINT device_configs_night_start_check
    CHECK (beecounter_night_start_minute BETWEEN 0 AND 1439);
ALTER TABLE device_configs
    DROP CONSTRAINT IF EXISTS device_configs_night_end_check;
ALTER TABLE device_configs
    ADD CONSTRAINT device_configs_night_end_check
    CHECK (beecounter_night_end_minute BETWEEN 0 AND 1439);
ALTER TABLE device_configs
    DROP CONSTRAINT IF EXISTS device_configs_night_max_traffic_check;
ALTER TABLE device_configs
    ADD CONSTRAINT device_configs_night_max_traffic_check
    CHECK (beecounter_night_max_traffic >= 0);
