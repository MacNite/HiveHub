# HiveHub built-in dashboard

A small, dependency-free single-page dashboard that ships **inside** the HiveHub
server, so a self-hosted install gets a nice web UI without running HivePal.

It mirrors the data groups of beehivemonitoring.com / HivePal — a fixed sidebar
(Overview, Temperature, Weight, Environment, Audio, Frequency bands, Vibration,
Battery & power, Connectivity, Counter, Insights, Device & admin) with a device dropdown
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
| `ENABLE_PUBLIC_EMBEDS` | `false` to switch off **Publish data** (public chart embeds) server-side |

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
  update (queues the device to flash on its next check-in). When a **HiveInside**
  node is paired, the same panel lists the firmware version and board each node
  advertises, so a relayed update can be verified after it lands. Each node's row
  carries an inline `latest x.y.z` flag naming the newest uploaded HiveInside
  release — highlighted when it is newer than what that node runs, which is the
  same comparison the backend applies before it will queue a relay.
- **Status:** per-subsystem health (OK / Fault) plus an **In-hive sensors** list —
  one row per wireless node paired to a hive (HolyIot, RuuviTag, HiveInside,
  HiveHeart, HiveScale) with the firmware version it advertises. Only HiveInside
  broadcasts one; the others report none and show an em-dash.
- **Calibration:** start/stop calibration mode and fit a load-cell temperature
  coefficient.
- **Publish data (admin):** **Device & admin → Publish data** turns a chosen slice —
  one metric, the hives you tick, a rolling period — into a public chart with its
  own secret link, served without a login at `/embed/chart/{token}` for
  `<iframe>` embedding in a club website or blog (plus JSON and CSV of the same
  numbers). Only the labels you type are published: no device IDs, hive numbers
  or any other reading travel with it, and a publication can be taken offline or
  revoked at any time. The panel is only shown when the server has publishing
  enabled (`ENABLE_PUBLIC_EMBEDS`). See
  [../../docs/publish-embed.md](../../docs/publish-embed.md).
- **Backup & restore (admin):** **Admin → Download / backup data** saves the
  selected readings (any devices, hives and period — everything by default) as an
  `.ndjson` file in the same format the scale writes to its SD card, and **Import
  SD card data** reads it back. Re-importing is idempotent, and a whole-server
  backup is restored device-by-device in one upload — so the database can be
  re-deployed, or one beekeeper's history handed over when they move to their own
  server. See [../../docs/backup-restore.md](../../docs/backup-restore.md).

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

### Chart conventions

Two colour roles, and which one a series gets is decided by one question — *does
the order of these series mean anything?*

- **Colour = identity** (`PALETTE` in `charts.js`): which **hive**. Six hues in a
  fixed order, cycled by position. The order is a colour-blind safety property,
  not taste — adjacent slots are the ones a reader has to tell apart, so green
  and red must never end up next to each other. A hive keeps the same colour in
  the top-bar chip, the selection strip and every chart.
- **Colour = order** (`--band-1`…`--band-5` in `style.css`, via `bandColor()`):
  which **frequency band**. One hue in monotone lightness steps, low → high, so
  the colour itself carries the order. Light mode steps light → dark; dark mode
  re-steps dim → bright so the weak end still clears the card.

That split decides the layout of the two acoustic pages. Bands share a unit and
are read against each other at a moment, so all of a hive's bands go on **one**
chart; hives are compared across a whole range, so each hive gets **its own**
chart, tagged with its palette colour in the heading. Frequency bands and
Vibration are laid out identically for that reason — they are read one after the
other, and the vibration bands simply continue below 50 Hz where the microphones
stop. The spectrum/waterfall view (which encodes age as fadedness, and so cannot
show a trend) stays available, folded under the time charts.

A series colour may be a CSS custom property such as `var(--band-3)`: the legend
swatch follows the cascade and the canvas resolves it at draw time, so both
survive a theme switch without a re-render.

The public embed pages served by **Publish data** live next door in
`server/embed/` (their own shell, stylesheet and loader) and reuse
`assets/charts.js` verbatim, so a published chart is drawn by exactly the same
code as the dashboard chart it came from.

The files are served by FastAPI via a `StaticFiles` mount and are included in the
Docker image automatically (`Dockerfile` does `COPY . .`).

### Local preview without a database

Serve the folder statically and point the API calls at a mock — see
`test-data/mock-server/` for synthetic data, or run the server itself with
`ENABLE_LOCAL_DASHBOARD=true` against your real Postgres.
