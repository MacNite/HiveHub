#!/usr/bin/env python3
"""Hive-name resolution tests against a real PostgreSQL database.

A hive is labelled with the name set on the device itself (its setup portal /
AP mode), which every upload carries in `hives[].name`. That name used to be
shadowed forever by whatever `device_channels.name` held — a copy made at claim
time — so a hive renamed on the device kept its old label in the dashboard and
in HivePal. These tests pin the rules that replaced it:

  * the reported name is what a hive is labelled with when nobody overrode it,
    and a rename on the device is picked up on the next upload;
  * a name typed into the dashboard still wins, and survives device renames;
  * clearing that name hands the hive back to the device;
  * an old reading replayed from an SD-card import cannot resurrect an old name;
  * the one-time backfill in init_db() drops stored names that were only ever
    copies of the device's own name, which is what unsticks existing installs.

Like test_pairing_lifecycle.py these need a live database: the behaviour lives
in SQL (an upsert with a CASE and two guards), not in logic that can be
unit-tested.

Usage:
    DATABASE_URL=postgresql://... PYTHONPATH=server python3 test_hive_names.py
    # --require-db turns "cannot connect" into a failure instead of a skip.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# The server modules read these at import time.
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("HIVEPAL_SERVICE_API_KEY", "test-service-key-0123456789")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789abcdef")

REQUIRE_DB = "--require-db" in sys.argv

from db import db_pool, get_conn, init_db  # noqa: E402

try:
    db_pool.open()
    db_pool.wait(timeout=10)
    init_db()
except Exception as exc:  # pragma: no cover - environment dependent
    if REQUIRE_DB:
        print(f"FAIL: --require-db was passed but the database is unreachable: {exc}")
        sys.exit(1)
    print(f"SKIP: no usable database ({type(exc).__name__}); set DATABASE_URL to run these.")
    sys.exit(0)

from devices import apply_device_channels, fetch_device_channels  # noqa: E402
from measurements import create_measurement  # noqa: E402
from schemas import DeviceChannelsUpdateIn, MeasurementIn  # noqa: E402

DEVICE = "hive_names_01"
API_KEY = "k" * 32
NOW = datetime.now(timezone.utc)
FAILURES = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


def reset():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in ("hive_readings", "measurements", "device_commands",
                          "device_configs", "device_members", "device_channels",
                          "insight_alerts", "devices"):
                cur.execute(f"DELETE FROM {table};")
            conn.commit()


def upload(names: dict[int, str], minutes_ago: int = 0):
    """One measurement carrying a hives[] array with the given per-hive names."""
    payload = MeasurementIn(
        device_id=DEVICE,
        claim_code="ABCD-1234",
        timestamp=NOW - timedelta(minutes=minutes_ago),
        firmware_version="0.30.4",
        hives=[{"index": idx, "name": name, "weight_kg": 40.0 + idx}
               for idx, name in names.items()],
    )
    return create_measurement(payload, x_api_key=API_KEY)


def labels():
    """What the read APIs say each hive should be called."""
    return fetch_device_channels(DEVICE)["names"]


def stored():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_number, name, device_name FROM device_channels "
                "WHERE device_id = %s ORDER BY channel_number;",
                (DEVICE,),
            )
            return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def set_names(**kwargs):
    return apply_device_channels(DEVICE, DeviceChannelsUpdateIn(**kwargs))


print("=== a hive is labelled with the name its device reports ===")
reset()
upload({1: "shire-01", 2: "shire-02"}, minutes_ago=30)
check("both hives named from the upload", labels(), {"1": "shire-01", "2": "shire-02"})
check("nothing stored as an override", fetch_device_channels(DEVICE)["custom_names"], {})
check("reported names kept separately", fetch_device_channels(DEVICE)["device_names"],
      {"1": "shire-01", "2": "shire-02"})

print("\n=== renaming a hive on the device is picked up on the next upload ===")
upload({1: "shire-01", 2: "shire-03"}, minutes_ago=20)
check("hive 2 follows the device", labels(), {"1": "shire-01", "2": "shire-03"})
check("legacy scale_2 field follows too",
      fetch_device_channels(DEVICE)["scale_2_display_name"], "shire-03")

print("\n=== an older reading cannot resurrect the old name ===")
upload({2: "shire-02"}, minutes_ago=600)
check("replayed SD-card row ignored", labels()["2"], "shire-03")

print("\n=== a name typed in the dashboard wins, and survives a device rename ===")
set_names(names={"2": "Buckfast colony"})
check("override wins", labels()["2"], "Buckfast colony")
upload({2: "shire-04"}, minutes_ago=10)
check("override survives the rename", labels()["2"], "Buckfast colony")
check("device name still tracked underneath", stored()[2][1], "shire-04")

print("\n=== clearing the override hands the hive back to the device ===")
set_names(names={"2": "  "})
check("blank clears the override", stored()[2][0], None)
check("device name is used again", labels()["2"], "shire-04")

print("\n=== an override that only copied the device's name follows a rename ===")
set_names(names={"2": "shire-04"})
check("stored as typed", stored()[2][0], "shire-04")
upload({2: "shire-05"}, minutes_ago=5)
check("copy did not stick", stored()[2][0], None)
check("hive follows the device", labels()["2"], "shire-05")

print("\n=== init_db unsticks names copied before the device_name column existed ===")
reset()
upload({1: "shire-01", 2: "shire-02"}, minutes_ago=60)
upload({1: "shire-01", 2: "shire-03"}, minutes_ago=30)
# Recreate the pre-migration state: a stored copy of the name the hive had when
# it was claimed, and no record of what the device itself reports.
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE device_channels SET name = 'shire-02', device_name = NULL, "
            "device_name_at = NULL WHERE device_id = %s AND channel_number = 2;",
            (DEVICE,),
        )
        # A genuine override on hive 1 — one the device never reported — must
        # survive the same backfill.
        cur.execute(
            "UPDATE device_channels SET name = 'Orchard', device_name = NULL, "
            "device_name_at = NULL WHERE device_id = %s AND channel_number = 1;",
            (DEVICE,),
        )
        conn.commit()
check("stale name before the backfill", labels()["2"], "shire-02")
init_db()
check("copy dropped, device name shown", labels()["2"], "shire-03")
check("real override kept", labels()["1"], "Orchard")
init_db()  # idempotent: a second boot must not clear the override
check("override survives another boot", labels()["1"], "Orchard")

reset()
print()
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
    sys.exit(1)
print("All hive-name checks passed.")
