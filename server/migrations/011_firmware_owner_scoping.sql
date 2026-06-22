-- 011_firmware_owner_scoping.sql
--
-- Make firmware updates owner-specific with an accept-to-apply gate.
--
-- Until now a firmware release was global: the most recent active release per
-- target was served to EVERY device, so an upload from one owner was picked up
-- and auto-flashed by every scale. Two changes fix that:
--
--   1. firmware_releases.owner_user_id — the HivePal user a release belongs to.
--      Uploads from HivePal are scoped to the uploading device's owner; a NULL
--      owner is a global / "official" release (e.g. pushed via the master-key
--      POST /api/v1/firmware/releases) that any device may fall back to. The
--      firmware-check resolves the polling device's owner and serves the owner's
--      release first, falling back to a global one.
--
--   2. devices.approved_firmware_version — the version the owner has approved for
--      that specific device in HivePal. The ESP32 self-update (hivescale target)
--      is only served once approved_firmware_version equals the latest available
--      version, so devices never auto-flash an unapproved build; the owner accepts
--      the update from the HivePal setup panel.
--
-- This is also performed idempotently by init_db() at startup; the file exists so
-- deployments that apply migrations manually pick up the change too.

-- 1. Owner scoping for releases.
ALTER TABLE firmware_releases
    ADD COLUMN IF NOT EXISTS owner_user_id TEXT;

-- Releases are identified by (owner_user_id, target, version). NULLs are distinct
-- in a plain unique index, so key on COALESCE(owner_user_id, '') to collapse
-- global rows to one per (target, version). Replaces the (target, version) index
-- from 010. The upsert uses ON CONFLICT (COALESCE(owner_user_id, ''), target, version).
DROP INDEX IF EXISTS firmware_releases_target_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS firmware_releases_owner_target_version_key
    ON firmware_releases (COALESCE(owner_user_id, ''), target, version);

-- 2. Per-device accept-to-apply gate.
ALTER TABLE devices
    ADD COLUMN IF NOT EXISTS approved_firmware_version TEXT;
