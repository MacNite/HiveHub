"""Database pool, connection helper and schema bootstrap (init_db)."""

import hashlib

from psycopg_pool import ConnectionPool

from config import DATABASE_URL, DB_POOL_MAX_SIZE, DB_POOL_MIN_SIZE


db_pool = ConnectionPool(
    DATABASE_URL,
    min_size=DB_POOL_MIN_SIZE,
    max_size=DB_POOL_MAX_SIZE,
    open=False,
)


def get_conn():
    return db_pool.connection()


def hash_claim_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    claim_code_hash TEXT,
                    claim_code TEXT,
                    claimed_at TIMESTAMPTZ,
                    display_name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ,
                    last_firmware_version TEXT
                );

                CREATE TABLE IF NOT EXISTS device_members (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    UNIQUE (device_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS device_channels (
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    channel_number INTEGER NOT NULL CHECK (channel_number BETWEEN 1 AND 18),
                    name TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (device_id, channel_number)
                );

                -- A device now carries up to 18 hives (firmware v0.20.0), so the
                -- channel naming map must accept channel numbers 1..18, not just
                -- the original two. Relax the legacy IN (1, 2) CHECK on databases
                -- created before multi-hive support.
                ALTER TABLE device_channels
                    DROP CONSTRAINT IF EXISTS device_channels_channel_number_check;
                ALTER TABLE device_channels
                    ADD CONSTRAINT device_channels_channel_number_check
                    CHECK (channel_number BETWEEN 1 AND 18);

                CREATE TABLE IF NOT EXISTS measurements (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    measurement_id TEXT,
                    measured_at TIMESTAMPTZ NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    scale_1_weight_kg DOUBLE PRECISION,
                    scale_2_weight_kg DOUBLE PRECISION,
                    hive_1_temp_c DOUBLE PRECISION,
                    hive_2_temp_c DOUBLE PRECISION,
                    hive_1_humidity_percent DOUBLE PRECISION,
                    hive_2_humidity_percent DOUBLE PRECISION,
                    ambient_temp_c DOUBLE PRECISION,
                    ambient_humidity_percent DOUBLE PRECISION,
                    battery_voltage DOUBLE PRECISION,
                    battery_soc_percent DOUBLE PRECISION,
                    battery_alert BOOLEAN,
                    battery_monitor_ok BOOLEAN,
                    solar_monitor_ok BOOLEAN,
                    solar_bus_voltage_v DOUBLE PRECISION,
                    solar_shunt_voltage_mv DOUBLE PRECISION,
                    solar_load_voltage_v DOUBLE PRECISION,
                    solar_current_ma DOUBLE PRECISION,
                    solar_power_mw DOUBLE PRECISION,
                    network_transport TEXT,
                    calibration_mode BOOLEAN,
                    boot_count BIGINT,
                    time_source TEXT,
                    rssi_dbm INTEGER,
                    firmware_version TEXT,
                    config_version INTEGER,
                    sd_ok BOOLEAN,
                    rtc_ok BOOLEAN,
                    sht_ok BOOLEAN,
                    scale_1_raw BIGINT,
                    scale_2_raw BIGINT,
                    -- INMP441 stereo microphone columns
                    mic_ok                   BOOLEAN,
                    mic_sample_rate_hz       INTEGER,
                    mic_sample_frames        INTEGER,
                    mic_left_ok              BOOLEAN,
                    mic_left_rms_dbfs        DOUBLE PRECISION,
                    mic_left_peak_dbfs       DOUBLE PRECISION,
                    mic_left_rms_normalized  DOUBLE PRECISION,
                    mic_right_ok             BOOLEAN,
                    mic_right_rms_dbfs       DOUBLE PRECISION,
                    mic_right_peak_dbfs      DOUBLE PRECISION,
                    mic_right_rms_normalized DOUBLE PRECISION,
                    -- INMP441 FFT frequency band energy columns (dBFS)
                    mic_left_band_sub_bass_dbfs  DOUBLE PRECISION,
                    mic_left_band_hum_dbfs       DOUBLE PRECISION,
                    mic_left_band_piping_dbfs    DOUBLE PRECISION,
                    mic_left_band_stress_dbfs    DOUBLE PRECISION,
                    mic_left_band_high_dbfs      DOUBLE PRECISION,
                    mic_right_band_sub_bass_dbfs DOUBLE PRECISION,
                    mic_right_band_hum_dbfs      DOUBLE PRECISION,
                    mic_right_band_piping_dbfs   DOUBLE PRECISION,
                    mic_right_band_stress_dbfs   DOUBLE PRECISION,
                    mic_right_band_high_dbfs     DOUBLE PRECISION,
                    -- BeeCounter entrance counter columns (per hive)
                    bee_counter_1_ok                BOOLEAN,
                    bee_counter_1_protocol_version  INTEGER,
                    bee_counter_1_status_flags      INTEGER,
                    bee_counter_1_uptime_s          BIGINT,
                    bee_counter_1_num_gates         INTEGER,
                    bee_counter_1_gates_healthy     INTEGER,
                    bee_counter_1_total_in          BIGINT,
                    bee_counter_1_total_out         BIGINT,
                    bee_counter_1_interval_in       BIGINT,
                    bee_counter_1_interval_out      BIGINT,
                    bee_counter_1_glitch_count      BIGINT,
                    bee_counter_1_busy_retries      INTEGER,
                    bee_counter_1_read_attempts     INTEGER,
                    bee_counter_1_latch_succeeded   BOOLEAN,
                    bee_counter_2_ok                BOOLEAN,
                    bee_counter_2_protocol_version  INTEGER,
                    bee_counter_2_status_flags      INTEGER,
                    bee_counter_2_uptime_s          BIGINT,
                    bee_counter_2_num_gates         INTEGER,
                    bee_counter_2_gates_healthy     INTEGER,
                    bee_counter_2_total_in          BIGINT,
                    bee_counter_2_total_out         BIGINT,
                    bee_counter_2_interval_in       BIGINT,
                    bee_counter_2_interval_out      BIGINT,
                    bee_counter_2_glitch_count      BIGINT,
                    bee_counter_2_busy_retries      INTEGER,
                    bee_counter_2_read_attempts     INTEGER,
                    bee_counter_2_latch_succeeded   BOOLEAN,
                    -- LIS3DH/LIS2DH12 per-hive vibration columns (mg)
                    accel_1_ok                      BOOLEAN,
                    accel_1_sample_rate_hz          INTEGER,
                    accel_1_sample_count            INTEGER,
                    accel_1_range_g                 INTEGER,
                    accel_1_rms_mg                  DOUBLE PRECISION,
                    accel_1_peak_mg                 DOUBLE PRECISION,
                    accel_1_band_swarm_mg           DOUBLE PRECISION,
                    accel_1_band_fanning_mg         DOUBLE PRECISION,
                    accel_1_band_activity_mg        DOUBLE PRECISION,
                    accel_2_ok                      BOOLEAN,
                    accel_2_sample_rate_hz          INTEGER,
                    accel_2_sample_count            INTEGER,
                    accel_2_range_g                 INTEGER,
                    accel_2_rms_mg                  DOUBLE PRECISION,
                    accel_2_peak_mg                 DOUBLE PRECISION,
                    accel_2_band_swarm_mg           DOUBLE PRECISION,
                    accel_2_band_fanning_mg         DOUBLE PRECISION,
                    accel_2_band_activity_mg        DOUBLE PRECISION,
                    -- HolyIot 25015 in-hive BLE sensor columns (per hive)
                    ble_1_humidity_percent          DOUBLE PRECISION,
                    ble_1_pressure_hpa              DOUBLE PRECISION,
                    ble_2_humidity_percent          DOUBLE PRECISION,
                    ble_2_pressure_hpa              DOUBLE PRECISION,
                    raw_json JSONB NOT NULL
                );

                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_soc_percent DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_alert BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS battery_monitor_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_monitor_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_bus_voltage_v DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_shunt_voltage_mv DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_load_voltage_v DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_current_ma DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS solar_power_mw DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS network_transport TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS calibration_mode BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS boot_count BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS time_source TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS firmware_version TEXT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS config_version INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sd_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS rtc_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sht_ok BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS scale_1_raw BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS scale_2_raw BIGINT;
                -- mic columns (idempotent for existing deployments)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_ok                   BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_sample_rate_hz       INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_sample_frames        INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_ok              BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_rms_dbfs        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_peak_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_rms_normalized  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_ok             BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_rms_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_peak_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_rms_normalized DOUBLE PRECISION;
                -- fft band columns (idempotent)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_sub_bass_dbfs  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_hum_dbfs       DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_piping_dbfs    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_stress_dbfs    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_left_band_high_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_sub_bass_dbfs DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_hum_dbfs      DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_piping_dbfs   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_stress_dbfs   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS mic_right_band_high_dbfs     DOUBLE PRECISION;

                -- bee counter columns (idempotent for existing deployments)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_protocol_version  INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_status_flags      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_uptime_s          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_num_gates         INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_gates_healthy     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_total_in          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_total_out         BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_interval_in       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_interval_out      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_glitch_count      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_busy_retries      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_read_attempts     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_1_latch_succeeded   BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_protocol_version  INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_status_flags      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_uptime_s          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_num_gates         INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_gates_healthy     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_total_in          BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_total_out         BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_interval_in       BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_interval_out      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_glitch_count      BIGINT;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_busy_retries      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_read_attempts     INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bee_counter_2_latch_succeeded   BOOLEAN;

                -- Widen the two counters HiveTraffic's wire revision 3 grew from
                -- 16 to 32 bits (see migrations/024_bee_counter_wide_counters.sql).
                -- ADD COLUMN IF NOT EXISTS above cannot do this: the columns
                -- already exist on every deployment created before v3, as
                -- INTEGER. Postgres INTEGER is SIGNED 32-bit and stops at
                -- 2147483647, while the device can report up to 4294967295 —
                -- and the glitch tally is designed to arrive at exactly that
                -- when it saturates, which would raise "integer out of range"
                -- and fail the entire measurement, not just that field.
                --
                -- Guarded on the current type rather than run unconditionally:
                -- ALTER COLUMN ... TYPE rewrites the table, and init_db() runs
                -- on every process start.
                -- All four were created together and are widened together, so
                -- one of them is a sound guard for the set.
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'measurements'
                          AND column_name = 'bee_counter_1_uptime_s'
                          AND data_type = 'integer'
                    ) THEN
                        ALTER TABLE measurements
                            ALTER COLUMN bee_counter_1_uptime_s     TYPE BIGINT,
                            ALTER COLUMN bee_counter_1_glitch_count TYPE BIGINT,
                            ALTER COLUMN bee_counter_2_uptime_s     TYPE BIGINT,
                            ALTER COLUMN bee_counter_2_glitch_count TYPE BIGINT;
                    END IF;
                END $$;

                -- accelerometer (per-hive vibration) columns (idempotent)
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_sample_rate_hz    INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_sample_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_range_g           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_rms_mg            DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_peak_mg           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_swarm_mg     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_fanning_mg   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_1_band_activity_mg  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_ok                BOOLEAN;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_sample_rate_hz    INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_sample_count      INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_range_g           INTEGER;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_rms_mg            DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_peak_mg           DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_swarm_mg     DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_fanning_mg   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS accel_2_band_activity_mg  DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_1_humidity_percent    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_1_pressure_hpa        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_2_humidity_percent    DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS ble_2_pressure_hpa        DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hive_1_humidity_percent   DOUBLE PRECISION;
                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS hive_2_humidity_percent   DOUBLE PRECISION;

                -- beehivemonitoring.com GATT sensors (HiveHeart / HiveScale), idempotent
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

                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS api_key_hash TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS display_name TEXT;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_firmware_version TEXT;
                -- Accept-to-apply OTA gate: the ESP32 self-update only proceeds
                -- once the device owner approves a specific version for THIS device
                -- in HivePal. check_firmware returns update=true only when
                -- approved_firmware_version equals the latest available version, so
                -- a fielded device never auto-flashes an unapproved build.
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS approved_firmware_version TEXT;

                -- Board/architecture the device last reported on its OTA check
                -- (?board=esp32 / esp32-c6). Lets the HivePal status/approve flow
                -- resolve the latest release for THIS device's board instead of
                -- approving a version that only exists for the other architecture.
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS last_board TEXT;

                -- Retired / decommissioned devices can be hidden from the local
                -- dashboard's hive picker without deleting their history. The
                -- admin "Visible devices" panel toggles this flag; a hidden
                -- device still ingests data and stays fully addressable in the
                -- API — it is only dropped from the top-bar hive picker.
                ALTER TABLE devices ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false;

                ALTER TABLE measurements ADD COLUMN IF NOT EXISTS measurement_id TEXT;
                CREATE UNIQUE INDEX IF NOT EXISTS measurements_device_measurement_id_key
                    ON measurements (device_id, measurement_id)
                    WHERE measurement_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_measurements_device_time
                    ON measurements (device_id, measured_at DESC);

                -- ── Per-hive readings (normalized child of measurements) ──────
                -- One row per hive per measurement cycle, so a single ESP32 can
                -- carry up to 18 hives without a per-hive column explosion. The
                -- legacy measurements.scale_1/2_* / hive_1/2_* columns stay
                -- populated for hives 1–2 (historical continuity + the existing
                -- column-based read/insights/temp-comp paths); this table is the
                -- source of truth for ALL hives the read path exposes.
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

                CREATE TABLE IF NOT EXISTS device_configs (
                    device_id TEXT PRIMARY KEY,
                    send_interval_seconds INTEGER NOT NULL DEFAULT 600,
                    scale1_offset BIGINT NOT NULL DEFAULT 0,
                    scale1_factor DOUBLE PRECISION NOT NULL DEFAULT -7050.0,
                    scale2_offset BIGINT NOT NULL DEFAULT 0,
                    scale2_factor DOUBLE PRECISION NOT NULL DEFAULT -7050.0,
                    config_version INTEGER NOT NULL DEFAULT 1,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    -- Load-cell temperature compensation (see server/tempcomp.py)
                    tempco_enabled BOOLEAN NOT NULL DEFAULT false,
                    tempco_source TEXT NOT NULL DEFAULT 'ambient',
                    tempco_ref_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0,
                    scale1_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    scale2_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0
                );

                -- Temperature-compensation columns (idempotent for existing deployments)
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_enabled BOOLEAN NOT NULL DEFAULT false;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_source TEXT NOT NULL DEFAULT 'ambient';
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_ref_temp_c DOUBLE PRECISION NOT NULL DEFAULT 20.0;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale1_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale2_tempco_kg_per_c DOUBLE PRECISION NOT NULL DEFAULT 0.0;
                -- Per-hive calibration / temp-comp maps for hives beyond 1–2 (JSON
                -- keyed by hive_index). scale1/2_* columns stay for hives 1–2.
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS tempco_by_hive JSONB;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS scale_offsets_by_hive JSONB;

                -- HiveTraffic night mode (migration 025). Per device, not per
                -- hive: every counter on one hub shares an apiary and a sunset.
                -- The window is LOCAL minutes since midnight and may wrap
                -- (20:00-06:00 is 1200 -> 360); `timezone` is a POSIX TZ string
                -- and is load-bearing, because the device clock is UTC and
                -- without it the window drifts an hour at each DST change.
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_night_mode_enabled BOOLEAN NOT NULL DEFAULT false;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_night_start_minute INTEGER NOT NULL DEFAULT 1200;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_night_end_minute INTEGER NOT NULL DEFAULT 360;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_night_max_traffic INTEGER NOT NULL DEFAULT 0;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT '';
                -- An out-of-range minute makes the firmware refuse the window
                -- outright, which presents as "night mode never happened" with
                -- nothing saying why. Reject it where a human can see it.
                ALTER TABLE device_configs DROP CONSTRAINT IF EXISTS device_configs_night_start_check;
                ALTER TABLE device_configs ADD CONSTRAINT device_configs_night_start_check
                    CHECK (beecounter_night_start_minute BETWEEN 0 AND 1439);
                ALTER TABLE device_configs DROP CONSTRAINT IF EXISTS device_configs_night_end_check;
                ALTER TABLE device_configs ADD CONSTRAINT device_configs_night_end_check
                    CHECK (beecounter_night_end_minute BETWEEN 0 AND 1439);
                ALTER TABLE device_configs DROP CONSTRAINT IF EXISTS device_configs_night_max_traffic_check;
                ALTER TABLE device_configs ADD CONSTRAINT device_configs_night_max_traffic_check
                    CHECK (beecounter_night_max_traffic >= 0);

                -- Inspection timeout (migration 027). The safety net for an
                -- inspection nobody switched off: without it one forgotten
                -- button press means an indefinite hive-data blackout that
                -- looks exactly like a dead sensor. Per device and editable
                -- from the dashboard; the hub picks it up with the rest of
                -- /config and persists it, so an offline boot still ends its
                -- inspections.
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS inspection_timeout_minutes INTEGER NOT NULL DEFAULT 60;
                ALTER TABLE device_configs DROP CONSTRAINT IF EXISTS device_configs_inspection_timeout_check;
                ALTER TABLE device_configs ADD CONSTRAINT device_configs_inspection_timeout_check
                    CHECK (inspection_timeout_minutes BETWEEN 1 AND 1440);

                -- HiveTraffic emitter-bank enables (migration 026). One
                -- IRLB8721 MOSFET per MCP23017 since the 2026-08 board, so each
                -- third of the entrance is independently switchable: bank 1 =
                -- gates 00..07, bank 2 = 10..17, bank 3 = 20..27. Measured at
                -- 3.3 V, one bank draws ~0.14 A, two ~0.22 A, three ~0.30 A.
                -- All TRUE by default: a counter runs its whole entrance until
                -- someone deliberately narrows it. Three booleans rather than a
                -- bitmask because the dashboard draws three checkboxes and a
                -- config dump should be readable. No CHECK forbidding all-false
                -- here — the dashboard refuses it and the counter refuses to
                -- apply it, and a constraint would break a PATCH that turns the
                -- last one off and another one on at once.
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_bank1_enabled BOOLEAN NOT NULL DEFAULT true;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_bank2_enabled BOOLEAN NOT NULL DEFAULT true;
                ALTER TABLE device_configs ADD COLUMN IF NOT EXISTS beecounter_bank3_enabled BOOLEAN NOT NULL DEFAULT true;

                CREATE TABLE IF NOT EXISTS firmware_releases (
                    id BIGSERIAL PRIMARY KEY,
                    version TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS target TEXT NOT NULL DEFAULT 'hivescale';
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS crc32 BIGINT;
                -- Owner scoping: a release uploaded from HivePal is private to the
                -- uploading device's owner (owner_user_id = that HivePal user id).
                -- A NULL owner_user_id is a global / "official" release (e.g. pushed
                -- via the master-key POST /api/v1/firmware/releases) that any device
                -- may fall back to. See latest_release_for_owner / check_firmware.
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS owner_user_id TEXT;

                -- Board/architecture a hivescale image was built for (esp32 vs
                -- esp32-c6). These two SoCs (Xtensa vs RISC-V) take incompatible
                -- images, so OTA must match on board; check_firmware filters on it.
                -- hiveinside is single-board (nrf54lm20a, stamped or NULL); the
                -- beecounter target is single-architecture (NULL).
                ALTER TABLE firmware_releases
                    ADD COLUMN IF NOT EXISTS board TEXT;

                -- Backfill existing hivescale releases from their board-stamped
                -- filename (rename_firmware.py: hivehub_esp32_<v> /
                -- hivehub_esp32-c6_<v>; legacy builds used the hivescale_ prefix).
                -- Detection is token-based, so either prefix works. 'esp32-c6'
                -- contains 'esp32', so tag C6 first, then plain esp32 excluding
                -- the C6 names.
                UPDATE firmware_releases SET board = 'esp32-c6'
                    WHERE board IS NULL AND target = 'hivescale'
                      AND (filename ILIKE '%esp32-c6%' OR filename ILIKE '%esp32c6%'
                           OR filename ILIKE '%xiao%');
                UPDATE firmware_releases SET board = 'esp32'
                    WHERE board IS NULL AND target = 'hivescale'
                      AND filename ILIKE '%esp32%'
                      AND filename NOT ILIKE '%esp32-c6%' AND filename NOT ILIKE '%esp32c6%';

                -- A release is identified by (owner_user_id, target, board, version):
                -- each owner keeps their own release per (target, board, version),
                -- and the same version can also exist as a global release
                -- (owner_user_id NULL) and per board (one esp32 + one esp32-c6 build).
                -- NULLs are distinct in a plain unique index, so we key on
                -- COALESCE(owner_user_id, '') / COALESCE(board, '') to collapse
                -- global / boardless rows to one per (target, version). The upsert
                -- relies on this index for its ON CONFLICT inference. Drop the legacy
                -- globally-unique version constraint and the older indexes it
                -- replaced (including the pre-board (owner, target, version) one).
                ALTER TABLE firmware_releases
                    DROP CONSTRAINT IF EXISTS firmware_releases_version_key;
                DROP INDEX IF EXISTS firmware_releases_target_version_key;
                DROP INDEX IF EXISTS firmware_releases_owner_target_version_key;
                CREATE UNIQUE INDEX IF NOT EXISTS firmware_releases_owner_target_board_version_key
                    ON firmware_releases (COALESCE(owner_user_id, ''), target, COALESCE(board, ''), version);

                -- Retire the deprecated ESP32-C6 HiveInside board (removed from the
                -- ecosystem): HiveInside now means the nRF54LM20A, and the relay no
                -- longer matches per board, so any lingering esp32-c6 hiveinside
                -- release is deactivated here so it is never served. See migration
                -- 020_drop_hiveinside_c6.sql.
                UPDATE firmware_releases SET active = false
                    WHERE target = 'hiveinside' AND board = 'esp32-c6';

                -- Hive inspections (migration 027). Recorded as intervals
                -- rather than as a flag on each reading: one row per inspection
                -- is what lets a chart shade the window, the insight engine
                -- skip it, and every raw reading stay exactly as the hub sent
                -- it — an inspection hides data from the interpreters, it never
                -- deletes it. See migrations/027_inspections.sql for the column
                -- semantics.
                CREATE TABLE IF NOT EXISTS device_inspections (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    hive_indexes SMALLINT[],
                    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    ended_at TIMESTAMPTZ,
                    source TEXT NOT NULL DEFAULT 'device',
                    end_reason TEXT,
                    requested_at TIMESTAMPTZ,
                    acknowledged_at TIMESTAMPTZ,
                    note TEXT,
                    created_by TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                -- At most one OPEN inspection per device. This is the constraint
                -- that makes every trigger a toggle: the button, HivePal's
                -- button and the timeout all need exactly one unambiguous row
                -- to close when the hive is shut again.
                CREATE UNIQUE INDEX IF NOT EXISTS device_inspections_open_idx
                    ON device_inspections (device_id)
                    WHERE ended_at IS NULL;

                -- Every read is "which inspections overlap this time range".
                CREATE INDEX IF NOT EXISTS device_inspections_device_time_idx
                    ON device_inspections (device_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS device_commands (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    command_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    claimed_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                );

                -- How many times a command has been handed to a device (added
                -- after the table shipped). A device that claims a command and
                -- then dies — or simply loses the fire-and-forget result POST —
                -- used to strand the row in 'claimed' forever, since nothing
                -- expired it and only 'pending' rows are ever served again. The
                -- counter bounds the retry so a command that reliably kills the
                -- device cannot loop indefinitely. See expire_stale_claimed_commands.
                ALTER TABLE device_commands
                    ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0;

                -- Sweeping stale claims scans by status + claim time.
                CREATE INDEX IF NOT EXISTS device_commands_claimed_idx
                    ON device_commands (status, claimed_at)
                    WHERE status = 'claimed';

                -- Persisted lifecycle of sensor-based insight alerts so HivePal
                -- can show a *history* (alerts are otherwise recomputed live and
                -- never stored). One row per distinct alert occurrence: while an
                -- alert keeps firing the same row is updated (last_seen_at bumped);
                -- when it stops firing it is resolved (resolved_at set). A later
                -- recurrence of the same detector creates a fresh row. The partial
                -- unique index guarantees at most one *active* row per detector.
                CREATE TABLE IF NOT EXISTS insight_alerts (
                    id BIGSERIAL PRIMARY KEY,
                    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                    alert_key TEXT NOT NULL,
                    category TEXT NOT NULL,
                    channel INTEGER NOT NULL,
                    severity TEXT NOT NULL,
                    peak_severity TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
                    source TEXT NOT NULL DEFAULT '',
                    window_start TIMESTAMPTZ,
                    window_end TIMESTAMPTZ,
                    first_seen_at TIMESTAMPTZ NOT NULL,
                    last_seen_at TIMESTAMPTZ NOT NULL,
                    resolved_at TIMESTAMPTZ,
                    update_count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                CREATE UNIQUE INDEX IF NOT EXISTS insight_alerts_active_uniq
                    ON insight_alerts (device_id, alert_key)
                    WHERE resolved_at IS NULL;

                CREATE INDEX IF NOT EXISTS insight_alerts_device_first_seen_idx
                    ON insight_alerts (device_id, first_seen_at DESC);

                -- Alert-notification bookkeeping (added after the table shipped).
                -- notified_at / notified_severity record the last severity a row
                -- was *dispatched* at (e-mail / Web Push), so the reconciler fires
                -- once when an alert first appears and again only if it escalates,
                -- and never re-sends the same state after a restart.
                ALTER TABLE insight_alerts
                    ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
                ALTER TABLE insight_alerts
                    ADD COLUMN IF NOT EXISTS notified_severity TEXT;

                -- Local-dashboard login accounts. The dashboard is auth-gated when
                -- enabled (ENABLE_LOCAL_DASHBOARD): the first visit creates the
                -- initial admin via the setup wizard, after which every data /
                -- control endpoint requires a valid session. Passwords are stored
                -- as salted PBKDF2-HMAC-SHA256 hashes, never in plaintext.
                CREATE TABLE IF NOT EXISTS dashboard_users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'viewer')),
                    email TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_login_at TIMESTAMPTZ
                );

                -- Contact email for insights-based alerts. Added after the table
                -- shipped, so existing databases need the column back-filled.
                ALTER TABLE dashboard_users
                    ADD COLUMN IF NOT EXISTS email TEXT;

                -- Small key/value store for dashboard-wide settings, currently the
                -- auto-generated session signing secret (so logins survive restarts
                -- without requiring DASHBOARD_SESSION_SECRET to be configured).
                CREATE TABLE IF NOT EXISTS dashboard_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );

                -- Browser / PWA Web Push subscriptions, one row per device+user
                -- that opted in from the dashboard. endpoint is the push service
                -- URL (unique per subscription); p256dh/auth are the client keys
                -- used to encrypt the payload. failure_count lets the sender prune
                -- endpoints the push service has permanently rejected (404/410).
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES dashboard_users(id) ON DELETE CASCADE,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_used_at TIMESTAMPTZ
                );

                -- Published charts: the slices of hive data an admin has chosen
                -- to serve publicly (see server/publish.py). `token` is the
                -- capability the embed URL carries, so it is random and unique;
                -- `series` holds the resolved {device_id, hive, label} list, and
                -- the public payload ships only the labels. Deleting a row (or
                -- clearing `enabled`) revokes the embed immediately.
                CREATE TABLE IF NOT EXISTS published_charts (
                    id BIGSERIAL PRIMARY KEY,
                    token TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    subtitle TEXT,
                    metric TEXT NOT NULL,
                    chart_type TEXT NOT NULL DEFAULT 'line',
                    aggregate TEXT NOT NULL DEFAULT 'none',
                    series JSONB NOT NULL,
                    range_days INTEGER NOT NULL DEFAULT 30,
                    options JSONB NOT NULL DEFAULT '{}'::jsonb,
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    created_by BIGINT REFERENCES dashboard_users(id) ON DELETE SET NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    view_count BIGINT NOT NULL DEFAULT 0,
                    last_viewed_at TIMESTAMPTZ
                );
                """
            )
            conn.commit()
