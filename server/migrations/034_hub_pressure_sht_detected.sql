-- 034_hub_pressure_sht_detected.sql — keep two fields the hub already sends.
--
-- The hub firmware uploads ambient_pressure_hpa (when a BME280 is fitted) and
-- sht_detected (the boot-time ambient-sensor detection, next to the per-cycle
-- sht_ok). Neither was declared in the ingest schema, so both were dropped
-- before raw_json was written and never stored anywhere. They now have typed
-- columns; readings uploaded before this change cannot be recovered.
--
-- init_db() in server/db.py adds the same columns on startup; this is the
-- standalone migration for an already-running database.
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ambient_pressure_hpa DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sht_detected BOOLEAN;
