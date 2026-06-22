# HiveScale documentation

Reference docs for the HiveScale dual beehive scale. For the project overview,
hardware list, firmware/server setup, and API summary, start with the
[main README](../README.md). The pages below go deeper on individual topics.

## Hardware & wiring

- [wiring.md](wiring.md) — full ESP32 pin map and wiring for every sensor and module.
- [../pcb-design/README.md](../pcb-design/README.md) — KiCad Scale Module / Power Module pinout and fabrication notes.
- [accelerometer.md](accelerometer.md) — the vibration science and ~20 Hz pre-swarm signal (and the legacy wired LIS3DH/LIS2DH12 driver; stock builds now read vibration over BLE).

## In-hive BLE sensors & bee counters

- [holyiot-ble-sensor.md](holyiot-ble-sensor.md) — HolyIot 25015 beacon (temp / humidity / pressure / acceleration) and pairing.
- [ruuvitag-ble-sensor.md](ruuvitag-ble-sensor.md) — RuuviTag four-in-one beacon on the same scan bridge.
- [beehivemonitoring-gatt.md](beehivemonitoring-gatt.md) — beehivemonitoring.com HiveHeart (in-hive) and HiveScale (weight) over GATT.
- [hivetraffic-bee-counter.md](hivetraffic-bee-counter.md) — HiveTraffic wireless entrance bee counter (BLE/GATT).

## Firmware behaviour

- [offgrid-firmware-notes.md](offgrid-firmware-notes.md) — solar (INA219) and LiPo (MAX17048) power telemetry, and the wake/deep-sleep cycle.
- [calibration-mode.md](calibration-mode.md) — fast-cycle calibration mode (firmware + backend).
- [ap-mode-sd-download.md](ap-mode-sd-download.md) — AP/setup mode, the setup button, and the SD-card download + HivePal re-import.

## Backend, API & insights

- [api.md](api.md) — complete REST API reference (device + HivePal app endpoints, payload, schema).
- [temperature-compensation.md](temperature-compensation.md) — backend load-cell temperature-drift correction and the fit endpoint.
- [insights.md](insights.md) — rule-based colony insight detector catalogue.
- [insights-sources-tldr.md](insights-sources-tldr.md) — TL;DR of the research literature behind the insights.

## Deployment & testing

- [docker-install.md](docker-install.md) — generic Docker / Docker Compose deployment.
- [truenas-install.md](truenas-install.md) — TrueNAS Scale (Custom App) deployment.
- [test-commands.md](test-commands.md) — `curl` examples for exercising the backend.
