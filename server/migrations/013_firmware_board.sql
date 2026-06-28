-- 013_firmware_board.sql — tag each hivescale firmware release with the board /
-- chip architecture it was built for (esp32 vs esp32-c6) so OTA never serves a
-- 30-pin ESP32 (Xtensa) image to an ESP32-C6 (RISC-V) or vice versa.
--
-- The device now reports its board in the firmware check (?board=...), and the
-- server only serves a release whose board matches. init_db() in server/main.py
-- applies the same changes idempotently; this is the standalone migration for an
-- already-running database.

ALTER TABLE firmware_releases ADD COLUMN IF NOT EXISTS board TEXT;

-- The board a device last reported on its OTA check (?board=esp32 / esp32-c6), so
-- the HivePal status/approve flow resolves the latest release for THIS device's
-- architecture rather than approving a version that only exists for the other one.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_board TEXT;

-- Backfill existing hivescale releases from their board-stamped filename
-- (firmware/rename_firmware.py names them hivescale_esp32_<v> / hivescale_esp32-c6_<v>).
-- Order matters: 'esp32-c6' contains the substring 'esp32', so tag the C6 builds
-- first, then plain esp32 excluding the C6 names. beecounter / hiveinside targets
-- are single-architecture and intentionally left with board = NULL.
UPDATE firmware_releases SET board = 'esp32-c6'
    WHERE board IS NULL AND target = 'hivescale'
      AND (filename ILIKE '%esp32-c6%' OR filename ILIKE '%esp32c6%' OR filename ILIKE '%xiao%');
UPDATE firmware_releases SET board = 'esp32'
    WHERE board IS NULL AND target = 'hivescale'
      AND filename ILIKE '%esp32%'
      AND filename NOT ILIKE '%esp32-c6%' AND filename NOT ILIKE '%esp32c6%';

-- board joins the release identity so one esp32 build and one esp32-c6 build of the
-- same version can coexist. Replace the (owner, target, version) unique index with
-- one that also includes the board.
DROP INDEX IF EXISTS firmware_releases_owner_target_version_key;
CREATE UNIQUE INDEX IF NOT EXISTS firmware_releases_owner_target_board_version_key
    ON firmware_releases (COALESCE(owner_user_id, ''), target, COALESCE(board, ''), version);
