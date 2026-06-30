# HiveHub website

A small, dependency-free static site for HiveHub:

- **`index.html`** — landing page describing the features and a step-by-step
  "set up your own HiveHub" guide.
- **`insights.html`** — a page explaining the rule-based Insights module: the
  four severities, the twelve-detector catalogue (grouped by swarming, queen /
  brood, activity and overwintering), how optional sensors are fused in, and the
  research the thresholds are based on. Mirrors `docs/insights.md` and
  `docs/insights-sources-tldr.md`.
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
`https://macnite.github.io/HiveHub/`).

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
| Wireless sensors *(up to 6)* | `ENABLE_BLE_SCAN`, scan seconds, active scan, company id, `HIVEINSIDE_USE_GATT`, `BLE_OVERRIDE_*`, per-slot `INHIVE_*` / `WSCALE_*` / `WBEECNT_*` type, protocol and GATT UUIDs |
| INA219 solar monitor | `ENABLE_INA219_SOLAR`, I2C address |
| MAX17048 LiPo fuel gauge | `ENABLE_MAX17048_BATTERY`, alert percent |

> DS18B20 and the INMP441 mics default to **off** in the tool (toggle them on if
> fitted). The wired LIS3DH/LIS2DH12 accelerometer is a legacy option — current
> builds get in-hive vibration from a BLE sensor (HiveInside FFT bands;
> HolyIot/RuuviTag low-rate); see
> [`docs/accelerometer.md`](../docs/accelerometer.md).

### Wireless sensors

Use **➕ Add wireless sensor** to attach up to six BLE sensors — at most **2
in-hive sensors, 2 scales and 2 bee counters**. Each row picks a type from a
dropdown:

| Type | Transport | Category | Firmware |
|---|---|---|---|
| HolyIot 25015 | BLE beacon | in-hive | ✅ supported |
| HiveInside ESP32-C6 | GATT | in-hive | ✅ supported |
| HiveHeart *(beehivemonitoring.com)* | GATT | in-hive | ✅ supported (GATT decode) |
| HiveScale *(beehivemonitoring.com)* | GATT | scale | ✅ supported (GATT decode) |
| HiveTraffic *(entrance bee counter)* | GATT | bee counter | ✅ supported |
| RuuviTag 4-in-1 | BLE beacon | in-hive | ✅ supported |

GATT devices have their service / characteristic UUIDs pre-filled (editable).
For HiveHeart / HiveScale you can also paste the device MAC to pre-pair via
`secrets.h`, or leave it blank and pair it later in the provisioning portal.
The three **collision-avoidance** overrides (`BLE_OVERRIDE_DS18B20` /
`_MICS` / `_ACCEL`) appear once an in-hive sensor is added, so a paired sensor
can replace the matching wired sensor per hive.

HiveHeart and HiveScale are decoded by the firmware (see
[`docs/beehivemonitoring-gatt.md`](../docs/beehivemonitoring-gatt.md)).

> Every sensor type in the catalog above is read by the current firmware. The
> separate `ENABLE_WIRELESS_SCALE` flag (written when you add a *scale*-category
> sensor) is still captured for a future build — but the beehivemonitoring.com
> HiveScale weight itself is already decoded over GATT via `ENABLE_BEEHIVE_GATT`.
