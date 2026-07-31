# Downloading, backing up and restoring measurement data

The built-in dashboard can hand you every reading the server holds as a single
file, and load that file back in later. Use it to

- **keep a backup** before re-deploying the Docker stack or wiping the database,
- **migrate a beekeeper** off a shared server onto their own HiveHub, or
- **archive** one device (or one hive) for analysis elsewhere.

Both halves live on **Device & admin**: **Download / backup data** saves the
file, **Import SD card data** reads it back.

> **Scope:** the backup holds **measurements** — every reading, including the
> per-hive readings of a multi-hive device. It does **not** carry device
> configuration (scale offsets/factors, temperature compensation), hive display
> names, dashboard accounts, firmware releases or insight history. For a
> complete copy of everything, take a `pg_dump` of the Postgres database as well;
> see [docker-install.md](docker-install.md).

---

## The file format

The download is **NDJSON**: one JSON object per line, each one a measurement
payload in exactly the shape the ESP32 writes to its SD card and posts to
`POST /api/v1/measurements`. That is deliberate — it means a download can be fed
straight back in through the same **Import SD card data** upload that accepts a
card pulled off a scale in AP mode (see
[ap-mode-sd-download.md](ap-mode-sd-download.md)), with no separate restore tool.

```json
{"device_id":"hive_scale_dual_01","timestamp":"2026-07-31T06:10:00+00:00","scale_1_weight_kg":42.5,"ambient_temp_c":18.4,"hives":[{"index":1,"weight_kg":42.5,"temp_c":34.6}]}
```

Two fields are always written from what the **database** stored rather than from
the payload the device sent:

- **`device_id`** — so a row can never claim a device other than the one it is
  filed under.
- **`timestamp`** — set to the reading's stored `measured_at`. A scale with no
  working RTC sends 1970 (or no timestamp at all) and the ingest path clamps that
  to the server clock; exporting the raw value would re-import every such reading
  onto the wrong instant, and collapse them all onto a single one.

Claim codes are never included.

---

## Downloading

**Device & admin → Download / backup data.** Everything starts ticked, so
pressing **Download backup** without touching anything backs up the whole server.

| Filter | Effect |
|---|---|
| **Devices** | Which devices to include. Hidden (retired) devices are included too — hiding only removes a device from the hive picker. |
| **Hives** | Which hives' readings to include, **by number, across every device in the download**. Readings that belong to the device rather than to a hive — ambient temperature, battery, solar, connectivity, the stereo microphone — are always included. |
| **Period** | Optional `From` / `To`. Leave both blank for the full history. |

The panel shows how many readings the current filters match, and over what
period, before anything is saved — the download itself is a plain browser
navigation, so an empty or mistaken selection would otherwise only become obvious
in the saved file.

The file is named after what it covers, e.g.
`hivehub-backup-hive_scale_dual_01-20260731.ndjson` for one device or
`hivehub-backup-3-devices-20260731.ndjson` for several.

---

## Restoring

**Device & admin → Import SD card data → Upload SD data.** Re-importing is
idempotent: `(device_id, measured_at)` is the natural key, so rows already in the
database are counted as duplicates and skipped. Uploading the same backup twice
inserts nothing the second time.

What happens next depends on how many devices the file names:

- **One device.** The readings are imported into it. If that device is not the
  one currently selected, the dashboard offers to send them to the device that
  actually recorded them instead of mis-attaching them.
- **Several devices** (a whole-server backup). The dashboard asks whether to
  restore each device's readings into that device, and the server files every row
  under the device stamped into it.

### The devices have to exist first

A device row is created by the **claim / check-in flow**, not by an upload — a
file cannot conjure a device with no key and no owner into existence. A restore
that names a device this server has never seen is refused, naming it.

After a redeploy this resolves itself: the scale checks in on its next cycle
(every `send_interval_seconds`, 10 minutes by default), registers, and the import
then succeeds. So the order is **let the device check in once → then restore**.

---

## Migrating a beekeeper to their own server

1. On the old server, **Download / backup data**, tick only that beekeeper's
   device(s), and save the file.
2. Point their scale at the new server (`SERVER_URL` in `secrets.h`, or the
   AP-mode setup portal) and wait for one check-in, so the device registers
   itself there.
3. On the new server's dashboard, **Import SD card data** and upload the file.
4. Re-create what the backup does not carry: hive names
   (**Device & admin → Hive names**) and the device configuration — in
   particular the **scale offsets and factors** and any temperature-compensation
   coefficients (**Device & admin → Configuration**). Copy them off the old
   server before you decommission it.
5. Once the data is verified on the new server, remove the device from the old
   one (**Device & admin → Delete readings**, or drop the device row directly).

---

## API

Both endpoints are part of the local dashboard API and need a dashboard login
session (`ENABLE_LOCAL_DASHBOARD=true`).

### `GET /api/v1/local/export/measurements`

Streams the backup. All parameters are optional.

| Parameter | Meaning |
|---|---|
| `device_id` | Repeatable. Omit for **every** device on the server. |
| `hive` | Repeatable hive index (1–18). Omit for every hive. |
| `start_at` / `end_at` | ISO-8601 bounds on `measured_at`. |

Responds with `application/x-ndjson` and a `Content-Disposition` filename. The
body is streamed a page of rows at a time, so exporting years of readings does
not build the whole file in memory.

```bash
curl -b cookies.txt -OJ \
  "http://<host>:31115/api/v1/local/export/measurements?device_id=hive_scale_dual_01"
```

### `GET /api/v1/local/export/measurements/summary`

Same filters (minus `hive`), returning what a download would contain rather than
the data: a per-device row count with first/last timestamps, the total, and the
filename the download would use. Row counts are unaffected by the hive filter,
which trims fields inside a row rather than dropping rows.

### `POST /api/v1/local/devices/{device_id}/measurements/import`

The existing SD-card upload (admin only), multipart with the file under `file`:

| Field | Meaning |
|---|---|
| `force=true` | Import a file recorded by a *different* device into `{device_id}` anyway — for a device that was re-provisioned under a new id. |
| `route_by_device=true` | Import each row into the device stamped into it, instead of pinning the whole file onto `{device_id}`. This is how a multi-device backup is restored. Rows with no `device_id` (older backups) fall back to `{device_id}`. |

Returns the totals plus a `devices` breakdown — one entry per device the import
touched.
