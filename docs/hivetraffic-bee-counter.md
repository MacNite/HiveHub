# HiveTraffic — wireless entrance bee counter (BLE/GATT)

HiveTraffic is the entrance bee counter (the `2026-easy-bee-counter` /
*HiveTraffic* board) read over Bluetooth LE. **BLE/GATT is the only supported
BeeCounter transport** — the old wired I2C BeeCounter path (slave addresses
fixed slave addresses, register polling, a latch/reset command, firmware
updates over the wire) has
been removed from the firmware, server and portal entirely. There is no wired
fallback: a hive without a paired (and reachable) HiveTraffic counter simply
reports no bee-counter data (`bee_counter.ok=false` when paired but
unreachable; nothing at all when unpaired).

Once per upload cycle HiveHub acts as a **GATT client**: it connects to each
paired HiveTraffic MAC, reads one JSON measurement characteristic and folds the
counts into the `bee_counter_{slot}_*` fields.

**Any hive can have a HiveTraffic counter.** The counter is resolved from the
dynamic hive registry (`bee_counter_client.cpp::bleRunCycleRegistry` walks
`hivecfg::gHives[]` for `beecounter` pairings), exactly like the HiveHeart /
HiveScale GATT client, so it works on any hive up to `MAX_HIVES`.
Connection-based reads share the `MAX_GATT_READS_PER_CYCLE` per-cycle budget.

## Firmware updates

Counters are updated **over GATT**, by the same relay that serves HiveInside:
HiveHub streams the image from the backend straight into the counter's OTA
characteristics without ever buffering it (`firmware/src/gatt_ota.cpp`, driven by
`relayFirmwareOverGatt` in `hivehub_network.cpp`). The counter verifies the
image's size and CRC-32 before it swaps app slots, so a corrupted relay aborts
and leaves the old firmware running.

(The old OTA-over-I2C path is gone for good, along with the rest of the wired
transport. The `update_beecounter` command name is reused, but it names a
completely different mechanism.)

To update a counter:

1. Build HiveTraffic (`pio run`). Its `rename_firmware.py` names the resulting
   **application** image `hivetraffic_esp32-c6_<ver>.bin` — not a merged factory
   image — so it can be uploaded as-is.
2. Upload it in the dashboard with target *HiveTraffic counter*, or `POST` it to
   the firmware upload endpoint with `target=beecounter`.

   The `target` is what routes the release, and it is always explicit — the
   backend never infers it from the filename. What the filename does control is
   the **board**: `board_from_filename` reads `esp32-c6` out of it and
   `resolve_release_board` rejects the upload if the declared board disagrees,
   which is what keeps a cross-architecture image away from a counter. The
   leading `hivetraffic` token is a convenience only: `targetFromFilename` in
   the dashboard matches `/hivetraffic|beecounter/` and pre-selects the target
   for you, so an image named with either prefix is targeted correctly and
   neither is silently mis-filed.
3. Press **Relay to counter** next to the hive, or call
   `POST /api/v1/devices/{id}/commands/update-beecounter?slot=N`.

The HiveHub picks the command up on its next upload cycle and streams the image;
the counter reboots and the following measurement read reports the new version.

Each row under **Firmware → HiveTraffic counters** is headed by the *hive* name.
The **?** beside it names the counter itself — its reported local name, if it
sends one, and the BLE address it is paired on — which is what tells two
counters apart when both show, say, a failed relay.

**The counter stops counting for the whole transfer.** It parks the IR emitters
and pauses gate polling while writing flash, so every bee crossing during those
minutes is lost. Relay at night or in poor flying weather.

That advice still holds with [night mode](#night-mode) enabled, and the two do
not collide: a suspended counter stays advertising and connectable, so the relay
runs exactly as it would in daylight. The counter refuses a *new* suspension
while a transfer is in progress, HiveHub does not suspend a counter while a
relay is unfinished, and the post-OTA reboot clears any suspension anyway — the
next cycle re-arms it if the window is still open. This is the main reason night
mode is a sensing suspension rather than deep sleep, which would have made the
recommended relay window the one time of day a counter is unreachable.

### Version gate

The relay is refused with `409` unless the release is strictly newer than the
version the counter reports, mirroring the HiveInside gate. That version is the
`"ver"` field of the measurement JSON, carried through as
`hives[].bee_counter.version` and read back out of `hive_readings.raw_json`.

Counters running firmware from before `"ver"` existed report no version. They
are never gated — there is nothing to compare against — but nothing confirms the
update took either, beyond the counter coming back and reporting a version at
all. Pass `force=true` to reflash the same version after an interrupted update.

### No authentication

The counter's OTA service is unauthenticated, exactly like HiveInside's:
anything in BLE radio range can push an image, and the only protections are
proximity and the ESP32's own image validation. Signed firmware is worth having
before treating this as secure against a nearby active attacker.

## Enabling

Build with `ENABLE_WIRELESS_BEECOUNTER=1` (the
[configurator](../website/configurator.html) emits this when you add a
*HiveTraffic* wireless sensor), then pair each counter's MAC:

* in the **provisioning portal** — press **➕ Add HiveTraffic counter** on any
  hive and enter/copy its MAC. The counter has its own lane, so it sits
  alongside that hive's in-hive beacon (HiveInside, HolyIot, RuuviTag), its
  wired DS18B20 and its HiveHeart rather than competing with them; or
* seed it in `secrets.h` via a `HIVE_i_JSON` blob's `bl` entry
  (`{"t":"beecounter","m":"AA:BB:CC:DD:EE:FF"}`) for any hive, or via the legacy
  `WBEECNT_1_MAC` / `WBEECNT_2_MAC` macros for hives 1–2.

Portal pairings live in the hive registry; the legacy `WBEECNT_n_MAC` seeds and
`counter_mac{0,1}` keys are migrated into the registry on first boot.

## GATT contract

All HiveTraffic devices share one service/characteristic (overridable via
`BEECOUNTER_GATT_SERVICE_UUID` / `BEECOUNTER_GATT_CHAR_UUID`):

| | UUID |
| --- | --- |
| Service | `8e8b0101-7a1c-4b9e-9a2f-1d6e0b9c1a01` |
| Measurement characteristic (READ) | `8e8b0102-7a1c-4b9e-9a2f-1d6e0b9c1a01` |
| Control characteristic (READ/WRITE) | `8e8b0103-7a1c-4b9e-9a2f-1d6e0b9c1a01` |

The characteristic returns a compact JSON document — **totals only**:

```json
{ "fw":5, "ver":"0.3.0", "uptime_s":1234, "status":15, "num_gates":24,
  "mcps_healthy":3, "total_in":100, "total_out":95, "glitches":2, "idle_s":0,
  "banks":7 }
```

`fw` is the wire-protocol revision; `ver` is the counter's own image version,
which is what the OTA version gate compares and what confirms an update took.
They move independently, and `ver` is absent on firmware that predates it.

An optional `"name"` string is read too and forwarded as
`hives[].bee_counter.device_name`: the local name the counter calls itself, so
a HiveHub running several of them can say which row is which. No counter
firmware sends it yet — the decoder accepts it whenever one starts to, and a
document without it parses exactly as before. Until then the dashboard falls
back to `hives[].bee_counter.mac`, the paired address, which HiveHub records
before it dials and therefore reports even for a counter that never answered.

HiveHub reads it, fills a totals-only `beecnt::Snapshot`, and disconnects.
The wire format is totals-only by design: no latch/reset command exists over
BLE, so a missed connection can never lose counts.

### Four wire revisions, all supported

`firmware/include/bee_counter_wire.h` reads `fw` **first** and branches on it.
Every revision parses, and they produce the same record:

| | `fw:2` | `fw:3` | `fw:4` | `fw:5` |
| --- | --- | --- | --- | --- |
| Expander health field | `gates_healthy` | `mcps_healthy` | `mcps_healthy` | `mcps_healthy` |
| `uptime_s` | 16-bit on the device, clamped at 65535 (18 h 12 min) | 32-bit | 32-bit | 32-bit |
| `glitches` | 16-bit, pinned at 65535 | 32-bit, saturating | 32-bit, saturating | 32-bit, saturating |
| `idle_s` + status bit `0x80` | — | — | night-mode countdown | night-mode countdown |
| `banks` | — | — | — | enabled emitter MOSFETs |

`fw:4` and `fw:5` are purely additive, so the parser read those documents
correctly before it knew `idle_s` or `banks` existed — it skips unknown keys.
What it could not do is tell a *suspended* or *narrowed* counter from a broken
one, which is the whole reason for reading them. A counter too old to report
`banks` is running all three, so the field defaults to `7` rather than `0`;
reading its absence as "everything is off" would misrepresent every counter the
OTA relay has not reached yet.

This is not politeness toward old firmware. **A counter keeps reporting `fw:2`
until the OTA relay updates it, and the relay reads this very characteristic
before it can update anything** — so a parser that understood only `fw:3` would
strand exactly the devices that need updating. Conversely, HiveHub must ship its
tolerant parser *before* any counter emitting `fw:3`, or every counter goes
unreadable in the window between the two deployments.

`mcps_healthy` counts MCP23017 **port expanders** answering on the counter's I2C
bus — 0..3, while `num_gates` is 24; each healthy expander covers eight gates.
It is not a count of working gates, which is exactly what the old name invited
(a perfectly healthy counter reported `gates_healthy: 3` next to
`num_gates: 24`). The value's meaning did not change in v3, only its name.

It is also **live** health, not a boot-time snapshot: a chip that dies
mid-deployment drops out of the count within a few polls and clears its status
bit, and one that recovers is counted again. Nothing may treat it as fixed after
boot.

HiveHub normalizes both revisions onto the `mcps_healthy` key when it forwards
the reading as `hives[].bee_counter`, so the server and stored history see one
name regardless of what the counter emitted. Readings taken before this change
carry `gates_healthy` in `hive_readings.raw_json` with the same meaning.

The decoder is covered by `test-data/test_bee_counter_wire.cpp`, which parses a
captured document of each revision and asserts they decode identically. It runs
in CI on a plain host compiler — no ESP32 and no counter required.

## Night mode

A HiveTraffic counter's 48 IR emitters are its power budget: 24 series pairs
behind 22 Ω ballast, all lit together for the settle+read window of every 5 ms
poll — roughly 0.5–1.0 A peak and, at the pulsed sampler's ~35 % duty, an
average an order of magnitude above the ~18 mA the ESP32-C6 and its three port
expanders draw between them. On an off-grid hive it is the whole supply.

European honey bees are diurnal. Flight requires light — they do not fly in
darkness at any temperature — and stops below roughly 10 °C regardless, so
overnight that draw buys nothing. Night mode parks the emitters.

**It is off by default.** Turn it on per device under **Device & admin →
Bee counter night mode**, or `PATCH` the config fields directly (see
[api.md](api.md#get-apiv1devicesdevice_idconfig)).

| Setting | Meaning |
| --- | --- |
| Enable night mode | Master switch |
| Night starts / ends | **Local** wall-clock times. The window may cross midnight; setting both the same disables it rather than covering the whole day |
| Timezone | POSIX TZ string. The device clock is UTC, so without one the window drifts an hour at each DST change |
| Postpone above | Crossings in the last cycle above which night mode waits for the next one. 0 goes by the clock alone |

### How it works

The counter has no RTC, no NVS and no network, and this design deliberately does
not give it any. HiveHub owns the schedule and tells the counter only a bounded
**duration** — "stop sensing for N seconds" — re-armed once per upload cycle for
as long as the window lasts:

1. `bee_counter_client.cpp` reads the measurement characteristic as usual.
2. `night_mode.h::decide()` weighs the local clock, the window, the traffic gate
   and whether a firmware relay is unfinished.
3. If it says suspend, a 5-byte `SET_IDLE` frame goes out **on the same
   connection** — no extra scan, no extra connect.
4. The counter reports `idle_s` and status bit `0x80` until the grant expires.

Every path that is not certain returns "keep counting": no valid local time, no
traffic baseline yet, a window whose ends are equal, a relay in flight. The
worst outcome of a bug here is a counter that counts, which is what it did
before the feature existed. The grant is deliberately short (two upload cycles,
capped at an hour on both sides), so a HiveHub that dies mid-night cannot leave
a counter blind — the deadline simply runs out.

The traffic gate is the "not yet" rule: if more bees crossed in the last cycle
than the threshold, night mode waits. It is evaluated from the totals of the
*previous* cycle, held in RTC memory across HiveHub's own deep sleep, which for
a threshold like "fewer than 100 crossings in the last ten minutes" is the
intended reading. A hive with no baseline yet — first cycle after a reboot — is
postponed rather than waved through: "I don't know how busy this is" is not "it
is quiet".

### Why not deep sleep

Deep sleep saves the residual ~18 mA on top of what parking the emitters already
saves, under 10 % of the total, and costs the measurement read (every night row
would carry `bee_counter.ok=false`), the firmware relay path that is
specifically recommended for night use, the ability to cancel a wrong schedule,
the counter's RAM-held lifetime totals, and a truthful `uptime_s`. The ESP32-C6's
deep-sleep timer also runs off a temperature-dependent internal RC oscillator:
over an 8–12 h sleep in a hive that swings 10–25 °C, expect minutes of drift and
up to ~30 minutes worst case. Re-arming a short grant against HiveHub's DS3231
(±2 ppm) means nothing accumulates. See HiveTraffic's `docs/ble-mode.md` for the
device-side reasoning.

### Reading the data

The counter's totals are frozen while suspended, so the differenced interval
across the window is a genuine zero rather than a gap — the same value a quiet
night produces anyway. What `idle_s` adds is the ability to tell that zero from
a counter whose emitter FETs have died, which otherwise produces an identical
row. It is stored on every reading as `hives[].bee_counter.idle_s`.

Counters running firmware older than `fw:4` have no control characteristic. The
write is skipped and they keep counting; the OTA relay will bring them up to a
firmware that can be suspended in the normal course of things.

## Emitter banks

Night mode decides *when* a counter stops. This decides *how much of it runs at
all*, and it applies around the clock.

The counter's 48 IR emitters sit behind three IRLB8721 MOSFETs, one per
MCP23017, so each third of the entrance is independently switchable:

| Bank | Gates | Expander |
| --- | --- | --- |
| 1 | 00–07 | U2 @ 0x20 |
| 2 | 10–17 | U3 @ 0x21 |
| 3 | 20–27 | U4 @ 0x22 |

Measured on the counter's 3.3 V rail:

| Banks enabled | Gates counted | Draw |
| --- | --- | --- |
| 1 | 8 | ~0.14 A |
| 2 | 16 | ~0.22 A |
| 3 (default) | 24 | ~0.30 A |

Roughly 80 mA per bank on top of a ~60 mA floor. Dropping one saves about as
much current as a quarter of a night of night mode, except it saves it all day,
which makes it the coarsest and most effective power control the counter has.
The two compose rather than compete: a counter can be running one bank *and* be
suspended.

Turn a bank off when the hive entrance is physically narrower than 24 gates,
when part of it is closed for the season, or when an off-grid supply will not
carry the whole board.

### Setup

Dashboard → **HiveTraffic setup** → *Emitter banks*: three checkboxes, all
ticked by default. The setting is per **device** (every counter paired to one
hub shares it) and applies to every paired counter, exactly like the night
window above it.

| Field | Notes |
| --- | --- |
| Bank 1 / 2 / 3 | One checkbox per MOSFET. All three enabled unless you say otherwise |

At least one must stay enabled. The dashboard refuses to save all three off and
the API rejects it with `400`, because the counter refuses a mask of zero
outright — it keeps whatever mask it had — so storing one would leave three
unticked boxes next to a counter cheerfully counting all 24 gates, with nothing
saying why. A counter that should count nothing is unpaired instead.

### How it works

1. `/api/v1/devices/{id}/config` delivers the three booleans; `fetchRemoteConfig`
   assembles them into a bitmask (`beeBankMask`) and persists it in NVS, so a
   hub that boots without WiFi still narrows its counters.
2. `bee_counter_client.cpp` reads the measurement characteristic as usual, and
   compares the `banks` value the counter just **reported** with the configured
   mask.
3. If they differ, a 2-byte `SET_BANKS` frame (`0x03` + mask) goes out on the
   same connection — no extra scan, no extra connect.
4. The counter applies it, darkens the MOSFETs of the disabled banks, and skips
   those gates entirely rather than reading them as "clear".

Comparing against what the counter *reported* — rather than against a
HiveHub-side memory of what it last sent — is the whole self-healing property.
The counter deliberately does not persist the mask, so one that browned out,
watchdogged or rebooted out of an OTA comes back running all 24 gates; the next
cycle's read shows the disagreement and fixes it. A HiveHub that remembered "I
already configured this one" would leave that counter wide open indefinitely.

Counters running firmware older than `fw:5` do not understand the opcode. The
write is gated on the reported revision and simply skipped, so they keep running
all three banks; the OTA relay will bring them up in the normal course of things.

### Reading the data

A switched-off bank's eight gates stop contributing to `total_in` / `total_out`
permanently, which is character for character what a dead emitter FET produces.
`banks` is the only thing that separates them, which is why it is stored on
every reading as `hives[].bee_counter.banks` — including the `7` of a counter
nobody has narrowed. A field that appeared only when it was interesting would
make "all banks on" and "counter too old to say" the same absence.

`num_gates` keeps reporting **24**: it describes what is wired, which has not
changed. Active gates are `popcount(banks) * 8`.

Expect the totals to drop roughly in proportion when a bank is switched off, and
do not compare a narrowed counter's numbers against its own earlier history —
the interval charts will show a step, and it is real.

## Intervals are differenced server-side

The wire format carries only the **monotonic lifetime totals**. The backend
derives each interval as `total_now − total_prev` between consecutive readings,
so a missed connection loses nothing and a counter reboot (totals going
backwards) is handled cleanly. See `2026-easy-bee-counter/docs/ble-mode.md` for
the device side and the rationale.

This differencing happens in two places, with identical semantics:

* The insight engine (`server/insights.py::_extract_counter_series`), which
  feeds the swarm/foraging detectors and the insight cards.
* The measurement read APIs (`server/measurements.py::difference_bee_counter_intervals`,
  applied by `serialize_measurements`), which backfill the `NULL`
  `interval_in`/`interval_out` columns from the totals before returning rows.
  Without this, display clients that chart the interval fields directly (HivePal's
  bee-counter panel) would read every BLE row as zero traffic. Only `NULL`
  intervals are filled, so historical rows from the removed wired path — which
  reported a real device interval — are left untouched.

Because the BLE path reports no per-interval or per-gate detail, those columns
arrive `NULL` for HiveTraffic readings; the derived interval is authoritative.

## Relationship to the other BLE subsystems

HiveHub already connects out over GATT for HiveInside and the
beehivemonitoring.com sensors (see `beehivemonitoring-gatt.md`). HiveTraffic
reuses the same connect-by-MAC pattern (`bee_counter_client.cpp::bleRunCycleRegistry`,
modelled on `beehive_gatt.cpp`), bringing the NimBLE stack up once per cycle for
the paired counters and tearing it down afterwards.
