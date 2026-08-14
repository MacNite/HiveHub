-- 024_bee_counter_wide_counters.sql — widen the BeeCounter uptime and glitch
-- columns to BIGINT for HiveTraffic wire revision 3.
--
-- The counter's measurement document widened two fields from 16 to 32 bits:
--
--   * uptime_s, which used to clamp at 65535 (18 h 12 min), so an always-on
--     counter reported one constant number for its whole deployment;
--   * glitches, a diagnostic tally that saturates rather than wrapping.
--
-- Both landed in INTEGER columns. Postgres INTEGER is SIGNED 32-bit, so it tops
-- out at 2147483647 while the device can now report up to 4294967295 — and the
-- glitch tally in particular is *designed* to arrive at exactly that value when
-- it saturates. Inserting one raises "integer out of range" and fails the whole
-- measurement, losing every other reading in it, so this is not a theoretical
-- range issue.
--
-- BIGINT matches what the totals columns (bee_counter_N_total_in/out) have
-- always used for the same reason. Widening is metadata-only in Postgres for
-- INTEGER -> BIGINT on these columns' size class, but it does rewrite the table;
-- on a large measurements table run it in a maintenance window.
--
-- The 0..3 expander-health value is NOT touched here. It is renamed rather than
-- resized on the wire (gates_healthy -> mcps_healthy) and rides in
-- hive_readings.raw_json for BLE counters, so it needs no column change. The
-- legacy measurements.bee_counter_N_gates_healthy columns are left exactly as
-- they are: they hold real readings from the removed wired era under the old
-- name and meaning, and repurposing them would make old and new rows silently
-- incomparable.
--
-- init_db() in server/db.py applies the same widening idempotently; this file is
-- the standalone migration for an already-running database. Safe to re-run.

BEGIN;

ALTER TABLE measurements
    ALTER COLUMN bee_counter_1_uptime_s     TYPE BIGINT,
    ALTER COLUMN bee_counter_1_glitch_count TYPE BIGINT,
    ALTER COLUMN bee_counter_2_uptime_s     TYPE BIGINT,
    ALTER COLUMN bee_counter_2_glitch_count TYPE BIGINT;

COMMIT;
