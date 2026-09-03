-- Retain the normalized claim code so an authenticated administrator can read
-- it back in Device & admin. Existing devices populate this on their next
-- upload after installing firmware that honours the config endpoint's
-- claim_code_required recovery flag.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS claim_code TEXT;
