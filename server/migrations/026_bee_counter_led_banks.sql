-- 026_bee_counter_led_banks.sql — per-bank emitter enables for HiveTraffic.
--
-- The 2026-08 counter board puts one IRLB8721 MOSFET behind each MCP23017, so
-- each third of the entrance is independently switchable:
--
--     bank 1 -> U2, gates 00..07
--     bank 2 -> U3, gates 10..17
--     bank 3 -> U4, gates 20..27
--
-- Measured on the counter's 3.3 V rail: one bank ~0.14 A, two ~0.22 A, three
-- ~0.30 A. Roughly 80 mA per bank on top of a ~60 mA floor, which makes this
-- the coarsest power control the counter has — for an entrance narrower than
-- 24 gates, or a supply that will not carry the full board. It stacks with
-- night mode (025) rather than replacing it: night mode decides WHEN the
-- counter stops, this decides HOW MUCH of it runs at all.
--
--   beecounter_bank1_enabled  gates 00..07
--   beecounter_bank2_enabled  gates 10..17
--   beecounter_bank3_enabled  gates 20..27
--
-- All three default to TRUE, so nothing changes for an existing device: a
-- counter runs its whole entrance until someone deliberately narrows it.
--
-- Per DEVICE rather than per hive, for the same reason night mode is: the
-- alternative is a JSONB column and a second editor for a distinction nobody
-- has asked for. A hub whose hives need different bank masks is a hub whose
-- counters should be split across two devices.
--
-- Deliberately three booleans rather than one bitmask integer. The dashboard
-- draws three checkboxes, the API is read by humans, and "banks = 5" is a
-- worse thing to find in a config dump than "bank2 = false". The firmware
-- assembles the mask; the database stores what was ticked.
--
-- There is intentionally NO constraint forbidding all three false. The
-- dashboard refuses to save it and the counter refuses to apply it (a mask of
-- 0 leaves the previous mask in force), so the database is the wrong place for
-- a third opinion — and a CHECK here would fail a PATCH that turns the last
-- one off and another one on in the same request.
--
-- init_db() in server/main.py applies the same columns idempotently; this is
-- the standalone migration for an already-running database.

ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_bank1_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_bank2_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE device_configs
    ADD COLUMN IF NOT EXISTS beecounter_bank3_enabled BOOLEAN NOT NULL DEFAULT true;
