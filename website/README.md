# HiveScale website

A small, dependency-free static site for HiveScale:

- **`index.html`** — landing page describing the features and a step-by-step
  "set up your own HiveScale" guide.
- **`configurator.html`** — an in-browser tool that builds a firmware
  `secrets.h` from a form (device identity, Wi-Fi, sensors, BLE/GATT options,
  off-grid power modules) and lets you copy or download it. Everything runs
  client-side; no values are sent anywhere.
- **`assets/`** — shared stylesheet and the configurator JavaScript.

There is no build step — it is plain HTML/CSS/JS.

## Preview locally

Just open `website/index.html` in a browser, or serve the folder:

```bash
cd website
python3 -m http.server 8080
# then open http://localhost:8080/
```

## Publish on GitHub Pages

A workflow at `.github/workflows/pages.yml` deploys this folder automatically.
Enable it once:

1. Push these files to `main`.
2. In the repository, go to **Settings → Pages**.
3. Under **Build and deployment → Source**, choose **GitHub Actions**.

After that, every push to `main` that touches `website/` republishes the site at
`https://<owner>.github.io/<repo>/` (for this repo:
`https://macnite.github.io/HiveScale/`).

> Prefer the "Deploy from a branch" option instead of Actions? Point Pages at
> the `/website` folder isn't supported directly (only `/` or `/docs`), so the
> Actions workflow above is the simplest way to publish from `website/`.

## The secrets.h configurator

The configurator mirrors the firmware feature flags found in
`firmware/include/secrets.example.h` and `firmware/include/config.h`:

| Sensor / module | Macro(s) it writes |
|---|---|
| DS18B20 in-hive temperature | `ENABLE_DS18B20_HIVE_TEMP` |
| INMP441 microphones | `ENABLE_INMP441_MICS`, pins, sample rate/frames |
| LIS3DH / LIS2DH12 accelerometers | `ENABLE_LIS3DH_ACCEL`, addresses, range, ODR, sample count |
| Wireless sensors *(up to 6)* | `ENABLE_HOLYIOT_BLE`, scan seconds, active scan, company id, `HIVEINSIDE_USE_GATT`, `BLE_OVERRIDE_*`, per-slot `INHIVE_*` / `WSCALE_*` / `WBEECNT_*` type, protocol and GATT UUIDs |
| INA219 solar monitor | `ENABLE_INA219_SOLAR`, I2C address |
| MAX17048 LiPo fuel gauge | `ENABLE_MAX17048_BATTERY`, alert percent |

### Wireless sensors

Use **➕ Add wireless sensor** to attach up to six BLE sensors — at most **2
in-hive sensors, 2 scales and 2 bee counters**. Each row picks a type from a
dropdown:

| Type | Transport | Category | Firmware |
|---|---|---|---|
| HolyIot 25015 | BLE beacon | in-hive | ✅ supported |
| HiveInside ESP32-C6 | GATT | in-hive | ✅ supported |
| HiveHeart *(beehivemonitoring.com)* | GATT | in-hive | ✅ mapped (UUIDs pre-filled) |
| HiveScale *(beehivemonitoring.com)* | GATT | scale | ⚠ placeholder |
| BeeCounter *(beehivemonitoring.com)* | GATT | bee counter | ⚠ placeholder |
| RuuviTag 4-in-1 | BLE beacon | in-hive | ⚠ placeholder |

GATT devices have their service / characteristic UUIDs pre-filled (editable).
The three **collision-avoidance** overrides (`BLE_OVERRIDE_DS18B20` /
`_MICS` / `_ACCEL`) appear once an in-hive sensor is added, so a paired sensor
can replace the matching wired sensor per hive.

> **Placeholder types are not yet supported by the firmware.** The tool still
> lets you select them and writes their macros to `secrets.h` so a future build
> can pick them up; this is called out clearly in the UI. The wireless scale and
> bee-counter categories are likewise captured for a future build.
