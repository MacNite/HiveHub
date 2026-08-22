"""Tests for the HiveTraffic emitter-bank config guard (server/devices.py).

Run: python3 -m pytest test-data/test_bee_counter_banks.py
 or: PYTHONPATH=server python3 test-data/test_bee_counter_banks.py   (no DB needed)

A counter's 48 IR emitters sit behind three MOSFETs, one per group of eight
gates, and each can be switched off to save roughly 80 mA at 3.3 V. All three
off is the one combination that must never be stored: the counter refuses a mask
of zero outright — it keeps whatever mask it had — so a stored all-off row would
leave the dashboard showing three unticked boxes next to a counter cheerfully
counting all 24 gates, with nothing anywhere saying why.

The subtle half is that a PATCH carries only what CHANGED. "Is the last bank
being turned off?" therefore cannot be answered from the patch alone — turning
bank 3 off is perfectly fine on its own and fatal if 1 and 2 are already off —
so the guard has to merge the patch onto the stored row first. That merge is
what this file pins; it is the kind of thing that looks obviously right and is
obviously wrong the first time somebody sends a one-field patch.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# devices.py pulls in the app's config module, which requires these to exist. No
# database or network is touched — fetch_device_config is stubbed out below.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import devices  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from schemas import DeviceConfig, DeviceConfigUpdate  # noqa: E402


_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def stored(b1=True, b2=True, b3=True):
    """Install a stored config with the given bank state and return it."""
    cfg = DeviceConfig(
        device_id="dev-1",
        beecounter_bank1_enabled=b1,
        beecounter_bank2_enabled=b2,
        beecounter_bank3_enabled=b3,
    )
    devices.fetch_device_config = lambda device_id: cfg
    return cfg


def rejects(fields):
    """Does the guard refuse this patch against the currently stored config?"""
    try:
        devices.reject_all_banks_off("dev-1", dict(fields))
    except HTTPException as exc:
        return exc.status_code == 400
    return False


def test_defaults_are_all_on():
    # The whole feature is opt-in: an existing device keeps counting its full
    # entrance until someone deliberately narrows it.
    cfg = DeviceConfig(device_id="dev-1")
    check(
        "all three banks default to enabled",
        cfg.beecounter_bank1_enabled
        and cfg.beecounter_bank2_enabled
        and cfg.beecounter_bank3_enabled,
    )
    patch = DeviceConfigUpdate()
    check(
        "an empty patch touches no bank field",
        not any(f in patch.model_dump(exclude_unset=True)
                for f in devices.BEECOUNTER_BANK_FIELDS),
    )


def test_turning_one_bank_off_is_fine():
    stored()
    check("bank 1 off, 2 and 3 on", not rejects({"beecounter_bank1_enabled": False}))
    check("bank 2 off, 1 and 3 on", not rejects({"beecounter_bank2_enabled": False}))
    check("bank 3 off, 1 and 2 on", not rejects({"beecounter_bank3_enabled": False}))


def test_turning_two_banks_off_is_fine():
    # One bank on is a legitimate configuration — an eight-gate entrance at
    # ~0.14 A instead of ~0.30 A is most of the point of the feature.
    stored()
    check(
        "two off in one patch leaves one on",
        not rejects({
            "beecounter_bank1_enabled": False,
            "beecounter_bank2_enabled": False,
        }),
    )


def test_the_last_bank_cannot_be_turned_off():
    # The case a patch-only check would miss entirely: this patch touches ONE
    # field and sets it to False, which is indistinguishable from the perfectly
    # legal patch above without the stored row.
    stored(b1=False, b2=False, b3=True)
    check(
        "turning off the last enabled bank is rejected",
        rejects({"beecounter_bank3_enabled": False}),
    )
    check(
        "turning off an already-off bank is a no-op, not a rejection",
        not rejects({"beecounter_bank1_enabled": False}),
    )


def test_all_three_off_in_one_patch_is_rejected():
    stored()
    check(
        "a patch disabling all three is rejected",
        rejects({
            "beecounter_bank1_enabled": False,
            "beecounter_bank2_enabled": False,
            "beecounter_bank3_enabled": False,
        }),
    )


def test_a_patch_may_swap_which_bank_is_on():
    # Why there is no database CHECK constraint: this patch passes through a
    # moment where bank 3 is off and bank 1 is not yet on, and a column-level
    # constraint has no way to see them as one edit.
    stored(b1=False, b2=False, b3=True)
    check(
        "turning the last one off while turning another on is allowed",
        not rejects({
            "beecounter_bank3_enabled": False,
            "beecounter_bank1_enabled": True,
        }),
    )


def test_unrelated_patches_skip_the_check_entirely():
    # The guard costs a config read, so it must not run on every PATCH. With
    # all banks already off in the stored row (which the guard itself makes
    # unreachable, but a hand-edited database could produce), a patch that
    # touches no bank field still has to sail through — it is not this edit's
    # business to fail on a state it did not create.
    stored(b1=False, b2=False, b3=False)
    check(
        "a send-interval patch is not blocked by bank state",
        not rejects({"send_interval_seconds": 900}),
    )


def main():
    test_defaults_are_all_on()
    test_turning_one_bank_off_is_fine()
    test_turning_two_banks_off_is_fine()
    test_the_last_bank_cannot_be_turned_off()
    test_all_three_off_in_one_patch_is_rejected()
    test_a_patch_may_swap_which_bank_is_on()
    test_unrelated_patches_skip_the_check_entirely()

    if _failures:
        print(f"\n{_failures} check(s) FAILED")
        return 1
    print("\nAll emitter-bank config checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
else:
    def test_all():
        assert main() == 0
