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
| HolyIot 25015 BLE sensor | `ENABLE_HOLYIOT_BLE`, scan seconds, active scan, company id |
| GATT BLE sensor *(experimental)* | `ENABLE_GATT_BLE` (+ optional UUIDs) |
| INA219 solar monitor | `ENABLE_INA219_SOLAR`, I2C address |
| MAX17048 LiPo fuel gauge | `ENABLE_MAX17048_BATTERY`, alert percent |

> **GATT BLE is not yet supported by the firmware.** The tool still lets you
> select it and writes `ENABLE_GATT_BLE` so a future build can pick it up, but
> the current firmware ignores the flag. This is called out clearly in the UI.
