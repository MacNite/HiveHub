-- 010_firmware_release_target_version_unique.sql
--
-- Firmware releases were originally keyed on a globally UNIQUE(version). That
-- meant a HiveScale, BeeCounter and HiveInside build could not share a version
-- number: uploading e.g. HiveInside 0.3.0 would collide with HiveScale 0.3.0 and
-- the upsert's ON CONFLICT (version) would overwrite the existing row's target,
-- filename, active flag and crc32.
--
-- Releases are really identified by (target, version), so drop the legacy
-- single-column unique constraint and replace it with a composite unique index.
-- The upload upsert uses ON CONFLICT (target, version) against this index.
--
-- This is also performed idempotently by init_db() at startup; the file exists
-- so deployments that apply migrations manually pick up the change too.

ALTER TABLE firmware_releases
    DROP CONSTRAINT IF EXISTS firmware_releases_version_key;

CREATE UNIQUE INDEX IF NOT EXISTS firmware_releases_target_version_key
    ON firmware_releases (target, version);
