"""Tests for the sub-device OTA relay gating (server/commands.py).

Covers both relay targets — HiveInside (nRF54 node) and beecounter (HiveTraffic
counter) — which share one code path and must therefore share one set of rules.

Run: python3 -m pytest test-data/test_relay_ota_gate.py
 or: PYTHONPATH=server python3 test-data/test_relay_ota_gate.py

Covers:
  * the slot check — `slot` is a hive index, so every hive (1..MAX_HIVES) can be
    targeted, not just the two the legacy bleSensorMac0/1 globals could reach;
  * the version gate, FOR EVERY TARGET — a release is only relayed when it is
    strictly newer than the version the node reports, with an unknown version
    never blocking and `force` overriding the comparison;
  * the local dashboard's relay endpoint — that one exists at all, and that it
    goes through the same shared helper (and therefore the same gate) as the
    master-key and HivePal callers;
  * that firmware downloads are never gzipped. This lives here, next to the
    relay it protects, because compressing them is not a cosmetic problem: a
    compressed response carries no Content-Length, the ESP32 sizes its download
    from that header, and the relay died with "invalid firmware content length
    -1" before it ever opened a BLE session.

The commands import needs a handful of env vars (config.py reads them at import
time); dummy values are injected below so the test runs without a real database
or FastAPI runtime. db.py builds its pool with open=False, so importing it does
not connect.
"""

import asyncio
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

# Satisfy config.py's required env vars so `import commands` works offline.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from fastapi import HTTPException  # noqa: E402

import auth  # noqa: E402
import commands  # noqa: E402
import middleware  # noqa: E402
from schemas import MAX_HIVES  # noqa: E402

_failures = 0


def check(label, cond):
    global _failures
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        _failures += 1


# Every relay target, so a new one cannot be added without inheriting the rules
# below. Keyed the same way commands.py keys its own dispatch.
TARGETS = tuple(commands.RELAY_COMMAND_TYPES)


def gate(current, release, force=False, target="hiveinside"):
    """Run the version gate with `current` as the node's reported version."""
    with mock.patch.object(commands, "reported_subdevice_version", return_value=current):
        return commands.check_relay_is_newer(target, "dev-1", 1, release, force)


def test_every_hive_index_is_a_valid_slot():
    for n in (1, 2, 3, 9, MAX_HIVES):
        commands.check_relay_slot(n)
        check(f"slot {n} accepted", True)


def test_out_of_range_slots_are_rejected():
    for bad in (0, -1, MAX_HIVES + 1):
        try:
            commands.check_relay_slot(bad)
            check(f"slot {bad} rejected", False)
        except HTTPException as e:
            check(f"slot {bad} rejected with 400", e.status_code == 400)


def test_newer_release_passes_and_reports_what_it_replaces():
    for t in TARGETS:
        # The returned value is the version being replaced, so a caller can render
        # "0.4.0 -> 0.4.1" without a second request.
        check(f"[{t}] 0.4.0 -> 0.4.1 allowed", gate("0.4.0", "0.4.1", target=t) == "0.4.0")
        # Numeric, not lexical: 10 > 9.
        check(f"[{t}] 0.9.9 -> 0.10.0 allowed", gate("0.9.9", "0.10.0", target=t) == "0.9.9")
        # Uneven component counts compare as tuples.
        check(f"[{t}] 0.4 -> 0.4.1 allowed", gate("0.4", "0.4.1", target=t) == "0.4")


def test_same_or_older_release_is_refused():
    for t in TARGETS:
        for current, release in (("0.4.1", "0.4.1"), ("0.4.1", "0.4.0"), ("1.0.0", "0.9.9")):
            try:
                gate(current, release, target=t)
                check(f"[{t}] {current} -> {release} refused", False)
            except HTTPException as e:
                check(
                    f"[{t}] {current} -> {release} refused with 409 naming both versions",
                    e.status_code == 409 and current in e.detail and release in e.detail,
                )


def test_refusal_names_the_device_it_is_about():
    """A 409 that says "HiveInside" while the operator pressed relay on a bee
    counter is worse than no message. The label comes from RELAY_LABELS."""
    for t in TARGETS:
        try:
            gate("0.4.1", "0.4.1", target=t)
            check(f"[{t}] refused", False)
        except HTTPException as e:
            check(f"[{t}] 409 names the target device",
                  commands.RELAY_LABELS[t] in e.detail)


def test_unknown_version_never_blocks():
    # A node that never reported a version cannot be compared against — and an
    # update may be exactly what a silent node needs. A HiveTraffic counter on
    # firmware that predates the "ver" field is exactly this case.
    for t in TARGETS:
        check(f"[{t}] unknown current allowed", gate(None, "0.4.1", target=t) is None)


def test_force_overrides_the_comparison():
    for t in TARGETS:
        check(f"[{t}] force allows the same version",
              gate("0.4.1", "0.4.1", force=True, target=t) == "0.4.1")
        check(f"[{t}] force allows an older release",
              gate("0.5.0", "0.4.1", force=True, target=t) == "0.5.0")


def test_every_relay_target_is_queueable_and_retryable():
    """A target that is not in all three places is unreachable or unrecoverable.

    A missing schema literal makes create_command reject the row outright; a
    missing RETRYABLE entry means an abandoned relay fails permanently on the
    first lost result POST instead of going back on the queue.
    """
    from schemas import DeviceCommandIn

    for t, cmd in commands.RELAY_COMMAND_TYPES.items():
        check(f"[{t}] {cmd} is an accepted command type",
              DeviceCommandIn(command_type=cmd).command_type == cmd)
        check(f"[{t}] {cmd} is retryable", cmd in commands.RETRYABLE_COMMAND_TYPES)
        check(f"[{t}] has a human label", bool(commands.RELAY_LABELS.get(t)))


class _FakeCursor:
    """Records the SQL a function issues, so the sweep's rules can be asserted."""

    def __init__(self, rows=None):
        self.statements = []
        self._rows = rows or []

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _sweep_statements():
    """Run expire_stale_claimed_commands against a stubbed connection."""
    cur = _FakeCursor()
    with mock.patch.object(commands, "get_conn", return_value=_FakeConn(cur)):
        commands.expire_stale_claimed_commands()
    return cur.statements


def test_abandoned_relay_is_requeued_then_failed():
    """A claim nobody reported on must not sit in 'claimed' forever.

    Regression guard: next_command only ever serves 'pending' rows, so before
    this sweep existed a lost result POST stranded a relay permanently — one sat
    'claimed' for over six hours with the HiveHub alive and polling throughout,
    and (once the button reflected command state) it disabled the UI with it.
    """
    stmts = _sweep_statements()
    check("sweep issues both a requeue and a give-up pass", len(stmts) == 2)
    if len(stmts) != 2:
        return
    requeue, giveup = stmts

    check("requeue targets only stale claims",
          "SET status = 'pending'" in requeue[0] and "status = 'claimed'" in requeue[0])
    check("requeue is bounded by an attempt cap",
          "attempts < %s" in requeue[0] and commands.MAX_COMMAND_ATTEMPTS in requeue[1])
    check("requeue is restricted to retryable command types",
          "command_type = ANY(%s)" in requeue[0]
          and list(commands.RETRYABLE_COMMAND_TYPES) in requeue[1])
    check("give-up marks the row failed with a stated reason",
          "SET status = 'failed'" in giveup[0] and "timed out" in giveup[0])
    check("both passes use the same staleness threshold",
          requeue[1][0] == commands.STALE_CLAIM_MINUTES
          and giveup[1][0] == commands.STALE_CLAIM_MINUTES)


def test_destructive_commands_are_never_silently_repeated():
    """Only a relay may be handed out again after an abandoned claim.

    Re-running a lost factory_reset / reset_wifi because its result POST went
    missing would destroy device state the operator asked for exactly once.
    """
    check("update_hiveinside is retryable",
          "update_hiveinside" in commands.RETRYABLE_COMMAND_TYPES)
    for destructive in ("factory_reset", "reset_preferences", "reset_wifi"):
        check(f"{destructive} is not retryable",
              destructive not in commands.RETRYABLE_COMMAND_TYPES)


def test_stale_threshold_clears_a_real_relay():
    """The threshold must not cut off a legitimately running transfer.

    The hub polls once per upload cycle (10 min by default) and a relay streams
    a few hundred KB over BLE synchronously, so the window has to sit clear of
    that without leaving a dead claim parked for hours.
    """
    check("threshold exceeds one default 10-minute cycle",
          commands.STALE_CLAIM_MINUTES > 10)
    check("threshold still recovers within the hour",
          commands.STALE_CLAIM_MINUTES <= 30)


def _echo_app(body: bytes):
    """Minimal ASGI app returning `body` with an explicit Content-Length."""
    async def app(scope, receive, send):
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
    return app


def _get(app, path: str):
    """Drive an ASGI app through one gzip-capable GET; return (headers, body).

    Hand-rolled rather than using starlette's TestClient because that needs
    httpx, which is not in server/requirements.txt and so is not present in CI.
    """
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "scheme": "http",
        "headers": [(b"accept-encoding", b"gzip")],
        "client": ("test", 1), "server": ("test", 80),
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return headers, body


def test_firmware_downloads_are_never_compressed():
    """A firmware download must keep its Content-Length and its exact bytes."""
    body = b"\xa5" * 8192
    app = middleware.SelectiveGZipMiddleware(_echo_app(body), minimum_size=1024)
    headers, payload = _get(app, "/firmware/hiveinside_nrf54lm20a_0.4.2.bin")
    check("firmware keeps Content-Length", headers.get("content-length") == str(len(body)))
    check("firmware is not gzipped", "content-encoding" not in headers)
    check("firmware bytes are unaltered", payload == body)


def test_other_responses_are_still_compressed():
    """The exclusion must be surgical — measurement JSON still gets gzipped."""
    body = b'{"rows":[' + b'{"weight_kg":12.345},' * 500 + b'{}]}'
    app = middleware.SelectiveGZipMiddleware(_echo_app(body), minimum_size=1024)
    headers, payload = _get(app, "/api/v1/devices/dev-1/measurements")
    check("JSON is gzipped", headers.get("content-encoding") == "gzip")
    check("JSON is actually smaller", len(payload) < len(body))


def test_dashboard_exposes_a_relay_endpoint():
    """The local dashboard must be able to queue a relay itself.

    Regression guard: uploading a HiveInside .bin only registers a release, and
    the dashboard used to have no way to start the transfer — the only callers
    were the master-key and HivePal endpoints. An operator who uploaded an image
    and waited therefore saw the node sit on its old version forever.
    """
    import local_dashboard

    routes = [
        r for r in local_dashboard.router.routes
        if r.path.endswith("/hiveinside/update")
    ]
    check("relay route is registered", len(routes) == 1)
    if not routes:
        return
    check("relay route is a POST", "POST" in routes[0].methods)
    check(
        "relay route is admin-gated",
        any(d.dependency is auth.require_dashboard_admin for d in routes[0].dependencies),
    )


def test_dashboard_relay_delegates_through_the_shared_gate():
    """The endpoint must not reimplement the release lookup or the version gate.

    Going through queue_relay_firmware_update is what makes the dashboard's 400 /
    404 / 409 behaviour identical to the other two callers'.
    """
    import local_dashboard

    endpoints = {
        "hiveinside": local_dashboard.local_queue_hiveinside_update,
        "beecounter": local_dashboard.local_queue_beecounter_update,
    }
    for t, fn in endpoints.items():
        with mock.patch.object(local_dashboard, "queue_relay_firmware_update") as queue:
            queue.return_value = {"id": 7, "status": "pending"}
            result = fn("dev-1", slot=3, force=True)

        check(f"[{t}] returns the helper's result unchanged",
              result == {"id": 7, "status": "pending"})
        check(
            f"[{t}] delegates with the target, command type, slot and force",
            queue.call_args == mock.call(
                "dev-1", t, commands.RELAY_COMMAND_TYPES[t], 3, True),
        )


def test_large_crc_survives_into_the_relay_payload():
    """A CRC above 2^31 must reach the device intact, as a JSON number.

    Regression guard for the half of all CRC-32 values with the high bit set.
    The HiveHub parsed this field with `payload["crc32"] | 0`, and ArduinoJson
    deduces the type from the default: a signed 32-bit int cannot hold
    2602123579, so it fell back to 0, told the node to expect a zero CRC, and
    the node rejected a fully transferred image with OTA_ERR_CRC. The firmware
    now uses `| 0U`; this side guards the value the server puts on the wire —
    it must stay an exact integer, never truncated, clamped or stringified.
    """
    big_crc = 2602123579  # hiveinside 0.4.2 — the image that exposed this
    check("test CRC really is above the signed-32-bit boundary", big_crc > 2**31 - 1)

    captured = {}

    def fake_create_command(device_id, payload):
        captured["payload"] = payload.payload
        return {"id": 1, "status": "pending"}

    for t, cmd in commands.RELAY_COMMAND_TYPES.items():
        captured.clear()
        with mock.patch.object(commands, "get_device_owner_id", return_value=None), \
             mock.patch.object(commands, "latest_release_for_owner",
                               return_value=("0.4.2", "image_0_4_2.bin", big_crc, None)), \
             mock.patch.object(commands, "reported_subdevice_version", return_value="0.4.0"), \
             mock.patch.object(commands, "create_command", side_effect=fake_create_command):
            commands.queue_relay_firmware_update("dev-003", t, cmd, 1)

        sent = captured.get("payload", {})
        check(f"[{t}] crc32 reaches the payload unchanged", sent.get("crc32") == big_crc)
        check(f"[{t}] crc32 is an int, not a string", isinstance(sent.get("crc32"), int))
        check(f"[{t}] the image URL is included", bool(sent.get("url")))


def test_zz_no_check_failures():
    """Fail the pytest run if any check() above failed.

    check() tallies failures and prints them, but raises nothing — so under
    pytest (how CI runs this file) every assertion here was reported green no
    matter what it found. pytest executes tests in definition order, so this
    runs last and turns the tally into a real failure. Named to sort last for
    any runner that orders alphabetically instead.
    """
    assert _failures == 0, f"{_failures} check(s) failed — see the FAIL lines above"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_failures} failure(s)")
    sys.exit(1 if _failures else 0)
