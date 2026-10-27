#!/usr/bin/env python3
"""Backend checks that need a real PostgreSQL database.

Covers behaviour that lives in SQL or in how connections are used, which the
mock-based suites cannot see:

- dashboard sessions are checked against the live account on every request, so
  deleting an account or changing its password revokes what was issued before;
- the chart read paths never hold a pooled connection while asking for another.
  The pool here has ONE connection, so a nested checkout times out instead of
  passing quietly the way it does with the production pool of ten;
- the upload routes (SD import, firmware) authenticate before they parse the
  body, and still accept a real upload once authenticated;
- the bulk/SD import de-duplicates on measurement_id like live ingest, and a
  timestamp without a UTC offset is stored as UTC instead of failing.

Usage:
    DATABASE_URL=postgresql://... PYTHONPATH=server python3 test_backend_db.py
    # --require-db turns "cannot connect" into a failure instead of a skip.
"""
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# The server modules read these at import time.
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("HIVEPAL_SERVICE_API_KEY", "test-service-key-0123456789")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789abcdef")
os.environ.setdefault("DASHBOARD_SESSION_SECRET", "test-dashboard-secret-0123456789abcdef")
os.environ.setdefault("ENABLE_LOCAL_DASHBOARD", "true")
os.environ.setdefault("HIVEPAL_JWT_SECRET", "test-hivepal-jwt-secret-0123456789abcdef")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ["FIRMWARE_DIR"] = tempfile.mkdtemp(prefix="hivehub-fw-")
os.environ["DB_POOL_MIN_SIZE"] = "1"
os.environ["DB_POOL_MAX_SIZE"] = "1"

REQUIRE_DB = "--require-db" in sys.argv

from db import db_pool, get_conn, init_db  # noqa: E402

# A nested checkout on a one-connection pool waits for this long, then fails.
db_pool.timeout = 3

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

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402

import auth  # noqa: E402
import firmware  # noqa: E402
import local_dashboard  # noqa: E402
import main  # noqa: E402
import middleware  # noqa: E402
import measurements  # noqa: E402
import publish  # noqa: E402
from measurements import bulk_import_measurements  # noqa: E402
from schemas import MeasurementIn  # noqa: E402

FAILURES = []
DEVICE = "hivehub-dbtest"


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


def reset():
    with get_conn() as conn:
        with conn.cursor() as cur:
            for table in ("hive_readings", "measurements", "device_configs", "device_members",
                          "firmware_releases", "devices", "dashboard_users"):
                cur.execute(f"DELETE FROM {table};")
            conn.commit()


def session_for(token):
    request = Request({
        "type": "http", "method": "GET", "path": "/", "root_path": "", "scheme": "http",
        "query_string": b"", "server": ("hub", 80),
        "headers": [(b"cookie", f"{auth.DASHBOARD_SESSION_COOKIE}={token}".encode())],
    })
    return auth.current_dashboard_session(request)


def seed_device():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devices (device_id, claim_code_hash, claim_code) VALUES (%s, 'x', 'x');",
                (DEVICE,),
            )
            cur.execute(
                "INSERT INTO device_members (device_id, user_id, role) VALUES (%s, 'u-1', 'owner');",
                (DEVICE,),
            )
            conn.commit()


def seed_measurements(n):
    seed_device()
    start = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        MeasurementIn.model_validate({
            "device_id": DEVICE,
            "timestamp": (start + timedelta(minutes=10 * i)).isoformat(),
            "scale_1_weight_kg": 40.0 + i * 0.01,
        })
        for i in range(n)
    ]
    bulk_import_measurements(DEVICE, rows)


def test_session_revocation():
    print("=== dashboard sessions follow the live account ===")
    reset()
    alice = auth.create_dashboard_user("alice", "first-password", "admin")
    bob = auth.create_dashboard_user("bob", "bobs-password", "admin")
    token = auth.create_dashboard_session_token(alice)
    check("fresh session accepted", (session_for(token) or {}).get("username"), "alice")

    updated = auth.set_dashboard_user_password(alice["id"], "second-password")
    check("password change bumps the version", updated["session_version"], alice["session_version"] + 1)
    check("session from before the change refused", session_for(token), None)
    reissued = auth.create_dashboard_session_token(updated)
    check("session issued after the change accepted", (session_for(reissued) or {}).get("username"), "alice")

    bob_token = auth.create_dashboard_session_token(bob)
    auth.delete_dashboard_user(bob["id"])
    check("session of a deleted account refused", session_for(bob_token), None)
    auth.create_dashboard_user("bob", "another-password", "viewer")
    check("old token does not carry over to a re-created namesake", session_for(bob_token), None)


def test_chart_reads_do_not_nest_connections():
    print("=== chart reads release their connection before enriching ===")
    reset()
    seed_measurements(20)
    try:
        rows = local_dashboard.local_list_measurements(DEVICE, max_points=0)
        check("dashboard chart rows on a one-connection pool", len(rows), 20)
    except Exception as exc:
        check("dashboard chart rows on a one-connection pool", type(exc).__name__, "no error")
    try:
        out = publish._measurements_for([DEVICE], datetime(2026, 1, 1, tzinfo=timezone.utc), 1500)
        check("published chart rows on a one-connection pool", len(out[DEVICE]), 20)
    except Exception as exc:
        check("published chart rows on a one-connection pool", type(exc).__name__, "no error")


def refuse_parsing(*_a, **_k):
    raise AssertionError("the body was parsed before the caller was authenticated")


def test_uploads_authenticate_before_parsing():
    print("=== upload routes refuse unauthenticated bodies before parsing them ===")
    reset()
    seed_device()
    client = TestClient(main.app)
    big_json = json.dumps({"measurements": [{"device_id": DEVICE, "scale_1_weight_kg": 1.0}] * 20000})
    files = {"file": ("fw.bin", io.BytesIO(b"\0" * 4096), "application/octet-stream")}
    with mock.patch.object(measurements, "parse_json_body", side_effect=refuse_parsing), \
            mock.patch.object(local_dashboard, "read_upload_form", side_effect=refuse_parsing), \
            mock.patch.object(firmware, "read_upload_form", side_effect=refuse_parsing):
        r = client.post(f"/api/v1/app/devices/{DEVICE}/measurements/import", content=big_json,
                        headers={"content-type": "application/json"})
        check("HivePal JSON import without a service key", r.status_code, 401)
        r = client.post(f"/api/v1/local/devices/{DEVICE}/measurements/import", files=files)
        check("local SD import without a session", r.status_code, 401)
        r = client.post(f"/api/v1/local/devices/{DEVICE}/firmware", files=files, data={"version": "1.0.0"})
        check("local firmware upload without a session", r.status_code, 401)
        r = client.post(f"/api/v1/app/devices/{DEVICE}/firmware", files=files, data={"version": "1.0.0"})
        check("HivePal firmware upload without a service key", r.status_code, 401)


def test_uploads_work_once_authenticated():
    print("=== upload routes accept real uploads once authenticated ===")
    reset()
    seed_device()
    client = TestClient(main.app)
    admin = auth.create_dashboard_user("admin", "admin-password", "admin")
    client.cookies.set(auth.DASHBOARD_SESSION_COOKIE, auth.create_dashboard_session_token(admin))
    lines = "\n".join(
        json.dumps({"device_id": DEVICE, "timestamp": f"2026-06-01T00:{m:02d}:00Z", "scale_1_weight_kg": 40.0})
        for m in range(5)
    )
    r = client.post(f"/api/v1/local/devices/{DEVICE}/measurements/import",
                    files={"file": ("measurements.ndjson", io.BytesIO(lines.encode()), "application/x-ndjson")},
                    data={"force": "false"})
    check("local SD import status", r.status_code, 200)
    check("local SD import inserted", r.json().get("inserted"), 5)
    r = client.post(f"/api/v1/local/devices/{DEVICE}/measurements/import",
                    files={"file": ("measurements.ndjson", io.BytesIO(lines.encode()), "application/x-ndjson")},
                    data={"force": "maybe"})
    check("invalid boolean form field", r.status_code, 422)
    r = client.post(f"/api/v1/local/devices/{DEVICE}/firmware",
                    files={"file": ("hivescale-esp32-c6-9.9.9.bin", io.BytesIO(b"\xe9" + b"\0" * 4095),
                                    "application/octet-stream")},
                    data={"version": "9.9.9", "board": "esp32-c6", "active": "false"})
    check("local firmware upload status", r.status_code, 200)
    check("local firmware upload version", r.json().get("version"), "9.9.9")
    r = client.post(f"/api/v1/local/devices/{DEVICE}/firmware",
                    files={"file": ("x.bin", io.BytesIO(b"\0"), "application/octet-stream")})
    check("firmware upload without a version", r.status_code, 422)

    hivepal = {
        "x-hivepal-service-key": os.environ["HIVEPAL_SERVICE_API_KEY"],
        "authorization": "Bearer " + jwt.encode({"sub": "u-1"}, os.environ["HIVEPAL_JWT_SECRET"],
                                                algorithm="HS256"),
    }
    body = {"measurements": [
        {"device_id": DEVICE, "timestamp": f"2026-06-02T00:{m:02d}:00Z", "scale_1_weight_kg": 41.0}
        for m in range(3)
    ]}
    r = client.post(f"/api/v1/app/devices/{DEVICE}/measurements/import", json=body, headers=hivepal)
    check("HivePal JSON import status", r.status_code, 200)
    check("HivePal JSON import inserted", r.json().get("inserted"), 3)
    r = client.post(f"/api/v1/app/devices/{DEVICE}/measurements/import", json={"measurements": []},
                    headers=hivepal)
    check("HivePal JSON import with an invalid body", r.status_code, 422)


def test_upload_size_caps():
    print("=== upload routes keep a byte cap of their own ===")
    client = TestClient(main.app)
    with mock.patch.object(middleware, "SD_IMPORT_MAX_FILE_SIZE", 1024), \
            mock.patch.object(middleware, "UPLOAD_ENVELOPE_BYTES", 0):
        r = client.post(f"/api/v1/app/devices/{DEVICE}/measurements/import", content=b"x" * 4096,
                        headers={"content-type": "application/json"})
        check("import body over its cap", r.status_code, 413)


def stored(device_id=DEVICE):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT measurement_id, measured_at, scale_1_weight_kg FROM measurements "
                "WHERE device_id = %s ORDER BY id;",
                (device_id,),
            )
            return cur.fetchall()


def m(**fields):
    return MeasurementIn.model_validate({"device_id": DEVICE, **fields})


def test_naive_timestamps_are_utc():
    print("=== a timestamp without a UTC offset is stored as UTC ===")
    reset()
    client = TestClient(main.app)
    r = client.post("/api/v1/measurements", headers={"x-api-key": "device-key-0123456789abcdef"},
                    json={"device_id": DEVICE, "timestamp": "2026-06-15T12:00:00", "scale_1_weight_kg": 40.0})
    check("live ingest of a naive timestamp", r.status_code, 200)
    check("stored as that instant in UTC", stored()[0][1],
          datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc))
    result = bulk_import_measurements(DEVICE, [m(timestamp="2026-06-15T13:00:00", scale_1_weight_kg=41.0)])
    check("bulk import of a naive timestamp", result["inserted"], 1)


def test_import_dedups_on_measurement_id():
    print("=== the import de-duplicates on measurement_id, like live ingest ===")
    reset()
    seed_device()
    client = TestClient(main.app)
    # A hub without a valid clock: no timestamp, so live ingest stores server time.
    r = client.post("/api/v1/measurements", headers={"x-api-key": "device-key-0123456789abcdef"},
                    json={"device_id": DEVICE, "measurement_id": "boot1-1", "scale_1_weight_kg": 40.0})
    check("live ingest without a clock", r.status_code, 200)

    # The SD backup carries the same line (still no timestamp) plus newer ones,
    # two of which share a timestamp but are different readings.
    sd = [
        m(measurement_id="boot1-1", scale_1_weight_kg=40.0),
        m(measurement_id="boot1-2", timestamp="2026-06-01T00:10:00Z", scale_1_weight_kg=40.1,
          hives=[{"index": 3, "weight_kg": 30.1}]),
        m(measurement_id="boot1-3", timestamp="2026-06-01T00:10:00Z", scale_1_weight_kg=40.2,
          hives=[{"index": 3, "weight_kg": 30.2}]),
        m(measurement_id="boot1-3", timestamp="2026-06-01T00:10:00Z", scale_1_weight_kg=40.2),
        m(timestamp="2026-06-01T00:20:00Z", scale_1_weight_kg=40.3),
    ]
    try:
        result = bulk_import_measurements(DEVICE, sd)
    except Exception as exc:
        result = {"error": type(exc).__name__}
    check("import with an id already ingested live",
          (result.get("inserted"), result.get("duplicates")), (3, 2))
    check("distinct ids sharing a timestamp are both kept",
          sorted(r[0] for r in stored() if r[0] in ("boot1-2", "boot1-3")), ["boot1-2", "boot1-3"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.measurement_id, h.weight_kg FROM hive_readings h "
                "JOIN measurements m ON m.id = h.measurement_id WHERE m.device_id = %s ORDER BY 1;",
                (DEVICE,),
            )
            hives = cur.fetchall()
    check("hive readings attached to their own row", hives, [("boot1-2", 30.1), ("boot1-3", 30.2)])

    again = bulk_import_measurements(DEVICE, sd)
    check("re-importing the same file inserts nothing", (again["inserted"], again["duplicates"]), (0, 5))
    check("row count unchanged", len(stored()), 4)

    # A row the pre-check misses (here: the lookup is skipped) still lands on
    # the conflict guard instead of aborting the batch.
    with mock.patch.object(measurements, "split_new_and_duplicate", side_effect=lambda keys, _e: (
            list(dict.fromkeys(keys)), len(keys) - len(dict.fromkeys(keys)))):
        try:
            raced = bulk_import_measurements(DEVICE, sd[1:3])
        except Exception as exc:
            raced = {"error": type(exc).__name__}
    check("conflict on measurement_id counted, not raised",
          (raced.get("inserted"), raced.get("duplicates")), (0, 2))


test_session_revocation()
test_chart_reads_do_not_nest_connections()
test_naive_timestamps_are_utc()
test_import_dedups_on_measurement_id()
test_uploads_authenticate_before_parsing()
test_uploads_work_once_authenticated()
test_upload_size_caps()
reset()

if FAILURES:
    print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("\nAll backend database checks passed.")
