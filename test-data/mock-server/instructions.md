# HiveHub mock server — setup & usage

A **lightweight, self-contained mock of the HiveHub backend**, pre-loaded with
realistic dummy data for one dual-channel scale. It exists so the
[HivePal](https://github.com/martinhrvn/hive-pal) maintainer can explore and
develop against the HiveHub interface — the `/api/v1/app/...` endpoints HivePal
calls, the auth model, the response shapes, and the insights engine — **without
running PostgreSQL or any hardware**.

* Single container, no database. Just FastAPI + in-memory data.
* Mirrors the exact request/response shapes of the real backend
  (`server/`) and reuses the real `insights.py` **verbatim**, so insights
  responses are identical to production.
* Interactive API explorer (Swagger UI) at **`/docs`**.

> This is a **review/demo tool**, not a production backend: data lives in memory
> and resets on restart. To get the same dummy data into a *real* HiveHub
> stack instead, see [Option B](#option-b--seed-a-real-hivescale-backend).

---

## TL;DR

```bash
cd mock-server
docker compose up --build
```

Then open <http://localhost:31115/docs> and click **Authorize** / **Try it out**.

Everything you need:

| Thing | Value |
|---|---|
| Base URL | `http://localhost:31115` |
| Device key — `X-API-Key` | `demo-device-key` |
| HivePal service key — `X-HivePal-Service-Key` | `demo-hivepal-service-key` |
| HivePal user — `X-User-Id` | any value works (e.g. `demo-user`) |
| Demo devices (both serve the full dataset) | `hive_scale_dual_01` (claim code `ABCD-1234`), `hive_scale_dual_02` (`WXYZ-5678`) |
| Dummy data span | `2025-01-01` → `2026-05-31`, 30-min samples (~24,800 points) |

> **You'll see data no matter what.** The mock is deliberately lenient for review:
> **every device serves the same demo dataset, and any `X-User-Id` may read any
> device** — so whichever device you claim from HivePal (and whatever user IDs
> HivePal uses) the charts and insights populate. No claim is even required; the
> devices already appear in `GET /api/v1/app/devices`. (The real HiveHub
> backend enforces per-user `owner/admin/viewer` roles — see the fidelity note
> below.)

---

## What's inside

A dual-channel HiveHub unit (a "dual beehive scale", so each measurement
carries both `scale_1_*` / Hive A and `scale_2_*` / Hive B), with:

* a full **seasonal weight curve** (winter decline → spring build-up → honey
  harvests → autumn feeding → next year), with the classic **diurnal foraging
  saw-tooth** (heaviest at dawn, lightest mid-afternoon);
* brood-nest temperatures (~35 °C in season), ambient temp/humidity;
* off-grid telemetry (solar harvest, LiPo state-of-charge, cellular status);
* INMP441 acoustic RMS + FFT bands; BeeCounter entrance traffic;
* scripted events so the **insights** endpoint returns live alerts — a
  **pre-swarm watch on Hive A** (unstable broodnest + queen piping in late May
  2026), foraging-intensity info, and active brood rearing on Hive B. A
  historical swarm departure (24 May 2025) and a robbing episode (Aug 2025) are
  visible in the raw measurement history.

The full endpoint surface is mirrored — device endpoints (`X-API-Key`) and all
HivePal app endpoints (`X-HivePal-Service-Key` + `X-User-Id`):
claim, list/remove devices, config (GET/PATCH), channels (GET/PATCH),
measurements (+ date range) and latest, members (list/share/revoke),
calibration start/stop, firmware upload, and insights (+ summary).

---

## Option A — run the mock (recommended)

### With Docker (lightest, nothing to install)

```bash
cd mock-server
docker compose up --build          # serves on http://localhost:31115
```

or without compose:

```bash
cd mock-server
docker build -t hivescale-mock .
docker run --rm -p 31115:8000 hivescale-mock
```

### Without Docker (plain Python ≥ 3.10)

```bash
cd mock-server
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 31115
```

Verify it's up:

```bash
curl http://localhost:31115/health          # {"status":"ok"}
curl http://localhost:31115/                 # orientation: demo ids, keys, data span
```

---

## Connecting HivePal to the mock

HivePal's backend talks to HiveHub through two settings (see
`apps/backend/.env.example` in hive-pal):

```bash
HIVESCALE_API_BASE_URL=http://localhost:31115
HIVESCALE_SERVICE_API_KEY=demo-hivepal-service-key
```

Set those, start HivePal, and its HiveHub features (device list, charts,
insights, calibration, firmware, sharing) will be driven by this mock. Both demo
devices already appear in HivePal's device list and serve the full dataset, so
the charts populate immediately. To exercise the pairing flow, claim a device
with code `ABCD-1234` or `WXYZ-5678` — either way the claimed device shows the
dummy data.

**Networking note:** if your HivePal backend also runs in Docker, `localhost`
won't reach the mock container. Either run both on the same Docker network and
use `http://hivescale-mock:8000`, or use `http://host.docker.internal:31115`.
(HivePal's own default is `http://localhost:8000`; if you'd rather match that,
change the port mapping in `docker-compose.yml` to `"8000:8000"`.)

---

## Exploring the API

The easiest path is the Swagger UI at **`http://localhost:31115/docs`** — every
endpoint, schema, and the auth headers are there to try interactively.

A few `curl` examples (HivePal app endpoints need both headers):

```bash
# List devices for the user
curl http://localhost:31115/api/v1/app/devices \
  -H "X-HivePal-Service-Key: demo-hivepal-service-key" \
  -H "X-User-Id: demo-user"

# A week of measurements (date-range filter; newest first)
curl "http://localhost:31115/api/v1/app/devices/hive_scale_dual_01/measurements?start_at=2025-05-15T00:00:00Z&end_at=2025-05-22T00:00:00Z&limit=10000" \
  -H "X-HivePal-Service-Key: demo-hivepal-service-key" \
  -H "X-User-Id: demo-user"

# Insights — returns a live pre-swarm watch on Hive A
curl http://localhost:31115/api/v1/app/devices/hive_scale_dual_01/insights \
  -H "X-HivePal-Service-Key: demo-hivepal-service-key" \
  -H "X-User-Id: demo-user"

# Claim the still-unclaimed second device
curl -X POST http://localhost:31115/api/v1/app/devices/claim \
  -H "Content-Type: application/json" \
  -H "X-HivePal-Service-Key: demo-hivepal-service-key" \
  -H "X-User-Id: demo-user" \
  -d '{"claim_code":"WXYZ-5678","display_name":"Second stand"}'
```

Auth behaves like production: missing/invalid `X-API-Key` or
`X-HivePal-Service-Key` → `401`; a user with no membership on a device → `403`.

---

## Option B — seed a real HiveHub backend

If you'd rather load the **same dummy data into a production-faithful HiveHub**
(the real `server/` + PostgreSQL), use `seed.py`. It replays the data as device
measurements through `POST /api/v1/measurements` (exactly as the ESP32 firmware
would), so the device is auto-created and becomes claimable from HivePal.

1. Start a real HiveHub backend (from the repo root):

   ```bash
   cd docker
   cp .env.example .env          # set API_KEY, HIVEPAL_SERVICE_API_KEY, DB password
   docker compose up -d          # API on http://localhost:31115
   ```

2. Seed it (no extra Python deps needed):

   ```bash
   cd mock-server
   python seed.py --base-url http://localhost:31115 --api-key <your-API_KEY>
   ```

   Useful flags: `--interval-minutes 60` (fewer points/requests),
   `--limit 500` (quick smoke test), `--dry-run` (print one sample payload and
   exit), `--workers 8`.

3. In HivePal, claim the device with code `ABCD-1234`. The insights, charts and
   latest-measurement views will now be backed by the real database.

---

## Configuration

Environment variables (set in `docker-compose.yml`, `docker run -e`, or your
shell for the uvicorn command):

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | `demo-device-key` | Device key required in `X-API-Key`. |
| `HIVEPAL_SERVICE_API_KEY` | `demo-hivepal-service-key` | Service key for HivePal app endpoints. Use the same value in HivePal's `HIVESCALE_SERVICE_API_KEY`. |
| `DEMO_USER_ID` | `demo-user` | The `X-User-Id` that owns the pre-claimed demo device. |
| `INTERVAL_MINUTES` | `30` | Sample cadence for the dummy data. Lower = denser graphs + more memory (`30` ≈ 24,800 points; `60` ≈ 12,400). |

---

## About the dummy data

The dataset is **synthetic but modelled on documented honey-bee "scale hive"
behaviour**, calibrated so the bundled detectors fire sensibly:

* Annual weight cycle and beekeeper management events (spring build-up, honey
  harvests, autumn feeding) within a realistic ~13–61 kg band.
* The within-day weight saw-tooth follows Meikle et al. (2008), *"Within-day
  variation in continuous hive weight data as a measure of honey bee colony
  activity."*
* Acoustic/temperature swarm signals follow the sources cited in
  `../docs/insights-sources-tldr.md` and `server/insights.py` (Stalidzans &
  Berzonis 2013; Ramsey et al. 2020; MSPB arXiv 2311.10876).

The data is fully reproducible (seeded RNG). Generation is deterministic, so
restarting the container yields the identical dataset.

---

## Mock vs. production — fidelity notes

* **Faithful:** endpoint paths, the service-key/user-id auth model (401s),
  request/response shapes (built from the same key set as
  `measurement_row_to_dict`), and the insights engine (the real `insights.py`,
  copied verbatim).
* **Simplified for the demo:**
  * **Lenient per-device access** — any `X-User-Id` may read any device, every
    device serves the same dataset, and `claim` succeeds for a known code even
    if already claimed. This is so a reviewer sees data without wiring up user
    IDs. Production enforces `owner/admin/viewer` roles (403 otherwise).
  * **Higher measurement limit** — the app measurement endpoint allows the full
    dataset; production caps `limit` at 10000.
  * No persistence (in-memory; resets on restart); `/firmware/{filename}`
    returns a tiny placeholder binary; firmware uploads are accepted and
    acknowledged but not stored; the insights "now" is **anchored to the dataset
    end (2026-05-31)** rather than the wall clock, so alerts stay meaningful no
    matter when you run it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Claimed a device but **"No measurements for the selected range"** | Fixed — every device now serves the dataset. Rebuild the image (`docker compose up --build`) and refresh; your already-claimed device will show data without re-claiming. |
| `401 Invalid API key` / `Invalid HivePal service key` | Send the right header (`X-API-Key` for device endpoints; `X-HivePal-Service-Key` + `X-User-Id` for app endpoints) with the values above. |
| HivePal (in Docker) can't reach the mock | Use `http://host.docker.internal:31115` or a shared Docker network — not `localhost`. |
| Graphs look too sparse/dense | Adjust `INTERVAL_MINUTES`. |
| `seed.py` reports failures | Check `--base-url` is reachable and `--api-key` matches the real server's `API_KEY`. |
