-- 031_hive_device_names.sql — follow the hive name the device itself reports.
--
-- device_channels.name was the only name the read APIs ever returned, so a hive
-- renamed in the firmware's setup portal (AP mode) kept its old label forever:
-- every upload carried the new name in hives[].name, but a stored channel name
-- shadowed it. The stored name is now an explicit *override* and the name the
-- device last reported is kept beside it, so a rename in AP mode is picked up on
-- the next upload while a name typed into the dashboard still wins.
--
--   name           — the override (NULL = follow the device's own name)
--   device_name    — the name the device last reported for this hive
--   device_name_at — measured_at of the reading device_name came from, so an
--                    out-of-order SD-card import cannot resurrect an old name.
--
-- init_db() in server/main.py applies the same change idempotently; this is the
-- standalone migration for an already-running database.

ALTER TABLE device_channels ADD COLUMN IF NOT EXISTS device_name TEXT;
ALTER TABLE device_channels ADD COLUMN IF NOT EXISTS device_name_at TIMESTAMPTZ;

-- 1. Stored names that the device itself once reported were never deliberate
--    overrides — they are copies made at claim time or by saving the pre-filled
--    name form. Drop them so those hives follow the device again. Guarded on
--    device_name IS NULL, which is only true before this migration has filled it
--    in, so re-running (init_db on every boot) cannot touch a later override.
UPDATE device_channels dc
   SET name = NULL
 WHERE dc.device_name IS NULL
   AND dc.name IS NOT NULL
   AND dc.name <> ''
   AND EXISTS (
       SELECT 1 FROM hive_readings hr
        WHERE hr.device_id = dc.device_id
          AND hr.hive_index = dc.channel_number
          AND hr.name = dc.name
   );

-- 2. Seed device_name from the newest reading that carried one.
UPDATE device_channels dc
   SET device_name = l.name,
       device_name_at = l.measured_at
  FROM (
       SELECT DISTINCT ON (device_id, hive_index)
              device_id, hive_index, name, measured_at
         FROM hive_readings
        WHERE name IS NOT NULL AND name <> ''
          AND hive_index BETWEEN 1 AND 18
        ORDER BY device_id, hive_index, measured_at DESC
  ) l
 WHERE dc.device_id = l.device_id
   AND dc.channel_number = l.hive_index
   AND dc.device_name IS NULL;

-- 3. Hives that reported a name but never had a channel row (never renamed in
--    the app or dashboard) get one, so the name is served without waiting for
--    the next upload.
INSERT INTO device_channels (device_id, channel_number, device_name, device_name_at)
SELECT l.device_id, l.hive_index, l.name, l.measured_at
  FROM (
       SELECT DISTINCT ON (device_id, hive_index)
              device_id, hive_index, name, measured_at
         FROM hive_readings
        WHERE name IS NOT NULL AND name <> ''
          AND hive_index BETWEEN 1 AND 18
        ORDER BY device_id, hive_index, measured_at DESC
  ) l
  JOIN devices d ON d.device_id = l.device_id
ON CONFLICT (device_id, channel_number) DO NOTHING;
