# HiveHub built-in dashboard

A small, dependency-free single-page dashboard that ships **inside** the HiveHub
server, so a self-hosted install gets a nice web UI without running HivePal.

It mirrors the data groups of beehivemonitoring.com / HivePal — a fixed sidebar
(Overview, Temperature, Weight, Environment, Audio, Frequency bands, Battery &
power, Connectivity, Counter, Insights, Device & admin) with a device dropdown
and a hive selector in the top bar. The hive selector is built dynamically from
the hives a device actually reports (up to **18** per device), labelled with
their configured names.

## Login & accounts

The dashboard API (`/api/v1/local/*`) serves **every device on this server**, so
it is protected by **username + password login**:

- **First run** (no accounts yet) shows a **setup wizard** that creates the
  initial **admin** account.
- After that, every visit requires signing in. Sessions are kept in an HttpOnly
  cookie (default 7 days, see `DASHBOARD_SESSION_TTL_HOURS`).
- **Roles:** `admin` can see everything *and* change configuration, calibration,
  firmware and manage users; `viewer` is read-only. Admins add/remove accounts
  from **Device & admin → Dashboard users**; anyone can change their own password
  from **Device & admin → Your account**.
- **Alert email:** each account can store a contact **email** (optional, set from
  **Device & admin → Your account** or when an admin creates a user). It is the
  destination for insights-based alerts once alert notifications are enabled.

This makes it safe to expose to the internet, but serving it over **HTTPS** (set
`DASHBOARD_COOKIE_SECURE=true`) and/or behind a reverse proxy is still
recommended. Leave the dashboard **off** on multi-tenant deployments — HivePal
keeps using the authenticated `/api/v1/app/*` API.

## Enable it

Set the flag (see `server/.env.example` / `docker/.env.example`):

```bash
ENABLE_LOCAL_DASHBOARD=true
```

Then open it at:

```
http://<your-host>:31115/dashboard
```

When the flag is **off** (the default) the `/api/v1/local/*` routes return 404
and `/dashboard` is not mounted, so a normal deployment is unaffected.

Related settings (all optional, see `server/.env.example`):

| Variable | Purpose |
|---|---|
| `DASHBOARD_SESSION_SECRET` | Pin the session-signing secret (auto-generated + persisted if blank) |
| `DASHBOARD_SESSION_TTL_HOURS` | Login lifetime in hours (default `168`) |
| `DASHBOARD_COOKIE_SECURE` | `true` to send the session cookie over HTTPS only |

## What it can do

- **Monitoring:** latest-value cards and time-series charts (day / 7 days / month
  / year / 5 years) for weight, temperatures, humidity & pressure, audio levels,
  FFT frequency bands, battery & solar (including the battery of each wireless
  in-hive sensor), connectivity and bee-counter traffic, plus the rule-based
  insight summary.
- **Per-chart tools:** every diagram's legend entries are clickable to show/hide
  that series (on spectrum charts: the *Older* / *Latest* lines); the y-axis
  min/max labels are click-to-edit for a custom range, with a **Reset y-axis**
  button appearing while a manual range is pinned; and a **⤓ CSV** button
  downloads exactly the data the chart currently shows.
- **Insights history:** the Insights view lists the persisted lifecycle of every
  alert — active *and* resolved — with an all/active/resolved filter, so you can
  see warnings that have since cleared, not just the current state.
- **Configuration:** edit device config (send interval, scale offsets/factors,
  temperature-compensation settings) and rename each hive the device reports (up
  to 18) — the labels used across every chart and card. Saving bumps the config
  version so the device applies it on its next check-in.
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
| `assets/auth.js` | `fetch` wrappers for the login API (`/api/v1/local/auth/*`) |
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
