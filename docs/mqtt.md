# MQTT bridge & Home Assistant integration

HiveHub can **optionally** mirror every measurement to an MQTT broker in addition
to storing it in PostgreSQL, so readings flow into
[Home Assistant](https://www.home-assistant.io/), Node-RED, openHAB or any other
MQTT consumer. The bridge is **off by default** and purely **additive**: it runs
in a background thread and is fail-soft, so a broker outage (or a missing
`paho-mqtt`) never blocks or fails ingestion or the API.

The implementation is `server/mqtt_publisher.py`.

## Enabling

Set `MQTT_ENABLED=true` and point `MQTT_HOST` at your broker. All configuration
is via environment variables (see `server/.env.example` / `docker/.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `MQTT_ENABLED` | `false` | Master switch. When off, the bridge is a no-op. |
| `MQTT_HOST` | `localhost` | Broker hostname/IP (e.g. the Mosquitto add-on). |
| `MQTT_PORT` | `1883` | Broker port (usually `8883` with TLS). |
| `MQTT_USERNAME` | *(empty)* | Optional broker username. |
| `MQTT_PASSWORD` | *(empty)* | Optional broker password. |
| `MQTT_TLS` | `false` | Connect over TLS. |
| `MQTT_CLIENT_ID` | `hivescale-backend` | MQTT client id. |
| `MQTT_BASE_TOPIC` | `hivescale` | Root of every topic (see below). |
| `MQTT_QOS` | `0` | QoS for all publishes. |
| `MQTT_RETAIN` | `true` | Retain `state` messages so a fresh subscriber gets the last reading. |
| `MQTT_HA_DISCOVERY` | `true` | Publish Home Assistant discovery configs. Set `false` to publish state only. |
| `MQTT_HA_DISCOVERY_PREFIX` | `homeassistant` | Must match Home Assistant's discovery prefix. |
| `MQTT_HA_EXPIRE_AFTER` | `0` | If >0, HA marks an entity *unavailable* after this many seconds without an update. Keep `0` for devices on long deep-sleep cycles. |
| `MQTT_KEEPALIVE` | `60` | MQTT keep-alive interval (seconds). Rarely changed; not in the example files. |
| `MQTT_MAX_HIVES` | `18` | Upper bound for per-hive flat-key detection; mirror the firmware's `MAX_HIVES`. Rarely changed; not in the example files. |

```bash
MQTT_ENABLED=true
MQTT_HOST=192.168.1.10        # your broker (e.g. the Mosquitto add-on)
MQTT_PORT=1883
MQTT_USERNAME=hivescale       # optional
MQTT_PASSWORD=...             # optional
MQTT_HA_DISCOVERY=true        # auto-create Home Assistant entities
```

> **Why `hivescale`?** The default base topic (and the HA `identifiers`) keep the
> old `hivescale` name on purpose — changing it would orphan existing retained
> topics and HA entities. It is unrelated to the third-party
> beehivemonitoring.com "HiveScale" product.

## Topics

With `MQTT_BASE_TOPIC=hivescale` (the default):

| Topic | Payload |
|---|---|
| `hivescale/bridge/availability` | `online` / `offline` — the bridge's last-will; flips to `offline` if the backend drops off the broker. |
| `hivescale/<device_id>/availability` | `online` / `offline` — per device. |
| `hivescale/<device_id>/state` | Retained JSON of the latest reading (every non-null measurement field). |

The `state` message is the whole measurement as JSON — including the nested
`hives[]` array and every flat field. Even when HA discovery is off, everything
is here for custom templated sensors.

## Home Assistant discovery

When `MQTT_HA_DISCOVERY=true`, the bridge publishes
[MQTT-discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
configs the first time it sees each device (and each hive / in-hive module), so
entities appear automatically. Make sure the
[MQTT integration](https://www.home-assistant.io/integrations/mqtt/) is set up
and pointed at the same broker.

### The hub device

One Home Assistant device per ESP32 (`manufacturer: HiveHub`), carrying the
device-wide and per-hive sensors:

- **Device-wide:** ambient temperature & humidity, battery voltage & charge,
  solar power & bus voltage, Wi-Fi signal, firmware version.
- **Per hive:** weight (and a temperature-compensated weight for hives 1–2),
  in-hive temperature & humidity, and bee-counter totals & per-interval counts.

The per-hive temperature/humidity here is the hive's **resolved** value — the
one picked from the highest-priority source available for that hive.

### Per-module sub-devices

Any in-hive **wireless module** a hive carries is published as **its own Home
Assistant device**, nested under the hub via `via_device`, so each physical
device is listed separately with its own entities. Discovery is **per entity**
and fires only once that field actually arrives, so a hive without a given
module never sprouts empty entities, and a module never gets entities for
readings it does not produce. A field seen for the first time later simply adds
its entity then.

| Sub-device | Manufacturer / model | Entities |
|---|---|---|
| **Hive N HiveHeart** | beehivemonitoring.com / HiveHeart | temperature, humidity, sound frequency, sound energy, sound peak, battery, signal |
| **Hive N scale** | beehivemonitoring.com / HiveScale | weight, temperature, humidity, pressure, battery, signal |
| **Hive N HolyIOT** | HolyIOT / In-hive BLE sensor | temperature, humidity, pressure, battery, signal, vibration, vibration peak |
| **Hive N RuuviTag** | Ruuvi / RuuviTag | temperature, humidity, pressure, battery, signal, vibration, vibration peak |
| **Hive N HiveInside** | HiveHub / HiveInside (nRF54LM20A) | temperature, humidity, battery, battery voltage, signal, firmware version, vibration + swarm/fanning/activity bands, sound level + sub-bass/hum/piping/stress/high bands |

Each module exposes its **own** temperature/humidity, even though the hive's
resolved temperature/humidity also appears on the hub device. This is
deliberate: a hive fitted with **both** a HiveHeart and a HiveScale has two
independent temperature/humidity probes, and this surfaces both instead of only
the single value the hive resolves to.

> The three **in-hive BLE beacons** (HolyIOT, HiveInside, RuuviTag) share one
> sub-device slot — a hive carries at most one of them — and its manufacturer,
> model and entity names follow the beacon the firmware actually heard, reported
> as `ble_<n>_sensor_type` (`ble.sensor_type` in the nested `hives[]` form). A
> beacon that reports no type at all is announced generically as
> *HiveHub / In-hive BLE sensor*, and if the type only becomes known later — or
> the beacon is swapped for a different brand — discovery is re-published and
> Home Assistant updates the device in place (the entity `unique_id`s do not
> depend on the type).
>
> Home Assistant assigns an `entity_id` **once**, when it first registers an
> entity, and never rewrites it afterwards — so an entity that was created while
> the beacon was announced under a different identity keeps the old slug (e.g.
> `sensor.…_holyiot_battery` on a device now correctly named *HiveInside*) even
> though its displayed name updates. The reading is correct; only the id is
> historical. Rename it in Home Assistant (entity settings → *Entity ID*) if the
> stale slug bothers you.
>
> Their entity sets differ because the hardware does: the HolyIOT and the
> RuuviTag have a barometer and send raw axes the hub turns into a vibration
> RMS/peak, while the HiveInside has no barometer but runs its vibration and
> acoustic FFTs on board and reports finished bands, a cell voltage and its
> firmware version. Since entities are announced per field, each beacon gets
> exactly what it can populate.

> A BLE beacon's **temperature** is a special case. The firmware does not put it
> in the hive's `ble` object at all: the hive arbitrates a single temperature
> between the wired DS18B20, the beacon and a HiveHeart, and records the winner
> in `temp_source`. When the beacon won (`temp_source == "ble"`, the default
> whenever a beacon reports one), the bridge mirrors that value onto
> `ble_<n>_temp_c` so it also appears on the beacon's own device — a beacon that
> measures temperature and humidity with one SHT4x otherwise showed only the
> humidity there. A hive whose temperature came from a wired probe or a
> HiveHeart gets no beacon temperature entity: the reading then belongs to
> another sensor. The hive-level `hive_<n>_temp_c` entity on the hub device is
> unaffected either way.

> **Entities left behind by a previous beacon are deleted.** A hive slot keeps
> the same Home Assistant device identifiers whichever beacon sits in it, so a
> slot that used to hold a HolyIOT and now holds a HiveInside inherits the
> HolyIOT's retained discovery configs — its *pressure* entity survives on the
> HiveInside device and reads *Unknown* forever, because a retained config is
> never removed by simply not re-publishing it. Each known beacon type therefore
> declares what it can report at all, and anything outside that set is retracted
> with an empty retained payload on the discovery topic, which removes the entity
> from Home Assistant and clears it from the broker. A beacon whose type is
> unknown declares nothing and never has entities deleted.

> The HiveScale's **pressure** commonly reads a flat **1000 hPa**: on most units
> the barometer is not activated by the manufacturer, so this is the sensor's
> idle default rather than a real ambient reading. See
> [beehivemonitoring-gatt.md](beehivemonitoring-gatt.md).

### Flat keys

Discovery entities are keyed on flat JSON fields in the `state` payload. The
per-module fields use `<source>_<n>_<field>` names — `hiveheart_<n>_temp_c`,
`hivescale_<n>_weight_kg`, `ble_<n>_pressure_hpa`, etc. — which are exactly the
names the legacy flat firmware already emits for hives 1–2. For newer firmware
that sends the nested `hives[]` array, the bridge flattens each hive's
`hiveheart` / `hivescale` / `ble` sub-object onto the same keys, so both payload
shapes produce identical entities.

A BLE beacon's **vibration and acoustic** values are the exception: the firmware
maps them onto the hive's `accel` / `mic` objects so they reuse the
wired-accelerometer and microphone schema all the way into the `hive_readings`
columns. In the nested `hives[]` payload only the BLE path writes those objects,
so the bridge copies them onto `ble_<n>_accel_*` / `ble_<n>_mic_*` keys and
attributes them to the beacon. Legacy flat payloads are deliberately left alone
there — in that shape the same `accel_<n>_*` / `mic_left_*` keys are shared with
a wired accelerometer and the stereo INMP441 build, so the beacon could
otherwise be credited with another sensor's readings.

Each entity's `value_template` renders nothing when its field is absent from a
given reading, so Home Assistant keeps the last known value instead of flapping
to *unknown*.

## Behaviour notes

- **Retained state + last-will.** `state` and availability topics are retained,
  and the bridge registers an MQTT last-will, so subscribers always see the
  latest reading and the bridge's online/offline status.
- **Re-announce on reconnect.** After a broker reconnect the bridge clears its
  "already discovered" bookkeeping and re-publishes discovery, so Home Assistant
  recovers its entities even if the broker dropped the retained discovery
  configs.
- **Fail-soft.** Any MQTT error is logged and swallowed; ingestion and the API
  are never affected. If `paho-mqtt` is not installed, the bridge logs once and
  disables itself.
