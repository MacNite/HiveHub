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
