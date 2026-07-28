"""Tests for the HiveInside OTA relay gating (server/commands.py).

Run: python3 -m pytest test-data/test_hiveinside_ota_gate.py
 or: PYTHONPATH=server python3 test-data/test_hiveinside_ota_gate.py

Covers:
  * the slot check — `slot` is a hive index, so every hive (1..MAX_HIVES) can be
    targeted, not just the two the legacy bleSensorMac0/1 globals could reach;
  * the version gate — a release is only relayed when it is strictly newer than
    the version the node advertises, with an unknown version never blocking and
    `force` overriding the comparison.

The commands import needs a handful of env vars (config.py reads them at import
time); dummy values are injected below so the test runs without a real database
or FastAPI runtime. db.py builds its pool with open=False, so importing it does
not connect.
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Satisfy config.py's required env vars so `import commands` works offline.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from fastapi import HTTPException  # noqa: E402

import commands  # noqa: E402
from schemas import MAX_HIVES  # noqa: E402

_failures = 0


def check(label, cond):
    global _failures
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        _failures += 1


def gate(current, release, force=False):
    """Run the version gate with `current` as the node's advertised version."""
    with mock.patch.object(commands, "reported_hiveinside_version", return_value=current):
        return commands.check_hiveinside_is_newer("dev-1", 1, release, force)


def test_every_hive_index_is_a_valid_slot():
    for n in (1, 2, 3, 9, MAX_HIVES):
        commands.check_hiveinside_slot(n)
        check(f"slot {n} accepted", True)


def test_out_of_range_slots_are_rejected():
    for bad in (0, -1, MAX_HIVES + 1):
        try:
            commands.check_hiveinside_slot(bad)
            check(f"slot {bad} rejected", False)
        except HTTPException as e:
            check(f"slot {bad} rejected with 400", e.status_code == 400)


def test_newer_release_passes_and_reports_what_it_replaces():
    # The returned value is the version being replaced, so a caller can render
    # "0.4.0 -> 0.4.1" without a second request.
    check("0.4.0 -> 0.4.1 allowed", gate("0.4.0", "0.4.1") == "0.4.0")
    # Numeric, not lexical: 10 > 9.
    check("0.9.9 -> 0.10.0 allowed", gate("0.9.9", "0.10.0") == "0.9.9")
    # Uneven component counts compare as tuples.
    check("0.4 -> 0.4.1 allowed", gate("0.4", "0.4.1") == "0.4")


def test_same_or_older_release_is_refused():
    for current, release in (("0.4.1", "0.4.1"), ("0.4.1", "0.4.0"), ("1.0.0", "0.9.9")):
        try:
            gate(current, release)
            check(f"{current} -> {release} refused", False)
        except HTTPException as e:
            check(
                f"{current} -> {release} refused with 409 naming both versions",
                e.status_code == 409 and current in e.detail and release in e.detail,
            )


def test_unknown_version_never_blocks():
    # A node that never advertised a version cannot be compared against — and an
    # update may be exactly what a silent node needs.
    check("unknown current allowed", gate(None, "0.4.1") is None)


def test_force_overrides_the_comparison():
    check("force allows the same version", gate("0.4.1", "0.4.1", force=True) == "0.4.1")
    check("force allows an older release", gate("0.5.0", "0.4.1", force=True) == "0.5.0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_failures} failure(s)")
    sys.exit(1 if _failures else 0)
