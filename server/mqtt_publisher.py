"""Optional MQTT bridge for HiveScale.

When enabled (``MQTT_ENABLED=true``), every measurement that lands on the
device-facing ingest endpoint is mirrored to an MQTT broker as a retained JSON
state message. This lets HiveScale feed Home Assistant, Node-RED, openHAB or any
other MQTT consumer **in addition to** its own PostgreSQL store — it is purely
additive and never blocks or fails ingestion.

Topic layout (``MQTT_BASE_TOPIC`` defaults to ``hivescale``)::

    hivescale/bridge/availability        -> "online" / "offline"  (bridge LWT)
    hivescale/<device_id>/availability   -> "online" / "offline"  (per device)
    hivescale/<device_id>/state          -> retained JSON of the latest reading

With ``MQTT_HA_DISCOVERY=true`` (the default when MQTT is on) the bridge also
publishes Home Assistant MQTT-discovery configs the first time it sees each
device, so a curated set of sensors (weights, temperatures, humidity, battery,
solar, signal, bee-counter totals…) appears automatically under one HA device.

Everything here degrades gracefully: if ``paho-mqtt`` is not installed or the
broker is unreachable, the bridge logs once and turns itself into a no-op.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("hivescale.mqtt")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── Configuration (all via environment) ──────────────────────────────────────
MQTT_ENABLED = _env_bool("MQTT_ENABLED", False)
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "") or None
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "") or None
MQTT_TLS = _env_bool("MQTT_TLS", False)
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "hivescale-backend")
MQTT_BASE_TOPIC = os.environ.get("MQTT_BASE_TOPIC", "hivescale").strip("/")
MQTT_QOS = int(os.environ.get("MQTT_QOS", "0"))
MQTT_RETAIN = _env_bool("MQTT_RETAIN", True)
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))

# Home Assistant MQTT discovery.
MQTT_HA_DISCOVERY = _env_bool("MQTT_HA_DISCOVERY", True)
MQTT_HA_DISCOVERY_PREFIX = os.environ.get("MQTT_HA_DISCOVERY_PREFIX", "homeassistant").strip("/")
# Mark a sensor "unavailable" in HA if no update arrives within this many
# seconds. 0 disables it (devices on long deep-sleep cycles can report rarely).
MQTT_HA_EXPIRE_AFTER = int(os.environ.get("MQTT_HA_EXPIRE_AFTER", "0"))


# ── Home Assistant discovery sensor catalogue ────────────────────────────────
# Each entry maps a measurement JSON key to the HA sensor metadata used to build
# its discovery config. Only fields that are broadly useful as Home Assistant
# entities are listed — the full reading is always available in the raw `state`
# JSON for anyone templating their own sensors.
#
# Tuple: (json_key, friendly_name, unit, device_class, state_class, icon)
_HA_SENSORS: list[tuple[str, str, Optional[str], Optional[str], Optional[str], Optional[str]]] = [
    ("scale_1_weight_kg",        "Scale 1 weight",        "kg",   "weight",          "measurement", "mdi:scale"),
    ("scale_2_weight_kg",        "Scale 2 weight",        "kg",   "weight",          "measurement", "mdi:scale"),
    ("hive_1_temp_c",            "Hive 1 temperature",    "°C",   "temperature",     "measurement", None),
    ("hive_2_temp_c",            "Hive 2 temperature",    "°C",   "temperature",     "measurement", None),
    ("hive_1_humidity_percent",  "Hive 1 humidity",       "%",    "humidity",        "measurement", None),
    ("hive_2_humidity_percent",  "Hive 2 humidity",       "%",    "humidity",        "measurement", None),
    ("ambient_temp_c",           "Ambient temperature",   "°C",   "temperature",     "measurement", None),
    ("ambient_humidity_percent", "Ambient humidity",      "%",    "humidity",        "measurement", None),
    ("battery_voltage_v",        "Battery voltage",       "V",    "voltage",         "measurement", None),
    ("battery_soc_percent",      "Battery charge",        "%",    "battery",         "measurement", None),
    ("solar_power_mw",           "Solar power",           "mW",   None,              "measurement", "mdi:solar-power"),
    ("solar_bus_voltage_v",      "Solar bus voltage",     "V",    "voltage",         "measurement", None),
    ("rssi_dbm",                 "Wi-Fi signal",          "dBm",  "signal_strength", "measurement", None),
    ("bee_counter_1_total_in",   "Bee counter 1 total in",  None, None, "total_increasing", "mdi:bee"),
    ("bee_counter_1_total_out",  "Bee counter 1 total out", None, None, "total_increasing", "mdi:bee"),
    ("bee_counter_2_total_in",   "Bee counter 2 total in",  None, None, "total_increasing", "mdi:bee"),
    ("bee_counter_2_total_out",  "Bee counter 2 total out", None, None, "total_increasing", "mdi:bee"),
    ("bee_counter_1_interval_in",  "Bee counter 1 interval in",  None, None, "measurement", "mdi:bee"),
    ("bee_counter_1_interval_out", "Bee counter 1 interval out", None, None, "measurement", "mdi:bee"),
    ("firmware_version",         "Firmware version",      None,   None,              None,          "mdi:chip"),
]


class MqttPublisher:
    """Thread-safe, fail-soft MQTT bridge.

    A single instance is created at import time. ``start()`` is called from the
    FastAPI startup hook and ``stop()`` from shutdown. ``publish_measurement``
    is safe to call from any request thread — paho's network loop runs in its
    own background thread, so publishing only enqueues.
    """

    def __init__(self) -> None:
        self._client = None
        self._lock = threading.Lock()
        self._connected = False
        self._started = False
        # Devices we have already published HA discovery for, this process.
        self._discovered: set[str] = set()

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if not MQTT_ENABLED:
            logger.info("MQTT bridge disabled (set MQTT_ENABLED=true to enable)")
            return
        try:
            import paho.mqtt.client as mqtt
        except Exception:  # pragma: no cover - import guard
            logger.error(
                "MQTT_ENABLED=true but paho-mqtt is not installed; MQTT bridge disabled"
            )
            return

        try:
            # paho-mqtt 2.x requires an explicit callback API version.
            try:
                client = mqtt.Client(
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                    client_id=MQTT_CLIENT_ID,
                )
            except AttributeError:  # pragma: no cover - paho-mqtt 1.x fallback
                client = mqtt.Client(client_id=MQTT_CLIENT_ID)

            if MQTT_USERNAME:
                client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
            if MQTT_TLS:
                client.tls_set()

            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect

            # Last will: if the bridge drops, the broker marks it offline.
            client.will_set(
                self._bridge_availability_topic(), "offline", qos=MQTT_QOS, retain=True
            )

            client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE)
            client.loop_start()
            self._client = client
            self._started = True
            logger.info(
                "MQTT bridge connecting to %s:%s (base topic '%s', HA discovery %s)",
                MQTT_HOST, MQTT_PORT, MQTT_BASE_TOPIC,
                "on" if MQTT_HA_DISCOVERY else "off",
            )
        except Exception:
            logger.exception("Failed to start MQTT bridge; continuing without it")
            self._client = None
            self._started = False

    def stop(self) -> None:
        client = self._client
        if not client:
            return
        try:
            client.publish(
                self._bridge_availability_topic(), "offline", qos=MQTT_QOS, retain=True
            )
            client.loop_stop()
            client.disconnect()
        except Exception:
            logger.debug("Error during MQTT shutdown", exc_info=True)
        finally:
            self._client = None
            self._started = False
            self._connected = False

    # ── paho callbacks ───────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # reason_code is an int (paho 1.x) or ReasonCode (2.x); both compare to 0.
        ok = (reason_code == 0) if isinstance(reason_code, int) else (
            getattr(reason_code, "is_failure", False) is False
        )
        if not ok:
            logger.warning("MQTT connect failed: %s", reason_code)
            self._connected = False
            return
        self._connected = True
        logger.info("MQTT bridge connected to %s:%s", MQTT_HOST, MQTT_PORT)
        try:
            client.publish(
                self._bridge_availability_topic(), "online", qos=MQTT_QOS, retain=True
            )
        except Exception:
            logger.debug("Could not publish bridge availability", exc_info=True)
        # Re-publish discovery after a reconnect so HA recovers its entities.
        self._discovered.clear()

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected = False
        logger.warning("MQTT bridge disconnected; paho will retry automatically")

    # ── topics ───────────────────────────────────────────────────────────────
    def _bridge_availability_topic(self) -> str:
        return f"{MQTT_BASE_TOPIC}/bridge/availability"

    def _device_availability_topic(self, device_id: str) -> str:
        return f"{MQTT_BASE_TOPIC}/{device_id}/availability"

    def _device_state_topic(self, device_id: str) -> str:
        return f"{MQTT_BASE_TOPIC}/{device_id}/state"

    # ── publishing ───────────────────────────────────────────────────────────
    def publish_measurement(
        self,
        device_id: str,
        state: dict[str, Any],
        measured_at: datetime,
        display_name: Optional[str] = None,
    ) -> None:
        """Publish one measurement. Never raises — failures are logged, not propagated."""
        client = self._client
        if not client:
            return
        try:
            payload = dict(state)
            payload["device_id"] = device_id
            payload["measured_at"] = measured_at.isoformat()
            # Normalise the two firmware battery field names into one HA-friendly key.
            if payload.get("battery_voltage_v") is None and payload.get("battery_voltage") is not None:
                payload["battery_voltage_v"] = payload["battery_voltage"]

            if MQTT_HA_DISCOVERY and device_id not in self._discovered:
                self._publish_discovery(client, device_id, display_name)
                self._discovered.add(device_id)

            client.publish(
                self._device_availability_topic(device_id),
                "online", qos=MQTT_QOS, retain=True,
            )
            client.publish(
                self._device_state_topic(device_id),
                json.dumps(payload, default=str),
                qos=MQTT_QOS, retain=MQTT_RETAIN,
            )
        except Exception:
            logger.debug("MQTT publish failed for device %s", device_id, exc_info=True)

    def _device_block(self, device_id: str, display_name: Optional[str]) -> dict[str, Any]:
        return {
            "identifiers": [f"hivescale_{device_id}"],
            "name": display_name or device_id,
            "manufacturer": "HiveScale",
            "model": "ESP32 Dual Beehive Scale",
        }

    def _publish_discovery(self, client, device_id: str, display_name: Optional[str]) -> None:
        state_topic = self._device_state_topic(device_id)
        availability_topic = self._device_availability_topic(device_id)
        device_block = self._device_block(device_id, display_name)

        for key, name, unit, device_class, state_class, icon in _HA_SENSORS:
            node = device_id.replace("/", "_").replace("+", "_").replace("#", "_")
            unique_id = f"hivescale_{node}_{key}"
            # Render nothing when the field is absent from a given reading so HA
            # keeps the last known value instead of flapping to "unknown".
            value_template = (
                "{% if value_json." + key + " is defined and value_json." + key
                + " is not none %}{{ value_json." + key + " }}{% endif %}"
            )
            config: dict[str, Any] = {
                "name": name,
                "unique_id": unique_id,
                "object_id": unique_id,
                "state_topic": state_topic,
                "value_template": value_template,
                "availability": [
                    {"topic": availability_topic},
                    {"topic": self._bridge_availability_topic()},
                ],
                "availability_mode": "all",
                "device": device_block,
            }
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            if state_class:
                config["state_class"] = state_class
            if icon:
                config["icon"] = icon
            if MQTT_HA_EXPIRE_AFTER > 0:
                config["expire_after"] = MQTT_HA_EXPIRE_AFTER

            topic = f"{MQTT_HA_DISCOVERY_PREFIX}/sensor/{node}/{key}/config"
            client.publish(topic, json.dumps(config), qos=MQTT_QOS, retain=True)

        logger.info("Published Home Assistant MQTT discovery for device %s", device_id)


# Process-wide singleton used by the FastAPI app.
publisher = MqttPublisher()
