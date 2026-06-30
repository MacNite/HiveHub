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

from tempcomp import TEMP_SOURCE_FIELD, compensate_weight

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
# Largest hive index to expose to Home Assistant. Discovery only ever fires for
# hives a device actually reports, so this is just an upper bound for the flat-key
# detection below.
_MQTT_MAX_HIVES = int(os.environ.get("MQTT_MAX_HIVES", "18"))

# Tuple: (json_key, friendly_name, unit, device_class, state_class, icon)
# Device-level sensors (one set per device, published once).
_HA_SENSORS: list[tuple[str, str, Optional[str], Optional[str], Optional[str], Optional[str]]] = [
    ("ambient_temp_c",           "Ambient temperature",   "°C",   "temperature",     "measurement", None),
    ("ambient_humidity_percent", "Ambient humidity",      "%",    "humidity",        "measurement", None),
    ("battery_voltage_v",        "Battery voltage",       "V",    "voltage",         "measurement", None),
    ("battery_soc_percent",      "Battery charge",        "%",    "battery",         "measurement", None),
    ("solar_power_mw",           "Solar power",           "mW",   None,              "measurement", "mdi:solar-power"),
    ("solar_bus_voltage_v",      "Solar bus voltage",     "V",    "voltage",         "measurement", None),
    ("rssi_dbm",                 "Wi-Fi signal",          "dBm",  "signal_strength", "measurement", None),
    ("firmware_version",         "Firmware version",      None,   None,              None,          "mdi:chip"),
]


def _hive_sensors(n: int):
    """HA sensor definitions for a single hive (published once per hive index seen)."""
    sensors = [
        (f"scale_{n}_weight_kg",        f"Hive {n} weight",            "kg", "weight",      "measurement",      "mdi:scale"),
        (f"hive_{n}_temp_c",            f"Hive {n} temperature",       "°C", "temperature", "measurement",      None),
        (f"hive_{n}_humidity_percent",  f"Hive {n} humidity",          "%",  "humidity",    "measurement",      None),
        (f"bee_counter_{n}_total_in",   f"Hive {n} bees in (total)",   None, None,          "total_increasing", "mdi:bee"),
        (f"bee_counter_{n}_total_out",  f"Hive {n} bees out (total)",  None, None,          "total_increasing", "mdi:bee"),
        (f"bee_counter_{n}_interval_in",  f"Hive {n} bees in",         None, None,          "measurement",      "mdi:bee"),
        (f"bee_counter_{n}_interval_out", f"Hive {n} bees out",        None, None,          "measurement",      "mdi:bee"),
    ]
    # Temperature-compensated weight. The backend's compensation model carries a
    # coefficient only for scales 1 and 2 (scale{1,2}_tempco_kg_per_c), so the
    # compensated entity is only meaningful for those two hives; for the rest the
    # raw weight above is the authoritative value.
    if n <= 2:
        sensors.insert(
            1,
            (f"scale_{n}_weight_kg_compensated", f"Hive {n} weight (temp-compensated)",
             "kg", "weight", "measurement", "mdi:scale-balance"),
        )
    return sensors


def _flatten_hives(payload: dict) -> None:
    """Expand the nested hives[] array (firmware v0.20.0+) into the flat
    scale_N_/hive_N_/bee_counter_N_ keys the HA sensor templates are keyed on."""
    for h in (payload.get("hives") or []):
        n = h.get("index")
        if not isinstance(n, int):
            continue
        if h.get("weight_kg") is not None:
            payload[f"scale_{n}_weight_kg"] = h["weight_kg"]
        if h.get("temp_c") is not None:
            payload[f"hive_{n}_temp_c"] = h["temp_c"]
        if h.get("humidity_percent") is not None:
            payload[f"hive_{n}_humidity_percent"] = h["humidity_percent"]
        bc = h.get("bee_counter") or {}
        for k in ("total_in", "total_out", "interval_in", "interval_out"):
            if bc.get(k) is not None:
                payload[f"bee_counter_{n}_{k}"] = bc[k]


def _present_hive_indices(payload: dict) -> set[int]:
    """Hive indices that have any data in this (already flattened) payload."""
    idx: set[int] = set()
    for h in (payload.get("hives") or []):
        n = h.get("index")
        if isinstance(n, int):
            idx.add(n)
    for n in range(1, _MQTT_MAX_HIVES + 1):
        if payload.get(f"scale_{n}_weight_kg") is not None or payload.get(f"hive_{n}_temp_c") is not None:
            idx.add(n)
    return idx


def _apply_tempco(payload: dict, tempco: tuple) -> None:
    """Add temperature-compensated weights to an already-flattened payload.

    ``tempco`` is ``(source, ref_temp_c, scale1_coeff, scale2_coeff)`` as returned
    by the backend's ``load_tempco_configs`` (only present when the device has
    compensation enabled with a non-zero coefficient). Mirrors the read-path
    ``attach_temperature_compensation`` but for one live reading: there is no
    history here, so the *instantaneous* temperature is used (the DB/read path
    keeps the EMA-smoothed version for charts). The coefficient model only covers
    scales 1 and 2, so only those two compensated keys are emitted.
    """
    source, ref_temp, c1, c2 = tempco
    temp = payload.get(TEMP_SOURCE_FIELD.get(source, "ambient_temp_c"))
    for n, coeff in ((1, c1), (2, c2)):
        w = payload.get(f"scale_{n}_weight_kg")
        if w is None:
            continue
        payload[f"scale_{n}_weight_kg_compensated"] = compensate_weight(
            w, temp, ref_temp, coeff
        )
        # Mirror onto the nested hive entry too, so consumers of the raw `hives[]`
        # array (not just the flat keys) also see the compensated value.
        for h in (payload.get("hives") or []):
            if h.get("index") == n and h.get("weight_kg") is not None:
                h["weight_kg_compensated"] = payload[f"scale_{n}_weight_kg_compensated"]
    payload["tempco_applied"] = True


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
        # Devices we have already published device-level HA discovery for.
        self._discovered: set[str] = set()
        # Per-device set of hive indices we have published hive discovery for, so
        # a hive that appears later (e.g. added in the portal) still gets entities.
        self._discovered_hives: dict[str, set[int]] = {}

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
        # Both the device-level and per-hive discovery state must be cleared —
        # otherwise hives already seen this process keep their entry in
        # _discovered_hives and their per-hive sensors are never re-announced
        # after a broker restart that dropped the retained discovery configs.
        self._discovered.clear()
        self._discovered_hives.clear()

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

    def is_active(self) -> bool:
        """True once the bridge has a live client (MQTT enabled and started).

        Lets callers skip work (e.g. a tempco config lookup) when MQTT is off.
        """
        return self._client is not None

    # ── publishing ───────────────────────────────────────────────────────────
    def publish_measurement(
        self,
        device_id: str,
        state: dict[str, Any],
        measured_at: datetime,
        display_name: Optional[str] = None,
        tempco: Optional[tuple] = None,
    ) -> None:
        """Publish one measurement. Never raises — failures are logged, not propagated.

        ``tempco`` (optional) is the device's temperature-compensation config; when
        given, ``scale_{1,2}_weight_kg_compensated`` are added to the payload.
        """
        client = self._client
        if not client:
            return
        try:
            payload = dict(state)
            payload["device_id"] = device_id
            payload["measured_at"] = measured_at.isoformat()
            # Expand the multi-hive array into flat per-hive keys for the HA templates.
            _flatten_hives(payload)
            # Normalise the two firmware battery field names into one HA-friendly key.
            if payload.get("battery_voltage_v") is None and payload.get("battery_voltage") is not None:
                payload["battery_voltage_v"] = payload["battery_voltage"]
            # Add temperature-compensated weights (scales 1/2) for the live feed.
            if tempco is not None:
                _apply_tempco(payload, tempco)

            if MQTT_HA_DISCOVERY:
                if device_id not in self._discovered:
                    self._publish_sensor_configs(client, device_id, display_name, _HA_SENSORS)
                    self._discovered.add(device_id)
                # Publish per-hive discovery for any hive index not yet seen.
                seen = self._discovered_hives.setdefault(device_id, set())
                for n in sorted(_present_hive_indices(payload)):
                    if n not in seen:
                        self._publish_sensor_configs(client, device_id, display_name, _hive_sensors(n))
                        seen.add(n)

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
            "manufacturer": "HiveHub",
            "model": "ESP32 Dual Beehive Scale",
        }

    def _publish_sensor_configs(self, client, device_id: str, display_name: Optional[str],
                                sensors) -> None:
        state_topic = self._device_state_topic(device_id)
        availability_topic = self._device_availability_topic(device_id)
        device_block = self._device_block(device_id, display_name)

        for key, name, unit, device_class, state_class, icon in sensors:
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
