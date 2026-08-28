-- 027_inspections.sql — hive inspections, the window while the box is open.
--
-- A beekeeper lifting two supers off a scale produces a 40 kg cliff, and a
-- brood nest with the crown board off loses several degrees in a minute. Those
-- readings are true — the load cell really did carry 40 kg less — and they are
-- also worthless as colony data, and worse than worthless as alert input: the
-- weight-drop detectors read an inspection as robbing or a swarm.
--
-- So an inspection is recorded as an interval, not as a flag on each reading.
-- One row per inspection means the read path can shade the window on a chart,
-- the insight engine can skip it, and nothing has to be deleted or nulled in
-- the measurements table — every raw reading stays exactly as the hub sent it
-- and still comes out of the CSV export.
--
--   started_at / ended_at  the window. ended_at NULL = still in progress; at
--                          most one open inspection per device (partial unique
--                          index below), which is what makes a button press
--                          "toggle" rather than "start another one".
--   hive_indexes           NULL = the whole hub, which is what the physical
--                          button means (you are standing at the stand, not at
--                          one box). A HivePal request may name specific hives.
--   source                 who started it: 'device' (the button, learned from
--                          the flag on an upload), 'api' (HivePal or another
--                          integration), 'dashboard'.
--   end_reason             'device' | 'api' | 'dashboard' | 'timeout'. A
--                          timeout is the safety net for an inspection nobody
--                          ended; without it one forgotten press is an
--                          indefinite hive-data blackout that looks exactly
--                          like a dead sensor.
--   requested_at /         an API-started inspection is only *requested* until
--   acknowledged_at        the hub picks the command up on its next wake — the
--                          hub deep-sleeps, so this is never instantaneous and
--                          the caller needs to be able to see the difference.
--   note                   free text ("removed 2 supers"), so a later reader
--                          knows why the step in the weight trace is there.
--
-- init_db() in server/db.py creates the same objects idempotently; this is the
-- standalone migration for an already-running database.

CREATE TABLE IF NOT EXISTS device_inspections (
    id BIGSERIAL PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    hive_indexes SMALLINT[],
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    source TEXT NOT NULL DEFAULT 'device',
    end_reason TEXT,
    requested_at TIMESTAMPTZ,
    acknowledged_at TIMESTAMPTZ,
    note TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one open inspection per device. The button toggles, HivePal's button
-- toggles, and the timeout closes — none of which can mean "nest a second
-- inspection inside the first", and all of which need one unambiguous row to
-- close when the hive is shut again.
CREATE UNIQUE INDEX IF NOT EXISTS device_inspections_open_idx
    ON device_inspections (device_id)
    WHERE ended_at IS NULL;

-- Every read is "which inspections overlap this chart's time range".
CREATE INDEX IF NOT EXISTS device_inspections_device_time_idx
    ON device_inspections (device_id, started_at DESC);

-- How long an inspection may run before the hub and the server both end it.
-- Per device, editable from the dashboard's device panel; the hub picks it up
-- with the rest of /config and persists it, so a hub that boots without WiFi
-- still ends its inspections.
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS inspection_timeout_minutes INTEGER NOT NULL DEFAULT 60;
ALTER TABLE device_configs
    DROP CONSTRAINT IF EXISTS device_configs_inspection_timeout_check;
ALTER TABLE device_configs
    ADD CONSTRAINT device_configs_inspection_timeout_check
    CHECK (inspection_timeout_minutes BETWEEN 1 AND 1440);
