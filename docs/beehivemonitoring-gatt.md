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
   the provisioning portal and fill the *HiveHeart / HiveScale* MAC fields.

> A MAC is always required — it is the connection address. The UUIDs identify
> *what* to read, not *which* device, so configuring UUIDs alone is not enough
> (the firmware logs `No HiveHeart/HiveScale paired; skipping`).

HiveHeart slot 1/2 map to hive 1/2 and supply `hive_N_temp_c` /
`hive_N_humidity_percent` when no wired probe / HolyIot sensor already does.

## Payload layout

Bytes 0–3 are a header/timestamp; sensor fields start at byte 4. Decoders live
in `firmware/include/beehive_decode.h` and are unit-tested against real captures
in `tests/test_beehive_decode.cpp`:

```
g++ -std=c++17 -I firmware/include tests/test_beehive_decode.cpp -o /tmp/t && /tmp/t
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
| fft | 12–19 | 8 raw bytes (raw_json only) |

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
