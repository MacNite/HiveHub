# HiveScale — Docker Installation Guide

This guide covers deploying the HiveScale backend on any Linux system with Docker and Docker Compose. The process is essentially the same as the [TrueNAS installation](truenas-install.md), but without the TrueNAS-specific UI steps.

---

## Prerequisites

- A Linux server or VPS (Ubuntu 22.04+ / Debian 12+ recommended)
- [Docker Engine](https://docs.docker.com/engine/install/) ≥ 24
- [Docker Compose](https://docs.docker.com/compose/install/) ≥ 2 (usually bundled as `docker compose`)
- A directory for persistent database storage (e.g. `/opt/hivescale/db`)

---

## Step 1 — Create storage directories

```bash
sudo mkdir -p /opt/hivescale/db
sudo mkdir -p /opt/hivescale/firmware   # optional: for OTA binary hosting
```

---

## Step 2 — Create the environment file

```bash
cd /opt/hivescale
cp /path/to/repo/docker/.env.example .env
nano .env
```

Fill in all values:

```env
# Master/admin key (X-API-Key header) for server-to-server tooling:
# firmware-release registration, command queueing, latest-measurements, time.
# Devices no longer need this — each device registers its own per-device key.
API_KEY=change-this-to-a-long-random-string

# Service key used by HivePal (X-HivePal-Service-Key header)
HIVEPAL_SERVICE_API_KEY=change-this-to-another-long-random-string

# Shared secret (HS256) used to verify HivePal's per-user Bearer tokens.
# Must match HivePal's JWT_SECRET. Required for HivePal app endpoints.
HIVEPAL_JWT_SECRET=change-this-to-match-hivepal-jwt-secret

# PostgreSQL password — must match POSTGRES_PASSWORD below
POSTGRES_PASSWORD=change-this-database-password

# Public base URL used to build OTA firmware download links
# Use HTTPS in production (behind your reverse proxy / Cloudflare)
PUBLIC_BASE_URL=https://your-domain.example

# Timezone for logs and timestamps
TZ=Europe/Berlin

# ── Abuse / DoS protection (optional — sensible defaults shown) ──
# Per-client-IP request rate limit; set RATE_LIMIT_ENABLED=false to disable
RATE_LIMIT_ENABLED=true
RATE_LIMIT_DEFAULT=120/minute
# Max JSON request body (bytes); 262144 = 256 KiB
MAX_BODY_BYTES=262144
# Max uploaded firmware binary (bytes); 16777216 = 16 MiB
MAX_FIRMWARE_BYTES=16777216
```

> Generate strong random keys with:
> ```bash
> openssl rand -hex 32
> ```

---

## Step 3 — Create the Docker Compose file

Copy the compose file from the repo or create `/opt/hivescale/docker-compose.yml`:

```yaml
services:
  hivescale-api:
    image: ghcr.io/macnite/hivescale-api:latest
    depends_on:
      hivescale-db:
        condition: service_healthy
    environment:
      API_KEY: ${API_KEY}
      HIVEPAL_SERVICE_API_KEY: ${HIVEPAL_SERVICE_API_KEY}
      HIVEPAL_JWT_SECRET: ${HIVEPAL_JWT_SECRET}
      DATABASE_URL: postgresql://hivescale:${POSTGRES_PASSWORD}@hivescale-db:5432/hivescale
      PUBLIC_BASE_URL: ${PUBLIC_BASE_URL}
      FIRMWARE_DIR: /app/firmware
      TZ: ${TZ}
      # Abuse/DoS protection (optional — defaults applied if omitted)
      RATE_LIMIT_ENABLED: ${RATE_LIMIT_ENABLED:-true}
      RATE_LIMIT_DEFAULT: ${RATE_LIMIT_DEFAULT:-120/minute}
      MAX_BODY_BYTES: ${MAX_BODY_BYTES:-262144}
      MAX_FIRMWARE_BYTES: ${MAX_FIRMWARE_BYTES:-16777216}
    ports:
      - "31115:8000"
    volumes:
      - /opt/hivescale/firmware:/app/firmware
    restart: unless-stopped

  hivescale-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: hivescale
      POSTGRES_USER: hivescale
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      TZ: ${TZ}
    healthcheck:
      test:
        - CMD-SHELL
        - pg_isready -U hivescale -d hivescale
      interval: 30s
      timeout: 5s
      retries: 5
    restart: unless-stopped
    volumes:
      - /opt/hivescale/db:/var/lib/postgresql/data
```

---

## Step 4 — Start the stack

```bash
cd /opt/hivescale
docker compose up -d
```

Check that both containers are running:

```bash
docker compose ps
```

View live logs:

```bash
docker compose logs -f
```

---

## Step 5 — Verify

```bash
curl http://localhost:31115/health
```

Expected:
```json
{ "status": "ok" }
```

Interactive API docs: `http://your-server-ip:31115/docs`

---

## Step 6 — Configure the firmware

Edit `firmware/include/secrets.h`:

```cpp
#define API_BASE_URL  "https://your-domain.example"   # HTTPS — the firmware verifies the TLS cert
#define API_KEY       "a-unique-per-device-key"        # generate with: openssl rand -hex 32
```

Give each device a unique `API_KEY`; the backend registers it against the
device's `device_id` on first contact. Re-flash the device, or update the key
via the provisioning portal if it is already deployed.

> **HTTPS is required.** The firmware verifies the backend's TLS certificate
> against the Let's Encrypt root CA bundled in `firmware/include/ca_cert.h`, so
> `API_BASE_URL` must point at an HTTPS endpoint with a valid certificate — see
> [Exposing to the internet](#exposing-to-the-internet-optional). For a different
> CA, replace the certificate in `ca_cert.h`.

---

## OTA firmware updates

Place compiled firmware binaries in `/opt/hivescale/firmware/`, then register them:

```bash
curl -X POST http://localhost:31115/api/v1/firmware/releases \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"version": "0.5.0", "filename": "hivescale-0.5.0.bin", "active": true, "target": "hivescale"}'
```

`target` defaults to `hivescale`; use `beecounter` to publish an image that the
HiveScale relays to a BeeCounter over I2C. App clients (HivePal) can also upload
the binary directly via `POST /api/v1/app/devices/{id}/firmware` instead of
copying it into `FIRMWARE_DIR` first — see [api.md](api.md).

The device checks for updates every 6 hours and on every measurement cycle.

---

## Updating the application

```bash
cd /opt/hivescale
docker compose pull
docker compose up -d
```

The database volume is preserved — no data is lost on updates.

---

## Backups

**Logical dump (recommended):**
```bash
docker compose exec hivescale-db pg_dump -U hivescale hivescale > hivescale-backup-$(date +%Y%m%d).sql
```

**Restore from dump:**
```bash
docker compose exec -T hivescale-db psql -U hivescale hivescale < hivescale-backup-20260501.sql
```

---

## Exposing to the internet (optional)

The firmware verifies the backend's TLS certificate, so devices must reach the
API over **HTTPS with a valid certificate**. The recommended setup:

- **Reverse proxy with HTTPS (recommended)** — run Nginx or Caddy in front of the API and obtain a TLS certificate via Let's Encrypt. Caddy does this automatically with a single config line. The bundled `firmware/include/ca_cert.h` already trusts the Let's Encrypt root, so no firmware change is needed.
- **Tailscale / WireGuard** — put both the server and the ESP32 (via a companion device or router) on a private VPN. You still need a valid TLS certificate the device will trust on the address it connects to.
- **Port forwarding** — forward port `31115` on your router to the server, but terminate TLS in front of it (e.g. via a proxy) so the device can verify the certificate; the firmware will reject a plain-HTTP endpoint or an untrusted certificate.

> **Rate limiting & request size:** the API enforces a per-client-IP rate limit
> and a request-body cap out of the box (see the environment variables above).
> When running behind a reverse proxy or Cloudflare, the API reads the real
> client IP from `CF-Connecting-IP` / `X-Forwarded-For`, so per-device limits
> work correctly. For defence in depth, also enable limits at the proxy — e.g.
> Nginx `limit_req` + `client_max_body_size 256k`, Caddy `rate_limit`, or a
> Cloudflare WAF rate-limiting rule on `/api/v1/*`. Make sure the proxy sets the
> forwarded-IP header and that Cloudflare SSL/TLS mode is **Full (strict)**.

---

## Troubleshooting

**Containers exit on startup:**
```bash
docker compose logs hivescale-api
docker compose logs hivescale-db
```
Common causes: wrong `DATABASE_URL` password, missing environment variable, or port `31115` already in use.

**Port already in use:**
```bash
sudo ss -tlnp | grep 31115
```
Change the host-side port mapping in the compose file (e.g. `"31116:8000"`) if something else is using `31115`.

**Measurements not stored:**
Use the test commands in [test-commands.md](test-commands.md) to verify the API key and payload format.

**OTA firmware URL does not resolve:**
Ensure `PUBLIC_BASE_URL` is set to an address the ESP32 can reach — not `localhost`.