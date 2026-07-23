# HiveHub — TrueNAS Scale Installation Guide

This guide walks you through deploying the HiveHub backend on **TrueNAS Scale** using the built-in **Custom App** feature (which uses Docker Compose under the hood via Helm/Kubernetes). No external tooling is required.

---

## Prerequisites

- TrueNAS Scale (any recent version with the Apps feature enabled)
- A dataset on your pool for persistent database storage (e.g. `tank/hivescale-db`)
- The HiveHub Docker image is public on GHCR — no authentication required to pull it

---

## Step 1 — Create a dataset for the database

The PostgreSQL container needs a persistent volume to survive reboots and app upgrades. Create a dedicated ZFS dataset for it first.

1. Go to **Storage → Create Dataset**.
2. Name: `hivescale-db` (or any name you prefer).
3. Parent dataset: choose your main pool (e.g. `tank`).
4. Leave all other settings at their defaults and click **Save**.

Note the full path — you will need it in Step 3. It is typically `/mnt/<pool>/hivescale-db`, e.g. `/mnt/tank/hivescale-db`.

---

## Step 2 — Open the Custom App wizard

1. Go to **Apps** in the TrueNAS Scale UI.
2. Click **Discover Apps → Custom App** (top right).
3. Give the app a name, e.g. `hivescale`.

---

## Step 3 — Configure the app

TrueNAS Custom App uses a Docker Compose-compatible YAML editor. Paste the following configuration and adjust the values in `< >` brackets:

```yaml
services:
  hivescale-api:
    image: ghcr.io/macnite/hivehub:latest
    depends_on:
      hivescale-db:
        condition: service_healthy
    environment:
      API_KEY: <your-master-admin-key>
      HIVEPAL_SERVICE_API_KEY: <your-hivepal-service-key>
      HIVEPAL_JWT_SECRET: <match-hivepal-jwt-secret>
      DATABASE_URL: postgresql://hivescale:<db-password>@hivescale-db:5432/hivescale
      PUBLIC_BASE_URL: http://<truenas-ip-or-hostname>:31115
      TZ: Europe/Berlin
      # This app is reached directly on the LAN (no reverse proxy in front), so
      # do NOT trust client-supplied forwarded-IP headers — a client could spoof
      # them to bypass rate limiting. Only set this true if you add a proxy that
      # overwrites CF-Connecting-IP / X-Forwarded-For.
      TRUST_PROXY_HEADERS: "false"
    ports:
      - "31115:8000"
    restart: unless-stopped

  hivescale-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: hivescale
      POSTGRES_USER: hivescale
      POSTGRES_PASSWORD: <db-password>
      TZ: Europe/Berlin
    healthcheck:
      test:
        - CMD-SHELL
        - pg_isready -U hivescale -d hivescale
      interval: 30s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    volumes:
      - /mnt/<pool>/hivescale-db:/var/lib/postgresql/data
```

**Values to replace:**

| Placeholder | What to set |
|---|---|
| `<your-master-admin-key>` | A long random string used as the master/admin key for server-to-server tooling (firmware releases, command queueing). Devices register their own per-device keys automatically and do **not** need this value. |
| `<your-hivepal-service-key>` | A separate long random string for HivePal integration (can be left as a placeholder if you are not using HivePal) |
| `<match-hivepal-jwt-secret>` | The same secret as HivePal's `JWT_SECRET`, used to verify per-user Bearer tokens. Required for HivePal app endpoints; placeholder is fine if not using HivePal. |
| `<db-password>` | A strong password for the PostgreSQL user (must match in both services) |
| `<truenas-ip-or-hostname>` | The LAN IP or hostname of your TrueNAS box — used to build OTA firmware download URLs |
| `/mnt/<pool>/hivescale-db` | Full path to the dataset you created in Step 1 |
| `Europe/Berlin` | Your timezone — adjust for correct timestamps in logs |

> **Security note:** Use randomly generated strings for all keys and secrets
> (`API_KEY`, `HIVEPAL_SERVICE_API_KEY`, `HIVEPAL_JWT_SECRET`, and the DB
> password). You can generate one with:
> ```bash
> openssl rand -hex 32
> ```
> Note `HIVEPAL_JWT_SECRET` must be set to the **same** value as HivePal's
> `JWT_SECRET`, not a fresh random string.

---

## Step 4 — Set port permissions (if needed)

Port `31115` is above 1024 and does not require special permissions. If TrueNAS warns about host network port binding, confirm the override.

---

## Step 5 — Deploy

Click **Install** (or **Save**). TrueNAS will pull the images and start the containers. The first start may take 1–2 minutes while PostgreSQL initialises.

Check the app status under **Apps → Installed Apps**. Both containers should show as **Running**.

---

## Step 6 — Verify the installation

From any machine on your network, run:

```bash
curl http://<truenas-ip>:31115/health
```

Expected response:
```json
{ "status": "ok" }
```

You can also open `http://<truenas-ip>:31115/docs` in a browser to access the interactive API documentation.

---

## Step 7 — Configure the firmware

Update `firmware/include/secrets.h` on your ESP32 project:

```cpp
#define API_BASE_URL  "https://<your-domain>"   // HTTPS — the firmware verifies the TLS cert
#define API_KEY       "<unique-per-device-key>"  // generate with: openssl rand -hex 32
```

Give each device a unique `API_KEY`; the backend registers it against the
device's `device_id` on first contact. Then re-flash the device (or push the key
via the provisioning portal if already deployed).

> **HTTPS is required by default.** The firmware verifies the backend's TLS certificate
> against the Let's Encrypt root CA bundled in `firmware/include/ca_cert.h`. Put
> the API behind a reverse proxy that terminates TLS with a valid certificate;
> by default, a plain `http://…:31115` address is rejected by the device. For a
> CA other than Let's Encrypt, replace the certificate in `ca_cert.h`.

> **Trusted-LAN HTTP opt-in:** To use a plain HTTP API only on an isolated,
> trusted LAN, set `#define ALLOW_INSECURE_HTTP 1` in `secrets.h` and change
> `API_BASE_URL` to `http://<truenas-ip>:31115`. HTTPS still works with TLS
> verification enabled. HTTP exposes the device API key, data, commands, and
> OTA download to network observers, so never use this setting on an untrusted
> network.

---

## Uploading firmware binaries for OTA

To make OTA updates available, you need to copy firmware `.bin` files into the container's `FIRMWARE_DIR` (default: `/app/firmware`).

The simplest approach is to use the TrueNAS **Shell** to copy the file directly:

```bash
# Copy a firmware binary into the running API container
docker cp hivescale-0.5.0.bin <container-name>:/app/firmware/hivescale-0.5.0.bin
```

To find the container name:
```bash
docker ps | grep hivescale-api
```

Then register the release via the API:
```bash
curl -X POST http://<truenas-ip>:31115/api/v1/firmware/releases \
  -H "X-API-Key: <your-strong-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"version": "0.5.0", "filename": "hivescale-0.5.0.bin", "active": true}'
```

> **Tip:** For a more permanent setup, mount a second TrueNAS dataset as `/app/firmware` in the compose YAML so firmware binaries persist across container recreations:
> ```yaml
> hivescale-api:
>   volumes:
>     - /mnt/<pool>/hivescale-firmware:/app/firmware
> ```

---

## Updating the application

When a new version of the Docker image is published:

1. Go to **Apps → Installed Apps → hivescale**.
2. Click **Update** (TrueNAS will pull the new `latest` image and recreate the container).
3. The database is preserved via the mounted dataset — no data is lost.

Alternatively, from the TrueNAS shell:
```bash
docker pull ghcr.io/macnite/hivehub:latest
# Then restart the app from the UI
```

---

## Backups

Back up the PostgreSQL data by snapshotting the ZFS dataset:

```bash
zfs snapshot tank/hivescale-db@$(date +%Y%m%d)
```

For a logical dump (portable backup):
```bash
docker exec <db-container-name> pg_dump -U hivescale hivescale > hivescale-backup.sql
```

---

## Troubleshooting

**App does not start / stays in "Deploying" state:**
Check the container logs from the TrueNAS shell:
```bash
docker logs <container-name>
```

**API is unreachable from the network:**
- Confirm the port is not blocked by TrueNAS firewall rules.
- Verify the `ports` mapping in the compose YAML is `31115:8000`.

**Database connection errors in API logs:**
- Confirm the `DATABASE_URL` password matches `POSTGRES_PASSWORD` exactly.
- The `depends_on` health check ensures the API waits for PostgreSQL to be ready, but if the DB dataset path is wrong the container will fail to start — check the volume mount path.

**`/health` returns OK but measurements are not stored:**
- Each device registers its own per-device `API_KEY` on first contact; if a device later presents a *different* key for the same `device_id` it gets `401`. To re-register a key, clear the stored hash: `UPDATE devices SET api_key_hash = NULL WHERE device_id = '…';`
- Confirm the device can complete a TLS handshake (valid certificate, NTP reachable for clock sync) — a failed handshake looks like a silent upload failure on the device.
- Use the test commands in [test-commands.md](test-commands.md) to submit a measurement manually and inspect the response.
