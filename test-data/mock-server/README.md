# HiveHub mock server

A lightweight, self-contained mock of the HiveHub backend, pre-loaded with
realistic dummy data for one dual-channel scale (2025-01-01 → 2026-05-31). It
lets the [HivePal](https://github.com/martinhrvn/hive-pal) maintainer review and
develop against the HiveHub interface — without PostgreSQL or hardware.

```bash
cd mock-server
docker compose up --build      # → http://localhost:31115/docs
```

**See [instructions.md](instructions.md) for full setup, demo credentials,
connecting HivePal, and loading the data into a real HiveHub backend.**

| File | Purpose |
|---|---|
| `app.py` | FastAPI mock — mirrors the full HiveHub API surface, in-memory. |
| `hive_data.py` | Realistic dummy-data generator (no external deps). |
| `insights.py` | Copied verbatim from `../server/insights.py` for faithful insights. |
| `seed.py` | Optional: push the same data into a *real* HiveHub backend. |
| `plot_data.py` | Optional (dev): render the dummy dataset to a PNG for a sanity check. |
| `Dockerfile`, `docker-compose.yml`, `requirements.txt` | Container setup. |
