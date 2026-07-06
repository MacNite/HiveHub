"""Tests for SD-card bulk-import de-duplication.

Run: python3 server/test_sd_import.py   (no database or FastAPI required)

Re-uploading the same SD download must never create duplicate measurement rows.
The natural key is (device_id, measured_at); within one import request the device
is fixed by the URL, so we de-duplicate on measured_at alone. These tests cover
the pure logic in sd_import.split_new_and_duplicate, which the import endpoint
uses to decide which rows to insert.
"""

import io
import tarfile
from datetime import datetime, timedelta, timezone

from sd_import import (
    distinct_source_device_ids,
    parse_sd_measurements,
    split_new_and_duplicate,
)


NOW = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def _ts(minutes):
    return NOW + timedelta(minutes=minutes)


# 1. Fresh import into an empty device: everything is new, nothing duplicated.
keys = [_ts(0), _ts(10), _ts(20)]
new, dupes = split_new_and_duplicate(keys, set())
check("fresh import keeps all rows", new == keys)
check("fresh import reports no duplicates", dupes == 0)

# 2. The SD backup file commonly contains the same line twice (retry cache replay).
keys = [_ts(0), _ts(0), _ts(10)]
new, dupes = split_new_and_duplicate(keys, set())
check("in-file duplicate collapsed to one row", new == [_ts(0), _ts(10)])
check("in-file duplicate counted once", dupes == 1)

# 3. Re-uploading the exact same file a second time inserts nothing.
existing = {_ts(0), _ts(10), _ts(20)}
keys = [_ts(0), _ts(10), _ts(20)]
new, dupes = split_new_and_duplicate(keys, existing)
check("re-upload of identical file inserts nothing", new == [])
check("re-upload counts every row as duplicate", dupes == 3)

# 4. Overlapping upload: only the genuinely new rows are inserted.
existing = {_ts(0), _ts(10)}
keys = [_ts(0), _ts(10), _ts(20), _ts(30)]
new, dupes = split_new_and_duplicate(keys, existing)
check("overlapping upload inserts only new rows", new == [_ts(20), _ts(30)])
check("overlapping upload counts the overlap", dupes == 2)

# 5. Invariant: inserted + duplicates always equals the number of input rows.
keys = [_ts(0), _ts(0), _ts(10), _ts(20), _ts(20)]
existing = {_ts(20)}
new, dupes = split_new_and_duplicate(keys, existing)
check("inserted + duplicates == received", len(new) + dupes == len(keys))


# ── SD file parsing (parse_sd_measurements) ─────────────────────────────────
# The local dashboard and the mock HivePal proxy both parse the raw SD download
# (NDJSON or the uncompressed TAR) client/server-side before de-duplicating.

# 6. Raw NDJSON: objects are kept in order; blank, non-object and corrupt lines
#    are skipped (not counted as records).
ndjson = (
    b'{"device_id":"d1","timestamp":"2024-06-15T12:00:00Z","scale_1_weight_kg":42.1}\n'
    b"\n"
    b"not-json\n"
    b'{"device_id":"d1","timestamp":"2024-06-15T12:15:00Z"}\n'
    b"[1,2,3]\n"
)
res = parse_sd_measurements(ndjson, "measurements.ndjson")
check("ndjson keeps the two valid objects", len(res.records) == 2)
check("ndjson skips corrupt line and JSON array", res.skipped == 2)
check("ndjson preserves record fields", res.records[0]["scale_1_weight_kg"] == 42.1)


def _make_tar(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


_tar = _make_tar(
    [
        (
            "measurements.ndjson",
            b'{"device_id":"d1","timestamp":"2024-06-15T12:00:00Z"}\n'
            b'{"device_id":"d1","timestamp":"2024-06-15T12:15:00Z"}\n',
        ),
        ("cache.ndjson", b'{"device_id":"d1","timestamp":"2024-06-15T12:30:00Z"}\n'),
        ("README.txt", b"not measurements\n"),
    ]
)

# 7. TAR download: every *.ndjson member is parsed, other members are ignored.
res = parse_sd_measurements(_tar, "hivescale-sd-data.tar")
check("tar reads all ndjson members", len(res.records) == 3)
check("tar ignores non-ndjson members", res.skipped == 0)

# 8. A mislabelled upload (wrong extension) is still detected via the USTAR magic.
res = parse_sd_measurements(_tar, "download.bin")
check("tar is sniffed even without a .tar name", len(res.records) == 3)

# 9. Empty input yields no records and no crash.
res = parse_sd_measurements(b"", "measurements.ndjson")
check("empty file yields no records", res.records == [] and res.skipped == 0)


# ── Device-mismatch guard (distinct_source_device_ids) ──────────────────────
# The firmware stamps every backup line with the device that recorded it, so the
# import endpoint can refuse a card uploaded against the wrong device before its
# readings are silently re-pinned onto (and mis-mapped to) that device.

# 10. A single-card backup reports exactly its one source device.
recs = [
    {"device_id": "hive-a", "timestamp": "2024-06-15T12:00:00Z"},
    {"device_id": "hive-a", "timestamp": "2024-06-15T12:15:00Z"},
]
check("single-device backup yields one id", distinct_source_device_ids(recs) == ["hive-a"])

# 11. Ids are de-duplicated and returned in first-seen order.
recs = [
    {"device_id": "hive-b"},
    {"device_id": "hive-a"},
    {"device_id": "hive-b"},
]
check("distinct ids keep first-seen order", distinct_source_device_ids(recs) == ["hive-b", "hive-a"])

# 12. Rows without a (string) device_id are ignored — an older backup that
#     predates the field must not trip the guard (empty list == no mismatch).
recs = [
    {"timestamp": "2024-06-15T12:00:00Z"},
    {"device_id": ""},
    {"device_id": "   "},
    {"device_id": 123},
]
check("id-less rows never trip the guard", distinct_source_device_ids(recs) == [])

# 13. The endpoint's mismatch test: a foreign card is caught, the matching one
#     passes cleanly.
recs = [{"device_id": "hive-a"}, {"device_id": "hive-a"}]
check(
    "foreign card is flagged for the target device",
    [d for d in distinct_source_device_ids(recs) if d != "hive-b"] == ["hive-a"],
)
check(
    "matching card raises no mismatch",
    [d for d in distinct_source_device_ids(recs) if d != "hive-a"] == [],
)


if _failures:
    raise SystemExit(f"{_failures} check(s) failed")
print("\nAll SD-import parsing and de-duplication checks passed.")
