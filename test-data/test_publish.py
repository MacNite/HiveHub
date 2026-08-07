"""Tests for "Publish data" — the public chart embeds (server/publish.py).

Run: python3 -m pytest test-data/test_publish.py
 or: PYTHONPATH=server python3 test-data/test_publish.py

Publishing punches a deliberate hole in an otherwise fully login-protected
dashboard, so the checks below are mostly about the shape of that hole:

  * the metric registry maps a metric + hive to the right measurement keys,
    including the legacy stereo microphone columns for hives 1–2;
  * point building coalesces candidate keys, drops non-numeric values and
    buckets daily aggregates the way the dashboard's daily-max chart does;
  * a publication cannot be created for an unknown device, for the same hive
    twice, or for a per-hive metric with no hive;
  * the public payload carries labels and numbers only — no device_id, no hive
    number, none of the other columns the private API returns;
  * the public routes really are public (no session dependency) while every
    management route requires a session, and writes require an admin;
  * a token that is not a token never reaches a query, and the embed page only
    serves its whitelisted assets.

config.py reads a few env vars at import time; dummy values are injected below so
the file runs with no database and no FastAPI runtime. db.py builds its pool with
open=False, so importing it does not connect.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret")

from fastapi import HTTPException  # noqa: E402

import publish  # noqa: E402
from schemas import PublishedSeriesIn  # noqa: E402


NOW = datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc)

_failures = 0


def check(name, condition):
    global _failures
    status = "ok" if condition else "FAIL"
    if not condition:
        _failures += 1
    print(f"[{status}] {name}")


def row(minutes_ago, **fields):
    """One measurement row as the chart query serializes it (newest first)."""
    return {"measured_at": NOW - timedelta(minutes=minutes_ago), **fields}


# ── metric registry ──────────────────────────────────────────────────────────


def test_metric_keys():
    check("weight prefers the temperature-compensated column",
          publish.metric_keys("weight", 3)
          == ["scale_3_weight_kg_compensated", "scale_3_weight_kg"])
    check("hive temperature reads the per-hive key",
          publish.metric_keys("hive_temp", 7) == ["hive_7_temp_c"])
    # Pre-multi-hive rows only carry the flat stereo mic columns, where left is
    # hive 1 and right is hive 2 — without the fallback those hives would plot
    # nothing on historical data.
    check("hive 1 sound falls back to the legacy left channel",
          publish.metric_keys("sound_rms", 1) == ["mic_1_rms_dbfs", "mic_left_rms_dbfs"])
    check("hive 2 sound falls back to the legacy right channel",
          publish.metric_keys("sound_rms", 2) == ["mic_2_rms_dbfs", "mic_right_rms_dbfs"])
    check("hive 5 sound has no legacy fallback (it would alias hive 2's mic)",
          publish.metric_keys("sound_rms", 5) == ["mic_5_rms_dbfs"])
    check("device metrics ignore the hive number",
          publish.metric_keys("ambient_temp", None) == ["ambient_temp_c"])

    try:
        publish.metric_keys("weight", None)
        check("a per-hive metric without a hive raises", False)
    except ValueError:
        check("a per-hive metric without a hive raises", True)


def test_every_metric_is_resolvable():
    for metric, spec in publish.METRICS.items():
        hive = None if spec["scope"] == "device" else 1
        keys = publish.metric_keys(metric, hive)
        check(f"{metric}: resolves to at least one key", bool(keys))
        check(f"{metric}: no unsubstituted placeholder left", not any("{" in k for k in keys))
        check(f"{metric}: declares label/unit/digits",
              bool(spec["label"]) and "unit" in spec and isinstance(spec["digits"], int))


# ── point building ───────────────────────────────────────────────────────────


def test_series_points_basic():
    rows = [
        row(0, scale_1_weight_kg_compensated=42.5),
        row(10, scale_1_weight_kg_compensated=42.0),
        row(20, scale_1_weight_kg_compensated=41.0),
    ]
    points = publish.series_points(rows, ["scale_1_weight_kg_compensated"])
    check("one point per reading", len(points) == 3)
    # The query returns newest first; a chart draws left to right.
    check("points come out oldest first", [p[1] for p in points] == [41.0, 42.0, 42.5])
    check("timestamps are UTC ISO-8601 with a Z", points[0][0].endswith("Z"))


def test_series_points_coalesce_and_skip():
    rows = [
        row(0, scale_1_weight_kg=10.0),                                    # fallback key
        row(10, scale_1_weight_kg_compensated=11.0, scale_1_weight_kg=99.0),  # first key wins
        row(20),                                                           # nothing to plot
        row(30, scale_1_weight_kg_compensated="12.5"),                     # numeric string
        row(40, scale_1_weight_kg_compensated="n/a"),                      # not a number
        row(50, scale_1_weight_kg_compensated=True),                       # a flag, not a value
    ]
    keys = ["scale_1_weight_kg_compensated", "scale_1_weight_kg"]
    values = [p[1] for p in publish.series_points(rows, keys)]
    check("rows with no value are skipped", len(values) == 3)
    check("the first non-null candidate key wins", 11.0 in values and 99.0 not in values)
    check("numeric strings are coerced", 12.5 in values)
    check("non-numeric and boolean values are dropped",
          all(isinstance(v, float) for v in values))


def test_series_points_daily_aggregates():
    day1 = datetime(2024, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"measured_at": day1.replace(hour=6), "v": 10.0},
        {"measured_at": day1.replace(hour=14), "v": 30.0},
        {"measured_at": day1.replace(hour=20), "v": 20.0},
        {"measured_at": day1.replace(day=2, hour=9), "v": 50.0},
    ]
    mx = publish.series_points(rows, ["v"], "daily_max")
    check("daily_max yields one point per day", len(mx) == 2)
    check("daily_max keeps the day's highest reading", mx[0][1] == 30.0)
    check("daily_max stamps the moment it happened", mx[0][0].startswith("2024-06-01T14:00"))

    mn = publish.series_points(rows, ["v"], "daily_min")
    check("daily_min keeps the day's lowest reading", mn[0][1] == 10.0)

    avg = publish.series_points(rows, ["v"], "daily_avg")
    check("daily_avg averages the day", abs(avg[0][1] - 20.0) < 1e-9)
    check("daily_avg stamps midday", avg[0][0].startswith("2024-06-01T12:00"))
    check("aggregated days come out in order", [p[0] for p in avg] == sorted(p[0] for p in avg))

    check("an empty series aggregates to nothing", publish.series_points([], ["v"], "daily_max") == [])


# ── publication validation ───────────────────────────────────────────────────


def _series(device_id="hive-01", hive=1, label="Linden"):
    return PublishedSeriesIn(device_id=device_id, hive=hive, label=label)


def test_validate_series():
    hive_spec = publish.METRICS["weight"]
    device_spec = publish.METRICS["ambient_temp"]

    with mock.patch.object(publish, "_known_device_ids", return_value={"hive-01"}):
        ok = publish._validate_series(hive_spec, [_series(hive=1), _series(hive=2, label="Oak")])
        check("valid series are normalised to plain dicts",
              ok == [{"device_id": "hive-01", "hive": 1, "label": "Linden"},
                     {"device_id": "hive-01", "hive": 2, "label": "Oak"}])

        # A device metric published "per hive" would silently plot the whole
        # collector's ambient reading under one hive's name.
        dev = publish._validate_series(device_spec, [_series(hive=2)])
        check("device metrics drop the hive number", dev[0]["hive"] is None)

        for name, spec, series in [
            ("a per-hive metric needs a hive", hive_spec, [PublishedSeriesIn(device_id="hive-01", label="X")]),
            ("the same hive cannot be published twice", hive_spec, [_series(hive=1), _series(hive=1)]),
            ("a device metric collapses to one series per device", device_spec, [_series(hive=1), _series(hive=2)]),
        ]:
            try:
                publish._validate_series(spec, series)
                check(name, False)
            except HTTPException as exc:
                check(name, exc.status_code == 400)

    with mock.patch.object(publish, "_known_device_ids", return_value=set()):
        try:
            publish._validate_series(hive_spec, [_series()])
            check("an unknown device is rejected", False)
        except HTTPException as exc:
            check("an unknown device is rejected", exc.status_code == 404)


# ── public payload ───────────────────────────────────────────────────────────


CHART = {
    "id": 1,
    "token": "tok_abcdefgh",
    "title": "Apiary weight",
    "subtitle": "Bee club Musterstadt",
    "metric": "weight",
    "chart_type": "line",
    "aggregate": "none",
    "series": [
        {"device_id": "hive-01", "hive": 1, "label": "Linden"},
        {"device_id": "hive-02", "hive": 1, "label": "Orchard"},
    ],
    "range_days": 30,
    "options": {"theme": "dark", "height": 400, "show_legend": True, "show_updated": True},
    "enabled": True,
}


def _payload():
    per_device = {
        "hive-01": [row(0, scale_1_weight_kg_compensated=42.0),
                    row(60, scale_1_weight_kg_compensated=40.0)],
        "hive-02": [row(0, scale_1_weight_kg_compensated=31.0),
                    row(60, scale_1_weight_kg_compensated=33.0)],
    }
    with mock.patch.object(publish, "_measurements_for", return_value=per_device):
        return publish.build_public_payload(CHART)


def test_public_payload_shape():
    p = _payload()
    check("the title travels with the payload", p["title"] == "Apiary weight")
    check("the metric's unit and precision are resolved server-side",
          p["unit"] == "kg" and p["digits"] == 2)
    check("presentation options are passed through",
          p["theme"] == "dark" and p["height"] == 400)
    check("one output series per published series", len(p["series"]) == 2)
    check("series get distinct palette colours",
          p["series"][0]["color"] != p["series"][1]["color"])
    check("the latest reading is summarised", p["series"][0]["latest"]["value"] == 42.0)
    check("the change over the period is computed", p["series"][0]["change"] == 2.0)
    check("a falling series reports a negative change", p["series"][1]["change"] == -2.0)
    check("updated_at is the newest point across all series", p["updated_at"] is not None)


def test_public_payload_leaks_nothing():
    """The public payload must not disclose the fleet behind the chart."""
    blob = json.dumps(_payload())
    check("no device_id key in the public payload", "device_id" not in blob)
    check("no device id value in the public payload", "hive-01" not in blob and "hive-02" not in blob)
    check("no hive numbers in the public payload", '"hive"' not in blob)
    # Everything the chart query fetches (battery, firmware, RSSI, …) stays put.
    for key in ("battery", "firmware", "rssi", "raw_json", "measurement_id", "config_version"):
        check(f"no {key} field in the public payload", key not in blob)


def test_public_payload_unknown_metric():
    chart = {**CHART, "metric": "retired_metric"}
    try:
        publish.build_public_payload(chart)
        check("a publication whose metric was removed fails cleanly", False)
    except HTTPException as exc:
        check("a publication whose metric was removed fails cleanly", exc.status_code == 410)


# ── route gating ─────────────────────────────────────────────────────────────


def _dependency_names(route):
    """Every dependency callable a route runs, by name (recursively)."""
    names = set()

    def walk(dependant):
        for dep in dependant.dependencies:
            if dep.call is not None:
                names.add(getattr(dep.call, "__name__", str(dep.call)))
            walk(dep)

    walk(route.dependant)
    return names


def _route(path, method="GET"):
    for r in publish.router.routes:
        if r.path == path and method in r.methods:
            return r
    return None


def test_public_routes_are_public():
    for path in ["/api/v1/public/charts/{token}",
                 "/api/v1/public/charts/{token}.csv",
                 "/embed/chart/{token}",
                 "/embed/assets/{filename}"]:
        r = _route(path)
        check(f"{path} is registered", r is not None)
        if r is None:
            continue
        deps = _dependency_names(r)
        check(f"{path} is gated on the publishing switch", "require_publishing" in deps)
        check(f"{path} needs no login",
              not {"require_dashboard_session", "require_dashboard_admin"} & deps)


def test_management_routes_need_a_login():
    reads = ["/api/v1/local/publish/metrics", "/api/v1/local/publish/charts"]
    writes = [("/api/v1/local/publish/charts", "POST"),
              ("/api/v1/local/publish/charts/{chart_id}", "PATCH"),
              ("/api/v1/local/publish/charts/{chart_id}", "DELETE")]
    for path in reads:
        deps = _dependency_names(_route(path))
        check(f"GET {path} requires a session",
              {"require_dashboard_session", "require_dashboard_admin"} & deps)
        check(f"GET {path} is gated on the publishing switch", "require_publishing" in deps)
    for path, method in writes:
        deps = _dependency_names(_route(path, method))
        check(f"{method} {path} requires an admin", "require_dashboard_admin" in deps)
        check(f"{method} {path} is gated on the publishing switch", "require_publishing" in deps)


def test_dashboard_advertises_the_feature():
    """The dashboard hides its Publish panel unless the server says publishing is
    on, so the flag has to ride along with the auth handshake."""
    import local_dashboard

    with mock.patch.object(local_dashboard, "ENABLE_PUBLIC_EMBEDS", True):
        check("auth handshake reports publishing when it is on",
              local_dashboard.dashboard_features() == {"publish": True})
    with mock.patch.object(local_dashboard, "ENABLE_PUBLIC_EMBEDS", False):
        check("auth handshake reports publishing when it is off",
              local_dashboard.dashboard_features() == {"publish": False})


def test_publishing_switch():
    with mock.patch.object(publish, "ENABLE_PUBLIC_EMBEDS", False):
        try:
            publish.require_publishing()
            check("ENABLE_PUBLIC_EMBEDS=false closes the feature", False)
        except HTTPException as exc:
            # 404, not 403: on a server that does not publish, these routes
            # should look like routes that do not exist.
            check("ENABLE_PUBLIC_EMBEDS=false closes the feature", exc.status_code == 404)
    with mock.patch.object(publish, "ENABLE_LOCAL_DASHBOARD", False):
        try:
            publish.require_publishing()
            check("publishing requires the local dashboard", False)
        except HTTPException as exc:
            check("publishing requires the local dashboard", exc.status_code == 404)


# ── tokens and assets ────────────────────────────────────────────────────────


def test_token_shape():
    check("minted tokens match the accepted pattern",
          all(publish.TOKEN_RE.match(publish._new_token()) for _ in range(20)))
    check("minted tokens are unique",
          len({publish._new_token() for _ in range(50)}) == 50)
    for bad in ["../../etc/passwd", "tok with space", "short", "a" * 65, "tok/../x", ""]:
        check(f"rejected as a token: {bad!r}", not publish.TOKEN_RE.match(bad))


def test_bad_token_never_reaches_a_query():
    # get_conn would raise on a real connection attempt; the guard must fire first.
    with mock.patch.object(publish, "get_conn", side_effect=AssertionError("queried the DB")):
        try:
            publish._load_public_chart("../../etc/passwd", count_view=False)
            check("a malformed token 404s before any query", False)
        except HTTPException as exc:
            check("a malformed token 404s before any query", exc.status_code == 404)


def test_embed_assets():
    for name, path in publish.EMBED_ASSETS.items():
        check(f"embed asset exists: {name}", path.is_file())
    check("the embed page reuses the dashboard's chart code, rather than a copy",
          publish.EMBED_ASSETS["charts.js"].name == "charts.js"
          and "dashboard" in publish.EMBED_ASSETS["charts.js"].parts)
    check("nothing else from the dashboard is reachable",
          set(publish.EMBED_ASSETS) == {"embed.js", "embed.css", "charts.js"})

    shell = (publish.EMBED_DIR / "embed.html").read_text(encoding="utf-8")
    check("the embed shell has both placeholders",
          "{{TOKEN}}" in shell and "{{TITLE}}" in shell)
    check("the embed shell ships no data of its own", "points" not in shell)


def test_title_escaping():
    check("a title cannot break out of the <title> element",
          publish._escape('<script>"x"</script>')
          == "&lt;script&gt;&quot;x&quot;&lt;/script&gt;")


def test_zz_no_check_failures():
    """Fail the pytest run if any check() above failed.

    check() tallies failures and prints them, but raises nothing — so under
    pytest (how CI runs this file) every assertion here would be reported green
    no matter what it found. pytest executes tests in definition order, so this
    runs last and turns the tally into a real failure.
    """
    assert _failures == 0, f"{_failures} check(s) failed — see the FAIL lines above"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{_failures} failure(s)")
    sys.exit(1 if _failures else 0)
