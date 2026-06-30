-- 014_device_channels_18.sql — allow per-hive display names for all hives a
-- device carries. A single ESP32 now reports up to 18 hives (firmware v0.20.0,
-- see 012_multi_hive.sql), but device_channels originally capped channel_number
-- at 1–2. Relax the CHECK so hives 3–18 can be named too.
--
-- init_db() in server/main.py applies the same change idempotently; this is the
-- standalone migration for an already-running database.

ALTER TABLE device_channels
    DROP CONSTRAINT IF EXISTS device_channels_channel_number_check;
ALTER TABLE device_channels
    ADD CONSTRAINT device_channels_channel_number_check
    CHECK (channel_number BETWEEN 1 AND 18);
