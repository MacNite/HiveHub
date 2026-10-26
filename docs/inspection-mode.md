# Inspection mode

*Firmware **0.25.5** · server **0.4.0** · issue [#173](https://github.com/MacNite/HiveHub/issues/173)*

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
| **GPIO9** (on-board USER/BOOT) | — | **Setup**: press during an awake cycle = AP mode, long press = factory reset |

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
  state changes on the press rather than at the next scheduled cycle. (Between
  0.25.0 and 0.25.3 it did **not**: the setup button's GPIO9 was armed in the
  same call and made the chip reject the whole wake mask, D2 included. 0.25.4
  arms only the pins the C6 can actually wake on.)
* Firmware 0.25.1 also checks the live D2 level at boot and safely classifies an
  otherwise unidentifiable C6 GPIO wake as an inspection press. This covers the
  0.25.0 failure where both the latched wake mask was empty and a quick tap had
  ended before the firmware could read the pin.
* Holding the button does the same as tapping it: one press, one toggle.
* The state lives in RTC memory and survives the sleeps between cycles. It does
  **not** survive a power cut or a firmware update: a hub that cold-boots comes
  up measuring. That is the safe direction to fail — losing an "on" costs one
  visible spike, losing an "off" costs a hive that goes quiet for good.

> **GPIO9 cannot wake the hub, and holding it at power-up flashes it.** Two
> separate ESP32-C6 facts, both of which limit what the on-board USER button can
> do — and firmware up to 0.25.3 respected neither:
>
> * Deep-sleep GPIO wakeup on the C6 is an **LP-IO** feature: only GPIO0–GPIO7
>   have the pad that stays alive through deep sleep. GPIO9 does not, so it can
>   never be a wake source. Worse, `esp_deep_sleep_enable_gpio_wakeup()` rejects
>   the *whole* mask when it meets a pin outside that range — and because the
>   firmware armed both buttons in one call and ignored the return value, GPIO9
>   silently disarmed **D2** along with itself. That is why neither button
>   appeared to do anything from deep sleep. Fixed in 0.25.4: the mask is
>   filtered to the pins the silicon can actually wake on, so D2 works again.
> * GPIO9 is also the **boot strapping pin**. Holding it down across a hardware
>   RESET or a power-up puts the chip into the serial bootloader — that is what
>   the BOOT button is for. "Hold USER, then press RESET" is a *flash* command,
>   not an AP-mode command, and no firmware change can make it one.
>
> So the USER button is only ever readable while the hub is **awake**, and from
> 0.25.4 the firmware makes the most of that window: it reads the button at the
> top of `setup()` and again at four points inside every upload cycle.
>
> In practice: **press and hold it**, and wait for the next scheduled wake — up
> to one send interval, so up to 10 minutes on the default cadence. The status
> LED blinks as the hub boots, and the AP is up about a second after that (a
> button held across the wake is caught before the measurement cycle starts, so
> that cycle is skipped). A press that starts mid-cycle is caught by one of the
> in-cycle polls instead and opens the AP when the cycle ends — but those polls
> are seconds apart, so a quick tap can fall between them. Hold, don't tap.
>
> It is a wait, not a guess — and if you can reach the board you can skip it
> entirely: press **RESET**, release it, *then* press and hold BOOT, and the AP
> is up in under a minute. Order matters, because holding BOOT across the RESET
> is the flashing gesture and lands you in the bootloader instead; see
> [ap-mode-sd-download.md](ap-mode-sd-download.md#dont-want-to-wait-for-the-next-wake-use-reset-first).
>
> If you would rather not wait at all, use **Start AP
> mode** in the dashboard — that is the intended route for a sealed hub, and it
> needs nobody at the enclosure. A physical button that opens the AP *instantly*
> would have to live on GPIO0–GPIO7 — which is what the optional D0 setup button
> below is.

**The 30-pin ESP32 DevKit is unchanged.** Its only spare button is BOOT on
GPIO0, a strapping pin whose failure mode is a beekeeper in a bee suit staring
at a hub sitting in the bootloader, so it keeps GPIO27 as the setup button and
has no inspection button. Inspection mode itself still works there through the
dashboard and the API.

### External setup button on D0 (optional)

If you want a physical AP button that works straight from deep sleep, wire a
momentary normally-open button between **D0 and GND** and add this to
`secrets.h`:

```cpp
#define ENABLE_EXTERNAL_SETUP_BUTTON 1
```

* **A press opens the provisioning AP.** GPIO0 has an LP-IO pad, so it is armed
  as a deep-sleep wake source alongside D2: a press wakes the hub and the AP is
  up a few seconds later, with no measurement cycle in between. A press while
  the hub is awake works too.
* **It is not a strapping pin on the C6** (those are GPIO4/5/8/9/15), so holding
  it across a power-up or a RESET boots normally — unlike BOOT/USER on GPIO9.
  (GPIO0 *is* a strapping pin on the classic 30-pin ESP32; that does not apply
  here.)
* **No factory reset.** Holding D0 for any length of time only opens the AP. A
  stuck or wet button on the outside of a box must not be able to wipe the hub,
  so the ten-second factory reset stays on the on-board USER button.
* **The AP has no password.** Mount the button where a passer-by cannot reach
  it — inside the enclosure, or keyed.
* On a long lead, add an external ~10 kΩ pull-up from D0 to 3.3 V (the internal
  pull-up is weak) so noise cannot wake the hub into AP mode.
* If the C6 ever reports a GPIO wake with an empty status register and every
  button is already released, the firmware still assumes the inspection button
  (the safe, reversible choice). In that rare case, press D0 again.
* Off by default, and ignored on the 30-pin board. D0 stays free when it is not
  enabled.

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
