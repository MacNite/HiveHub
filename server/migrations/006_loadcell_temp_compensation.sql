-- HiveScale load-cell temperature-compensation migration
-- Adds per-device, per-scale temperature-compensation coefficients to
-- device_configs. HX711/load-cell readings drift with temperature; the backend
-- corrects for this on read using these coefficients (see server/tempcomp.py
-- and server/main.py::attach_temperature_compensation). The raw weight in the
-- measurements table is never altered, so the correction can be re-tuned or
-- disabled at any time without losing data.
--
-- Columns:
--   tempco_enabled          master switch; compensation is a no-op until true
--   tempco_source           which temperature channel to use:
--                           'ambient' (SHT4x, default) | 'hive_1' | 'hive_2'
--   tempco_ref_temp_c       temperature at which the correction is zero (°C)
--   scaleN_tempco_kg_per_c  drift of the reported weight per °C, in kg/°C
--
-- Correction applied:
--   compensated_kg = raw_kg - coeff_kg_per_c * (temp_c - ref_temp_c)
--
-- Safe to run multiple times. init_db() in server/main.py creates the same
-- columns with IF NOT EXISTS, so applying this migration is optional for fresh
-- deployments and idempotent for existing ones.

BEGIN;

ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS tempco_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS tempco_source TEXT NOT NULL DEFAULT 'ambient';
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS tempco_ref_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS scale1_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS scale2_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;

COMMIT;
