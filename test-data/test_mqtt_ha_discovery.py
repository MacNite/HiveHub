"""Tests for the Home Assistant discovery identity of in-hive BLE beacons
(server/mqtt_publisher.py).

Run: python3 -m pytest test-data/test_mqtt_ha_discovery.py
 or: PYTHONPATH=server python3 test-data/test_mqtt_ha_discovery.py  (no broker needed)

One catalogue entry ("ble") covers three physically different beacons, so the
manufacturer, model and entity-name label are resolved per hive from the type
the firmware reports. A beacon whose type is not known on the first reading is
announced under the generic fallback and must be re-announced once the type
arrives — including for a HolyIOT, whose model string happens to equal the
fallback's, so only its manufacturer and label change.

No broker is involved: the publisher's paho client is replaced with a recorder
and the retained discovery configs are read back out of it.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# mqtt_publisher imports the app's tempcomp helper only, but keep the app's
# required env vars set so importing it stays safe if that changes.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import mqtt_publisher  # noqa: E402


_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


class _Recorder:
    """Stands in for the paho client — discovery configs are just published."""

    def __init__(self):
        self.messages = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.messages.append((topic, payload))


def announce(readings):
    """Publish each reading through a fresh publisher; return the discovery
    configs for hive 1's BLE beacon, oldest first, as (key, config) pairs."""
    pub = mqtt_publisher.MqttPublisher()
    pub._client = _Recorder()
    for ble in readings:
        pub.publish_measurement(
            "hive-test",
            {"hives": [{"index": 1, "ble": dict(ble, present=True)}]},
            datetime.now(timezone.utc),
            "hive-test",
        )
    configs = []
    for topic, payload in pub._client.messages:
        parts = topic.split("/")
        if parts[-1] == "config" and parts[-2].startswith("ble_1_"):
            configs.append((parts[-2], json.loads(payload)))
    return configs


def latest(configs, key):
    """The most recent discovery config published for one entity key."""
    for k, cfg in reversed(configs):
        if k == key:
            return cfg
    return None


BATTERY = {"battery_percent": 80, "rssi_dbm": -60}
HIVEINSIDE = dict(BATTERY, sensor_type="HiveInside", battery_mv=4018)
HOLYIOT = dict(BATTERY, sensor_type="HolyIot 25015")


# ── the type is known from the first reading ─────────────────────────────────
configs = announce([HIVEINSIDE])
cfg = latest(configs, "ble_1_battery_percent")
check("a HiveInside is announced as HiveInside from the start",
      cfg["name"] == "Hive 1 HiveInside battery"
      and cfg["device"]["model"] == "HiveInside (nRF54LM20A)")
check("its battery voltage lands on the same device",
      latest(configs, "ble_1_battery_mv")["device"]["identifiers"]
      == cfg["device"]["identifiers"])

configs = announce([HOLYIOT])
cfg = latest(configs, "ble_1_battery_percent")
check("a HolyIot is announced as HolyIOT from the start",
      cfg["name"] == "Hive 1 HolyIOT battery"
      and cfg["device"]["manufacturer"] == "HolyIOT")

configs = announce([dict(BATTERY, sensor_type="RuuviTag")])
check("a RuuviTag is announced as RuuviTag from the start",
      latest(configs, "ble_1_battery_percent")["name"] == "Hive 1 RuuviTag battery")

configs = announce([BATTERY])
check("an untyped beacon falls back to the generic BLE sensor identity",
      latest(configs, "ble_1_battery_percent")["name"] == "Hive 1 BLE sensor battery")


# ── the type arrives after the first entities were announced ─────────────────
# The regression this file exists for: the fallback and the HolyIOT identity
# share the model "In-hive BLE sensor", so a dedup tag keyed on the model alone
# left a late-typed HolyIot named "Hive 1 BLE sensor ..." for the life of the
# process.
for label, typed, manufacturer in (
    ("HiveInside", HIVEINSIDE, "HiveHub"),
    ("HolyIOT", HOLYIOT, "HolyIOT"),
):
    configs = announce([BATTERY, typed])
    cfg = latest(configs, "ble_1_battery_percent")
    check(f"a beacon typed later is re-announced as {label}",
          cfg["name"] == f"Hive 1 {label} battery"
          and cfg["device"]["manufacturer"] == manufacturer
          and cfg["device"]["name"] == f"hive-test Hive 1 {label}")
    check(f"the re-announced {label} keeps its entity and device ids",
          cfg["unique_id"] == "hivescale_hive-test_ble_1_battery_percent"
          and cfg["device"]["identifiers"] == ["hivescale_hive-test_hive1_ble"])

# A beacon physically swapped for another brand moves its existing entities to
# the new identity rather than leaving them under the old one.
configs = announce([HOLYIOT, HIVEINSIDE])
check("a beacon swapped HolyIot -> HiveInside re-announces every entity",
      latest(configs, "ble_1_battery_percent")["name"] == "Hive 1 HiveInside battery"
      and latest(configs, "ble_1_rssi_dbm")["name"] == "Hive 1 HiveInside signal")

# An unchanged identity must not re-publish configs on every single reading.
configs = announce([HIVEINSIDE, HIVEINSIDE, HIVEINSIDE])
check("a steady beacon is announced once per entity, not per reading",
      [k for k, _ in configs].count("ble_1_battery_percent") == 1)

# A field that only shows up later still gets its entity, under the identity in
# force at the time.
configs = announce([dict(BATTERY, sensor_type="HiveInside"), HIVEINSIDE])
check("a field seen later still gets an entity",
      latest(configs, "ble_1_battery_mv") is not None)
check("no entity is announced for a field the beacon never reports",
      latest(configs, "ble_1_pressure_hpa") is None)


def test_mqtt_ha_discovery():
    """pytest entry point — fails if any check above failed."""
    assert _failures == 0, f"{_failures} BLE discovery identity check(s) failed"


if __name__ == "__main__":
    if _failures:
        print(f"\n{_failures} check(s) FAILED.")
        sys.exit(1)
    print("\nAll BLE discovery identity checks passed.")
