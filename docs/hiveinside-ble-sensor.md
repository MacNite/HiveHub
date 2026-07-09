# HiveInside in-hive BLE sensor

**HiveInside** is an in-hive node that runs its vibration and acoustic FFTs on
board and broadcasts the finished results, so the HiveHub only has to scan for it
— no wiring into the hive. It supplies full FFT bands the wired sensors cannot,
and its temperature/humidity can replace the wired DS18B20/SHT4x for that hive.

Two hardware generations exist. **Both emit the exact same 26-byte
manufacturer-data frame**, so a current HiveHub decodes either out of the box:

| Board | How it is read | Advertising | OTA image |
|---|---|---|---|
| **HiveInside (nRF54LM20A)** — current | passive **beacon** scan (one shared window per cycle) | **always on**, no pairing window | signed Zephyr / MCUboot image (`zephyr.signed.bin`, a few hundred KB) |
| **Legacy HiveInside (ESP32-C6)** — prototype | **GATT** read after the scan locates it by MAC | pairing-mode dependent | ESP32 OTA image (>1 MB) |

Pick the matching type when pairing (portal / configurator) and when uploading
firmware — the board choice keeps the two architectures from receiving each
other's image.

The frame carries (little-endian; valid only when the matching **flags** bit is
set — a failed on-board sensor is reported as *absent*, never as `0.0`):

| Group (flags bit) | Fields | HiveHub field |
|---|---|---|
| SHT (bit 0) | temperature, humidity | `hive_{n}_temp_c`, `ble_{n}_humidity_percent` |
| accel (bit 1) | RMS + swarm/fanning/activity bands | `accel_{n}_*` (reused accelerometer fields) |
| mic (bit 2) | RMS + 5 acoustic bands | `mic_{left,right}_*` (slot 1 → left, 2 → right) |
| battery (bit 3) | percent, millivolts | `ble_{n}_battery_percent`, `ble_{n}_battery_mv` |

A [sample frame + reference decoder](../test-data/hiveinside_nrf54_beacon.py)
lives in `test-data/` (`python3 hiveinside_nrf54_beacon.py`).

---

## Pairing

1. **Firmware flag.** Set `ENABLE_BLE_SCAN 1` (the same shared passive scan used
   for HolyIot / RuuviTag). Only the **legacy ESP32-C6** node additionally needs
   `HIVEINSIDE_USE_GATT 1`; the nRF54 node is beacon-only.
2. **Pair from the setup portal.** Open the provisioning portal (short-press the
   setup button, join the `HiveHub-Setup-XXXX` AP), scan for BLE devices, and add
   the node's MAC to a hive. Choose the sensor type:
   - **HiveInside (nRF54LM20A) — beacon** for the current node, or
   - **Legacy HiveInside (ESP32-C6) — GATT** for the prototype.

> The nRF54 node has **no pairing window** — it is always discoverable, so there
> is nothing to long-press before scanning. Its button just forces an immediate
> measurement.

The same choice is available in the
[web configurator](https://macnite.github.io/HiveHub/configurator.html), which
emits the matching `HIVE_i_JSON` pre-seed and firmware flags.

---

## Firmware updates (OTA relay)

The HiveHub is the only Wi-Fi node, so it **relays** a HiveInside image over BLE
GATT (streamed straight from the HTTPS download into the node's OTA
characteristics; the CRC-32 is verified end-to-end before the node swaps slots).
The BLE OTA protocol — UUIDs, opcodes, status frame, CRC-32 — is unchanged
between the two boards; only the image differs.

Uploads are **board-stamped**. In the dashboard firmware tool, pick
`HiveInside` as the target and the board (`Legacy HiveInside (ESP32-C6)` or
`HiveInside (nRF54LM20A)`), or name the file so the board is auto-detected
(`hiveinside_nrf54lm20a_1.0.0.bin` / `hiveinside_esp32-c6_0.9.0.bin`). The server
refuses a release whose declared board disagrees with its filename, and only
relays a board-stamped release to a sensor that reported the same board (the
HiveHub forwards the node's `board` field, e.g. `nrf54lm20a`). A legacy
board-agnostic release is used only as a fallback, so a C6 image is never relayed
to an nRF54 unit or vice versa.

> **nRF54 / MCUboot semantics:** the signed Zephyr image is small (transfer is
> quick), and after the relay completes the node reboots into a *test* image and
> confirms it. A reverted (unconfirmed) update silently keeps the old firmware,
> so verify the node's reported `fw` version after an update.

See [api.md](api.md) for the `update-hiveinside` command and payload.
