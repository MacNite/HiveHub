# HiveHub built-in dashboard

A small, dependency-free single-page dashboard that ships **inside** the HiveHub
server, so a self-hosted install gets a nice web UI without running HivePal.

It mirrors the data groups of beehivemonitoring.com / HivePal — a fixed sidebar
(Overview, Temperature, Weight, Environment, Audio, Frequency bar, Battery &
power, Connectivity, Counter, Insights, Device & admin) with a device dropdown
and a hive selector in the top bar — but has **no user accounts and no login**.

> ⚠️ The dashboard talks to an **auth-free** API (`/api/v1/local/*`) that serves
> **every device on this server**. It is meant for a single-owner, self-hosted
> install reached over a trusted LAN or behind your own reverse proxy — not for a
> multi-tenant or internet-facing deployment. HivePal keeps using the
> authenticated `/api/v1/app/*` API.

## Enable it

Set the flag (see `server/.env.example` / `docker/env.example`):

```bash
ENABLE_LOCAL_DASHBOARD=true
```

Then open it at:

```
http://<your-host>:31115/dashboard
```

When the flag is **off** (the default) the `/api/v1/local/*` routes return 404
and `/dashboard` is not mounted, so a normal deployment is unaffected.

## What it can do

- **Monitoring:** latest-value cards and time-series charts (day / 7 days / month
  / year / 5 years) for weight, temperatures, humidity & pressure, audio levels,
  FFT frequency bands, battery & solar, connectivity and bee-counter traffic,
  plus the rule-based insight summary.
- **Configuration:** edit device config (send interval, scale offsets/factors,
  temperature-compensation settings) and rename the two hives (scale channels) —
  the labels used across every chart and card. Saving bumps the config version
  so the device applies it on its next check-in.
- **Firmware:** upload a `.bin`, see current-vs-latest status and approve an OTA
  update (queues the device to flash on its next check-in).
- **Calibration:** start/stop calibration mode and fit a load-cell temperature
  coefficient.

## How it's built

Plain HTML/CSS/ES-modules — **no build step**, matching `website/`:

| File | Purpose |
|---|---|
| `index.html` | App shell: top bar, sidebar, content area |
| `assets/style.css` | Honey-themed styling (shares tokens with `website/`) |
| `assets/api.js` | `fetch` wrappers for `/api/v1/local/*` |
| `assets/format.js` | Number/date formatting + a tiny DOM helper |
| `assets/charts.js` | Canvas line chart (multi-series, auto-scale, time axis) |
| `assets/views.js` | One renderer per sidebar data group |
| `assets/app.js` | Controller: selectors, sidebar, loading, polling |

The files are served by FastAPI via a `StaticFiles` mount and are included in the
Docker image automatically (`Dockerfile` does `COPY . .`).

### Local preview without a database

Serve the folder statically and point the API calls at a mock — see
`test-data/mock-server/` for synthetic data, or run the server itself with
`ENABLE_LOCAL_DASHBOARD=true` against your real Postgres.
