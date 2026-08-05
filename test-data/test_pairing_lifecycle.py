#!/usr/bin/env python3
"""Claim / release lifecycle tests against a real PostgreSQL database.

Unlike the other suites here, these need a live database: the whole point is
that the bug they guard lived in SQL, not in logic that can be unit-tested.
`claimed_at` was never cleared when the last member removed a device, so a
device removed in the app stayed claimed by nobody — invisible to everyone and
impossible to re-claim, because claim_device only matches `claimed_at IS NULL`.
Both docs described the intended behaviour for a long time while the code did
something else, which is exactly the kind of drift a test catches.

The endpoint functions are called directly, so the assertions run against the
real SQL and the real schema built by init_db().

Usage:
    DATABASE_URL=postgresql://... PYTHONPATH=server python3 test_pairing_lifecycle.py
    # --require-db turns "cannot connect" into a failure instead of a skip,
    # so CI can never go green by quietly skipping everything.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# The server modules read these at import time.
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("HIVEPAL_SERVICE_API_KEY", "test-service-key-0123456789")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789abcdef")

REQUIRE_DB = "--require-db" in sys.argv

from fastapi import HTTPException  # noqa: E402

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

import app_api  # noqa: E402
import local_dashboard  # noqa: E402
from devices import ensure_device_config  # noqa: E402
from schemas import ClaimDeviceIn, DeviceDeleteIn, ShareDeviceIn  # noqa: E402

FAILURES = []
MIGRATION = os.path.join(
    os.path.dirname(__file__), "..", "server", "migrations",
    "022_release_orphaned_devices.sql",
)


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


def expect_http(label, fn, status):
    """Assert fn() raises HTTPException with `status`; return it for detail checks."""
    try:
        fn()
    except HTTPException as exc:
        ok = exc.status_code == status
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: {exc.status_code} {exc.detail[:64]!r}")
        if not ok:
            FAILURES.append(label)
        return exc
    print(f"  FAIL {label}: expected HTTP {status}, nothing was raised")
    FAILURES.append(label)
    return None


def reset():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in ("hive_readings", "measurements", "device_commands",
                          "device_configs", "device_members", "device_channels",
                          "insight_alerts", "devices"):
                cur.execute(f"DELETE FROM {table};")
            conn.commit()


def claimed_at(device_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT claimed_at FROM devices WHERE device_id = %s;", (device_id,))
            row = cur.fetchone()
            return row[0] if row else "NO ROW"


def upload(device_id="hive_01", code="ABCD-1234"):
    """One measurement upload. Returns what the device is told about its claim."""
    return ensure_device_config(device_id, code, "0.24.9", "k" * 32, touch_last_seen=True)


def run_migration():
    with open(MIGRATION) as fh:
        sql = fh.read()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()


print("\n=== solo device: removing it releases the pairing ===")
reset()
check("first upload reports unclaimed", upload(), False)
app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234", display_name="Garden"), user_id="u1")
check("claimed_at set", claimed_at("hive_01") is not None, True)
check("upload now reports claimed", upload(), True)

result = app_api.remove_device_membership("hive_01", user_id="u1")
check("remove reports released", result["released"], True)
check("claimed_at cleared", claimed_at("hive_01"), None)
# The device learns the pairing is gone and starts sending its claim code again.
check("upload reports unclaimed again", upload(), False)
check("same claim code re-pairs it",
      app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="u1")["status"],
      "claimed")

print("\n=== shared device: only the last member out releases it ===")
reset()
upload()
app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="owner")
app_api.add_device_member("hive_01", ShareDeviceIn(user_id="friend", role="viewer"), user_id="owner")

result = app_api.remove_device_membership("hive_01", user_id="friend")
check("viewer leaving does not release", result["released"], False)
check("claimed_at still set", claimed_at("hive_01") is not None, True)

result = app_api.remove_device_membership("hive_01", user_id="owner")
check("last member leaving releases", result["released"], True)
check("claimed_at cleared", claimed_at("hive_01"), None)

print("\n=== owner release drops every member at once ===")
reset()
upload()
app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="owner")
app_api.add_device_member("hive_01", ShareDeviceIn(user_id="f1", role="viewer"), user_id="owner")
app_api.add_device_member("hive_01", ShareDeviceIn(user_id="f2", role="admin"), user_id="owner")

expect_http("a viewer cannot release",
            lambda: app_api.release_device_claim("hive_01", user_id="f1"), 403)
expect_http("an admin cannot release",
            lambda: app_api.release_device_claim("hive_01", user_id="f2"), 403)
result = app_api.release_device_claim("hive_01", user_id="owner")
check("every member removed", result["members_removed"], 3)
check("claimed_at cleared", claimed_at("hive_01"), None)
check("claimable by a different user",
      app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="new")["status"],
      "claimed")

print("\n=== claim errors: unknown code (404) vs already claimed (409) ===")
reset()
upload()
expect_http("unknown code -> 404",
            lambda: app_api.claim_device(ClaimDeviceIn(claim_code="NOPE-9999"), user_id="u1"), 404)
app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="u1")
exc = expect_http("already claimed by me -> 409",
                  lambda: app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="u1"), 409)
check("message points at my own device list", "device list" in (exc.detail if exc else ""), True)
exc = expect_http("claimed by someone else -> 409",
                  lambda: app_api.claim_device(ClaimDeviceIn(claim_code="ABCD-1234"), user_id="u2"), 409)
check("message points at the owner", "owner must release" in (exc.detail if exc else ""), True)

print("\n=== duplicate claim codes: the unclaimed device is preferred ===")
reset()
upload("hive_a", "SAME-CODE")
upload("hive_b", "SAME-CODE")
app_api.claim_device(ClaimDeviceIn(claim_code="SAME-CODE"), user_id="u1")
check("second claim finds the still-unclaimed one",
      app_api.claim_device(ClaimDeviceIn(claim_code="SAME-CODE"), user_id="u2")["status"],
      "claimed")
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM devices WHERE claimed_at IS NOT NULL;")
        check("both devices ended up claimed", cur.fetchone()[0], 2)

print("\n=== migration 022 repairs devices orphaned by the old behaviour ===")
reset()
upload("orphan", "ORPH-0001")
upload("healthy", "HLTH-0001")
app_api.claim_device(ClaimDeviceIn(claim_code="ORPH-0001"), user_id="u1")
app_api.claim_device(ClaimDeviceIn(claim_code="HLTH-0001"), user_id="u2")
# Reproduce the pre-fix breakage exactly: membership gone, claimed_at left behind.
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM device_members WHERE device_id = 'orphan';")
        conn.commit()
expect_http("orphan is unclaimable beforehand",
            lambda: app_api.claim_device(ClaimDeviceIn(claim_code="ORPH-0001"), user_id="u3"), 409)

run_migration()
check("orphan released", claimed_at("orphan"), None)
check("device still in use is untouched", claimed_at("healthy") is not None, True)
check("orphan claimable again",
      app_api.claim_device(ClaimDeviceIn(claim_code="ORPH-0001"), user_id="u3")["status"],
      "claimed")
run_migration()
check("re-running does not un-claim the fresh claim", claimed_at("orphan") is not None, True)

print("\n=== local dashboard device delete removes every dependent row ===")
reset()
upload("doomed", "DOOM-0001")
app_api.claim_device(ClaimDeviceIn(claim_code="DOOM-0001"), user_id="u1")
with get_conn() as conn:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO measurements (device_id, measured_at, raw_json) "
            "VALUES ('doomed', now(), '{}'::jsonb) RETURNING id;")
        measurement_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO hive_readings (measurement_id, device_id, hive_index, measured_at) "
            "VALUES (%s, 'doomed', 1, now());", (measurement_id,))
        cur.execute("INSERT INTO device_commands (device_id, command_type) VALUES ('doomed','reboot');")
        cur.execute("INSERT INTO device_channels (device_id, channel_number, name) VALUES ('doomed',1,'A');")
        conn.commit()

expect_http("wrong claim code refused", lambda: local_dashboard.local_delete_device(
    "doomed", DeviceDeleteIn(claim_code="WRONG-CODE", confirm_device_id="doomed")), 403)
expect_http("mismatched confirmation refused", lambda: local_dashboard.local_delete_device(
    "doomed", DeviceDeleteIn(claim_code="DOOM-0001", confirm_device_id="something-else")), 400)

result = local_dashboard.local_delete_device(
    "doomed", DeviceDeleteIn(claim_code="DOOM-0001", confirm_device_id="doomed"))
check("measurement count reported", result["measurements_deleted"], 1)
with get_conn() as conn:
    with conn.cursor() as cur:
        # hive_readings cascades from measurements; members/channels from devices.
        for table in ("devices", "measurements", "hive_readings", "device_commands",
                      "device_configs", "device_members", "device_channels"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE device_id = 'doomed';")
            check(f"{table} emptied", cur.fetchone()[0], 0)

reset()
print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
    sys.exit(1)
print("All pairing-lifecycle checks passed.")
