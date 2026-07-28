# HiveInside in-hive BLE sensor

**HiveInside** is an in-hive node that runs its vibration and acoustic FFTs on
board and broadcasts the finished results, so the HiveHub only has to scan for it
— no wiring into the hive. It supplies full FFT bands the wired sensors cannot,
and its temperature/humidity can replace the wired DS18B20/SHT4x for that hive.

HiveInside is the **XIAO nRF54LM20A Sense** node. It emits a 26-byte
manufacturer-data frame that a current HiveHub decodes out of the box:

| Board | How it is read | Advertising | OTA image |
|---|---|---|---|
| **HiveInside (nRF54LM20A)** | passive **beacon** scan (one shared window per cycle) | **always on**, no pairing window | signed Zephyr / MCUboot image (`zephyr.signed.bin`, a few hundred KB) |

> The earlier **ESP32-C6 HiveInside prototype** (which served the same frame over
> GATT and took a >1 MB ESP32 OTA image) has been **removed from the ecosystem**.
> HiveInside now unambiguously means the nRF54LM20A beacon node; there is no
> ESP32-C6 pairing type, GATT measurement path, or C6 firmware target anymore.

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
   for HolyIot / RuuviTag). The nRF54 node is beacon-only, so no GATT measurement
   flag is needed. (`HIVEINSIDE_OTA_ENABLED`, default 1, compiles in the BLE OTA
   relay described below.)
2. **Pair from the setup portal.** Open the provisioning portal (short-press the
   setup button, join the `HiveHub-Setup-XXXX` AP), scan for BLE devices, and add
   the node's MAC to a hive. Choose the sensor type
   **HiveInside (nRF54LM20A) — beacon**.

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
The relayed bytes are an nRF54 MCUboot image and are forwarded **opaquely** — the
HiveHub never runs its own ESP32 self-OTA architecture guard on them; only the
node verifies its own image. The nRF54 node is normally a **non-connectable
beacon** and opens a connectable OTA window on demand, so the relay locates it by
its identity address before connecting.

Uploads may be **board-stamped** `nrf54lm20a` (the only HiveInside board). In the
dashboard firmware tool, pick `HiveInside` as the target, or name the file so the
board is auto-detected (`hiveinside_nrf54lm20a_1.0.0.bin`). The server refuses a
release whose declared board disagrees with its filename. A HiveInside release now
unambiguously means an nRF54 image, so no per-board matching is done — the latest
active HiveInside release (nrf54lm20a-stamped or a legacy board-agnostic one) is
relayed.

> **How the board/version are learned.** The nRF54 beacon advertises its board and
> firmware version in a second manufacturer-data element in its **scan response**
> (magic `'I'`), distinct from the 26-byte measurement frame (magic `'H'`). The
> HiveHub active-scans by default, so both arrive together — no GATT connection is
> needed to learn them, and the HiveHub forwards the node's `board`/`fw` fields to
> the backend.

> **nRF54 / MCUboot semantics:** the signed Zephyr image is small (transfer is
> quick), and after the relay completes the node reboots into a *test* image and
> confirms it. A reverted (unconfirmed) update silently keeps the old firmware,
> so verify the node's reported `fw` version after an update.

The dashboard shows that version under **Device & admin → Firmware → HiveInside
nodes**: one row per hive whose node was heard, with the advertised version and
board (e.g. `v0.4.0 · nrf54lm20a`). The section only appears once a HiveInside
node has actually reported, and it keeps the last known identity when a node
misses a scan window, so it reads as "what this node is running", not "what was
in the last packet".

See [api.md](api.md) for the `update-hiveinside` command and payload.
