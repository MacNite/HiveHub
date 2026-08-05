-- 022_release_orphaned_devices.sql — one-off repair for devices that were left
-- claimed by nobody.
--
-- Before this release, removing a device in HivePal deleted the caller's
-- device_members row but left devices.claimed_at set. A device whose last
-- member removed it therefore stayed flagged as claimed with zero members:
--   * nobody could see its readings (every app read joins device_members), and
--   * nobody could ever claim it again, because the claim endpoint only matches
--     rows with claimed_at IS NULL — so the claim code returned
--     "404 No unclaimed device found with that claim code" forever, even while
--     the device was happily uploading.
--
-- app_api.remove_device_membership now clears claimed_at when the last member
-- leaves, so no new orphans are created. This migration repairs the ones an
-- existing database is already carrying.
--
-- Only devices with NO members at all are touched. A shared device always has
-- at least one member row, so nothing that is genuinely in use is affected, and
-- no measurements, config or channel names are modified. Safe to run repeatedly
-- and safe to skip if you would rather re-pair by hand.
--
-- After running it, an affected device becomes claimable again with its
-- original claim code. Firmware from this release onwards notices the release
-- on its next upload (the server answers "claimed": false) and starts sending
-- the claim code again by itself; older firmware that already latched its
-- "claim registered" flag needs the claim code re-submitted in the setup portal
-- or a factory reset before it will re-offer the code.

UPDATE devices d
SET claimed_at = NULL
WHERE d.claimed_at IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM device_members m WHERE m.device_id = d.device_id
  );
