# Project instructions for Claude Code

## Delivery policy — ask if data should be pushed to github directly

This repository uses a **"no remote writes"** workflow. When you finish making
changes, **do not publish them to GitHub automaticly** — allways ask if it should be pushed and a pr should be created.

**You MUST NOT, under any circumstances unless I explicitly ask in that very message:**

- run `git push` (to any branch or remote);
- create, update, merge, or comment on pull requests;
- create or move remote branches or tags;
- push files through the GitHub API / MCP tools (`create_or_update_file`,
  `push_files`, `create_pull_request`, etc.);
- otherwise transmit repository contents to GitHub or any external service.

Working **locally** is fine: edit files, run builds/tests, and commit to the
local branch if it helps you organize work. Just never send anything to the
remote.
---

## About this project

HiveHub is an ESP32-based data collector for beehive sensors and scales. It gathers weight, temperature, humidity, sound, vibration, power state, and network state from one or more hives (up to 16 per ESP32) and sends the readings to a self-hosted FastAPI backend backed by PostgreSQL, where they can be displayed in HivePal or the included frontend.

- `firmware/` — ESP32 PlatformIO project (`src/main.cpp` is the main source).
- `server/` — Python FastAPI backend and insights logic.
- `docker/` — Docker Compose deployment for the API and database.
- `pcb-design/` — KiCad breakout PCB design and fabrication outputs.
- `docs/` — hardware, API, deployment, and test documentation.
- `test-data/` — mock server and sample payloads.

Secrets live in `.env` / `secrets.h` files that are gitignored — never add real
credentials to tracked files or to a bundle you hand back.
