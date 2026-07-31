-- 021_device_command_attempts.sql — bound and recover abandoned device commands.
--
-- next_command marks a row 'claimed' and only ever serves 'pending' rows, and
-- nothing expired a claim. So a device that took a command and then died — or
-- merely lost the fire-and-forget result POST after a multi-minute BLE relay —
-- stranded that command in 'claimed' permanently: never retried, never failed,
-- and (once the dashboard started reflecting command state) leaving the node's
-- relay button disabled forever. A real HiveInside relay sat like this for over
-- six hours with the HiveHub alive and checking in the whole time.
--
-- `attempts` counts how many times a command has been handed out, so the sweep
-- in expire_stale_claimed_commands can requeue a retryable command a bounded
-- number of times and then fail it with a stated reason instead of retrying a
-- command that reliably kills the device. init_db() in server/db.py applies the
-- same change idempotently; this is the standalone migration for an
-- already-running database.
ALTER TABLE device_commands
    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS device_commands_claimed_idx
    ON device_commands (status, claimed_at)
    WHERE status = 'claimed';
