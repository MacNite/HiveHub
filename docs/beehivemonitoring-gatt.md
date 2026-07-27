# beehivemonitoring.com GATT sensors (HiveHeart / HiveScale)

HiveScale can read two commercial [beehivemonitoring.com](https://beehivemonitoring.com)
devices over Bluetooth:

| Device    | Category   | Measures |
|-----------|------------|----------|
| HiveHeart | in-hive    | temperature, humidity, battery, buzz frequency / energy / peak, 8-byte FFT |
| HiveScale | weight     | weight, raw weight, temperature, humidity, pressure, battery |

Unlike the HolyIot 25015 (a passive advertisement beacon), these devices are
read by **connecting** over GATT, subscribing to a single notify characteristic,
taking the one notification they push, and disconnecting:

```
connect(mac) → discover services → subscribe(513849EB-…-533D6E) → notify → close
```

Both products share the same service `0d01c3b8-eff2-44bc-9260-3256eb957268` and
characteristic `513849eb-913d-4f80-8c44-3f0685533d6e`; only the payload differs.

## Enabling

1. In the [secrets.h configurator](../website/configurator.html), add a wireless
   sensor and pick **HiveHeart** (in-hive) and/or **HiveScale** (scale). This
   writes `ENABLE_BEEHIVE_GATT 1` plus the shared UUIDs into `secrets.h`.
2. Optionally paste the device MAC in the configurator to pre-pair via
   `secrets.h` (`INHIVE_n_MAC` / `WSCALE_n_MAC`), or leave it blank.
3. Flash, then **pair the MAC**: either it was seeded from `secrets.h`, or open
   the provisioning portal, and under **Wireless sensors** click **➕ Add
   wireless sensor**, choose **HiveHeart** (in-hive) or **HiveScale** (scale),
   pick the target in the **Maps to** dropdown (Hive 1/2 or Scale 1/2), and paste
   the device MAC. The **Maps to** choice, not the row order, decides the slot.

> A MAC is always required — it is the connection address. The UUIDs identify
> *what* to read, not *which* device, so configuring UUIDs alone is not enough
> (the firmware logs `No HiveHeart/HiveScale paired; skipping`).

HiveHeart slot 1/2 map to hive 1/2 and supply `hive_N_temp_c` /
`hive_N_humidity_percent` when no wired probe / HolyIot sensor already does.

## One reader at a time

> **A HiveHeart / HiveScale accepts exactly ONE connection at a time.** Pick a
> single collector for each device: **HiveHub**, the **beehivemonitoring.com
> Hub** (their own gateway), or the **beehivemonitoring.com phone app** — never
> two at once.

This is a property of the devices, not a HiveHub limitation, and it follows from
how they are read. Unlike a HolyIot 25015 or a RuuviTag — passive beacons whose
advertisements any number of listeners can receive at the same time — these are
connection-based GATT peripherals. Reading one means *occupying* it:

```
connect(mac) → discover services → subscribe(513849EB-…-533D6E) → notify → close
```

While one central holds that link, the device **stops advertising entirely**, so
a second central cannot see it as connectable, let alone connect. Whoever gets
there first wins, and everyone else times out until the link is released.

So if HiveHub should read a HiveHeart:

- the **beehivemonitoring Hub** must not be powered on in range, or must not
  have that device in its own pairing list;
- the **phone app** must be fully closed — not just backgrounded. Both iOS and
  Android keep BLE links alive for a backgrounded app, so an app you "left" an
  hour ago can still be holding the device.

The reverse is equally true: while HiveHub is mid-read, the vendor app will fail
to connect. HiveHub only holds the link for the few seconds it takes to take one
notification (see `closeClient()` in `firmware/src/beehive_gatt.cpp`), so the
device is free again between wake cycles — but during a cycle it is busy.

## Troubleshooting `connect failed` / `status=13`

The common failure looks like this on the serial monitor (115200 baud):

```
[BHGATT] connecting to 2C:11:65:5C:FA:C7 (addr type 0) ...
E NimBLEClient: Connection failed; status=13
[BHGATT] connect failed (addr type 0)
[BHGATT] connecting to 2C:11:65:5C:FA:C7 (addr type 1) ...
E NimBLEClient: Connection failed; status=13
[BHGATT] connect failed (addr type 1)
```

`status` is a NimBLE host error code (`ble_hs.h` in the ESP-IDF tree):

| status | NimBLE symbol      | Meaning                                                          |
|--------|--------------------|------------------------------------------------------------------|
| 13     | `BLE_HS_ETIMEOUT`  | No connectable advertisement from that address within the window |
| 16     | `BLE_HS_EREJECT`   | The peer answered and actively refused the connection            |
| 30     | `BLE_HS_EDISABLED` | The BLE stack was de-initialised mid-operation                   |

**`status=13` is the informative one, mostly because of what it rules out.** It
means the firmware entered the initiating state, filtered for exactly that
address, and heard nothing connectable for the whole
`BEEHIVE_GATT_CONNECT_TIMEOUT_S` window (20 s) — tried once as a public address
(type 0) and once as a random address (type 1). Reaching this message at all
proves the configuration is already correct:

- `ENABLE_BEEHIVE_GATT` **is** compiled into the running firmware (otherwise
  there would be no `[BHGATT]` lines at all);
- the pairing survived the save, with the type `hiveheart`/`hivescale`;
- the MAC is well-formed (it was normalised and printed).

Nothing about UUIDs, payload decoding or slot mapping is involved yet. Only the
radio link failed. Work through, in order:

1. **Release the device.** Close the beehivemonitoring app on every phone in
   range and power off the beehivemonitoring Hub, then watch the next cycle.
   This is by far the most common cause — see [One reader at a
   time](#one-reader-at-a-time).
2. **Check range.** Run the portal's BLE scan and read the RSSI. A device that
   scans weaker than roughly −85 dBm often advertises fine but cannot hold a
   connection. Test with the hub a couple of metres away.
3. **Confirm the MAC is stable** across two scans a few minutes apart. It should
   be — these devices use a fixed address — but a MAC that changes between scans
   is a rotating private address, and pairing by MAC cannot work for that unit.
4. **Power-cycle the HiveHeart** (pull the battery for ~30 s). A device left
   bonded to another central can refuse new ones until it is reset.

> **Appearing in the BLE scan does not prove a device is connectable.** The
> portal lists everything that advertises, including devices advertising
> non-connectably. A device can be plainly visible in `/ble/scan` and still
> time out on every connect.

Later stages fail with their own distinct messages, which narrow things further:
`service not found` or `notify characteristic unavailable` means the MAC belongs
to some other device; `no notification within timeout` means the link came up
but nothing was pushed within `BEEHIVE_GATT_NOTIFY_TIMEOUT_S`.

### Cost of leaving an unreachable device paired

Each unreachable device burns `2 × BEEHIVE_GATT_CONNECT_TIMEOUT_S` = **40 s per
wake cycle** (one attempt per address type), and up to
`MAX_GATT_READS_PER_CYCLE` (4) devices are attempted per cycle — so a fully
unreachable set can add ~160 s of awake time to every cycle. On battery, unpair
a device you cannot reach rather than leaving it configured.

## Payload layout

Bytes 0–3 are a header/timestamp; sensor fields start at byte 4. Decoders live
in `firmware/include/beehive_decode.h` and are unit-tested against real captures
in `test-data/test_beehive_decode.cpp`:

```
g++ -std=c++17 -I firmware/include test-data/test_beehive_decode.cpp -o /tmp/t && /tmp/t
```

**HiveHeart** (validated: V=2.81 H=52.5% T=24.3°C f=66.9 Hz)

| Field | Bytes | Formula |
|---|---|---|
| battery_v | 4 | len>11: `(2000 + b4·1500/255)/1000`, else `(2500 + b4·1000/255)/1000` |
| humidity_pct | 5 | `b5·100/255` |
| temp_c | 6, 7lo | 12-bit signed `(b6 \| (b7&0x0F)<<8)` ÷10 |
| frequency_hz | 7hi, 8, 9lo2 | `(b7>>4 \| b8<<4 \| (b9&3)<<12)` ÷10 |
| energy | 9, 10 | `b9>>2 \| b10<<6` |
| peak | 11 | raw |
| fft | 12–19 | 8 raw bytes, packed-nibble spectrum — see [FFT encoding](#fft-encoding) |

**HiveScale** (validated: V=4.10 H=44.7% T=22.6°C P=1000.0 hPa W=1.04 kg)

| Field | Bytes | Formula |
|---|---|---|
| battery_v | 4 | `(2500 + b4·2000/255)/1000` |
| humidity_pct | 5 | `b5·100/255` |
| temp_c | 6, 7lo | 12-bit signed ÷10 |
| pressure_hpa | 7hi, 8 | `(10000 + signed12(b7>>4 \| b8<<4))` ÷10 |
| weight_kg | 9, 10 | `int16(b9 \| b10<<8)` ÷100 |
| raw_weight | 11–13 | 24-bit signed `b11 \| b12<<8 \| b13<<16` |

> **Pressure usually reads a flat 1000 hPa.** On most HiveScale units the
> barometer is **not activated by the manufacturer**, so `pressure_hpa` sits at
> its idle default of **1000.0 hPa** (as in the validated capture above) rather
> than reporting real ambient pressure. Treat a constant 1000 hPa as "no
> barometer", not as a plausible reading — and expect the HiveScale's
> `pressure` entity in Home Assistant / the MQTT bridge to be flat at 1000 hPa
> unless your unit has it enabled.

## Server

Migration `server/migrations/009_beehivemonitoring_gatt.sql` adds the
`hiveheart_N_*` and `hivescale_N_*` columns (also applied automatically by
`init_db()`); the raw FFT and timestamp stay in `raw_json`.

## FFT encoding

HiveHeart payload bytes **12–19** carry an 8-byte FFT. Each byte packs **two**
unsigned 4-bit values, **high nibble first**, so the 8 bytes decode to **16**
values:

```
bins.push((byte >> 4) & 0x0F)   # high nibble (first)
bins.push(byte & 0x0F)          # low nibble  (second)
```

Decoding example:

```
[103, 244, 83, 34, 17, 17, 0, 17]
        ->
[6, 7, 15, 4, 5, 3, 2, 2, 1, 1, 1, 1, 0, 0, 1, 1]
```

The **raw 8-byte array stays canonical** — it is what firmware forwards and what
the server stores in `raw_json` (never as 16 separate columns). The 16 decoded
values are derived on read (exposed as `hiveheart_N_fft_bins` and
`hives[].hiveheart.fft_bins`; the raw array remains available as
`hiveheart_N_fft` / `hives[].hiveheart.fft`).

The decoded values are **relative levels from 0 to 15**. They are **not** dB or
dBFS, and must not be compared with the INMP441 microphone dBFS bands.

### Frequency ranges

The 16 decoded values map to these ranges, in order:

| Bin | Range (Hz) | Bin | Range (Hz) |
|---|---|---|---|
| 1 | 0–93 | 9 | 751–844 |
| 2 | 94–187 | 10 | 854–937 |
| 3 | 188–281 | 11 | 938–1031 |
| 4 | 282–375 | 12 | 1032–1125 |
| 5 | 376–479 | 13 | 1126–1218 |
| 6 | 480–562 | 14 | 1219–1312 |
| 7 | 563–656 | 15 | 1313–1406 |
| 8 | 657–750 | 16 | 1407–1500 |

> **Known gap:** the supplied vendor table has a gap from **845 to 853 Hz**
> between bins 9 and 10 (and other non-uniform steps). These boundaries are
> preserved **verbatim** — not silently "corrected". The single source of truth
> is `FFT_RANGES_HZ` in `server/hiveheart_fft.py`; fix them there once confirmed
> against hardware and every consumer (backend, insights, dashboards) follows.

The prominent `frequency_hz` value (bytes 7hi/8/9lo2) is reported **independently**
by HiveHeart and may be computed differently from this compressed FFT histogram;
do not expect it to equal the FFT's dominant-bin midpoint.
