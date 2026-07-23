-- Make firmware retry/replay delivery idempotent. Existing historical rows do
-- not have a client ID and remain untouched; new firmware supplies one.
BEGIN;

ALTER TABLE measurements ADD COLUMN IF NOT EXISTS measurement_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS measurements_device_measurement_id_key
    ON measurements (device_id, measurement_id)
    WHERE measurement_id IS NOT NULL;

COMMIT;
