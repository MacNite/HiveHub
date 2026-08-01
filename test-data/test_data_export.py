"""Tests for the dashboard's data download / backup export.

Run: PYTHONPATH=server python3 test-data/test_data_export.py
     (no database or FastAPI required)

The export exists so a self-host can be re-deployed — or one beekeeper's history
moved to their own server — without losing readings, which only works if what
comes out can be fed straight back into the SD-card import. So the checks below
are mostly round-trip checks: build a record the way data_export does, run it
back through sd_import.parse_sd_measurements, and assert nothing was lost or
re-attributed along the way.
"""

import json
from datetime import datetime, timedelta, timezone

from data_export import (
    export_filename,
    export_record,
    iter_export_lines,
    ndjson_line,
)
from sd_import import group_records_by_device, parse_sd_measurements


NOW = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


# ── record shaping ───────────────────────────────────────────────────────────

# 1. A normal row is exported verbatim, with device_id and timestamp restated
#    from the columns the server actually stored.
rec = export_record(
    "hive-a",
    NOW,
    {"device_id": "hive-a", "timestamp": "2024-06-15T12:00:00+00:00",
     "scale_1_weight_kg": 42.5, "ambient_temp_c": 21.0},
)
check("payload fields survive the export", rec["scale_1_weight_kg"] == 42.5)
check("device-level fields survive the export", rec["ambient_temp_c"] == 21.0)
check("device_id comes from the stored row", rec["device_id"] == "hive-a")

# 2. The stored measured_at wins over whatever the device put in the payload. A
#    scale without a working RTC sends 1970; ingest clamped that to the server
#    clock, and re-importing the raw value would mis-date every such reading and
#    collapse them all onto one key.
rec = export_record(
    "hive-a", NOW, {"timestamp": "1970-01-01T00:00:00Z", "scale_1_weight_kg": 1.0}
)
check("stored timestamp replaces a bogus payload one",
      rec["timestamp"] == "2024-06-15T12:00:00+00:00")

# 3. A naive stored timestamp is read as UTC; an offset one is normalized to it.
check("naive timestamps are treated as UTC",
      export_record("hive-a", datetime(2024, 6, 15, 12, 0), {})["timestamp"]
      == "2024-06-15T12:00:00+00:00")
check("offset timestamps are normalized to UTC",
      export_record(
          "hive-a",
          datetime(2024, 6, 15, 14, 0, tzinfo=timezone(timedelta(hours=2))),
          {},
      )["timestamp"] == "2024-06-15T12:00:00+00:00")

# 4. A row cannot claim a device other than the one it is filed under, so a
#    backup can never smuggle readings onto a different hive when re-imported.
rec = export_record("hive-a", NOW, {"device_id": "hive-b"})
check("a row cannot claim a foreign device", rec["device_id"] == "hive-a")

# 5. Credentials never leave the server, even if an old row stored one.
rec = export_record("hive-a", NOW, {"claim_code": "ABCD-1234", "scale_1_raw": 7})
check("claim codes are stripped from the backup", "claim_code" not in rec)

# 6. Rows with no payload fall back to the stored columns, so a hand-inserted or
#    pre-raw_json row exports its readings instead of an empty line.
rec = export_record(
    "hive-a", NOW, None,
    fallback={"scale_1_weight_kg": 30.0, "hive_1_temp_c": None, "battery_voltage": 3.9},
)
check("payload-less rows fall back to stored columns", rec["scale_1_weight_kg"] == 30.0)
check("null fallback columns are not written", "hive_1_temp_c" not in rec)
check("fallback covers every stored column", rec["battery_voltage"] == 3.9)

# 7. The fallback never overwrites what the device actually sent.
rec = export_record(
    "hive-a", NOW, {"scale_1_weight_kg": 42.5}, fallback={"scale_1_weight_kg": 0.0}
)
check("payload wins over the column fallback", rec["scale_1_weight_kg"] == 42.5)


# ── hive filtering ───────────────────────────────────────────────────────────

PAYLOAD = {
    "device_id": "hive-a",
    "ambient_temp_c": 21.0,          # device-level, always kept
    "battery_voltage": 3.9,          # device-level, always kept
    "mic_left_rms_dbfs": -42.0,      # device-level (stereo mic), always kept
    "scale_1_weight_kg": 42.5,
    "scale_2_weight_kg": 38.0,
    "hive_1_temp_c": 34.5,
    "hive_2_temp_c": 33.0,
    "accel_2_rms_mg": 12.0,
    "bee_counter_1_total_in": 900,
    "ble_2_pressure_hpa": 1013.0,
    "hiveheart_1_energy": 5.0,
    "hivescale_2_weight_kg": 40.0,
    "hives": [{"index": 1, "weight_kg": 42.5}, {"index": 2, "weight_kg": 38.0},
              {"index": 7, "weight_kg": 31.0}],
}

rec = export_record("hive-a", NOW, PAYLOAD, hives=[1])
check("selected hive keeps its flat fields", rec["scale_1_weight_kg"] == 42.5)
check("selected hive keeps its per-sensor fields",
      rec["hive_1_temp_c"] == 34.5 and rec["bee_counter_1_total_in"] == 900
      and rec["hiveheart_1_energy"] == 5.0)
check("unselected hive fields are dropped",
      not any(k in rec for k in
              ("scale_2_weight_kg", "hive_2_temp_c", "accel_2_rms_mg",
               "ble_2_pressure_hpa", "hivescale_2_weight_kg")))
check("device-level fields survive a hive filter",
      rec["ambient_temp_c"] == 21.0 and rec["battery_voltage"] == 3.9
      and rec["mic_left_rms_dbfs"] == -42.0)
check("nested hives[] is filtered too",
      [h["index"] for h in rec["hives"]] == [1])

# 8. Hives beyond the legacy 1–2 columns live only in hives[]; selecting one must
#    keep it and drop the rest.
rec = export_record("hive-a", NOW, PAYLOAD, hives=[7])
check("a hives[]-only hive can be selected",
      [h["index"] for h in rec["hives"]] == [7])
check("selecting a hives[]-only hive drops the legacy columns",
      "scale_1_weight_kg" not in rec and "scale_2_weight_kg" not in rec)

# 9. Filtering everything out of hives[] drops the key rather than exporting an
#    empty array (the row still carries device-level telemetry).
rec = export_record("hive-a", NOW, PAYLOAD, hives=[3])
check("an empty hives[] is omitted", "hives" not in rec)
check("a hive-filtered row still carries the device", rec["ambient_temp_c"] == 21.0)

# 10. No selection means no filtering.
rec = export_record("hive-a", NOW, PAYLOAD, hives=[])
check("an empty hive selection exports everything",
      len(rec["hives"]) == 3 and rec["scale_2_weight_kg"] == 38.0)

# 11. String indices from JSON are matched like numbers.
rec = export_record("hive-a", NOW, {"hives": [{"index": "2"}, {"index": 3}]}, hives=[2])
check("string hive indices are matched", [h["index"] for h in rec["hives"]] == ["2"])


# ── NDJSON framing and round-trip ────────────────────────────────────────────

line = ndjson_line({"device_id": "hive-a", "scale_1_weight_kg": 42.5})
check("each record is one newline-terminated line",
      line.endswith("\n") and line.count("\n") == 1)

rows = [
    ("hive-a", NOW, {"scale_1_weight_kg": 42.5}, {}),
    ("hive-b", NOW + timedelta(minutes=10), {"scale_1_weight_kg": 12.0}, {}),
]
blob = "".join(iter_export_lines(rows))
parsed, skipped = parse_sd_measurements(blob.encode("utf-8"), "backup.ndjson")
check("an export parses back through the SD importer", len(parsed) == 2)
check("a round-trip drops nothing", skipped == 0)
check("every row keeps its own device on the way back",
      [r["device_id"] for r in parsed] == ["hive-a", "hive-b"])
check("values survive the round-trip", parsed[0]["scale_1_weight_kg"] == 42.5)
check("timestamps survive the round-trip",
      parsed[1]["timestamp"] == "2024-06-15T12:10:00+00:00")

# 12. A record holding a nested hives[] array survives JSON encoding intact —
#     this is what carries hives 3–18 on a multi-hive device.
blob = "".join(iter_export_lines([("hive-a", NOW, PAYLOAD, {})]))
back = json.loads(blob)
check("nested hives survive JSON encoding",
      [h["index"] for h in back["hives"]] == [1, 2, 7])


# ── multi-device restore routing ─────────────────────────────────────────────

recs = [
    {"device_id": "hive-b", "scale_1_weight_kg": 1.0},
    {"device_id": "hive-a", "scale_1_weight_kg": 2.0},
    {"device_id": "hive-b", "scale_1_weight_kg": 3.0},
]
groups = group_records_by_device(recs, "hive-a")
check("records are bucketed by their own device", sorted(groups) == ["hive-a", "hive-b"])
check("every row lands in its own bucket",
      len(groups["hive-b"]) == 2 and len(groups["hive-a"]) == 1)
check("buckets keep first-seen order", list(groups) == ["hive-b", "hive-a"])

# 13. Rows that predate the device_id field cannot be attributed, so they go to
#     the device the upload was addressed to — the old single-card behaviour.
groups = group_records_by_device(
    [{"scale_1_weight_kg": 1.0}, {"device_id": "  "}, {"device_id": 7}], "hive-a"
)
check("unattributable rows fall back to the upload target",
      list(groups) == ["hive-a"] and len(groups["hive-a"]) == 3)

# 14. A single-device backup routes as one bucket, so a routed restore of an
#     ordinary SD card behaves exactly like a normal import.
groups = group_records_by_device([{"device_id": "hive-a"}] * 3, "hive-a")
check("a single-device file stays one bucket", list(groups) == ["hive-a"])


# ── file naming ──────────────────────────────────────────────────────────────

check("one device is named after it",
      export_filename(["hive-a"], now=NOW) == "hivehub-backup-hive-a-20240615.ndjson")
check("several devices are named by count",
      export_filename(["hive-a", "hive-b"], now=NOW)
      == "hivehub-backup-2-devices-20240615.ndjson")
check("a date range is reflected in the name",
      export_filename(["hive-a"], datetime(2024, 1, 2), datetime(2024, 3, 4))
      == "hivehub-backup-hive-a-20240102_20240304.ndjson")
check("a device id cannot escape the filename",
      export_filename(['../../etc/pa"sswd'], now=NOW)
      == "hivehub-backup-etc-pa-sswd-20240615.ndjson")


if _failures:
    raise SystemExit(f"{_failures} check(s) failed")
print("\nAll data-export and restore-routing checks passed.")
