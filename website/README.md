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
  `secrets.h` from a form (device identity, Wi-Fi, up to 16 hives with their
  scale/sensor mapping, off-grid power modules) and lets you copy or download
  it. Everything runs client-side; no values are sent anywhere.
- **`build.html`** — the complete **"Build your own" guide**: the bill of
  materials with rough price estimates (rendered table + downloadable
  `assets/hivehub-bom.csv`), PCB ordering, assembly & wiring notes (power
  jumper, mux rule, I2C pull-up/brownout warning, collapsed hand-wiring pin
  reference), a condensed VS Code + PlatformIO firmware walkthrough,
  backend deployment and scale calibration. All steps are downloadable as
  `assets/hivehub-build-guide.pdf` (regenerate it after content changes —
  print `build.html` to PDF, e.g.
  `chromium --headless --print-to-pdf=assets/hivehub-build-guide.pdf build.html`).
- **`hardware-helper.html`** — a questionnaire that turns power / scale-count /
  BLE-device answers into a recommended build profile to carry into the build
  guide and config tool.
- **`device-support.html`** — the *"my device isn't supported yet"* guide:
  request support for a new wireless device as a GitHub issue, and capture the
  integration data (MAC, advertising payload or GATT service/characteristic) with
  nRF Connect. Mirrors [`docs/device-not-supported-yet.md`](../docs/device-not-supported-yet.md).
- **`dashboard-demo/`** — a backend-free, click-through demo of the built-in
  HiveHub dashboard (`server/dashboard/`) running on generated sample data. This
  is what the site's **"Dashboard demo"** links point at. Its shared CSS/JS are
  verbatim copies of `server/dashboard/assets/*`; only `index.html` and
  `assets/api.js` (the sample-data source) are demo-specific. See
  [`dashboard-demo/README.md`](dashboard-demo/README.md) for how to keep it in sync.
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
`firmware/include/secrets.example.h` and `firmware/include/config.h`, and its
**Hives & sensors** section mirrors the on-device provisioning portal's
per-hive model (see `firmware/src/portal.cpp` and
[`docs/multi-hive.md`](../docs/multi-hive.md)) rather than a fixed two-hive
layout:

| Sensor / module | Macro(s) it writes |
|---|---|
| Hives *(up to 16)* | `HIVE_COUNT` + one `HIVE_<n>_JSON` blob per hive (scale channel, DS18B20 ROM or BLE/GATT sensor) |
| INMP441 microphones | `ENABLE_INMP441_MICS`, pins, sample rate/frames |
| LIS3DH / LIS2DH12 accelerometers | `ENABLE_LIS3DH_ACCEL`, addresses, range, ODR, sample count |
| In-hive BLE bridge + GATT | `ENABLE_BLE_SCAN`, scan seconds, active scan, company id, `HIVEINSIDE_USE_GATT`, `ENABLE_BEEHIVE_GATT`, `ENABLE_WIRELESS_BEECOUNTER`, `BLE_OVERRIDE_*` — all derived from what the hives above use |
| MAX17048 LiPo fuel gauge | `ENABLE_MAX17048_BATTERY`, alert percent |

> The INMP441 mics default to **off** (toggle on if fitted). The wired
> LIS3DH/LIS2DH12 accelerometer is a legacy option genuinely capped at 2 hives
> by its I2C address space (SDO/SA0 only selects between `0x18`/`0x19`) —
> current builds get in-hive vibration from a BLE sensor instead (HiveInside
> FFT bands; HolyIot/RuuviTag low-rate); see
> [`docs/accelerometer.md`](../docs/accelerometer.md).

### Hives & sensors

Use **➕ Add hive** to add up to 16 hives (2 by default, matching the
historical starting point). Each hive card has:

- a **Scale** dropdown: no scale, an HX711 pin pair (classic board only), a
  NAU7802 channel (main bus or behind a TCA9548A mux), or a wireless
  beehivemonitoring.com HiveScale (MAC field). Already-used channels are
  hidden from other hives' dropdowns, same as the portal;
- an **in-hive sensor**: either one wired DS18B20 (ROM optional — blank falls
  back to probe enumeration order) or one wireless sensor, picked from the
  same catalog the portal offers:

| Type | Transport | Firmware |
|---|---|---|
| HolyIot 25015 | BLE beacon | ✅ supported, any hive |
| RuuviTag 4-in-1 | BLE beacon | ✅ supported, any hive |
| HiveInside ESP32-C6 | GATT | ✅ supported, any hive |
| HiveHeart *(beehivemonitoring.com)* | GATT | ✅ supported, any hive |
| HiveTraffic *(entrance bee counter)* | GATT | ✅ supported, any hive (the wired I2C BeeCounter is the one limited to hives 1-2) |

A hive can combine a wireless HiveScale (as its scale source) with a separate
in-hive sensor — the tool emits both into that hive's `bl` array, matching
`MAX_BLE_PER_HIVE`. The **collision-avoidance** overrides (`BLE_OVERRIDE_DS18B20`
/ `_MICS` / `_ACCEL`) and the shared BLE scan settings appear once any hive
uses a beacon/GATT sensor.

Each `HIVE_<n>_JSON` is the exact blob shape the portal itself saves to NVS
(`hiveToJson()`/`hiveFromJson()` in `firmware/src/hive_config.cpp`), so a
device can ship pre-configured for any number of hives before ever visiting
the portal. Pre-seeding is entirely optional — skip the section and configure
hives from the portal instead if you prefer.
