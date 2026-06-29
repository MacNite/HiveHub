#!/usr/bin/env python3
"""
Seed a *real* HiveHub backend with the same dummy data the mock serves.

This replays the generated time-series as device measurements through the
public device API (``POST /api/v1/measurements``, authenticated with
``X-API-Key``) - exactly as the ESP32 firmware would. Because each payload
carries the demo ``claim_code``, the backend auto-creates the device and makes
it claimable from HivePal (``POST /api/v1/app/devices/claim``).

Use this when you want the data inside a production-faithful HiveHub stack
(``server/`` + PostgreSQL, see ../docker) rather than the in-memory mock.

Standard library only - no extra dependencies.

Examples
--------
    # Seed a local real backend (interval 30 min = ~24.8k measurements)
    python seed.py --base-url http://localhost:31115 --api-key Super-Secret-Key

    # Quick smoke test: only the first 500 points, coarser cadence
    python seed.py --base-url http://localhost:31115 --api-key KEY \\
        --interval-minutes 120 --limit 500
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import hive_data


def build_payload(m: dict, claim_code: str) -> dict:
    """Turn a generated measurement record into a device POST body.

    The generator produces rows shaped like the API *response*
    (``measurement_row_to_dict``); the device *request* uses ``timestamp``
    instead of ``measured_at`` and omits server-assigned fields.
    """
    drop = {"id", "received_at", "measured_at", "battery_voltage"}
    payload = {k: v for k, v in m.items() if k not in drop}
    payload["timestamp"] = m["measured_at"].isoformat()
    payload["claim_code"] = claim_code
    return payload


def post_measurement(base_url: str, api_key: str, payload: dict, timeout: float = 30.0) -> tuple[bool, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/measurements",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": api_key},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (200 <= resp.status < 300, str(resp.status))
    except urllib.error.HTTPError as e:
        return (False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
    except Exception as e:  # noqa: BLE001 - report any transport error and continue
        return (False, repr(e))


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed a real HiveHub backend with dummy data.")
    ap.add_argument("--base-url", default="http://localhost:31115",
                    help="HiveHub base URL (default: http://localhost:31115)")
    ap.add_argument("--api-key", default="Super-Secret-Key",
                    help="HiveHub device API key (X-API-Key). Must match the server's API_KEY.")
    ap.add_argument("--interval-minutes", type=int, default=30,
                    help="Sample cadence to generate (default 30 = ~24.8k measurements; "
                         "raise to 60/120 for fewer requests).")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only send the first N measurements (0 = all). Handy for a smoke test.")
    ap.add_argument("--workers", type=int, default=8, help="Concurrent POST workers (default 8).")
    ap.add_argument("--dry-run", action="store_true", help="Generate and report counts, send nothing.")
    args = ap.parse_args()

    print(f"Generating dummy data (interval {args.interval_minutes} min)...", flush=True)
    measurements = hive_data.generate_measurements(args.interval_minutes)
    if args.limit > 0:
        measurements = measurements[: args.limit]

    span = (measurements[0]["measured_at"].isoformat(), measurements[-1]["measured_at"].isoformat())
    print(f"  device_id : {hive_data.DEVICE_ID}")
    print(f"  claim_code: {hive_data.CLAIM_CODE}")
    print(f"  points    : {len(measurements)}  ({span[0]} .. {span[1]})")

    if args.dry_run:
        print("Dry run - nothing sent. Example payload:")
        print(json.dumps(build_payload(measurements[len(measurements) // 2], hive_data.CLAIM_CODE),
                          indent=2, default=str))
        return 0

    print(f"Posting to {args.base_url} with {args.workers} workers...", flush=True)
    payloads = [build_payload(m, hive_data.CLAIM_CODE) for m in measurements]
    ok = 0
    fail = 0
    first_error = None
    total = len(payloads)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(post_measurement, args.base_url, args.api_key, p): i
                   for i, p in enumerate(payloads)}
        done = 0
        for fut in as_completed(futures):
            success, info = fut.result()
            done += 1
            if success:
                ok += 1
            else:
                fail += 1
                if first_error is None:
                    first_error = info
            if done % 1000 == 0 or done == total:
                print(f"  {done}/{total}  ok={ok} fail={fail}", flush=True)

    print(f"\nDone. {ok} sent, {fail} failed.")
    if fail:
        print(f"First error: {first_error}")
        print("Check the base URL, that the server is reachable, and that --api-key matches API_KEY.")
        return 1
    print(f"\nThe device '{hive_data.DEVICE_ID}' is now claimable in HivePal with "
          f"claim code '{hive_data.CLAIM_CODE}'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
