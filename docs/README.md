# HiveHub documentation

> **Project renamed: HiveScale → HiveHub.** What began as a dual beehive scale
> has grown into a general **data collector / hub for many types of beehive
> sensors and scales** (up to 16 hives per ESP32), so the project was renamed.
> A few internal identifiers (the database measurement columns, the OTA `target`
> value, the Docker image name, the device's stored-config namespace, MQTT
> topics) still use the old `hivescale` name on purpose — changing them would
> need data/firmware migrations — and the third-party **beehivemonitoring.com
> "HiveScale"** wireless weight scale is an unrelated product that keeps its own
> name.

Reference docs for **HiveHub**, an ESP32-based data collector for beehive
sensors and scales. HiveHub reads a range of sensors natively on the device —
a selectable **ambient sensor** (SHT4x/SHT40, SHT3x, or a BME280 that also
reports barometric pressure), **DS18B20** (in-hive
temperature), **INMP441** (in-hive sound),
**MAX17048** (LiPo battery), and **wired load cells via NAU7802 or HX711** — and
bridges wireless BLE/GATT sensors and scales on top. For the project overview,
hardware list, firmware/server setup, and API summary, start with the
[main README](../README.md). The pages below go deeper on individual topics.

## Hardware & wiring

- [multi-hive.md](multi-hive.md) — **up to 16 hives per ESP32** (firmware v0.20.0): NAU7802 I2C scales, TCA9548A mux, one DS18B20 per hive, the hive-centric portal, the BLE budget, and the data model.
- [wiring.md](wiring.md) — full ESP32 pin map and wiring for every sensor and module.
- [../pcb-design/README.md](../pcb-design/README.md) — the KiCad boards (all tested and working): **ESP32-C6 Scale Module (recommended)**, NAU7802 breakout, Power Module, and the legacy 30-pin Scale Module — pinouts and fabrication notes.
- [accelerometer.md](accelerometer.md) — the vibration science and ~20 Hz pre-swarm signal (read over BLE from an in-hive sensor; the old wired LIS3DH/LIS2DH12 driver has been removed).

## In-hive BLE sensors & bee counters

- [hiveinside-ble-sensor.md](hiveinside-ble-sensor.md) — HiveInside in-hive node (on-board FFT bands): the nRF54LM20A beacon, pairing, and OTA.
- [holyiot-ble-sensor.md](holyiot-ble-sensor.md) — HolyIot 25015 beacon (temp / humidity / pressure / acceleration) and pairing.
- [ruuvitag-ble-sensor.md](ruuvitag-ble-sensor.md) — RuuviTag four-in-one beacon on the same scan bridge.
- [beehivemonitoring-gatt.md](beehivemonitoring-gatt.md) — beehivemonitoring.com HiveHeart (in-hive) and HiveScale (weight) over GATT, why only one reader can connect at a time, and how to debug `connect failed` / `status=13`.
- [hivetraffic-bee-counter.md](hivetraffic-bee-counter.md) — HiveTraffic wireless entrance bee counter (BLE/GATT).
- [device-not-supported-yet.md](device-not-supported-yet.md) — **my device isn't in the list yet**: how to request support as a GitHub issue and capture the integration data with nRF Connect.

## Firmware behaviour

- [offgrid-firmware-notes.md](offgrid-firmware-notes.md) — LiPo (MAX17048) power telemetry and the wake/deep-sleep cycle.
- [calibration-mode.md](calibration-mode.md) — fast-cycle calibration mode (firmware + backend).
- [ap-mode-sd-download.md](ap-mode-sd-download.md) — AP/setup mode, the setup button, and the SD-card download + HivePal re-import.

## Backend, API & insights

- [../server/dashboard/README.md](../server/dashboard/README.md) — the optional **built-in web dashboard** (login-protected `/api/v1/local/*` API served at `/dashboard`) for single-owner self-hosts; [try the live demo](https://macnite.github.io/HiveHub/dashboard-demo/).
- [api.md](api.md) — complete REST API reference (device + HivePal app endpoints, payload, schema).
- [mqtt.md](mqtt.md) — optional **MQTT bridge** to Home Assistant / Node-RED / openHAB: topics, config, and per-hive / per-module Home Assistant auto-discovery.
- [temperature-compensation.md](temperature-compensation.md) — backend load-cell temperature-drift correction and the fit endpoint.
- [insights.md](insights.md) — rule-based colony insight detector catalogue.
- [insights-sources-tldr.md](insights-sources-tldr.md) — TL;DR of the research literature behind the insights.
- [notifications.md](notifications.md) — insight alert notifications by e-mail (SMTP) and Web Push (browser / PWA).

## Deployment & testing

- [backup-restore.md](backup-restore.md) — **download / back up every reading** from the dashboard and load it back after a redeploy, or hand it to a beekeeper moving to their own server.
- [docker-install.md](docker-install.md) — generic Docker / Docker Compose deployment.
- [truenas-install.md](truenas-install.md) — TrueNAS Scale (Custom App) deployment.
- [test-commands.md](test-commands.md) — `curl` examples for exercising the backend.

## Releases

Release notes for each tagged version, mirroring the
[GitHub releases](https://github.com/MacNite/HiveHub/releases).

- [releases/v0.3.md](releases/v0.3.md) — **V0.3**: selectable ambient sensor, per-module Home Assistant devices, idempotent ingest, safer self-host defaults, ESP32-C6 field hardening.
- [releases/v0.2.md](releases/v0.2.md) — **V0.2**: HiveHeart acoustic FFT, e-mail / Web Push alerts, dashboard chart overhaul, the nRF54LM20A HiveInside node.
- [releases/v0.1.md](releases/v0.1.md) — **V0.1**: first release — tested PCBs, multi-hive firmware, wireless in-hive sensors, the self-hosted backend.

## Audits

- [audits/full-code-documentation-ux-audit.md](audits/full-code-documentation-ux-audit.md) — repository-wide code, documentation, and UX audit (2026-07): findings, fixes, and roadmap.
