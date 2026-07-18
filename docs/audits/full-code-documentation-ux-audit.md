# HiveHub Full Code, Documentation, and UX Audit

Audit date: 2026-07-18 · Branch: `claude/hivehub-full-audit-31rfhh` · Baseline commit: `11d707f`

## Executive summary

HiveHub is in **unusually good shape for a self-hosted hobby/IoT project**. The
backend applies real security engineering (constant-time key comparison,
trust-on-first-use per-device key binding done verify-before-write, PBKDF2
password storage, path-traversal guards, streamed size-capped uploads, body-size
middleware, per-IP rate limiting, parametrized SQL everywhere), the code is
heavily and accurately commented, and the test suites that exist are meaningful
behavioral tests rather than mocks-asserting-mocks.

The audit still found real defects — all now fixed on this branch:

1. **A shipped firmware decode bug**: the HiveHeart BLE battery voltage was
   decoded ~0.26 V too high (wrong scale factor), confirmed by the project's own
   checked-in test vector and documentation. The test that would have caught it
   existed but was never wired into CI.
2. **A deployment-breaking image-name mismatch**: CI publishes
   `ghcr.io/macnite/hivehub`, but `docker/docker-compose.yml` and every install
   guide pulled `ghcr.io/macnite/hivescale-api:latest` — deployments following
   the shipped compose file silently stop receiving backend updates.
3. **OTA firmware binaries were not persisted** in the shipped compose file (no
   volume on `FIRMWARE_DIR`), so uploaded releases 404 after any container
   recreation while their DB rows survive.
4. **Documented env vars that silently did nothing** (`PUBLIC_BASE_URL`,
   `RATE_LIMIT_*`, `MAX_*`, dashboard settings) because the compose file never
   passed them into the container, plus insecure fallback credentials
   (`Super-Secret-Key` / `Strong-Password`) when no `.env` was present.
5. **Materially stale security documentation**: the README and one of two
   *duplicated, diverged* env-example files still described the local dashboard
   as "login-free"/"AUTH-FREE", although it has been login-gated since
   migration 015. A reader could reasonably decide not to enable a feature that
   is in fact safe, or worse, assume other docs are equally outdated.

Remaining high-priority work (deferred, needs owner decisions): migrating
`measurements.raw_json` retention policy for long-running installs, a
`SECURITY.md` contact, and frontend keyboard operability of the custom chart
interactions.

**Confidence:** High for the backend and deployment configuration (read in
full, tests executed). Medium for the ~5.7k-line dashboard/website JS and the
firmware `src/` beyond the decode/host-test surface (structural review +
targeted reads, not line-by-line). Browser-based UX walkthrough was not
possible in this environment (no running Postgres); the frontend review is
code-level.

## Scope and methodology

**Inspected:** all of `server/` (every module read in full except parts of
`insights.py`/`mqtt_publisher.py`, which were skimmed structurally),
`server/dashboard/` (index.html, app.js, api.js, auth.js, format.js in full;
views.js/charts.js targeted), `docker/`, `.github/workflows/`, `docs/` (all
files at least skimmed, key files in full), root `README.md`, `CLAUDE.md`,
`firmware/include/*.h` decode/test surface, `firmware/src/main.cpp`,
`test-data/` suites, `website/` structure.

**Commands run (baseline and final):**

```bash
DATABASE_URL=... PYTHONPATH=server python3 test-data/test_counter_rules.py
DATABASE_URL=... PYTHONPATH=server python3 test-data/test_accel_rules.py
DATABASE_URL=... PYTHONPATH=server python3 test-data/test_ble_sensor_rules.py
DATABASE_URL=... PYTHONPATH=server python3 test-data/test_sd_import.py
DATABASE_URL=... PYTHONPATH=server python3 -m pytest -q test-data/test_tempcomp.py test-data/test_hiveheart_fft.py
./firmware/host_test/run_tests.sh
g++ -std=gnu++17 -Wall -Wextra -Werror -I firmware/include test-data/test_ruuvi_decode.cpp
g++ -std=gnu++17 -Wall -Wextra -Werror -I firmware/include test-data/test_beehive_decode.cpp
node --check <every dashboard/website script>
docker compose config      # syntax + interpolation validation
```

**Not validated in this environment:** PlatformIO firmware builds (toolchain
download not feasible in the sandbox; covered by CI), a live server against
PostgreSQL, a browser-driven UX walkthrough, actual GHCR registry contents, and
MQTT/SMTP/Web Push delivery against real services.

**Assumptions:** the CI workflow (`backend-image.yml`) is authoritative for the
published image name; `docs/multi-hive.md` is authoritative that the advertised
hive maximum is 16 (the 18-slot registry is internal headroom).

## Architecture summary

- **Firmware** (`firmware/`, C++ / PlatformIO, targets `esp32dev` and
  `xiao_esp32c6`): wakes on a cycle, reads wired sensors (NAU7802/HX711 scales,
  DS18B20, SHT4x, INMP441, MAX17048) and bridges BLE/GATT sensors (HolyIot,
  RuuviTag, HiveInside, beehivemonitoring HiveHeart/HiveScale, HiveTraffic
  bee counter), assembles a JSON measurement, caches to SD on upload failure,
  and uploads over HTTPS (TLS-verified) to the backend. Polls config, commands
  and OTA between cycles. A provisioning AP portal handles field setup.
- **Backend** (`server/`, FastAPI + psycopg3/PostgreSQL, single container):
  `main.py` wires middleware (body cap → rate limit → gzip) and routers.
  Ingest (`measurements.py`) stores flat columns + `raw_json` + normalized
  `hive_readings` child rows; reads re-serialize with temperature compensation,
  per-hive flattening and bee-counter interval derivation. Three auth domains:
  per-device keys (TOFU-bound), HivePal service key + per-user HS256 JWTs with
  per-device role checks, and dashboard session cookies (PBKDF2 accounts).
  Background threads: insight reconciler (persists alert lifecycle),
  notification worker (SMTP + Web Push), optional MQTT bridge with Home
  Assistant discovery. Schema is bootstrapped idempotently by `init_db()`;
  `server/migrations/*.sql` mirror the same changes for manual application.
- **Frontend** (`server/dashboard/`, dependency-free ES modules): login-gated
  SPA at `/dashboard` — cross-device hive comparison picker, canvas charts,
  admin panels (config, OTA, calibration, users, SD import, deletion),
  installable PWA with Web Push. `website/` is a static marketing/docs site
  with a backend-free demo copy of the dashboard.
- **Deployment** (`docker/`): compose file with API + Postgres 16; image
  published by GitHub Actions to GHCR.

## Validation results

| Check | Baseline | Final result | Notes |
|---|---|---|---|
| `test_counter_rules.py` | ✅ pass | ✅ pass | |
| `test_accel_rules.py` | ✅ pass | ✅ pass | |
| `test_ble_sensor_rules.py` | ✅ pass | ✅ pass | |
| `test_sd_import.py` | ✅ pass | ✅ pass | |
| `pytest test_tempcomp.py test_hiveheart_fft.py` | ✅ 2 pass | ✅ 2 pass | env quirk: system `cryptography` clashed with pyo3; pip install fixed (not a repo defect) |
| Firmware host I2C tests (`run_tests.sh`) | ✅ 93 checks | ✅ 93 checks | |
| `test_ruuvi_decode.cpp` | ✅ pass | ✅ pass | not previously in CI — now added |
| `test_beehive_decode.cpp` | ❌ **1 failure** | ✅ pass | CODE-001 fixed; now in CI |
| `node --check` dashboard + website JS | ✅ pass | ✅ pass | |
| `docker compose config` | ✅ parses | ✅ parses | final version validates required-var enforcement + `DATABASE_URL` interpolation |
| PlatformIO builds (`esp32dev`, `xiao_esp32c6`) | not run (sandbox) | not run | covered by CI |

## Findings

### [CODE-001] HiveHeart battery voltage decoded ~0.26 V too high
- Severity: High · Confidence: High · Category: Correctness
- Affected files: `firmware/include/beehive_decode.h:74-78` (decodeHeart), `:121` (decodeScale)
- Evidence: `test-data/test_beehive_decode.cpp` (real captured payload, expected 2.806 V) failed at baseline with 3.070 V; `docs/beehivemonitoring-gatt.md:56/68` documents `(2000 + b4·1500/255)/1000` where the code computed `2.0 + b4/128`. The `if (len > 11)` guard was also dead code (the length check above already enforces `len >= 12`).
- Impact: every stored `hiveheart_N_battery_v` reading is ~0.2–0.3 V high — enough to mask a genuinely low battery on an in-hive sensor. HiveScale battery drifted ~6 mV from the documented formula (cosmetic).
- Root cause: transcription error when porting the reverse-engineered decoder; the covering test was never executed by CI.
- Action taken: **fixed** both formulas to the documented/validated scale; test now passes.
- Validation: `test_beehive_decode.cpp` all pass; ruuvi test unaffected.
- Remaining risk: historical stored readings remain wrong (they carry the raw decode); a backfill is not possible since only the decoded value is stored.

### [TEST-001] Checked-in decoder tests never ran in CI
- Severity: High · Confidence: High · Category: Testing
- Affected files: `.github/workflows/ci.yml`
- Evidence: `test_beehive_decode.cpp` / `test_ruuvi_decode.cpp` exist with build instructions in their headers, but no CI job compiled them — which is exactly how CODE-001 shipped.
- Action taken: **fixed** — added a "Build and run BLE decoder tests" step to the `firmware-host-tests` job (`-Wall -Wextra -Werror`).
- Remaining risk: none for these; see TEST-002 for uncovered areas.

### [COMPAT-001] Compose/docs pull a GHCR image CI no longer publishes
- Severity: High · Confidence: High · Category: Compatibility
- Affected files: `docker/docker-compose.yml:3`, `docs/docker-install.md`, `docs/truenas-install.md`, `README.md`
- Evidence: `.github/workflows/backend-image.yml:16` sets `IMAGE_NAME: ghcr.io/macnite/hivehub` (and has since its introduction), while all deployment artifacts referenced `ghcr.io/macnite/hivescale-api:latest`.
- Impact: a deployment following the shipped compose file either fails to pull or — worse — pins to a stale image that never receives fixes, with no error.
- Action taken: **fixed** — all references updated to `ghcr.io/macnite/hivehub:latest` (compose service names kept as-is; they are local identifiers).
- Remaining risk: could not verify GHCR contents from the sandbox; if `hivescale-api` was also being pushed by some out-of-repo process, the old name would still work — but the in-repo workflow is authoritative.

### [CODE-002] Uploaded OTA binaries lost on container recreation
- Severity: High · Confidence: High · Category: Correctness (deployment)
- Affected files: `docker/docker-compose.yml`
- Evidence: `FIRMWARE_DIR=/app/firmware` (config.py:44) had no volume in the shipped compose file, while `firmware_releases` rows live in the persisted database. `docs/docker-install.md` already showed a bind mount — the shipped compose file lagged behind its own documentation.
- Impact: after `docker compose pull && up -d`, every registered release 404s on download; devices with an approved update repeatedly fail OTA.
- Action taken: **fixed** — added a named `firmware-data` volume; troubleshooting entry added to README.
- Remaining risk: existing deployments must re-upload their releases once.

### [CONFIG-001] Documented env vars never reached the container; insecure defaults
- Severity: Medium (security-relevant) · Confidence: High · Category: Compatibility / Security
- Affected files: `docker/docker-compose.yml`, `docker/.env.example`, `docker/env.example`
- Evidence: `.env.example` documented `PUBLIC_BASE_URL`, `RATE_LIMIT_*`, `MAX_BODY_BYTES`, `MAX_FIRMWARE_BYTES`, but the compose `environment:` block never passed them through — setting them in `.env` silently did nothing (and without `PUBLIC_BASE_URL`, OTA URLs are relative, so device OTA cannot work). Meanwhile `API_KEY` defaulted to `Super-Secret-Key` and `POSTGRES_PASSWORD` to `Strong-Password`, and the `DATABASE_URL` default hard-coded `Strong-Password` — so changing `POSTGRES_PASSWORD` alone broke the API's DB connection.
- Action taken: **fixed** — all documented vars now pass through; `API_KEY`/`POSTGRES_PASSWORD` are required (`:?` — compose fails fast with a helpful message); `DATABASE_URL` default now interpolates `${POSTGRES_PASSWORD}` so the pair cannot drift; dashboard session vars added. Verified with `docker compose config`.
- Remaining risk: `:?` is a startup-behavior change: an existing deployment that relied on the insecure defaults will stop until it sets a `.env` — that is intentional.

### [DOC-001] Two diverged copies of the docker env example; stale "AUTH-FREE" security description
- Severity: Medium · Confidence: High · Category: Documentation
- Affected files: `docker/.env.example`, `docker/env.example` (deleted), `README.md`, `docs/README.md`
- Evidence: `docker/env.example` and `docker/.env.example` were near-duplicates with different content; `env.example` (and README lines 73/87/307/416-418, docs/README.md:46) described `/api/v1/local/*` as "AUTH-FREE"/"login-free" — false since migration `015_dashboard_auth.sql` added the login gate.
- Impact: users may leave a safe feature disabled, expose confusion about the actual security model, or edit the wrong example file.
- Action taken: **fixed** — consolidated into a single `docker/.env.example` (with dashboard + MQTT + proxy sections), deleted `env.example`, updated all references, rewrote the README's dashboard/local-API descriptions and endpoint table (now includes auth, visibility, import, delete, and history endpoints).

### [DOC-002] README/API docs claimed removed `cellular_*` fields are accepted
- Severity: Low · Confidence: High · Category: Documentation
- Affected files: `README.md:447`, `docs/api.md:163-164, 251-253, 290-291`
- Evidence: `MeasurementIn` (server/schemas.py) has no `cellular_ok`/`cellular_csq` and uses `extra="ignore"`; `docs/test-commands.md` already stated they were dropped — the README and api.md contradicted it.
- Action taken: **fixed** — both now state the fields were removed and are ignored; the api.md migrations note was also updated from "001–011" to the actual 001–018 range and mentions `hive_readings` + dashboard tables.

### [SEC-001] Unauthenticated firmware download endpoint
- Severity: Low · Confidence: High · Category: Security
- Affected files: `server/firmware.py:470-475` (`GET /firmware/{filename}`)
- Evidence: no dependency guards the route; filenames are validated on upload (`_SAFE_FIRMWARE_FILENAME`, basename + parent check) and path params cannot contain `/`, so traversal is not possible — but any release binary is world-downloadable by (guessable) name.
- Impact: firmware images are disclosed to anyone; for an open-source project the images are not secret, so this is accepted-risk territory, but it should be a documented decision.
- Action taken: documented here; no code change (devices fetch OTA images with plain HTTP GET, so adding auth is a firmware-coordinated change).
- Recommendation: if privacy of owner-uploaded builds matters, add a per-device token to OTA URLs in a coordinated firmware+server release.

### [SEC-002] Unauthenticated device/measurement creation for unknown device IDs (TOFU by design)
- Severity: Informational · Confidence: High · Category: Security
- Affected files: `server/measurements.py:418-459`, `server/devices.py:26-80`
- Evidence: `POST /api/v1/measurements` accepts any ≥16-char `X-API-Key` for a *new* `device_id` and registers that key (trust-on-first-use). Spoofing an existing device requires its key (verified before any write, with rollback on mismatch — done correctly). Rate limiting bounds abuse.
- Impact: anyone who can reach the API can create junk device rows/measurements under fresh IDs.
- Recommendation: acceptable for the design goal (zero-touch provisioning); consider a periodic cleanup of never-claimed devices with no recent data.

### [SEC-003] Dashboard sessions are stateless JWTs — password change/user deletion does not revoke them
- Severity: Low · Confidence: High · Category: Security
- Affected files: `server/auth.py:116-138, 273-291`
- Evidence: session tokens are HS256 JWTs valid until `exp` (default 168 h). Deleting a user or changing a password does not invalidate outstanding tokens (no session table / token version). `require_dashboard_session` never re-checks the DB; note `local_auth_status` does fall back gracefully when the user row is gone, confirming a deleted user's token still authenticates.
- Impact: a removed viewer/admin retains access up to 7 days; low severity because account management is admin-only and self-hosted single-owner.
- Recommendation (deferred): embed a per-user token-version claim (bump on password change/delete) or store a session id in `dashboard_settings`.

### [CODE-003] `local_list_measurements` accepts naive datetimes
- Severity: Informational · Confidence: Medium · Category: Correctness
- Affected files: `server/local_dashboard.py:371-401`, `server/app_api.py:231-265`
- Evidence: `start_at`/`end_at` query params are parsed by FastAPI as naive datetimes when the client omits a timezone; compared against `timestamptz` they are interpreted in the DB session timezone (`TZ=Europe/Berlin` in compose), which can shift range edges by hours. The shipped dashboard always sends ISO strings with `Z`, so it is unaffected.
- Recommendation: normalize naive inputs to UTC in the handlers if third-party clients are expected.

### [PERF-001] Insight summary recompute cost is well-managed (positive finding)
- Severity: Informational · Confidence: High · Category: Performance
- Evidence: the reconciler + persisted-state fast path (`_summary_from_persisted`, durable freshness marker) and the SQL-side chart decimation (`execute_measurement_query`) are both well-designed answers to earlier load problems and appear correct (stride over an index-only CTE, newest row kept, hard `limit` cap).
- Remaining watch-item: `measurements` grows unboundedly (`raw_json` per row); no retention/partitioning story yet. Long-running installs will eventually feel it — see roadmap.

### [A11Y-001] Canvas charts had no accessible name
- Severity: Medium · Confidence: High · Category: Accessibility
- Affected files: `server/dashboard/assets/views.js` (chartCard, spectrumChartCard), `website/dashboard-demo/assets/views.js`
- Evidence: `el("canvas")` produced unlabeled `<canvas>` elements; screen-reader users get nothing.
- Action taken: **fixed** — charts now render with `role="img"` and `aria-label="<title> chart"` (both the live dashboard and the demo copy). Numeric equivalents remain available in the Overview metric cards.

### [A11Y-002] Chart interactions are pointer-only; no reduced-motion handling needed
- Severity: Low · Confidence: Medium · Category: Accessibility
- Evidence: cursor scrubbing, y-axis pinning and the legend toggles are mouse/touch driven; legend toggles are focusable (buttons) but the scrub readout is unreachable by keyboard. No animations exist, so `prefers-reduced-motion` is moot. Focus-visible styles and `aria-pressed`/`aria-expanded`/`role="dialog"`/`aria-live` usage elsewhere are notably good.
- Recommendation (deferred): arrow-key scrubbing on a focused chart; a visually-hidden data table would be the thorough fix.

### [UX-001] Hive-picker popover: solid, with two rough edges
- Severity: Low · Confidence: Medium · Category: UX
- Affected screens: top bar hive picker (`app.js` renderPicker/openPicker)
- Evidence (code-level): Escape/outside-click close and `aria-expanded` are handled; focus moves into the search box on open. But focus is not trapped or restored on outside-click close, and the "device header ticks all hives" button nests a checkbox inside a `<button>` (works with click handlers, but is semantically odd for AT users).
- Recommendation (deferred): restore focus to the trigger on any close; make the header row a labeled checkbox instead of a button-wrapping-checkbox.

### [UX-002] Destructive actions are well-confirmed (positive finding)
- Evidence: measurement deletion requires the device claim code server-side plus a typed `window.confirm`; user deletion, calibration stop, and SD force-import all confirm with specific copy; the SD import device-mismatch guard (HTTP 409 with structured detail, `force` override) is exemplary.

### [MAINT-001] Dashboard and demo assets are hand-synced duplicates
- Severity: Low · Confidence: High · Category: Maintainability
- Affected files: `server/dashboard/assets/*` vs `website/dashboard-demo/assets/*`
- Evidence: the demo is a diverged copy (views.js differs); fixes must be applied twice (this audit had to).
- Recommendation: generate the demo from the live dashboard at Pages-deploy time, or document the sync step in `website/README.md`.

### [MAINT-002] `test_hiveheart_fft.py` bootstraps a stale env-var name
- Severity: Informational · Confidence: High · Category: Maintainability
- Evidence: `test-data/test_hiveheart_fft.py:26` sets `JWT_SECRET`, which no code reads (config uses `HIVEPAL_JWT_SECRET`). Harmless; left as-is to keep the diff focused.

## Documentation inventory

| File | Purpose | Accurate? | Action |
|---|---|---|---|
| `README.md` | Project front door | Was stale (dashboard auth, image name, cellular fields, missing env vars/endpoints) | **Updated** (also gained Testing, Troubleshooting, Contributing, Security sections) |
| `CLAUDE.md` | AI-contributor rules | Yes | Keep |
| `docs/README.md` | Docs index | One stale "auth-free" | **Updated** |
| `docs/api.md` | Full API reference | Mostly excellent; cellular fields + migration range stale | **Updated** |
| `docs/test-commands.md` | curl examples | Yes (verified against routes) | Keep |
| `docs/docker-install.md` | Docker guide | Good; image name stale | **Updated** (image name) |
| `docs/truenas-install.md` | TrueNAS guide | Good; image name stale | **Updated** (image name) |
| `docs/mqtt.md` | MQTT bridge | Yes | Keep (env-example path updated) |
| `docs/notifications.md` | Alerts by mail/push | Yes (matches notifications.py) | Keep |
| `docs/insights.md`, `docs/insights-sources-tldr.md` | Insight catalogue + literature | Yes (spot-checked against insights.py) | Keep |
| `docs/multi-hive.md` | Multi-hive design | Yes — authoritative for the 16-vs-18 question | Keep |
| `docs/wiring.md`, `docs/calibration-mode.md`, `docs/ap-mode-sd-download.md`, `docs/offgrid-firmware-notes.md`, `docs/temperature-compensation.md` | Hardware/firmware ops | Spot-checked, consistent | Keep |
| `docs/beehivemonitoring-gatt.md` | GATT byte layout | Yes — it was the code that was wrong (CODE-001) | Keep |
| `docs/holyiot-ble-sensor.md`, `docs/ruuvitag-ble-sensor.md`, `docs/hiveinside-ble-sensor.md`, `docs/hivetraffic-bee-counter.md`, `docs/device-not-supported-yet.md` | Sensor guides | Spot-checked | Keep |
| `docs/releases/v0.1.md` | Historical release notes | Historical | Keep (archive) |
| `docker/.env.example` | Compose env template | Was incomplete | **Rewritten** (now the single authoritative template) |
| `docker/env.example` | Duplicate template | Diverged + stale AUTH-FREE claim | **Deleted** (merged into `.env.example`) |
| `server/.env.example` | Bare-metal env template | Yes, current | Keep |
| `server/dashboard/README.md` | Dashboard guide | Yes, current (documents the login model) | Keep (env path updated) |
| `website/README.md`, `website/dashboard-demo/README.md`, `test-data/mock-server/README.md` | Sub-project readmes | Spot-checked | Keep |

## Frontend UX assessment

**Journeys reviewed (code-level):** first visit → setup wizard → login →
device/hive selection → range switching → per-group charts → admin
(config edit, channel rename, OTA upload/approve, calibration, SD import,
measurement deletion, user management) → logout → session expiry.

**Strong points:** clean auth gating with distinct setup/login/expired states;
race-guarded data loading (`loadSeq`) with per-device `allSettled` so one dead
device can't blank the rest; two-phase paint (charts first, panels second);
helpful empty states ("No readings in the last Nd — latest is …, widen the
range"); toasts with `role=alert/status`; `aria-live` status region;
focus-visible outlines; `aria-pressed` range buttons; theme handling without
flash including canvas redraw on theme change; excellent destructive-action
confirmation patterns; PWA + push handled as progressive enhancement.

**Weak points:** canvas charts were screen-reader-invisible (fixed: A11Y-001);
chart scrubbing is pointer-only (A11Y-002); hive-picker focus management gaps
(UX-001); 60 s auto-refresh is not user-controllable; the demo copy of the
dashboard must be hand-synced (MAINT-001).

**Responsive behavior:** flex/grid layout with `overflow-x` on tables and a
narrow-viewport breakpoint in style.css; charts resize on `resize` events.
Verified in code only — a device-matrix pass in a browser remains to be done.

**Prioritized UX recommendations:** (1) keyboard access to chart data,
(2) focus restore in the picker, (3) a visible "auto-refresh in Ns / pause"
affordance, (4) browser-matrix smoke test of the admin forms on mobile.

## Changes implemented

1. `firmware/include/beehive_decode.h` — corrected HiveHeart/HiveScale battery
   scale factors to the documented decoder; validated by the real-capture test
   (CODE-001).
2. `.github/workflows/ci.yml` — decode tests now build and run in CI
   (TEST-001).
3. `docker/docker-compose.yml` — correct image name (COMPAT-001); persistent
   `firmware-data` volume (CODE-002); pass-through of all documented env vars,
   required secrets fail-fast, `DATABASE_URL` derived from `POSTGRES_PASSWORD`
   (CONFIG-001).
4. `docker/.env.example` rewritten as the single authoritative template;
   duplicate `docker/env.example` deleted; references updated (DOC-001).
5. `README.md` — dashboard/local-API security model corrected, endpoint table
   completed, env-var table extended (`TRUST_PROXY_HEADERS`, `DASHBOARD_*`),
   image name fixed, removed-field claim fixed, new Testing / Troubleshooting /
   Contributing / Security sections (DOC-001/002, Phase 7).
6. `docs/api.md`, `docs/README.md`, `docs/docker-install.md`,
   `docs/truenas-install.md` — stale claims corrected (DOC-001/002,
   COMPAT-001).
7. `server/dashboard/assets/views.js` + demo copy — `role="img"` /
   `aria-label` on all chart canvases (A11Y-001).
8. This audit report.

## Deferred recommendations

- Session revocation for dashboard accounts (SEC-003) — needs a design choice
  (token versioning vs server-side sessions).
- Authenticated / tokenized firmware downloads (SEC-001) — needs a coordinated
  firmware release.
- Keyboard-accessible chart data (A11Y-002) and picker focus management
  (UX-001).
- De-duplicate the dashboard demo build (MAINT-001).
- Naive-datetime normalization on measurement range params (CODE-003).
- A `SECURITY.md` with a private contact channel.
- Cleanup job for never-claimed spam devices (SEC-002).

## Remaining risks

- Firmware `src/` (~6k lines) beyond the tested decode/I2C surface was reviewed
  structurally, not line-by-line; the PlatformIO builds were not run in this
  environment.
- The GHCR registry state could not be inspected; the image-name fix is derived
  from the workflow file.
- No browser-based validation of the dashboard was possible here; the UX/a11y
  findings are code-derived and the two JS edits were syntax-checked only
  (`node --check`) — the dashboard should be loaded once against a live backend
  before release.
- Historical HiveHeart battery readings stored before CODE-001's fix remain
  inflated; no backfill is possible.
- Unbounded `measurements` growth (no retention/partitioning) is untested at
  multi-year scale.

## Suggested roadmap

**Immediate:** merge this branch; re-upload any OTA binaries lost to CODE-002;
confirm deployments pull `ghcr.io/macnite/hivehub`.

**Near term:** dashboard session revocation; `SECURITY.md`; browser smoke test
of the dashboard (desktop + phone); demo-build de-duplication; naive-datetime
normalization.

**Longer term:** measurement retention/partitioning strategy (e.g. monthly
partitions or a `raw_json` TTL); keyboard-accessible charts; tokenized OTA
downloads; a small integration-test harness that boots the API against a
throwaway Postgres (docker) so the ingest→read→insights path is CI-covered
end-to-end.
