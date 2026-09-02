-- Retain the normalized claim code so an authenticated administrator can read
-- it back in Device & admin. Existing devices populate this on their next
-- upload that includes a claim code.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code TEXT;
