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

Uploads are **board-stamped** `nrf54lm20a` (the only HiveInside board). In the
dashboard firmware tool, pick `HiveInside` as the target; the **Board** field then
shows `HiveInside (nRF54LM20A)` **disabled** — there is nothing to choose, and the
release is always stamped `nrf54lm20a`. Naming the file
`hiveinside_nrf54lm20a_1.0.0.bin` keeps the stamp visible in the filename too. The
API still accepts an omitted board (legacy `board = NULL` releases), but the server
refuses a release whose declared board disagrees with its filename. A HiveInside release now
unambiguously means an nRF54 image, so no per-board matching is done — the latest
active HiveInside release (nrf54lm20a-stamped or a legacy board-agnostic one) is
relayed.

### Triggering a relay

Uploading a `.bin` only **registers** a release — nothing starts the transfer,
on any check-in. The relay is queued explicitly, either from the built-in
dashboard or over the API.

From the dashboard: **Device & admin → Firmware → HiveInside nodes**, then
**Relay to node** on the row for the hive you want to update. The button appears
only when the uploaded release would actually be accepted (see the guards
below), and requires an admin login.

The row then shows the relay's state — *relay queued*, *relaying…*, or *relay
failed* with the reason spelled out underneath (e.g. `HiveInside not found in
scan (asleep or out of range?)`). The button is disabled while a relay is
already queued or running, so a second click cannot stack a duplicate transfer.
A relay that succeeds leaves no badge: the node's advertised version changing is
the confirmation.

> Firmware downloads are deliberately excluded from response compression
> (`SelectiveGZipMiddleware`). A gzipped response has no `Content-Length`, and
> the ESP32 sizes its download from that header — compressing `/firmware/*` made
> every relay fail with `invalid firmware content length -1` before it opened a
> BLE session. Keep any reverse proxy in front of the API from re-compressing
> that path for the same reason.

Over the API:

```bash
curl -X POST -H "X-API-Key: $API_KEY" \
  "https://<host>/api/v1/devices/<device_id>/commands/update-hiveinside?slot=<hive>"
```

`slot` is the **hive index**, so any hive can be updated (up to `MAX_HIVES`), and
the HiveHub matches only HiveInside pairings when resolving the MAC. The relay
runs on the HiveHub's next upload cycle — about 10 minutes by default, or
whatever send interval the device is configured for. There is no separate
command poll; a command is picked up once per cycle, in every mode.

A relay that is claimed but never reported on (the HiveHub crashed, or simply
lost the fire-and-forget result POST after a multi-minute BLE transfer) is
swept back onto the queue after `STALE_CLAIM_MINUTES`, up to
`MAX_COMMAND_ATTEMPTS` times, and then failed with `timed out: claimed by the
device N time(s) without reporting a result`. Only relays are retried this way;
destructive commands such as `factory_reset` fail on the first timeout rather
than being silently repeated.

A hub that *resets* mid-relay no longer has to wait that out. It records the
command id in RTC memory before starting, and the next boot reports the attempt
explicitly — `hub reset during relay (panic/exception) — firmware transfer did
not complete` — against the still-open row. That closes the command on the first
cycle after the reset instead of an hour and three attempts later, and it names
the reset reason, so a crashing hub is distinguishable from one that is merely
out of range. The command is then genuinely failed rather than retried: re-queue
it yourself once the hub is healthy.

Two guards apply before the command is queued:

| Condition | Result |
|---|---|
| `slot` outside `1..18` | `400` |
| No active `hiveinside` release | `404` |
| Release not newer than the version the node advertises | `409` |

The version gate compares against `hives[].ble.firmware_version` — the version
the node itself broadcasts — so re-running the command after a successful update
is a no-op instead of a pointless multi-minute BLE transfer and a reboot of a
healthy hive sensor. A node that has never advertised a version is not gated.
Pass `force=true` to relay regardless, which is what you want after a reverted or
interrupted update where the node still runs the old image.

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

The dashboard shows that version in two places on **Device & admin**: the
**Status** card's *In-hive sensors* list (every paired wireless node, with the
version for the ones that advertise one — only HiveInside does), and
**Firmware → HiveInside nodes**: one row per hive whose node was heard, with the
advertised version and board (e.g. `v0.4.0 · nrf54lm20a`). The section only appears once a HiveInside
node has actually reported, and it keeps the last known identity when a node
misses a scan window, so it reads as "what this node is running", not "what was
in the last packet".

### Diagnosing a failed relay

Watch the HiveHub's serial console at 115200 across an attempt. A healthy relay
prints, in order:

```
[CMD] Received command 20: update_hiveinside
[HI-OTA] Downloading HiveInside firmware: https://…/hiveinside-….signed.bin
[HEAP] relay-start: free=… largest=… min_ever=…
[HI-OTA] Download open: 1093632 bytes; bringing up BLE
[HEAP] relay-download-open: free=… largest=… min_ever=…
[HI-OTA] connecting to … 
[HI-OTA] connected: MTU=247 chunk=244 image=1093632 bytes crc=0x…
[HI-OTA] relayed 32768/1093632 bytes            ← every 32 KB
[HI-OTA] device reports DONE — it will reboot into the new image
[HI-OTA] result: OK
```

Where it stops, and what it says, maps to a cause:

* **`MTU=23 chunk=20`** — MTU negotiation did not take. The transfer still works
  but runs roughly ten times longer, which makes every other timeout tighter.
* **`DATA write failed …`** — the relay now asks the node *why* instead of
  guessing. `STATUS unreadable — link is down` is a genuine radio/link loss;
  `device rejected the stream: state=0x… (…)` means the node refused the bytes
  (CRC, size, flash write, wrong state) and the transfer never had a chance. The
  same distinction reaches the dashboard, so the row's reason is now the node's
  own verdict rather than "BLE link lost?" for all of them.
* **`[HEAP] *** CORRUPTION DETECTED at stage: … ***`** — the allocator's own
  structures are damaged. The named stage is the one that did it; everything
  after that point in the boot is unreliable, including any crash that follows.
* **A boot banner instead of a result line** — the hub reset mid-relay. Read
  `Reset reason:` on the line after the wake reason: `panic/exception` means a
  crash, with the Guru Meditation dump immediately above it.

A panic dump decodes in place when the console is `pio device monitor` (both
environments set `monitor_filters = esp32_exception_decoder`). For a dump
captured elsewhere, feed the addresses to `addr2line` against the exact image
that is flashed — `rename_firmware.py` names it `hivehub_<board>_<version>.elf`,
and `firmware/ci/package_build.sh` copies it into `firmware/dist/`:

```bash
riscv32-esp-elf-addr2line -pfiaC \
  -e firmware/dist/hivehub_esp32-c6_<version>.elf 0x… 0x… 0x…
```

The dump's trailing `ELF file SHA256:` must match that file, or the decoded
names are fiction.

See [api.md](api.md) for the `update-hiveinside` command and payload.
