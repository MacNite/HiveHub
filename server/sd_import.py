"""Helpers for bulk-importing measurements parsed from a HiveHub SD card.

The ESP32 keeps an append-only ``/measurements.ndjson`` backup on its SD card.
A beekeeper can pull that file in AP mode and upload it later through the HivePal
web UI, which forwards the parsed records to
``POST /api/v1/app/devices/{device_id}/measurements/import``.

The same measurement can legitimately appear more than once — the backup file is
never pruned, the retry cache replays rows, and a beekeeper may upload the same
download twice. Re-importing must therefore be idempotent. We treat
``(device_id, measured_at)`` as the natural key for a reading: the firmware only
produces one sample per wake-up, so two rows for the same device and instant are
the same observation.

This module intentionally has **no** third-party imports so the de-duplication
logic can be unit-tested without a database or FastAPI.
"""

from __future__ import annotations

from typing import Hashable, Iterable


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
