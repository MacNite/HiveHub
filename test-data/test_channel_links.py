"""Tests for the channels PATCH normalisation (hive names + HivePal hive links).

Run: PYTHONPATH=server python3 -m pytest -q test-data/test_channel_links.py
     (no database required)
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://unused@localhost/unused")
os.environ.setdefault("API_KEY", "test-api-key-not-used-0000")

from devices import channel_updates  # noqa: E402
from schemas import DeviceChannelsUpdateIn  # noqa: E402


def test_names_merge_legacy_fields_and_map():
    names, links = channel_updates(
        DeviceChannelsUpdateIn(
            scale_1_display_name="North",
            names={"2": "South", "3": "East", "18": "Last"},
        )
    )
    assert names == {1: "North", 2: "South", 3: "East", 18: "Last"}
    assert links == {}


def test_names_outside_range_or_none_are_dropped():
    names, _ = channel_updates(
        DeviceChannelsUpdateIn(names={"0": "x", "19": "y", "abc": "z", "4": None})
    )
    assert names == {}


def test_hive_links_set_and_clear():
    _, links = channel_updates(
        DeviceChannelsUpdateIn(
            hive_ids={"1": " 7f3c-uuid ", "2": "", "3": None, "42": "ignored"}
        )
    )
    assert links == {1: "7f3c-uuid", 2: None, 3: None}


def test_empty_name_is_kept_so_a_name_can_be_cleared():
    names, _ = channel_updates(DeviceChannelsUpdateIn(names={"5": ""}))
    assert names == {5: ""}
