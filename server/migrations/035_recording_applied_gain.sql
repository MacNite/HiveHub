-- 035_recording_applied_gain.sql — the gain the node actually applied.
--
-- HiveInside 0.8.0 can choose the recording gain itself (gain_db = 127 in the
-- request, issue HiveInside#78) and reports the gain it applied in every DATA
-- header. `gain_db` keeps meaning "what was requested"; this column is what
-- the audio on disk was recorded with. NULL = unknown (node < 0.8.0, hub too
-- old to report it, or no audio arrived).
ALTER TABLE hive_recordings
    ADD COLUMN IF NOT EXISTS applied_gain_db INTEGER;
