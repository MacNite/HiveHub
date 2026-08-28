# Inspection mode

*Firmware **0.25.0** · server **0.4.0** · issue [#173](https://github.com/MacNite/HiveHub/issues/173)*

While a beekeeper has a hive open, its own sensors stop measuring the colony and
start measuring the inspection. A scale with two supers lifted off it reads tens
of kilos light. A brood nest with the crown board off drops several degrees in a
minute. Those readings are perfectly true and completely useless — and worse
than useless as alert input, because the weight-drop detectors read an
inspection as robbing or a swarm.

**Inspection mode** marks that window. The hub keeps measuring and keeps
uploading throughout; the backend records the window and keeps the readings
inside it out of the charts, the insights and the alert rules.

Nothing is deleted. Every reading the hub sent is stored exactly as it arrived
and still comes out of the CSV/NDJSON export. An inspection hides data from the
*interpreters*, it does not destroy it.

---

## What is and isn't paused

| Reporting normally | Held back |
| --- | --- |
| Ambient temperature / humidity / pressure | Hive weight (raw and compensated) |
| Battery voltage, state of charge, solar | In-hive temperature and humidity |
| WiFi RSSI, uplink, time source, SD/RTC health | In-hive acoustics and vibration |
| Boot count, firmware version, config version | Bee-counter totals and intervals |
| | Everything else keyed to a hive |

The hub's own sensors are deliberately untouched. They measure the box on the
post, not the colony, and they are how you tell *"the beekeeper had the hive
open"* from *"the hub died"*. A gap in the ambient trace really would be a
fault, so blanking it would be a lie.

## The button

On the **XIAO ESP32-C6** the buttons changed in firmware 0.25.0:

| Button | Was | Is now |
| --- | --- | --- |
| **D2** (external) | Setup: short press = AP mode, long press = factory reset | **Inspection**: one press toggles it |
| **GPIO9** (on-board USER/BOOT) | — | **Setup**: short press = AP mode, long press = factory reset |

The reasoning is in issue #173: the one external, weatherproofable button on a
hub sealed in an enclosure should be the thing a beekeeper actually presses at
the hive stand. AP mode and factory reset are installation and recovery actions
— you have the lid off anyway — and the portal can now also be opened remotely
(see below), so the on-board button is enough for them.

Wire any normally-open switch between D2 and GND; the pin uses `INPUT_PULLUP`.
A **momentary pushbutton** is the intended part. A keyed pushbutton is worth
considering for an apiary the public can reach.

* One press **toggles** inspection on or off.
* The press works while the hub is deep-asleep — D2 is a wake source, so the
  state changes on the press rather than at the next scheduled cycle.
* Holding the button does the same as tapping it: one press, one toggle.
* The state lives in RTC memory and survives the sleeps between cycles. It does
  **not** survive a power cut or a firmware update: a hub that cold-boots comes
  up measuring. That is the safe direction to fail — losing an "on" costs one
  visible spike, losing an "off" costs a hive that goes quiet for good.

> **GPIO9 is the ESP32-C6 boot strapping pin.** Holding it down across a
> hardware RESET or a power-up puts the chip into the serial bootloader — that
> is what the BOOT button is for. Deep-sleep wake does not re-latch strapping,
> so a press that wakes the hub runs the firmware normally. Just remember that
> "hold USER, then press RESET" is a *flash* command, not an AP-mode command.

**The 30-pin ESP32 DevKit is unchanged.** Its only spare button is BOOT on
GPIO0, a strapping pin whose failure mode is a beekeeper in a bee suit staring
at a hub sitting in the bootloader, so it keeps GPIO27 as the setup button and
has no inspection button. Inspection mode itself still works there through the
dashboard and the API.

## Auto-end

An inspection nobody switches off would blank a hive's data indefinitely, and a
blank hive looks exactly like a dead sensor. So both ends cap it:

* the **hub** ends its own inspection once it has run past the timeout, and
* the **server** closes any window past the timeout as `end_reason: "timeout"`,
  which covers the hub that lost power mid-inspection and never came back.

The default is **60 minutes**. Change it per device in the dashboard under
**Device & admin → Configuration → General → "Inspection timeout (min)"**
(1–1440). The hub picks the new value up with the rest of its config and
persists it, so a hub that boots without WiFi still ends its inspections.

Raise it for a genuinely long session (queen rearing, a comb swap) rather than
working around it.

## Starting one without walking to the hub

Three things open and close an inspection, and they are all the same toggle:

1. **The button**, above.
2. **The dashboard**: Device & admin → Configuration → **Inspection**. Start,
   end, and annotate the windows already recorded.
3. **The API** — `start_inspection` / `stop_inspection`, which is how HivePal's
   in-app buttons reach a hub. See [api.md](api.md#inspections).

Because the hub deep-sleeps between cycles, an API- or dashboard-started
inspection is **requested** until the hub next wakes and picks the command up.
The status endpoint reports both states separately (`pending` vs `active`), and
the dashboard badge says *"Requested — waiting for the device"* rather than
claiming something that has not happened yet. The hub confirms it by uploading a
reading with `inspection: true`.

An API inspection may name specific hives (`{"hives": [2, 3]}`); the physical
button always covers the whole hub, because a beekeeper pressing it is standing
at the stand rather than at one box.

## Reading it back

**On the charts.** Every per-hive chart shades its inspection windows in a soft
purple band with a rule at each edge, and the legend carries an "Inspection"
key. A ten-minute inspection on a month-wide chart is under a pixel wide, so
bands have a minimum width and both edges also get a tick on the time axis —
which is exactly the zoom level at which an unexplained step is most puzzling.

The hub's own power and network charts are never shaded: nothing on them is
affected by an inspection.

Bands are drawn only while a single device is charted. In a cross-device
comparison a band meaning *"a hive on the other hub was open"* drawn under this
hub's weight trace would invite exactly the wrong conclusion, so the shading is
dropped rather than made ambiguous.

**In the readings.** A reading inside a window comes back with its hive fields
`null` and `inspection: true`, so the dashboard can say *why* a value is missing
instead of showing an unexplained gap. The live status tile reads *"Inspection
in progress"*.

**In insights and alerts.** The insight engine reads the same masked rows, so
every weight-, temperature- and acoustics-based detector skips the window
automatically. No detector needed changing.

**In the export.** Not masked. `GET /api/v1/local/export/measurements` and the
SD backup return the raw numbers, as does each row's stored `raw_json`. If you
deliberately go looking for what the scale read while you were holding the
super, it is there.

## Notes

Every inspection carries an optional free-text note. `"removed 2 supers"` beside
a 40 kg step turns an alarming trace into a harvest record, and it is the half
of this feature that pays off months later. Set it when starting or ending an
inspection, or type it into the row in the dashboard's Inspection panel
afterwards — it saves as you leave the field.

## Data model

One row per inspection in `device_inspections` (migration
[027](../server/migrations/027_inspections.sql)), not a flag on each reading.
That is what lets a chart shade the window, the insight engine skip it and every
raw reading stay exactly as the hub sent it. A partial unique index allows **at
most one open inspection per device**, which is what makes every trigger a
toggle rather than a way to nest windows.

| Column | Meaning |
| --- | --- |
| `started_at` / `ended_at` | The window. `ended_at NULL` = still in progress. |
| `hive_indexes` | `NULL` = the whole hub (what the button means). |
| `source` | `device` (the button), `api` (HivePal), `dashboard`. |
| `end_reason` | `device` / `api` / `dashboard` / `timeout`. |
| `requested_at` / `acknowledged_at` | When an API request was queued, and when the hub actually picked it up. |
| `note` | Free text. |

## Related

- [ap-mode-sd-download.md](ap-mode-sd-download.md) — AP/setup mode and the setup button.
- [calibration-mode.md](calibration-mode.md) — the other device mode with a start/stop command pair.
- [api.md](api.md#inspections) — the REST endpoints.
- [multi-hive.md](multi-hive.md) — hive indexes and the per-hive data model.
