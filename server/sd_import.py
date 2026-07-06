"""Helpers for bulk-importing measurements parsed from a HiveHub SD card.

The ESP32 keeps an append-only ``/measurements.ndjson`` backup on its SD card.
A beekeeper can pull that file in AP mode and upload it later — either through the
HivePal web UI or the local dashboard that ships with HiveHub — which parse the
NDJSON/TAR download and import the records via
``POST /api/v1/app/devices/{device_id}/measurements/import`` (HivePal) or
``POST /api/v1/local/devices/{device_id}/measurements/import`` (local dashboard).

The same measurement can legitimately appear more than once — the backup file is
never pruned, the retry cache replays rows, and a beekeeper may upload the same
download twice. Re-importing must therefore be idempotent. We treat
``(device_id, measured_at)`` as the natural key for a reading: the firmware only
produces one sample per wake-up, so two rows for the same device and instant are
the same observation.

This module intentionally has **no** third-party imports so the parsing and
de-duplication logic can be unit-tested without a database or FastAPI.
"""

from __future__ import annotations

import json
from typing import Hashable, Iterable, NamedTuple


class SdImportParseResult(NamedTuple):
    """Outcome of parsing an uploaded SD-card file."""

    #: Parsed measurement objects, in file order.
    records: list[dict]
    #: Non-empty lines that could not be parsed as a JSON object (corrupt/truncated).
    skipped: int


# USTAR magic sits at offset 257 of a TAR header block.
_USTAR_MAGIC = b"ustar"


def _read_nul_terminated(value: bytes) -> str:
    """Return the text up to (but excluding) the first NUL byte, trimmed."""
    nul = value.find(b"\0")
    chunk = value if nul == -1 else value[:nul]
    return chunk.decode("latin1", "replace").strip()


def _looks_like_tar(data: bytes) -> bool:
    """True when the buffer looks like a TAR archive (USTAR magic at offset 257)."""
    return len(data) >= 512 and data[257:262] == _USTAR_MAGIC


def _parse_ndjson(text: str) -> SdImportParseResult:
    """Parse NDJSON text, skipping blank and unparseable lines."""
    records: list[dict] = []
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
        else:
            skipped += 1
    return SdImportParseResult(records, skipped)


def _extract_ndjson_from_tar(data: bytes) -> str:
    """Concatenate the contents of every ``*.ndjson`` member of an uncompressed
    (USTAR/GNU) TAR archive. Only regular-file entries are read."""
    chunks: list[str] = []
    offset = 0
    length = len(data)

    while offset + 512 <= length:
        header = data[offset : offset + 512]
        # A block of all-zero bytes marks the end of the archive.
        if not any(header):
            break

        name = _read_nul_terminated(header[0:100])
        # Size is a 0-padded octal string in bytes 124..136.
        size_field = _read_nul_terminated(header[124:136])
        try:
            size = int(size_field, 8) if size_field else 0
        except ValueError:
            break
        # typeflag '0' or NUL is a regular file.
        type_flag = header[156:157]

        offset += 512
        if size < 0:
            break

        if type_flag in (b"0", b"\0", b"") and name.lower().endswith(".ndjson"):
            chunks.append(data[offset : offset + size].decode("utf-8", "replace"))

        # Advance past the file data, rounded up to the next 512-byte boundary.
        offset += ((size + 511) // 512) * 512

    return "\n".join(chunks)


def parse_sd_measurements(
    data: bytes, filename: str | None = None
) -> SdImportParseResult:
    """Parse an uploaded SD-card file into measurement records.

    Accepts either a raw ``.ndjson`` backup or the ``hivescale-sd-data.tar``
    download (an uncompressed TAR whose ``*.ndjson`` members hold the readings).
    The archive type is decided from the filename extension first, then by
    sniffing the USTAR magic so a mislabelled upload still works.

    De-duplication is handled downstream keyed on ``(device_id, measured_at)``.
    """
    is_tar = (
        bool(filename) and filename.lower().endswith(".tar")
    ) or _looks_like_tar(data)

    text = (
        _extract_ndjson_from_tar(data)
        if is_tar
        else data.decode("utf-8", "replace")
    )
    return _parse_ndjson(text)



def distinct_source_device_ids(records: Iterable[dict]) -> list[str]:
    """Return the distinct, non-empty ``device_id`` values stamped into records.

    The firmware writes its own ``device_id`` into every backup line, so a
    well-formed single-card upload yields exactly one id. The import endpoint
    uses this to guard against attaching one scale's backup to a different
    device: if the file's id does not match the device it is being imported
    into, the readings would otherwise be silently re-pinned onto — and thus
    mis-mapped to — the wrong device. Ids are returned in first-seen order;
    rows without a (string) ``device_id`` are ignored so an older backup that
    predates the field never triggers a false mismatch.
    """
    seen: set[str] = set()
    out: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        raw = record.get("device_id")
        if not isinstance(raw, str):
            continue
        did = raw.strip()
        if did and did not in seen:
            seen.add(did)
            out.append(did)
    return out


def split_new_and_duplicate(
    keys: Iterable[Hashable],
    existing_keys: set,
) -> tuple[list, int]:
    """Partition ``keys`` into the ones worth inserting and a duplicate count.

    A key is a duplicate when it is already present in the database
    (``existing_keys``) or when it was already seen earlier in this same batch
    (the SD backup file commonly contains repeated lines). The returned list
    preserves first-seen order and contains each key at most once.

    Returns ``(new_keys, duplicate_count)`` where
    ``len(new_keys) + duplicate_count == len(keys)``.
    """
    seen: set = set()
    new: list = []
    duplicates = 0
    for key in keys:
        if key in seen or key in existing_keys:
            duplicates += 1
            continue
        seen.add(key)
        new.append(key)
    return new, duplicates
