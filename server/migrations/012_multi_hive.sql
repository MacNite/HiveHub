-- 012_multi_hive.sql — normalized per-hive readings so one ESP32 can carry up to
-- 18 hives (firmware v0.20.0) without a per-hive column explosion on measurements.
--
-- The legacy measurements.scale_1/2_* / hive_1/2_* columns are kept and still
-- populated for hives 1–2 (historical continuity + the existing column-based
-- read / insights / temp-compensation paths). This table is the source of truth
-- for ALL hives the read path exposes (flat scale_N_*/hive_N_* keys + a hives[]
-- array). init_db() in server/main.py creates the same objects idempotently; this
-- file is the standalone migration for an already-running database.

CREATE TABLE IF NOT EXISTS hive_readings (
    id                BIGSERIAL PRIMARY KEY,
    measurement_id    BIGINT NOT NULL REFERENCES measurements(id) ON DELETE CASCADE,
    device_id         TEXT NOT NULL,
    measured_at       TIMESTAMPTZ NOT NULL,
    hive_index        SMALLINT NOT NULL,          -- 1..18
    name              TEXT,
    weight_kg         DOUBLE PRECISION,
    raw_weight        BIGINT,
    scale_source      TEXT,                       -- hx711 | nau7802 | ...
    temp_c            DOUBLE PRECISION,
    temp_source       TEXT,                       -- ds18b20 | ble | hiveheart
    humidity_percent  DOUBLE PRECISION,
    accel_ok                BOOLEAN,
    accel_sample_count      INTEGER,
    accel_range_g           INTEGER,
    accel_rms_mg            DOUBLE PRECISION,
    accel_peak_mg           DOUBLE PRECISION,
    accel_band_swarm_mg     DOUBLE PRECISION,
    accel_band_fanning_mg   DOUBLE PRECISION,
    accel_band_activity_mg  DOUBLE PRECISION,
    ble_present       BOOLEAN,
    ble_sensor_type   TEXT,
    ble_humidity_percent DOUBLE PRECISION,
    ble_pressure_hpa  DOUBLE PRECISION,
    ble_battery_percent INTEGER,
    ble_rssi_dbm      INTEGER,
    bee_counter_ok           BOOLEAN,
    bee_counter_total_in     BIGINT,
    bee_counter_total_out    BIGINT,
    bee_counter_interval_in  BIGINT,
    bee_counter_interval_out BIGINT,
    raw_json          JSONB,
    UNIQUE (measurement_id, hive_index)
);

CREATE INDEX IF NOT EXISTS idx_hive_readings_device_hive_time
    ON hive_readings (device_id, hive_index, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_hive_readings_measurement
    ON hive_readings (measurement_id);

-- Per-hive scale calibration map (offset/factor keyed by hive_index → scale slot)
-- so all up to 18 hives can be calibrated; scale1/2_* columns stay for hives 1–2.
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_by_hive JSONB;
ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale_offsets_by_hive JSONB;
