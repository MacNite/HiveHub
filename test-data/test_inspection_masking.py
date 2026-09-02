"""Tests for inspection-window masking on the read path (server/inspections.py).

Run: python3 -m pytest test-data/test_inspection_masking.py
 or: PYTHONPATH=server python3 test-data/test_inspection_masking.py   (no DB needed)

An inspection is stored as an interval, not as a flag on each reading, and the
read path turns those intervals back into "blank this reading's hive fields".
Two things about that are easy to get wrong and expensive to get wrong:

1. **Which keys are hive keys.** Blanking too much takes out the ambient,
   battery and RSSI trace — precisely the evidence that the hub was alive while
   the hive was open, without which an inspection is indistinguishable from a
   dead device. Blanking too little leaves the 40 kg cliff in the chart the
   feature exists to remove. The classifier is structural (the first numeric
   path segment names the hive), so a per-hive family added later is covered for
   free — but the hub-level names and the legacy mic_left/mic_right stereo pair
   are exceptions it has to be told about.

2. **The window's edges and scope.** A hive-scoped inspection must not blank the
   hive next to it, and a reading a minute before the beekeeper arrived must
   survive. An OPEN inspection (ended_at NULL) has to mask everything after its
   start, which is the state a beekeeper standing at the hive is in.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# inspections.py imports the app config; nothing here touches a database or a
# network — list_inspections is stubbed out below.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import inspections  # noqa: E402
from schemas import DeviceInspection  # noqa: E402


_failures = 0
T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def window(start_min, end_min, hives=None, device="dev-1"):
    return DeviceInspection(
        id=1,
        device_id=device,
        hives=hives,
        started_at=T0 + timedelta(minutes=start_min),
        ended_at=None if end_min is None else T0 + timedelta(minutes=end_min),
        active=end_min is None,
        source="device",
        acknowledged_at=T0 + timedelta(minutes=start_min),
    )


_expiry_calls = []


def install(*windows):
    """Make list_inspections return exactly these windows, and count the sweeps.

    The timeout sweep is stubbed rather than skipped: masking runs it when it
    sees an open window (a hub that died mid-inspection would otherwise blank
    its own hive forever), and "does it run, and only then" is worth pinning.
    """
    _expiry_calls.clear()
    inspections.list_inspections = (
        lambda ids, start_at=None, end_at=None, limit=500: list(windows)
    )
    inspections.expire_timed_out_inspections = (
        lambda device_id=None: _expiry_calls.append(device_id)
    )


def reading(minute, device="dev-1"):
    """A reading with one hub-level and several per-hive fields, all non-null."""
    return {
        "device_id": device,
        "measured_at": T0 + timedelta(minutes=minute),
        # Hub-level — must survive any inspection.
        "ambient_temp_c": 21.5,
        "ambient_humidity_percent": 60.0,
        "battery_voltage": 4.05,
        "solar_power_mw": 900.0,
        "rssi_dbm": -67,
        "sd_ok": True,
        "firmware_version": "0.25.0",
        # Per-hive — masked while that hive is being inspected.
        "scale_1_weight_kg": 42.0,
        "scale_1_weight_kg_compensated": 41.8,
        "hive_1_temp_c": 34.9,
        "scale_2_weight_kg": 38.0,
        "hive_2_temp_c": 34.7,
        "bee_counter_3_total_in": 12345,
        "hiveheart_4_energy": 7.5,
        "mic_left_rms_dbfs": -41.0,
        "hives": [{"index": 1, "weight_kg": 42.0}, {"index": 2, "weight_kg": 38.0}],
    }


def test_hub_level_keys_are_never_masked():
    install(window(0, 60))
    m = inspections.mask_inspection_readings([reading(30)])[0]
    check("ambient temperature survives an inspection", m["ambient_temp_c"] == 21.5)
    check("ambient humidity survives an inspection", m["ambient_humidity_percent"] == 60.0)
    check("battery voltage survives an inspection", m["battery_voltage"] == 4.05)
    check("solar power survives an inspection", m["solar_power_mw"] == 900.0)
    check("RSSI survives an inspection", m["rssi_dbm"] == -67)
    check("sd_ok survives an inspection", m["sd_ok"] is True)
    check("firmware version survives an inspection", m["firmware_version"] == "0.25.0")


def test_hive_keys_are_masked_and_the_row_is_flagged():
    install(window(0, 60))
    m = inspections.mask_inspection_readings([reading(30)])[0]
    check("hive weight is blanked", m["scale_1_weight_kg"] is None)
    check("compensated weight is blanked too", m["scale_1_weight_kg_compensated"] is None)
    check("in-hive temperature is blanked", m["hive_1_temp_c"] is None)
    check("bee counter total is blanked", m["bee_counter_3_total_in"] is None)
    check("HiveHeart energy is blanked", m["hiveheart_4_energy"] is None)
    # The legacy wired stereo mic is hive 1/2 acoustics under an older name — the
    # one per-hive family the structural classifier cannot see.
    check("legacy stereo mic is blanked", m["mic_left_rms_dbfs"] is None)
    check("the hives[] array is emptied", m["hives"] == [])
    check("the row says it was an inspection", m["inspection"] is True)
    check("the row names the inspection", m["inspection_id"] == 1)


def test_readings_outside_the_window_are_untouched():
    install(window(10, 20))
    before, during, after = inspections.mask_inspection_readings(
        [reading(5), reading(15), reading(25)]
    )
    check("a reading before the inspection keeps its weight", before["scale_1_weight_kg"] == 42.0)
    check("a reading before the inspection is not flagged", before["inspection"] is False)
    check("a reading during the inspection is blanked", during["scale_1_weight_kg"] is None)
    check("a reading after the inspection keeps its weight", after["scale_1_weight_kg"] == 42.0)


def test_window_edges_are_inclusive():
    install(window(10, 20))
    at_start, at_end = inspections.mask_inspection_readings([reading(10), reading(20)])
    check("the reading at the start is masked", at_start["scale_1_weight_kg"] is None)
    check("the reading at the end is masked", at_end["scale_1_weight_kg"] is None)


def test_the_timeout_sweep_runs_only_when_a_window_is_open():
    # A closed window can never mask more than it already does, so the sweep
    # would be a write on every read for nothing.
    install(window(0, 60))
    inspections.mask_inspection_readings([reading(30)])
    check("no timeout sweep for a closed window", _expiry_calls == [])

    install(window(0, None))
    inspections.mask_inspection_readings([reading(30)])
    check("an open window triggers the timeout sweep", _expiry_calls == ["dev-1"])


def test_an_open_inspection_masks_everything_after_its_start():
    # ended_at NULL: the beekeeper has the hive open right now, which is the
    # state the live dashboard tiles are read in.
    install(window(10, None))
    before, during, later = inspections.mask_inspection_readings(
        [reading(5), reading(15), reading(600)]
    )
    check("before an open inspection is untouched", before["scale_1_weight_kg"] == 42.0)
    check("inside an open inspection is masked", during["scale_1_weight_kg"] is None)
    check("still masked hours later", later["scale_1_weight_kg"] is None)


def test_a_hive_scoped_inspection_leaves_the_other_hives_alone():
    # HivePal can open one hive; the hub next to it is still measuring a colony
    # nobody has touched, and blanking it would throw away good data.
    install(window(0, 60, hives=[1]))
    m = inspections.mask_inspection_readings([reading(30)])[0]
    check("the inspected hive is blanked", m["scale_1_weight_kg"] is None)
    check("the inspected hive's temperature is blanked", m["hive_1_temp_c"] is None)
    check("the untouched hive keeps its weight", m["scale_2_weight_kg"] == 38.0)
    check("the untouched hive keeps its temperature", m["hive_2_temp_c"] == 34.7)
    check("an unrelated hive's counter is untouched", m["bee_counter_3_total_in"] == 12345)
    check("only the inspected hive leaves hives[]",
          [h["index"] for h in m["hives"]] == [2])
    check("the row still records which hives", m["inspection_hives"] == [1])


def test_another_device_is_not_masked():
    # One hub being inspected says nothing about the hub next to it.
    install(window(0, 60, device="dev-1"))
    mine, theirs = inspections.mask_inspection_readings(
        [reading(30, device="dev-1"), reading(30, device="dev-2")]
    )
    check("the inspected device is masked", mine["scale_1_weight_kg"] is None)
    check("the other device is untouched", theirs["scale_1_weight_kg"] == 42.0)


def test_no_windows_is_a_no_op():
    install()
    m = inspections.mask_inspection_readings([reading(30)])[0]
    check("nothing is blanked when nothing was inspected", m["scale_1_weight_kg"] == 42.0)
    check("no inspection flag is invented", not m.get("inspection"))


def test_pending_inspection_does_not_mask_measurements():
    pending = window(0, None)
    pending.acknowledged_at = None
    install(pending)
    m = inspections.mask_inspection_readings([reading(30)])[0]
    check("a pending inspection keeps hive measurements", m["scale_1_weight_kg"] == 42.0)
    check("a pending inspection is not reported as active", not m.get("inspection"))


def test_mqtt_inspection_state_distinguishes_off_pending_and_active():
    inspections.expire_timed_out_inspections = lambda device_id=None: 0
    inspections.fetch_device_config = lambda device_id: SimpleNamespace(
        inspection_timeout_minutes=60
    )

    inspections.get_open_inspection = lambda device_id: None
    state = inspections.mqtt_inspection_state("dev-1", now=T0)
    check("MQTT reports an absent inspection as off",
          state == {"inspection_state": "off", "inspection_active": False,
                    "inspection_remaining_seconds": 0})

    pending = window(0, None)
    pending.acknowledged_at = None
    inspections.get_open_inspection = lambda device_id: pending
    state = inspections.mqtt_inspection_state("dev-1", now=T0 + timedelta(minutes=5))
    check("MQTT preserves the pending state without claiming it is active",
          state["inspection_state"] == "pending"
          and state["inspection_active"] is False
          and state["inspection_remaining_seconds"] == 0)

    active = window(0, None)
    inspections.get_open_inspection = lambda device_id: active
    state = inspections.mqtt_inspection_state("dev-1", now=T0 + timedelta(minutes=5))
    check("MQTT reports active mode and its remaining timeout",
          state["inspection_state"] == "active"
          and state["inspection_active"] is True
          and state["inspection_remaining_seconds"] == 55 * 60)


def test_naive_timestamps_are_treated_as_utc():
    # Rows read straight out of some clients arrive without a tzinfo; comparing
    # one against an aware window raises rather than misbehaving, which is a poor
    # way to find out a timestamp lost its offset.
    install(window(0, 60))
    m = reading(30)
    m["measured_at"] = m["measured_at"].replace(tzinfo=None)
    out = inspections.mask_inspection_readings([m])[0]
    check("a naive measured_at is still matched", out["scale_1_weight_kg"] is None)


def test_hive_index_classifier():
    idx = inspections._hive_index_for_key
    cases = {
        "scale_1_weight_kg": 1,
        "scale_18_weight_kg_compensated": 18,
        "hive_2_temp_c": 2,
        "bee_counter_3_total_in": 3,
        "hiveheart_4_energy": 4,
        "hivescale_2_battery_v": 2,
        "accel_1_rms_mg": 1,
        "ble_5_rssi_dbm": 5,
        "mic_7_band_hum_dbfs": 7,
        "mic_left_rms_dbfs": 1,
        "mic_right_peak_dbfs": 2,
        "ambient_temp_c": None,
        "battery_soc_percent": None,
        "solar_current_ma": None,
        "rssi_dbm": None,
        "calibration_mode": None,
        "measurement_id": None,
        "hive_count": None,
        "mic_sample_rate_hz": None,
    }
    for key, expected in cases.items():
        check(f"{key!r} maps to hive {expected}", idx(key) == expected)


def main():
    test_hub_level_keys_are_never_masked()
    test_hive_keys_are_masked_and_the_row_is_flagged()
    test_readings_outside_the_window_are_untouched()
    test_window_edges_are_inclusive()
    test_the_timeout_sweep_runs_only_when_a_window_is_open()
    test_an_open_inspection_masks_everything_after_its_start()
    test_a_hive_scoped_inspection_leaves_the_other_hives_alone()
    test_another_device_is_not_masked()
    test_no_windows_is_a_no_op()
    test_pending_inspection_does_not_mask_measurements()
    test_mqtt_inspection_state_distinguishes_off_pending_and_active()
    test_naive_timestamps_are_treated_as_utc()
    test_hive_index_classifier()

    if _failures:
        print(f"\n{_failures} check(s) FAILED")
        return 1
    print("\nAll inspection masking checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
else:
    def test_all():
        assert main() == 0
