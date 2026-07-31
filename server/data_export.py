"""Build re-importable measurement backups out of stored readings.

The dashboard's **Download / backup data** panel (Device & admin) uses this to
hand a self-hoster every reading the server holds, so the database can be wiped
and re-deployed — or one beekeeper's history moved to their own server — without
losing history.

The export deliberately produces the *same* shape the ESP32 writes to its SD
card: one JSON object per line, each one a ``MeasurementIn`` payload. That is
exactly what :mod:`sd_import` parses, so a download round-trips back in through
the existing "Import SD card data" upload with no extra format on either side.

Each line is rebuilt from the ``raw_json`` column — the verbatim payload the
device sent — with two fields forced to the values the server actually stored:

* ``device_id``, so a row can never claim a different device than the one it is
  filed under, and
* ``timestamp``, set to ``measured_at``. The stored instant is authoritative:
  a device without a working RTC sends 1970 (or no timestamp at all) and the
  ingest path clamps it to the server clock. Exporting the raw value would
  re-import every such reading onto the wrong instant — and collapse them all
  onto one key.

This module has **no** third-party imports, so the record shaping, hive
filtering and file naming can be unit-tested without a database or FastAPI.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Sequence


#: Rows fetched per database round-trip while streaming an export. Keeps the
#: pooled connection held for one page at a time instead of the whole download.
EXPORT_PAGE_ROWS = 2000

# Per-hive flat fields carry their hive index inside the key: scale_1_weight_kg,
# hive_2_temp_c, accel_1_rms_mg, bee_counter_2_total_in, ble_1_pressure_hpa,
# hiveheart_1_energy, hivescale_2_weight_kg … Every branch is anchored and
# followed by "_<digits>_", so the device-level keys that merely start the same
# way (mic_*, solar_*, battery_*, scale1_offset …) never match.
_HIVE_FIELD_RE = re.compile(
    r"^(?:hiveheart|hivescale|bee_counter|accel|scale|hive|ble)_(\d+)_"
)

# Columns used to reconstruct a record whose raw_json is missing. Every insert
# path stores raw_json, so this only covers rows written by hand or by a much
# older schema — but an export that silently emitted an empty line for them
# would lose data with no way to notice.
FALLBACK_COLUMNS = (
    "scale_1_weight_kg",
    "scale_2_weight_kg",
    "hive_1_temp_c",
    "hive_2_temp_c",
    "ambient_temp_c",
    "ambient_humidity_percent",
    "battery_voltage",
)


def _filter_hives(record: dict, hives: set[int]) -> None:
    """Drop everything in ``record`` belonging to a hive outside ``hives``.

    Trims both the nested ``hives[]`` array the multi-hive firmware sends and the
    flat ``scale_1_*`` / ``hive_2_*`` / ``accel_1_*`` … fields that hives 1–2 (and
    older firmware) use. Device-level telemetry — ambient temperature, battery,
    solar, network, the stereo microphone — is never per-hive and always kept, so
    a hive-filtered backup is still a complete record of the device itself.
    """
    nested = record.get("hives")
    if isinstance(nested, list):
        kept = [
            h for h in nested
            if isinstance(h, dict) and _as_index(h.get("index")) in hives
        ]
        if kept:
            record["hives"] = kept
        else:
            record.pop("hives", None)

    for key in [k for k in record if _HIVE_FIELD_RE.match(k)]:
        if int(_HIVE_FIELD_RE.match(key).group(1)) not in hives:
            record.pop(key)


def _as_index(value) -> Optional[int]:
    """Best-effort hive index from a nested entry ('3' and 3 both count)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def export_record(
    device_id: str,
    measured_at: datetime,
    raw_json: Optional[dict],
    fallback: Optional[dict] = None,
    hives: Optional[Iterable[int]] = None,
) -> dict:
    """Shape one stored measurement into a line of an SD-style backup.

    ``fallback`` supplies values for the legacy columns listed in
    ``FALLBACK_COLUMNS``; they are only used for keys the payload does not
    already carry, so a normal row is exported exactly as the device sent it.
    ``hives`` restricts the record to the given hive indices (all hives when
    omitted or empty).
    """
    record = dict(raw_json) if isinstance(raw_json, dict) else {}
    for key, value in (fallback or {}).items():
        if value is not None and record.get(key) is None:
            record[key] = value

    # Never export a credential, even if an old row happened to store one.
    record.pop("claim_code", None)

    selected = {n for n in (_as_index(h) for h in (hives or [])) if n}
    if selected:
        _filter_hives(record, selected)

    record["device_id"] = device_id
    record["timestamp"] = _isoformat(measured_at)
    return record


def _isoformat(value: datetime) -> str:
    """UTC ISO-8601 for a stored timestamp, naive values assumed to be UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def ndjson_line(record: dict) -> str:
    """Serialize one record as a newline-terminated NDJSON line."""
    return json.dumps(record, separators=(",", ":"), default=str) + "\n"


def iter_export_lines(
    rows: Iterable[tuple], hives: Optional[Iterable[int]] = None
) -> Iterator[str]:
    """Turn ``(device_id, measured_at, raw_json, fallback)`` rows into NDJSON.

    Streams lazily so a multi-year export never has to be materialized in memory.
    """
    for device_id, measured_at, raw_json, fallback in rows:
        yield ndjson_line(
            export_record(device_id, measured_at, raw_json, fallback, hives)
        )


_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_part(text: str) -> str:
    """Reduce free text to something safe to put in a Content-Disposition name."""
    # Separators are stripped from both ends too, so a device id full of them
    # (or one shaped like a path) cannot produce a leading "..", a trailing dot,
    # or an otherwise surprising download name.
    cleaned = _UNSAFE_FILENAME_RE.sub("-", str(text or "")).strip("-._")
    return cleaned[:60] or "export"


def export_filename(
    device_ids: Sequence[str],
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> str:
    """Name the download: which devices it covers and which period.

    One device is named after it (so several per-device backups sort together and
    are obvious to match up with the device they must be imported into); several
    devices get a count, since the file then holds every selected device's rows.
    """
    scope = (
        _safe_part(device_ids[0])
        if len(device_ids) == 1
        else f"{len(device_ids)}-devices"
    )
    if start_at or end_at:
        stamp = f"{_date_part(start_at, 'start')}_{_date_part(end_at, 'now')}"
    else:
        stamp = _date_part(now or datetime.now(timezone.utc), "now")
    return f"hivehub-backup-{scope}-{stamp}.ndjson"


def _date_part(value: Optional[datetime], fallback: str) -> str:
    return value.strftime("%Y%m%d") if value else fallback
