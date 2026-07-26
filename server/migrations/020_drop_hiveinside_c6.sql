-- 020_drop_hiveinside_c6.sql — retire the deprecated ESP32-C6 HiveInside board.
--
-- HiveInside is now unambiguously the nRF54LM20A beacon node; the ESP32-C6
-- HiveInside prototype was removed from the ecosystem. Any hiveinside firmware
-- release still stamped board = 'esp32-c6' must never be resolved as an OTA
-- image again (latest_hiveinside_release now applies no per-board matching and
-- would otherwise consider such a row). Deactivate them so they are never served
-- while keeping the rows for audit history. init_db() in server/db.py applies the
-- same change idempotently; this is the standalone migration for an already-
-- running database. The firmware files themselves are left on disk untouched.
UPDATE firmware_releases
    SET active = false
    WHERE target = 'hiveinside' AND board = 'esp32-c6';
