"""Insight alerts: lifecycle persistence, the background reconciler and the
HivePal insight endpoints."""

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

import psycopg
from fastapi import APIRouter, Depends, Query

from auth import require_device_role, require_hivepal_service_key, require_user_id
from config import (
    INSIGHTS_HISTORY_LOOKBACK_DAYS,
    INSIGHTS_RECONCILE_ENABLED,
    INSIGHTS_RECONCILE_INTERVAL_SECONDS,
    logger,
)
from db import get_conn
from insights import compute_insights, summarize
from measurements import MEASUREMENT_SELECT_COLUMNS, measurements_for_insights

router = APIRouter()


# ---------------------------------------------------------------------------
# Insight alert lifecycle persistence (history)
# ---------------------------------------------------------------------------

INSIGHT_SEVERITY_RANK = {"info": 1, "watch": 2, "warning": 3, "critical": 4}


# Last time each device's insight_alerts were reconciled/persisted (background
# reconciler or an opportunistic live compute). Lets the dashboard summary
# endpoint tell "reconciled recently, no active alerts" apart from "never
# reconciled", so a healthy hive is served cheaply from persisted state instead
# of paying for a live 14-day recompute on every load. See _summary_from_persisted.
#
# The in-process dict is only a fast path: the authoritative marker is persisted
# in dashboard_settings (see _RECONCILED_AT_KEY) so that a server restart — or a
# sibling worker process — does not forget that the reconciler ran and force the
# first dashboard load into the heavy live recompute.
_insights_reconciled_at: dict[str, datetime] = {}
_insights_reconciled_lock = threading.Lock()

_RECONCILED_AT_KEY = "insights_reconciled_at:{device_id}"


def _load_persisted_reconciled_at(device_id: str) -> Optional[datetime]:
    """Read the durable reconcile timestamp for a device (None when absent)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM dashboard_settings WHERE key = %s;",
                (_RECONCILED_AT_KEY.format(device_id=device_id),),
            )
            row = cur.fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except ValueError:
        return None


def persist_insights(device_id: str, alerts: list, computed_at: datetime) -> None:
    """
    Reconcile the freshly computed ``alerts`` for ``device_id`` against the
    persisted ``insight_alerts`` lifecycle table.

    * An alert that is already active (same ``alert_key``) has its latest
      snapshot refreshed and ``last_seen_at`` bumped to ``computed_at``.
    * A newly appearing alert is inserted as an active row.
    * An active row whose detector no longer fires is resolved
      (``resolved_at = computed_at``).

    Idempotent and safe to run concurrently with itself thanks to the partial
    unique index ``insight_alerts_active_uniq`` and ``ON CONFLICT``.
    """
    # ``alert.id`` is stable within a compute pass (e.g. "swarm-watch-ch1") and
    # is what we dedupe on. Guard against accidental duplicates in one pass.
    current = {alert.id: alert for alert in alerts}
    active_keys = list(current.keys())

    with get_conn() as conn:
        with conn.cursor() as cur:
            for key, alert in current.items():
                cur.execute(
                    """
                    INSERT INTO insight_alerts (
                        device_id, alert_key, category, channel, severity,
                        peak_severity, title, description, confidence, evidence,
                        source, window_start, window_end, first_seen_at, last_seen_at
                    ) VALUES (
                        %(device_id)s, %(alert_key)s, %(category)s, %(channel)s,
                        %(severity)s, %(severity)s, %(title)s, %(description)s,
                        %(confidence)s, %(evidence)s, %(source)s, %(window_start)s,
                        %(window_end)s, %(now)s, %(now)s
                    )
                    ON CONFLICT (device_id, alert_key) WHERE resolved_at IS NULL
                    DO UPDATE SET
                        category = EXCLUDED.category,
                        channel = EXCLUDED.channel,
                        severity = EXCLUDED.severity,
                        peak_severity = CASE
                            WHEN array_position(
                                     ARRAY['info', 'watch', 'warning', 'critical'],
                                     EXCLUDED.severity)
                               > array_position(
                                     ARRAY['info', 'watch', 'warning', 'critical'],
                                     insight_alerts.peak_severity)
                            THEN EXCLUDED.severity
                            ELSE insight_alerts.peak_severity
                        END,
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        confidence = EXCLUDED.confidence,
                        evidence = EXCLUDED.evidence,
                        source = EXCLUDED.source,
                        window_start = EXCLUDED.window_start,
                        window_end = EXCLUDED.window_end,
                        last_seen_at = EXCLUDED.last_seen_at,
                        update_count = insight_alerts.update_count + 1,
                        updated_at = now();
                    """,
                    {
                        "device_id": device_id,
                        "alert_key": key,
                        "category": alert.category,
                        "channel": alert.channel,
                        "severity": alert.severity,
                        "title": alert.title,
                        "description": alert.description,
                        "confidence": alert.confidence,
                        "evidence": psycopg.types.json.Jsonb(alert.evidence or {}),
                        "source": alert.source or "",
                        "window_start": alert.window_start,
                        "window_end": alert.window_end,
                        "now": computed_at,
                    },
                )

            # Resolve active alerts that are no longer firing. With an empty
            # active set, ``<> ALL(ARRAY[]::text[])`` is true for every row, so
            # all currently active alerts get resolved.
            cur.execute(
                """
                UPDATE insight_alerts
                SET resolved_at = %(now)s, updated_at = now()
                WHERE device_id = %(device_id)s
                  AND resolved_at IS NULL
                  AND alert_key <> ALL(%(active_keys)s::text[]);
                """,
                {
                    "device_id": device_id,
                    "now": computed_at,
                    "active_keys": active_keys,
                },
            )
            # Durable freshness marker, written in the same transaction as the
            # alert rows so it can never claim freshness for state that was
            # rolled back. Read by _summary_from_persisted after restarts.
            cur.execute(
                """
                INSERT INTO dashboard_settings (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now();
                """,
                (_RECONCILED_AT_KEY.format(device_id=device_id), computed_at.isoformat()),
            )
            conn.commit()
    # Mark this device's persisted insight state fresh as of ``computed_at`` so
    # the dashboard summary can be served from insight_alerts without a recompute.
    with _insights_reconciled_lock:
        _insights_reconciled_at[device_id] = computed_at


def reconcile_device_insights(
    device_id: str, lookback_days: int = INSIGHTS_HISTORY_LOOKBACK_DAYS
) -> int:
    """Compute insights for one device over the fixed lookback and persist them.

    Returns the number of alerts currently active after reconciliation.
    """
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()
    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    persist_insights(device_id, alerts, end_at)
    return len(alerts)


def reconcile_all_devices(
    lookback_days: int = INSIGHTS_HISTORY_LOOKBACK_DAYS,
) -> None:
    """Reconcile insight history for every device with recent measurements."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT device_id
                FROM measurements
                WHERE measured_at >= now() - make_interval(days => %s);
                """,
                (lookback_days,),
            )
            device_ids = [r[0] for r in cur.fetchall()]
    for device_id in device_ids:
        try:
            reconcile_device_insights(device_id, lookback_days)
        except Exception:  # one bad device must not stop the rest
            logger.exception("insight reconcile failed for device %s", device_id)


_reconcile_stop = threading.Event()
_reconcile_thread: Optional[threading.Thread] = None


def _reconcile_loop() -> None:
    # Small initial delay so startup (DB open, migrations) settles first.
    if _reconcile_stop.wait(15):
        return
    while True:
        try:
            reconcile_all_devices()
        except Exception:
            logger.exception("insight reconcile sweep failed")
        if _reconcile_stop.wait(INSIGHTS_RECONCILE_INTERVAL_SECONDS):
            return


def start_insight_reconciler() -> None:
    global _reconcile_thread
    if not INSIGHTS_RECONCILE_ENABLED:
        logger.info("insight reconciler disabled via INSIGHTS_RECONCILE_ENABLED")
        return
    if _reconcile_thread and _reconcile_thread.is_alive():
        return
    _reconcile_stop.clear()
    _reconcile_thread = threading.Thread(
        target=_reconcile_loop, name="insight-reconciler", daemon=True
    )
    _reconcile_thread.start()


def stop_insight_reconciler() -> None:
    _reconcile_stop.set()


def insight_history_row_to_dict(row: tuple) -> dict[str, Any]:
    (
        ia_id,
        alert_key,
        category,
        channel,
        severity,
        peak_severity,
        title,
        description,
        confidence,
        evidence,
        source,
        window_start,
        window_end,
        first_seen_at,
        last_seen_at,
        resolved_at,
        update_count,
    ) = row
    return {
        "id": ia_id,
        "alert_key": alert_key,
        "category": category,
        "channel": channel,
        "severity": severity,
        "peak_severity": peak_severity,
        "title": title,
        "description": description,
        "confidence": confidence,
        "evidence": evidence or {},
        "source": source or "",
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "first_seen_at": first_seen_at.isoformat() if first_seen_at else None,
        "last_seen_at": last_seen_at.isoformat() if last_seen_at else None,
        "resolved_at": resolved_at.isoformat() if resolved_at else None,
        "status": "active" if resolved_at is None else "resolved",
        "update_count": update_count,
    }


def build_insight_history(
    device_id: str,
    status: str = "all",
    category: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Query the persisted ``insight_alerts`` lifecycle table for one device.

    Shared by the HivePal app API and the local dashboard so both return an
    identical history payload (active + resolved alerts, newest first).
    """
    conditions = ["device_id = %(device_id)s"]
    params: dict[str, Any] = {"device_id": device_id, "limit": limit}
    if status == "active":
        conditions.append("resolved_at IS NULL")
    elif status == "resolved":
        conditions.append("resolved_at IS NOT NULL")
    if category:
        conditions.append("category = %(category)s")
        params["category"] = category
    if since is not None:
        conditions.append("last_seen_at >= %(since)s")
        params["since"] = since

    where_clause = " AND ".join(conditions)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, alert_key, category, channel, severity, peak_severity,
                       title, description, confidence, evidence, source,
                       window_start, window_end, first_seen_at, last_seen_at,
                       resolved_at, update_count
                FROM insight_alerts
                WHERE {where_clause}
                ORDER BY first_seen_at DESC, id DESC
                LIMIT %(limit)s;
                """,
                params,
            )
            rows = cur.fetchall()

    entries = [insight_history_row_to_dict(r) for r in rows]
    active_count = sum(1 for e in entries if e["status"] == "active")
    return {
        "device_id": device_id,
        "lookback_days": INSIGHTS_HISTORY_LOOKBACK_DAYS,
        "count": len(entries),
        "active_count": active_count,
        "alerts": entries,
    }


@router.get(
    "/api/v1/app/devices/{device_id}/insights",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights(
    device_id: str,
    lookback_days: int = Query(14, ge=1, le=90),
    user_id: str = Depends(require_user_id),
):
    """
    Compute current sensor-based alerts/insights for a device.

    See server/insights.py for the algorithms and their literature sources.
    The detectors run over the last `lookback_days` of measurements (default
    14 days, max 90). All channels (scale 1 and scale 2) are evaluated.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()

    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    return {
        "device_id": device_id,
        "computed_at": end_at.isoformat(),
        "lookback_days": lookback_days,
        "measurement_count": len(measurements),
        "alerts": [a.model_dump() for a in alerts],
    }


@router.get(
    "/api/v1/app/devices/{device_id}/insights/summary",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights_summary(
    device_id: str,
    user_id: str = Depends(require_user_id),
):
    """
    Highest-severity summary of current alerts, suitable for dashboard
    cards. Always uses the default 14-day lookback.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=14)

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()

    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    # Opportunistically keep the persisted history fresh on every summary hit,
    # in addition to the background reconciler. Never let a persistence error
    # break the read.
    try:
        persist_insights(device_id, alerts, end_at)
    except Exception:
        logger.exception("opportunistic insight persist failed for %s", device_id)
    summary = summarize(device_id, alerts, end_at)
    return {
        "device_id": summary.device_id,
        "computed_at": summary.computed_at.isoformat(),
        "alert_count": summary.alert_count,
        "highest_severity": summary.highest_severity,
        "highest_alert": (
            summary.highest_alert.model_dump() if summary.highest_alert else None
        ),
        "categories": summary.categories,
    }


@router.get(
    "/api/v1/app/devices/{device_id}/insights/history",
    dependencies=[Depends(require_hivepal_service_key)],
)
def get_device_insights_history(
    device_id: str,
    status: Literal["all", "active", "resolved"] = Query("all"),
    category: Optional[str] = Query(None),
    since: Optional[datetime] = Query(
        None, description="Only alerts last seen at or after this time (ISO 8601)"
    ),
    limit: int = Query(100, ge=1, le=500),
    user_id: str = Depends(require_user_id),
):
    """
    Persisted history of sensor-based alerts for a device.

    Unlike the live ``/insights`` endpoint (which recomputes the *current*
    state on every call), this returns the stored lifecycle of every alert the
    background reconciler has observed — including resolved ones — newest
    first. See ``persist_insights`` and ``server/insights.py``.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return build_insight_history(device_id, status, category, since, limit)


# Recomputing the 14-day insight pipeline (a full MEASUREMENT_SELECT_COLUMNS scan
# that de-TOASTs raw_json for every row, plus the Python detectors) on every
# dashboard load AND the 60s auto-refresh was the dominant load-time cost. The
# result barely changes minute-to-minute, so memoize it per device for a short
# window. The background reconciler still persists history independently.
_INSIGHTS_SUMMARY_TTL_SECONDS = 90
_insights_summary_cache: dict[str, tuple[float, dict]] = {}
_insights_summary_lock = threading.Lock()


def compute_local_insights_summary(device_id: str) -> dict:
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=14)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s AND measured_at >= %s
                ORDER BY measured_at ASC;
                """,
                (device_id, start_at),
            )
            rows = cur.fetchall()
    measurements = measurements_for_insights(rows)
    alerts = compute_insights(measurements, now=end_at)
    try:
        persist_insights(device_id, alerts, end_at)
    except Exception:
        logger.exception("opportunistic insight persist failed for %s", device_id)
    summary = summarize(device_id, alerts, end_at)
    return {
        "device_id": summary.device_id,
        "computed_at": summary.computed_at.isoformat(),
        "alert_count": summary.alert_count,
        "highest_severity": summary.highest_severity,
        "highest_alert": (
            summary.highest_alert.model_dump() if summary.highest_alert else None
        ),
        "categories": summary.categories,
    }


# Serve the dashboard summary from the reconciler-maintained ``insight_alerts``
# table when it's fresh enough, instead of re-running the heavy 14-day
# MEASUREMENT_SELECT_COLUMNS scan + Python detectors on the request path. The
# background reconciler (INSIGHTS_RECONCILE_INTERVAL_SECONDS, default 900s)
# refreshes every active device, so this window is generous enough that a healthy
# hive is always served from persisted state; beyond it we fall back to a live
# recompute so a disabled/stalled reconciler can't serve indefinitely stale cards.
_INSIGHTS_PERSISTED_MAX_AGE_SECONDS = max(INSIGHTS_RECONCILE_INTERVAL_SECONDS * 2, 1800)
_SEVERITY_RANK = {"critical": 4, "warning": 3, "watch": 2, "info": 1}


def _summary_from_persisted(device_id: str) -> Optional[dict]:
    """Build the insights summary from persisted ``insight_alerts`` (active rows).

    Returns None when there is no sufficiently fresh persisted state for the
    device, signalling the caller to fall back to a live recompute.
    """
    now = datetime.now(timezone.utc)

    def _fresh(ts: Optional[datetime]) -> bool:
        return (
            ts is not None
            and (now - ts).total_seconds() <= _INSIGHTS_PERSISTED_MAX_AGE_SECONDS
        )

    with _insights_reconciled_lock:
        reconciled_at = _insights_reconciled_at.get(device_id)
    if not _fresh(reconciled_at):
        # The in-process marker is cold (restart, or another worker did the
        # reconciling) — consult the durable one before falling back to the
        # expensive live recompute.
        persisted = _load_persisted_reconciled_at(device_id)
        if persisted is not None and (reconciled_at is None or persisted > reconciled_at):
            reconciled_at = persisted
            with _insights_reconciled_lock:
                _insights_reconciled_at[device_id] = persisted
    if not _fresh(reconciled_at):
        return None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT alert_key, category, channel, severity, title,
                       description, confidence, evidence, source,
                       window_start, window_end
                FROM insight_alerts
                WHERE device_id = %s AND resolved_at IS NULL;
                """,
                (device_id,),
            )
            rows = cur.fetchall()
    computed_at = reconciled_at.isoformat()
    if not rows:
        # Reconciled recently with nothing firing — a valid "all clear".
        return {
            "device_id": device_id,
            "computed_at": computed_at,
            "alert_count": 0,
            "highest_severity": None,
            "highest_alert": None,
            "categories": [],
        }
    alerts = [
        {
            "id": r[0],
            "category": r[1],
            "channel": r[2],
            "severity": r[3],
            "title": r[4],
            "description": r[5],
            "confidence": r[6],
            "evidence": r[7] or {},
            "source": r[8] or "",
            "window_start": r[9].isoformat() if r[9] else None,
            "window_end": r[10].isoformat() if r[10] else None,
        }
        for r in rows
    ]
    highest = max(alerts, key=lambda a: _SEVERITY_RANK.get(a["severity"], 0))
    return {
        "device_id": device_id,
        "computed_at": computed_at,
        "alert_count": len(alerts),
        "highest_severity": highest["severity"],
        "highest_alert": highest,
        "categories": sorted({a["category"] for a in alerts}),
    }
