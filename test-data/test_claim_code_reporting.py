#!/usr/bin/env python3
"""Static regression check for one-shot claim-code recovery."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sensors = (ROOT / "firmware" / "src" / "sensors.cpp").read_text()
network = (ROOT / "firmware" / "src" / "hivehub_network.cpp").read_text()
devices = (ROOT / "server" / "devices.py").read_text()

# Existing devices have claimRegistered persisted in NVS. The upgraded backend
# asks for the missing value, firmware clears the latch, and exactly the next
# cycle reports it. It must not leak into every later measurement or SD backup.
assert 'config["claim_code_required"] = bool(row and row[0])' in devices
assert 'doc["claim_code_required"] | false' in network
assert "clearClaimRegistered();" in network
assert 'claimCode.length() > 0 && !claimRegistered' in sensors

print("Claim-code reporting regression check passed.")
