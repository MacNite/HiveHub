#!/usr/bin/env python3
"""Recording lifecycle checks that need a real PostgreSQL database.

test_recordings.py covers the pure parts; this covers the timing and the
"who closes the row" rules, which live in SQL and can only be seen running:

- the streaming timeout counts from when the upload started, not from when the
  request was queued, so a hub that wakes late is not failed mid-session by the
  live player's own polling;
- a genuinely stalled upload still expires;
- a late /finalize or a late successful command result overrules a sweep that
  guessed wrong, while a real error is never wiped.

Usage:
    DATABASE_URL=postgresql://... PYTHONPATH=server python3 test_recordings_db.py
    # --require-db turns "cannot connect" into a failure instead of a skip.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789abcdef")
os.environ["RECORDINGS_DIR"] = tempfile.mkdtemp(prefix="hivehub-rec-")

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

import recordings  # noqa: E402
from schemas import RecordingFinalizeIn  # noqa: E402

FAILURES = []
DEVICE = "hivehub-rectest"


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got={got!r} want={want!r}")
    if not ok:
        FAILURES.append(label)


def reset():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM hive_recordings WHERE device_id = %s;", (DEVICE,))
            cur.execute("DELETE FROM device_commands WHERE device_id = %s;", (DEVICE,))
            cur.execute("DELETE FROM device_configs WHERE device_id = %s;", (DEVICE,))
            cur.execute("DELETE FROM devices WHERE device_id = %s;", (DEVICE,))
            cur.execute(
                "INSERT INTO devices (device_id, claim_code_hash, claim_code) VALUES (%s, 'x', 'x');",
                (DEVICE,),
            )
            conn.commit()


def sql(query, params=()):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            out = cur.fetchone() if cur.description else None
            conn.commit()
    return out


def new_recording(requested_ago_min=0):
    rec = recordings.request_recording(DEVICE, 1, 10)
    sql("UPDATE hive_recordings SET requested_at = now() - (%s || ' minutes')::interval "
        "WHERE id = %s;", (requested_ago_min, rec["id"]))
    cmd = sql("SELECT command_id FROM hive_recordings WHERE id = %s;", (rec["id"],))[0]
    return rec["id"], cmd


def state(rec_id):
    r = recordings.get_recording(rec_id)
    return r["status"], r["error"]


class PollingRequest:
    """An upload whose body arrives while the dashboard polls the recording."""

    def __init__(self, rec_id, chunks):
        self.rec_id = rec_id
        self.chunks = chunks
        self.seen = []

    async def stream(self):
        for c in self.chunks:
            # The live player reads the row between chunks, and every read runs
            # the stale sweep. That is exactly when the old timeout fired.
            self.seen.append(state(self.rec_id))
            yield c


def stream(rec_id, chunks):
    req = PollingRequest(rec_id, chunks)
    out = asyncio.run(recordings.stream_recording(DEVICE, rec_id, req))
    return out, req.seen


def finalize(rec_id, **fields):
    recordings.finalize_recording(DEVICE, rec_id, RecordingFinalizeIn(**fields))


def test_late_pickup_is_not_failed_mid_stream():
    print("A request the hub picks up 8 minutes late")
    rec_id, _ = new_recording(requested_ago_min=8)
    check("still requested before pickup", state(rec_id)[0], "requested")
    out, seen = stream(rec_id, [b"\x01\x00" * 400] * 3)
    check("streaming on every poll while the body arrives",
          [s for s, _ in seen], ["streaming"] * 3)
    check("every byte is recorded", out, {"status": "ok", "bytes": 2400})
    finalize(rec_id, ok=True, bytes=2400)
    check("ready after the report, with no stale error", state(rec_id), ("ready", None))


def test_stalled_stream_still_expires():
    print("An upload that started and then went silent")
    rec_id, _ = new_recording(requested_ago_min=1)
    sql("UPDATE hive_recordings SET status = 'streaming', "
        "started_at = now() - interval '6 minutes' WHERE id = %s;", (rec_id,))
    check("expired with the no-audio guess", state(rec_id),
          ("failed", recordings.SWEEP_NO_AUDIO_MSG))


def test_recent_stream_is_left_alone():
    print("An upload that started a minute ago on a request queued long before")
    rec_id, _ = new_recording(requested_ago_min=30)
    sql("UPDATE hive_recordings SET status = 'streaming', "
        "started_at = now() - interval '1 minute' WHERE id = %s;", (rec_id,))
    check("still streaming", state(rec_id), ("streaming", None))


def test_late_finalize_overrules_the_sweep():
    print("A /finalize that lands after the sweep guessed")
    for msg in (recordings.SWEEP_NO_AUDIO_MSG, recordings.SWEEP_NO_REPORT_MSG):
        rec_id, _ = new_recording()
        sql("UPDATE hive_recordings SET status = 'failed', bytes = 3200, error = %s "
            "WHERE id = %s;", (msg, rec_id))
        finalize(rec_id, ok=True, bytes=3200)
        check(f"successful report clears '{msg[:24]}...'", state(rec_id), ("ready", None))

    rec_id, _ = new_recording()
    sql("UPDATE hive_recordings SET status = 'failed', error = %s WHERE id = %s;",
        (recordings.SWEEP_NO_AUDIO_MSG, rec_id))
    finalize(rec_id, ok=False)
    check("a failed report keeps the sweep's reason when it brings none",
          state(rec_id), ("failed", recordings.SWEEP_NO_AUDIO_MSG))

    rec_id, _ = new_recording()
    sql("UPDATE hive_recordings SET status = 'streaming', error = 'truncated at the "
        "server cap of 4 bytes' WHERE id = %s;", (rec_id,))
    finalize(rec_id, ok=True)
    check("a real server-side error survives a successful report",
          state(rec_id)[1], "truncated at the server cap of 4 bytes")


def test_late_successful_command_result_overrules_the_sweep():
    print("A successful command result that lands after the sweep guessed")
    rec_id, cmd = new_recording()
    sql("UPDATE hive_recordings SET status = 'failed', bytes = 3200, error = %s "
        "WHERE id = %s;", (recordings.SWEEP_NO_AUDIO_MSG, rec_id))
    recordings.finalize_from_command_result(DEVICE, cmd, True, "recorded 3200 B")
    check("audio on disk: restored to ready", state(rec_id), ("ready", None))

    rec_id, cmd = new_recording()
    sql("UPDATE hive_recordings SET status = 'failed', error = %s WHERE id = %s;",
        (recordings.SWEEP_NO_AUDIO_MSG, rec_id))
    recordings.finalize_from_command_result(DEVICE, cmd, True, "recorded 0 B")
    check("no audio on disk: the sweep's verdict stands",
          state(rec_id), ("failed", recordings.SWEEP_NO_AUDIO_MSG))

    rec_id, cmd = new_recording()
    sql("UPDATE hive_recordings SET status = 'failed', bytes = 3200, "
        "error = 'node refused: busy' WHERE id = %s;", (rec_id,))
    recordings.finalize_from_command_result(DEVICE, cmd, True, "recorded")
    check("a verdict /finalize wrote is never rewritten",
          state(rec_id), ("failed", "node refused: busy"))


reset()
test_late_pickup_is_not_failed_mid_stream()
test_stalled_stream_still_expires()
test_recent_stream_is_left_alone()
test_late_finalize_overrules_the_sweep()
test_late_successful_command_result_overrules_the_sweep()
reset()
sql("DELETE FROM devices WHERE device_id = %s;", (DEVICE,))
db_pool.close()

if FAILURES:
    print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
    sys.exit(1)
print("\nAll recording database checks passed.")
