"""MQTT gets the same derived bee-counter intervals as the read APIs (audit F03).

Run: python3 -m pytest test-data/test_mqtt_counter_intervals.py
 or: PYTHONPATH=server python3 test-data/test_mqtt_counter_intervals.py

The HiveTraffic counter reports lifetime totals only. Home Assistant discovery
advertises bee_counter_N_interval_in/out entities, but the live payload never
carried an interval and the MQTT flattener only copies one that is present, so
those entities never received a value while the dashboard showed one. The
interval is now derived at publish time from the previous STORED reading of
the same counter (by measured_at, not arrival order) through the read APIs'
own differencing.

No database or broker: the baseline query is replaced with a scripted cursor
and the paho client with a recorder.
"""

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key-0123456789abcdef")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

import measurements  # noqa: E402
import mqtt_publisher  # noqa: E402

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_failures = 0


def check(name, condition):
    global _failures
    if not condition:
        _failures += 1
    print(f"[{'ok' if condition else 'FAIL'}] {name}")


def base(at, **bc):
    return {"measured_at": at, "bee_counter": dict(bc, ok=True)}


def counter(state, n):
    return next(h for h in state["hives"] if h["index"] == n)["bee_counter"]


class _Cursor:
    """Answers the baseline query, then the newer-reading query."""

    def __init__(self, baseline_rows, newer_rows):
        self._results = [baseline_rows, newer_rows]
        self.params = []

    def execute(self, sql, params):
        self.params.append(params)
        self._rows = self._results.pop(0)

    def fetchall(self):
        return self._rows


def fake_conn(cursor):
    @contextmanager
    def get_conn():
        class _Conn:
            @contextmanager
            def cursor(self):
                yield cursor
        yield _Conn()
    return get_conn


class _Recorder:
    def __init__(self):
        self.messages = []

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.messages.append((topic, payload))


# ── the pure derivation ──────────────────────────────────────────────────────
state = {"device_id": "hub-1", "measured_at": T0, "hives": [
    {"index": 1, "bee_counter": {"ok": True, "total_in": 110, "total_out": 205,
                                 "uptime_s": 10_600, "mac": "AA"}},
    {"index": 3, "bee_counter": {"ok": True, "total_in": 50, "total_out": 40,
                                 "uptime_s": 3000, "mac": "CC"}},
]}
derived = measurements.publish_intervals(state, {
    1: base(T0 - timedelta(minutes=10), total_in=100, total_out=200, uptime_s=10_000, mac="AA"),
    3: base(T0 - timedelta(minutes=40), total_in=20, total_out=10, uptime_s=600, mac="CC"),
}, newer=set())
check("totals 100 -> 110 publish an interval of 10", derived.get(1, {}).get("interval_in") == 10)
check("and 200 -> 205 out", derived.get(1, {}).get("interval_out") == 5)
# Hive 3 was skipped by the read rotation for three cycles: its baseline is the
# last reading that HAD it, 40 minutes back, not the previous upload.
check("a rotated counter is differenced against its own last reading",
      derived.get(3, {}).get("interval_in") == 30)

restart = measurements.publish_intervals(
    {"device_id": "hub-1", "measured_at": T0, "hives": [
        {"index": 1, "bee_counter": {"ok": True, "total_in": 150, "total_out": 150,
                                     "uptime_s": 60, "mac": "AA"}}]},
    {1: base(T0 - timedelta(minutes=10), total_in=100, total_out=100,
             uptime_s=10_000, mac="AA")}, newer=set())
check("a restart is read from uptime, exactly as in the read API",
      restart.get(1, {}).get("interval_in") == 150)

swapped = measurements.publish_intervals(
    {"device_id": "hub-1", "measured_at": T0, "hives": [
        {"index": 1, "bee_counter": {"ok": True, "total_in": 5000, "mac": "BB"}}]},
    {1: base(T0 - timedelta(minutes=10), total_in=100, mac="AA")}, newer=set())
check("a different counter has no baseline", swapped == {})

late = measurements.publish_intervals(state, {
    1: base(T0 - timedelta(minutes=10), total_in=100, total_out=200, uptime_s=10_000, mac="AA"),
}, newer={1})
check("a late replay whose span is already in a newer reading publishes no interval",
      late == {})

check("no baseline yet publishes nothing",
      measurements.publish_intervals(state, {}, newer=set()) == {})

maint = measurements.publish_intervals(
    {"device_id": "hub-1", "measured_at": T0, "hives": [
        {"index": 1, "bee_counter": {"ok": True, "total_in": 100, "maint_s": 300,
                                     "uptime_s": 10_600}}]},
    {1: base(T0 - timedelta(minutes=10), total_in=100, maint_s=0, uptime_s=10_000)},
    newer=set())
check("maintenance seconds are derived alongside",
      maint.get(1) == {"interval_in": 0, "interval_maint_s": 300})

# ── against stored history, through the database helper ──────────────────────
cur = _Cursor(
    baseline_rows=[(1, T0 - timedelta(minutes=10), 100, 200,
                    json.dumps({"index": 1, "bee_counter": {"ok": True, "uptime_s": 10_000,
                                                            "mac": "AA"}}))],
    newer_rows=[],
)
live = {"device_id": "hub-1", "measured_at": T0, "hives": [
    {"index": 1, "bee_counter": {"ok": True, "total_in": 110, "total_out": 205,
                                 "uptime_s": 10_600, "mac": "AA"}},
    {"index": 2, "bee_counter": {"ok": False, "polled": False, "mac": "BB"}},
]}
with mock.patch.object(measurements, "get_conn", fake_conn(cur)):
    measurements.attach_publish_intervals(live)
check("the stored baseline gives the live reading its interval",
      (counter(live, 1).get("interval_in"), counter(live, 1).get("interval_out")) == (10, 5))
check("only counters that answered are looked up", cur.params[0][1] == [1])
check("the baseline is strictly earlier, keyed on measured_at", cur.params[0][2] == T0)
check("a skipped counter gets no interval", "interval_in" not in counter(live, 2))


@contextmanager
def broken_conn():
    raise RuntimeError("database unavailable")
    yield  # pragma: no cover


quiet = {"device_id": "hub-1", "measured_at": T0, "hives": [
    {"index": 1, "bee_counter": {"ok": True, "total_in": 110}}]}
with mock.patch.object(measurements, "get_conn", broken_conn):
    measurements.attach_publish_intervals(quiet)
check("a failed lookup leaves the reading as it was (fail-soft)",
      "interval_in" not in counter(quiet, 1))

# ── and the state message Home Assistant receives ────────────────────────────
pub = mqtt_publisher.MqttPublisher()
pub._client = _Recorder()
pub.publish_measurement("hub-1", live, T0, "hub-1")
states = [json.loads(p) for t, p in pub._client.messages if t.endswith("/state")]
check("the state message carries the advertised interval entities",
      states and states[-1].get("bee_counter_1_interval_in") == 10
      and states[-1].get("bee_counter_1_interval_out") == 5)


def test_mqtt_counter_intervals():
    assert _failures == 0, f"{_failures} check(s) failed"


if __name__ == "__main__":
    print(f"\n{_failures} failure(s)")
    sys.exit(1 if _failures else 0)
