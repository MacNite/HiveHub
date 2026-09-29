-- 031_device_channel_hive_links.sql — remember which HivePal hive a slot is.
--
-- HivePal maps each of a device's hive slots (1..18) to one of its own hives.
-- Until now only the display name crossed over, so the link was a name match:
-- renaming the hive in HivePal, or two hives sharing a name, broke it. The
-- HivePal hive id is stored next to the name so the link survives both.
--
-- Nullable and free-form: HiveHub never resolves it, it only hands it back.
ALTER TABLE device_channels
    ADD COLUMN IF NOT EXISTS hivepal_hive_id TEXT;
