"""Tests for hive audio recordings (issue #71).

Run: PYTHONPATH=server python3 test-data/test_recordings.py
     (no database or FastAPI server required)

These cover the parts that are pure functions of their inputs — the WAV header
the playback endpoints synthesize, the storage path, and the row-to-API shaping
that decides whether a listener is told a recording is complete. The HTTP
plumbing needs a database and is exercised by hand against a real deployment.

The `complete` flag gets the most attention here on purpose. A recording with a
hole in it still plays as continuous audio, so a bug that reported an incomplete
recording as complete would produce a confident, wrong acoustic judgement and
leave no trace of why.
"""

import os
import re
import struct
import sys
from datetime import datetime, timezone

SERVER_DIR = os.path.join(os.path.dirname(__file__), "..", "server")
sys.path.insert(0, SERVER_DIR)

# recordings.py pulls in auth -> config, which reads these at import time. No
# database, network or disk is touched: everything checked below is a pure
# function of its arguments. RECORDINGS_DIR is pinned too, so the path
# assertions describe the code rather than whatever the deployment default
# happens to be.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("RECORDINGS_DIR", "/tmp/hivehub-test-recordings")

import recordings  # noqa: E402
from recordings import (  # noqa: E402
    SAMPLE_RATE,
    _path_for,
    _row_to_dict,
    wav_header,
)
from schemas import DeviceCommandIn  # noqa: E402

NOW = datetime(2026, 9, 8, 14, 3, tzinfo=timezone.utc)

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def row(**over):
    """A recording row in the column order recordings._SELECT returns."""
    base = dict(
        rec_id=7, device_id="hive-a", hive_index=2, status="ready",
        requested_at=NOW, started_at=NOW, completed_at=NOW,
        duration_ds=0, gain_db=0, sample_rate=SAMPLE_RATE,
        num_bytes=32000, crc32=123456, device_bytes=32000, device_crc32=123456,
        dropped_bytes=0, gaps=0, ring_overruns=0, ring_dropped_bytes=0,
        clipped_pct=0, error=None, requested_by="max",
        command_status="done", command_message=None,
    )
    base.update(over)
    return tuple(base.values())


# ── WAV header ───────────────────────────────────────────────────────────────
#
# Generated per request rather than stored, so a growing live recording never
# needs its header rewritten. That only works if it is exactly right.

h = wav_header(32000)
check("header is the canonical 44 bytes", len(h) == 44)
check("header starts with RIFF", h[0:4] == b"RIFF")
check("RIFF size counts everything after the first 8 bytes",
      struct.unpack("<I", h[4:8])[0] == 36 + 32000)
check("format is WAVE/fmt ", h[8:16] == b"WAVEfmt ")
check("fmt chunk is 16 bytes of PCM", struct.unpack("<I", h[16:20])[0] == 16)
check("audio format is uncompressed PCM", struct.unpack("<H", h[20:22])[0] == 1)
check("one channel", struct.unpack("<H", h[22:24])[0] == 1)
check("16 kHz sample rate", struct.unpack("<I", h[24:28])[0] == 16000)
check("byte rate is rate * channels * 2", struct.unpack("<I", h[28:32])[0] == 32000)
check("block align is 2", struct.unpack("<H", h[32:34])[0] == 2)
check("16 bits per sample", struct.unpack("<H", h[34:36])[0] == 16)
check("data chunk declares the payload size",
      h[36:40] == b"data" and struct.unpack("<I", h[40:44])[0] == 32000)

# A zero-length recording still has to produce a valid (silent) file rather than
# a truncated one: a failed session is listed alongside good ones.
h0 = wav_header(0)
check("an empty recording still yields a valid header",
      len(h0) == 44 and struct.unpack("<I", h0[40:44])[0] == 0)

check("a non-default sample rate is carried into the header",
      struct.unpack("<I", wav_header(100, 8000)[24:28])[0] == 8000)


# ── storage path ─────────────────────────────────────────────────────────────
#
# The filename comes from ids the database issued, but the device id arrives in
# a request header, so it is the part that has to be defanged.

p = _path_for("hive-a", 7)
check("path is <device>/<id>.pcm", p.parent.name == "hive-a" and p.name == "7.pcm")
check("a device id cannot traverse out of the recordings directory",
      ".." not in str(_path_for("../../etc/passwd", 1)))
check("separators are stripped from the device id",
      "/" not in _path_for("../../etc/passwd", 1).parent.name)

try:
    _path_for("///", 1)
    check("an entirely unusable device id is rejected", False)
except Exception:
    check("an entirely unusable device id is rejected", True)


# ── what a listener is told ──────────────────────────────────────────────────

d = _row_to_dict(row())
check("a clean ready recording is complete", d["complete"] is True)
check("seconds are derived from bytes and sample rate", d["seconds"] == 1.0)
check("matching CRCs report crc_ok", d["crc_ok"] is True)

check("audio the node dropped makes it incomplete",
      _row_to_dict(row(dropped_bytes=480))["complete"] is False)
check("a gap in transit makes it incomplete",
      _row_to_dict(row(gaps=1))["complete"] is False)
# The two seams have opposite remedies — one is the radio, one is this hub's
# upload — so they are separate columns and each has to fail `complete` alone.
check("audio the hub's own buffer refused makes it incomplete",
      _row_to_dict(row(ring_overruns=1))["complete"] is False)
check("the air and the hub's buffer are reported separately",
      _row_to_dict(row(gaps=3, ring_overruns=4))["gaps"] == 3
      and _row_to_dict(row(gaps=3, ring_overruns=4))["ring_overruns"] == 4)
check("a CRC mismatch makes it incomplete",
      _row_to_dict(row(device_crc32=999))["complete"] is False)
check("a CRC mismatch is reported as such",
      _row_to_dict(row(device_crc32=999))["crc_ok"] is False)
check("a recording still streaming is not yet complete",
      _row_to_dict(row(status="streaming"))["complete"] is False)
check("a failed recording is not complete",
      _row_to_dict(row(status="failed", error="node busy"))["complete"] is False)
check("a failure reason is passed through",
      _row_to_dict(row(status="failed", error="node busy"))["error"] == "node busy")

# Missing CRCs must not read as "verified".
no_crc = _row_to_dict(row(crc32=None, device_crc32=None))
check("an unverifiable recording is not claimed to be checksum-verified",
      no_crc["crc_ok"] is False)
check("a recording with no CRCs but a hub report is still complete",
      no_crc["complete"] is True)

# ── how sure we are about a recording ────────────────────────────────────────
#
# Three states, because the hub reports twice and either report can go missing:
# the detailed /finalize POST carries the CRC, the command result only says the
# session succeeded. Treating a missing /finalize as "contact lost" is what made
# the panel shout at recordings that were fine.

check("a checksummed recording is verified", d["confirmation"] == "verified")
check("a mismatched checksum is reported, not verified",
      _row_to_dict(row(device_crc32=999))["confirmation"] == "reported")

confirmed = _row_to_dict(row(device_bytes=None, device_crc32=None, crc32=None))
check("no detailed report but a succeeded command reads as confirmed",
      confirmed["confirmation"] == "confirmed")
check("a confirmed recording is complete", confirmed["complete"] is True)

# A session the hub never reported on at all: expire_stale_recordings() marks it
# ready because the audio is real and playable, but nothing ever confirmed it.
# Claiming such a recording is complete would be a quiet lie — the missing
# report is exactly the thing that would have said whether the tail is there.
abandoned = _row_to_dict(row(device_bytes=None, device_crc32=None, crc32=None,
                             command_status="claimed",
                             error="the hub did not send its report for this session"))
check("a recording with no report at all is unknown",
      abandoned["confirmation"] == "unknown")
check("a recording the hub never reported on is not called complete",
      abandoned["complete"] is False)
check("...but it still plays: status ready and bytes present",
      abandoned["status"] == "ready" and abandoned["bytes"] > 0)
check("...and it carries the reason", "did not send" in (abandoned["error"] or ""))

check("the hub's own account of the session is surfaced",
      _row_to_dict(row(command_message="node refused: busy"))["hub_message"]
      == "node refused: busy")

# A live session is requested with duration 0; the UI shows what was asked for.
check("an open-ended request reports zero requested duration",
      _row_to_dict(row(duration_ds=0))["requested_duration_s"] == 0.0)
check("a fixed request reports its duration in seconds",
      _row_to_dict(row(duration_ds=105))["requested_duration_s"] == 10.5)

# Bytes are what landed, and a partial recording must still report honestly.
partial = _row_to_dict(row(num_bytes=16000, device_bytes=32000, gaps=2))
check("seconds reflect what arrived, not what was expected", partial["seconds"] == 0.5)
check("a partial recording is not complete", partial["complete"] is False)


# ── the command the request actually queues ─────────────────────────────────
#
# DeviceCommandIn.command_type is a Literal, so a command type missing from it
# raises at construction — inside request_recording, AFTER the recording row has
# been inserted and committed. The row then sits at "requested" forever waiting
# for a hub that was never told anything, which looks exactly like a hub that is
# merely asleep. This shipped that way once; it is a one-line regression to make
# and an invisible one to diagnose, so pin it here.

try:
    DeviceCommandIn(command_type="record_audio",
                    payload={"slot": 1, "recording_id": 1,
                             "duration_ds": 0, "gain_db": 0})
    check("record_audio is an accepted device command type", True)
except Exception as exc:
    check(f"record_audio is an accepted device command type ({exc})", False)

try:
    DeviceCommandIn(command_type="definitely_not_a_command", payload={})
    check("an unknown command type is still rejected", False)
except Exception:
    check("an unknown command type is still rejected", True)


# ── the schema the queries are written against ──────────────────────────────
#
# init_db() in db.py is what actually builds and upgrades a deployment's schema;
# the files in server/migrations/ document the same changes for anyone applying
# them by hand. A column added to a migration but not to init_db therefore
# exists nowhere at runtime, and every query naming it fails with a 500 — on
# fresh installs as well as upgrades. That shipped once: ring_overruns and
# ring_dropped_bytes went into 030 and into recordings.py, but not into db.py,
# so listing recordings 500'd and the dashboard reported that scheduling one had
# failed. Reading the SQL is enough to catch it, and needs no database.

_recordings_sql = open(os.path.join(SERVER_DIR, "recordings.py")).read()
_init_db_sql = open(os.path.join(SERVER_DIR, "db.py")).read()

# Columns the SELECT reads (r.<col>) and the ones finalize writes (<col> = %s).
_queried = set(re.findall(r"\br\.([a-z0-9_]+)", recordings._SELECT))
_queried |= set(re.findall(r"^\s+([a-z0-9_]+) = %s,", _recordings_sql, re.M))

# What init_db actually creates: the hive_recordings CREATE TABLE body plus any
# ALTER ... ADD COLUMN that names the table.
_created = set(re.findall(
    r"CREATE TABLE IF NOT EXISTS hive_recordings \((.*?)\n\s*\);",
    _init_db_sql, re.S)[0].split())
_created |= set(re.findall(
    r"ALTER TABLE hive_recordings\s+ADD COLUMN IF NOT EXISTS ([a-z0-9_]+)",
    _init_db_sql))

_missing = sorted(_queried - _created)
check(f"every column the recordings SQL names is created by init_db "
      f"({'missing: ' + ', '.join(_missing) if _missing else 'none missing'})",
      not _missing)

# The same drift in the other direction: a migration file that init_db never
# learned about. Only hive_recordings is in this file's remit.
_migrations = os.path.join(SERVER_DIR, "migrations")
_migrated = set()
for _name in sorted(os.listdir(_migrations)):
    _sql = open(os.path.join(_migrations, _name)).read()
    _migrated |= set(re.findall(
        r"ALTER TABLE hive_recordings\s+ADD COLUMN IF NOT EXISTS ([a-z0-9_]+)",
        _sql))
_unapplied = sorted(_migrated - _created)
check(f"every hive_recordings migration column is also in init_db "
      f"({'missing: ' + ', '.join(_unapplied) if _unapplied else 'none missing'})",
      not _unapplied)


if _failures:
    raise SystemExit(f"{_failures} check(s) failed")
print("\nAll recording checks passed.")
