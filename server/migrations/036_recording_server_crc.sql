-- 036_recording_server_crc.sql — what the server itself received.
--
-- `crc32`/`bytes` used to be the only server-side account of a recording, and
-- `crc32` was the HUB's checksum, so "verified" meant the hub and node agreed
-- about bytes the server never looked at. A proxy that cut the upload short,
-- or the server's own size cap, still produced a verified, complete recording.
--
--   server_crc32  CRC-32 the server computed over the bytes it wrote to disk.
--                 NULL for recordings stored before this column existed.
--   hub_bytes     how many bytes the hub says it uploaded (its /finalize
--                 `bytes`). NULL when the hub did not report it.
--   truncated     the server stopped writing at MAX_RECORDING_BYTES.
ALTER TABLE hive_recordings
    ADD COLUMN IF NOT EXISTS server_crc32 BIGINT;
ALTER TABLE hive_recordings
    ADD COLUMN IF NOT EXISTS hub_bytes BIGINT;
ALTER TABLE hive_recordings
    ADD COLUMN IF NOT EXISTS truncated BOOLEAN NOT NULL DEFAULT false;
