-- 009_beehivemonitoring_gatt.sql
--
-- Adds columns for the beehivemonitoring.com GATT sensors:
--   * HiveHeart — in-hive sensor. Temperature/humidity already land in the
--     hive_N_* columns; this adds the acoustic frequency/energy/peak and the
--     battery voltage. The raw 8-byte FFT stays in raw_json.
--   * HiveScale — wireless weight scale: weight + raw weight plus the on-board
--     temperature/humidity/pressure/battery.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS) and backfilled from raw_json so existing
-- deployments pick up any values already uploaded inside the measurement body.
-- init_db() in main.py runs the same ADD COLUMN statements automatically; this
-- file is for applying the change manually to an older database.
BEGIN;

ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_frequency_hz     DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_energy           INTEGER;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_peak             INTEGER;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_1_battery_v        DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_frequency_hz     DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_energy           INTEGER;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_peak             INTEGER;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hiveheart_2_battery_v        DOUBLE PRECISION;

ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_weight_kg        DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_raw_weight       BIGINT;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_temp_c           DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_humidity_percent DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_pressure_hpa     DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_1_battery_v        DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_weight_kg        DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_raw_weight       BIGINT;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_temp_c           DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_humidity_percent DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_pressure_hpa     DOUBLE PRECISION;
ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hivescale_2_battery_v        DOUBLE PRECISION;

UPDATE measurements
SET
    hiveheart_1_frequency_hz     = COALESCE(hiveheart_1_frequency_hz,     NULLIF(raw_json->>'hiveheart_1_frequency_hz',     '')::double precision),
    hiveheart_1_energy           = COALESCE(hiveheart_1_energy,           NULLIF(raw_json->>'hiveheart_1_energy',           '')::integer),
    hiveheart_1_peak             = COALESCE(hiveheart_1_peak,             NULLIF(raw_json->>'hiveheart_1_peak',             '')::integer),
    hiveheart_1_battery_v        = COALESCE(hiveheart_1_battery_v,        NULLIF(raw_json->>'hiveheart_1_battery_v',        '')::double precision),
    hiveheart_2_frequency_hz     = COALESCE(hiveheart_2_frequency_hz,     NULLIF(raw_json->>'hiveheart_2_frequency_hz',     '')::double precision),
    hiveheart_2_energy           = COALESCE(hiveheart_2_energy,           NULLIF(raw_json->>'hiveheart_2_energy',           '')::integer),
    hiveheart_2_peak             = COALESCE(hiveheart_2_peak,             NULLIF(raw_json->>'hiveheart_2_peak',             '')::integer),
    hiveheart_2_battery_v        = COALESCE(hiveheart_2_battery_v,        NULLIF(raw_json->>'hiveheart_2_battery_v',        '')::double precision),
    hivescale_1_weight_kg        = COALESCE(hivescale_1_weight_kg,        NULLIF(raw_json->>'hivescale_1_weight_kg',        '')::double precision),
    hivescale_1_raw_weight       = COALESCE(hivescale_1_raw_weight,       NULLIF(raw_json->>'hivescale_1_raw_weight',       '')::bigint),
    hivescale_1_temp_c           = COALESCE(hivescale_1_temp_c,           NULLIF(raw_json->>'hivescale_1_temp_c',           '')::double precision),
    hivescale_1_humidity_percent = COALESCE(hivescale_1_humidity_percent, NULLIF(raw_json->>'hivescale_1_humidity_percent', '')::double precision),
    hivescale_1_pressure_hpa     = COALESCE(hivescale_1_pressure_hpa,     NULLIF(raw_json->>'hivescale_1_pressure_hpa',     '')::double precision),
    hivescale_1_battery_v        = COALESCE(hivescale_1_battery_v,        NULLIF(raw_json->>'hivescale_1_battery_v',        '')::double precision),
    hivescale_2_weight_kg        = COALESCE(hivescale_2_weight_kg,        NULLIF(raw_json->>'hivescale_2_weight_kg',        '')::double precision),
    hivescale_2_raw_weight       = COALESCE(hivescale_2_raw_weight,       NULLIF(raw_json->>'hivescale_2_raw_weight',       '')::bigint),
    hivescale_2_temp_c           = COALESCE(hivescale_2_temp_c,           NULLIF(raw_json->>'hivescale_2_temp_c',           '')::double precision),
    hivescale_2_humidity_percent = COALESCE(hivescale_2_humidity_percent, NULLIF(raw_json->>'hivescale_2_humidity_percent', '')::double precision),
    hivescale_2_pressure_hpa     = COALESCE(hivescale_2_pressure_hpa,     NULLIF(raw_json->>'hivescale_2_pressure_hpa',     '')::double precision),
    hivescale_2_battery_v        = COALESCE(hivescale_2_battery_v,        NULLIF(raw_json->>'hivescale_2_battery_v',        '')::double precision)
WHERE raw_json IS NOT NULL
  AND (
        raw_json ? 'hiveheart_1_frequency_hz' OR raw_json ? 'hiveheart_2_frequency_hz'
     OR raw_json ? 'hivescale_1_weight_kg'    OR raw_json ? 'hivescale_2_weight_kg'
  );

COMMIT;
