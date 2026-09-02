"""Hive inspections — the window while a beekeeper has the hive open.

An inspection is stored as an interval (``device_inspections``), never as a flag
on individual readings. That one decision is what makes the rest simple:

* **Nothing is deleted or nulled in the database.** Every reading the hub sent is
  stored exactly as it arrived and still comes out of the CSV export. An
  inspection hides data from the *interpreters* — charts, insights, alert rules
  — it does not destroy it.
* **The read path only has to know the intervals.** ``mask_inspection_readings``
  blanks the hive-specific fields of any reading that falls inside one, so the
  chart, the insight engine and the app endpoints all inherit the behaviour from
  a single place instead of each learning to filter.
* **The chart can draw it.** A start and an end is exactly what a shaded band
  needs; a per-row boolean would have to be re-derived into one.

Three things open and close inspections: the hub's external button (learned from
the ``inspection`` flag on each upload), an API call (HivePal's in-app button,
the dashboard), and the timeout that ends one nobody switched off.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_api_key, require_device_role, require_hivepal_service_key, require_user_id
from config import logger
from db import get_conn
from devices import ensure_device_config, fetch_device_config
from schemas import (
    MAX_HIVES,
    DeviceCommandIn,
    DeviceInspection,
    DeviceInspectionStatus,
    InspectionStartIn,
    InspectionStopIn,
    InspectionUpdateIn,
)

router = APIRouter()


INSPECTION_SELECT = """
    id, device_id, hive_indexes, started_at, ended_at, source, end_reason,
    requested_at, acknowledged_at, note, created_by
"""

# Reading keys that describe the HUB rather than a hive, and therefore survive an
# inspection untouched. They are the answer to "was the hub alive and well while
# the hive was open?", which stays a useful question — and a gap in the ambient
# trace really would mean a fault, so blanking these would be a lie.
HUB_LEVEL_PREFIXES = (
    "ambient_",
    "battery_",
    "solar_",
)
HUB_LEVEL_KEYS = frozenset(
    {
        "id",
        "device_id",
        "measurement_id",
        "measured_at",
        "created_at",
        "timestamp",
        "network_transport",
        "rssi_dbm",
        "firmware_version",
        "config_version",
        "boot_count",
        "time_source",
        "sd_ok",
        "rtc_ok",
        "sht_ok",
        "sht_detected",
        "calibration_mode",
        "hive_count",
        "inspection",
        "inspection_hives",
        "inspection_id",
    }
)


def _row_to_inspection(r) -> DeviceInspection:
    hives = list(r[2]) if r[2] else None
    return DeviceInspection(
        id=r[0],
        device_id=r[1],
        hives=hives,
        started_at=r[3],
        ended_at=r[4],
        active=r[4] is None,
        source=r[5],
        end_reason=r[6],
        requested_at=r[7],
        acknowledged_at=r[8],
        note=r[9],
        created_by=r[10],
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat a naive timestamp as UTC.

    Clients send both shapes and the columns are TIMESTAMPTZ; comparing a naive
    request timestamp against an aware stored one raises rather than misbehaving,
    which is a poor way to find out a phone omitted its offset.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Reading and writing inspections ────────────────────────────────────────────


def get_open_inspection(device_id: str) -> Optional[DeviceInspection]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {INSPECTION_SELECT} FROM device_inspections "
                "WHERE device_id = %s AND ended_at IS NULL "
                "ORDER BY started_at DESC LIMIT 1;",
                (device_id,),
            )
            r = cur.fetchone()
    return _row_to_inspection(r) if r else None


def open_inspection(
    device_id: str,
    *,
    hives: Optional[list[int]] = None,
    note: Optional[str] = None,
    started_at: Optional[datetime] = None,
    source: str = "device",
    created_by: Optional[str] = None,
    requested_at: Optional[datetime] = None,
    acknowledged_at: Optional[datetime] = None,
) -> DeviceInspection:
    """Open an inspection, or return the one already open.

    Idempotent on purpose. Every caller here is a toggle of some kind — a button
    press, an app button, an upload reporting the flag it has been reporting for
    the last three cycles — and "start" arriving twice must not nest two windows,
    which the partial unique index would refuse anyway.
    """
    ensure_device_config(device_id)
    existing = get_open_inspection(device_id)
    if existing is not None:
        # A device confirming an inspection the API requested is the one update
        # worth making to an already-open row: it is what turns "requested" into
        # "the hub is actually doing it" for the status endpoint.
        if acknowledged_at is not None and existing.acknowledged_at is None:
            return acknowledge_inspection(existing.id, acknowledged_at)
        return existing

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO device_inspections
                    (device_id, hive_indexes, started_at, source, note, created_by,
                     requested_at, acknowledged_at)
                VALUES (%s, %s, COALESCE(%s, now()), %s, %s, %s, %s, %s)
                RETURNING {INSPECTION_SELECT};
                """,
                (
                    device_id,
                    hives or None,
                    _aware(started_at),
                    source,
                    note,
                    created_by,
                    _aware(requested_at),
                    _aware(acknowledged_at),
                ),
            )
            r = cur.fetchone()
            conn.commit()
    logger.info("Inspection opened for %s (source=%s, hives=%s)", device_id, source, hives)
    return _row_to_inspection(r)


def acknowledge_inspection(inspection_id: int, at: Optional[datetime] = None) -> DeviceInspection:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE device_inspections
                SET acknowledged_at = COALESCE(acknowledged_at, %s), updated_at = now()
                WHERE id = %s
                RETURNING {INSPECTION_SELECT};
                """,
                (_aware(at) or _now(), inspection_id),
            )
            r = cur.fetchone()
            conn.commit()
    return _row_to_inspection(r)


def close_inspection(
    device_id: str,
    *,
    ended_at: Optional[datetime] = None,
    end_reason: str = "device",
    note: Optional[str] = None,
) -> Optional[DeviceInspection]:
    """Close the open inspection, if there is one. Returns None if there was not.

    ``ended_at`` is clamped to be at or after ``started_at``: a back-dated stop
    from an app whose clock is behind the hub's would otherwise store an interval
    that ends before it begins, which every overlap test then quietly ignores.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE device_inspections
                SET ended_at = GREATEST(started_at, COALESCE(%s, now())),
                    end_reason = %s,
                    note = COALESCE(%s, note),
                    updated_at = now()
                WHERE device_id = %s AND ended_at IS NULL
                RETURNING {INSPECTION_SELECT};
                """,
                (_aware(ended_at), end_reason, note, device_id),
            )
            r = cur.fetchone()
            conn.commit()
    if r is None:
        return None
    logger.info("Inspection closed for %s (reason=%s)", device_id, end_reason)
    return _row_to_inspection(r)


def update_inspection(device_id: str, inspection_id: int, note: Optional[str]) -> DeviceInspection:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE device_inspections
                SET note = %s, updated_at = now()
                WHERE id = %s AND device_id = %s
                RETURNING {INSPECTION_SELECT};
                """,
                (note, inspection_id, device_id),
            )
            r = cur.fetchone()
            conn.commit()
    if r is None:
        raise HTTPException(status_code=404, detail="Inspection not found for this device")
    return _row_to_inspection(r)


def list_inspections(
    device_ids: Iterable[str],
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    limit: int = 500,
) -> list[DeviceInspection]:
    """Inspections OVERLAPPING [start_at, end_at] for these devices.

    Overlap, not containment: the inspection that matters most to a chart is
    usually the one still open at the right-hand edge, and one that started
    before the window is exactly the one explaining the step at its left edge.
    """
    ids = [d for d in device_ids if d]
    if not ids:
        return []
    where = ["device_id = ANY(%s)"]
    params: list = [ids]
    if end_at is not None:
        where.append("started_at <= %s")
        params.append(_aware(end_at))
    if start_at is not None:
        where.append("(ended_at IS NULL OR ended_at >= %s)")
        params.append(_aware(start_at))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {INSPECTION_SELECT} FROM device_inspections "
                f"WHERE {' AND '.join(where)} ORDER BY started_at DESC LIMIT %s;",
                [*params, max(1, min(limit, 2000))],
            )
            rows = cur.fetchall()
    return [_row_to_inspection(r) for r in rows]


def expire_timed_out_inspections(device_id: Optional[str] = None) -> int:
    """Close inspections that have run past their device's timeout.

    The hub ends its own inspections too (firmware/src/inspection.cpp), and in
    the normal case it gets there first. This is the backstop for the cases it
    cannot cover: a hub that lost power mid-inspection and never came back, or
    one whose inspection was started through the API and which has not been
    heard from since. Without it those rows stay open forever and keep masking
    readings from a hive nobody is standing at.

    Closing the row is only half of it: a hub that is still flagging its uploads
    would re-open the window on its very next one, so each device whose window
    this closes is also sent a ``stop_inspection``. That is what makes the two
    ends agree rather than hope — and it reaches a hub that has been offline for
    a week the moment it comes back.

    Cheap and idempotent — called on the read paths rather than from a
    background job, exactly like expire_stale_claimed_commands().
    """
    where = ["i.ended_at IS NULL"]
    params: list = []
    if device_id:
        where.append("i.device_id = %s")
        params.append(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE device_inspections i
                SET ended_at = i.started_at
                        + (COALESCE(c.inspection_timeout_minutes, 60) || ' minutes')::interval,
                    end_reason = 'timeout',
                    updated_at = now()
                FROM device_configs c
                WHERE c.device_id = i.device_id
                  AND {' AND '.join(where)}
                  AND now() >= i.started_at
                        + (COALESCE(c.inspection_timeout_minutes, 60) || ' minutes')::interval
                RETURNING i.device_id;
                """,
                params,
            )
            closed = [r[0] for r in cur.fetchall()]
            conn.commit()
    for closed_device in set(closed):
        logger.info("Auto-ended inspection for %s past its timeout", closed_device)
        _queue_command(closed_device, "stop_inspection")
    return len(closed)


# ── The hub's own view: the `inspection` flag on each upload ───────────────────


def sync_from_measurement(
    device_id: str,
    flag: Optional[bool],
    measured_at: datetime,
    started_at_epoch: Optional[int] = None,
) -> None:
    """Reconcile the stored inspection window with what an upload just reported.

    The hub is the authority on its own button, so a run of ``inspection: true``
    uploads opens a window and the first ``false`` closes it. A hub too old to
    send the field at all sends None, which must change nothing: an inspection
    started from the dashboard against an old hub is still a real inspection, and
    treating "field absent" as "false" would close it on the next upload.
    """
    if flag is None:
        return
    open_row = get_open_inspection(device_id)
    if flag:
        if open_row is None:
            # Back-date to the press when the hub knew when that was. On a
            # ten-minute send interval the difference is up to ten minutes of
            # un-shaded spike right where the chart most needs the shading.
            started = measured_at
            if started_at_epoch:
                try:
                    reported = datetime.fromtimestamp(started_at_epoch, tz=timezone.utc)
                    # Only trust it if it is in the past and not absurdly so —
                    # a hub with a bad clock should not open a window in 2038.
                    if _aware(measured_at) - timedelta(days=1) <= reported <= _aware(measured_at):
                        started = reported
                except (OverflowError, OSError, ValueError):
                    pass
            open_inspection(
                device_id,
                started_at=started,
                source="device",
                acknowledged_at=measured_at,
            )
        elif open_row.acknowledged_at is None:
            # The hub has picked up an inspection the API requested.
            acknowledge_inspection(open_row.id, measured_at)
    elif open_row is not None:
        close_inspection(device_id, ended_at=measured_at, end_reason="device")


# ── Read-path masking ──────────────────────────────────────────────────────────


# The wired INMP441 stereo mic pair predates per-hive keys: its readings are
# named mic_left_* / mic_right_* rather than mic_1_* / mic_2_*, and left/right
# are hives 1 and 2. Without this they would be the one hive-specific family an
# inspection failed to mask.
LEGACY_STEREO_PREFIXES = {"mic_left_": 1, "mic_right_": 2}


def _hive_index_for_key(key: str) -> Optional[int]:
    """The hive a flat reading key belongs to, or None for a hub-level key.

    Every per-hive key in the read payload is ``<something>_<n>_<field>`` —
    ``scale_3_weight_kg``, ``hive_1_temp_c``, ``bee_counter_2_total_in``,
    ``hiveheart_4_energy``. Rather than enumerate the (long, and still growing)
    list of prefixes, find the first numeric path segment and treat it as the
    hive index, with the hub-level names above excluded first.
    """
    if key in HUB_LEVEL_KEYS or key.startswith(HUB_LEVEL_PREFIXES):
        return None
    for prefix, n in LEGACY_STEREO_PREFIXES.items():
        if key.startswith(prefix):
            return n
    for part in key.split("_"):
        if part.isdigit():
            n = int(part)
            return n if 1 <= n <= MAX_HIVES else None
    return None


def _mask_row(m: dict, hives: Optional[set[int]]) -> None:
    """Blank the hive-specific fields of one reading. ``hives`` None = all hives."""
    for key in list(m.keys()):
        if key == "hives":
            continue
        n = _hive_index_for_key(key)
        if n is not None and (hives is None or n in hives):
            m[key] = None
    if isinstance(m.get("hives"), list):
        m["hives"] = [
            h for h in m["hives"]
            if not (hives is None or h.get("index") in hives)
        ]


def mask_inspection_readings(measurements: list[dict]) -> list[dict]:
    """Blank hive readings that fall inside an inspection, and mark them.

    The single choke point every read path goes through, so charts, the app API,
    the insight engine and the alert rules all inherit the same behaviour: a
    reading taken with the hive open reports the hub's own sensors and nothing
    else, and carries ``inspection: true`` so the dashboard can say why rather
    than showing an unexplained gap.

    Deliberately NOT applied to the CSV/NDJSON export or to raw_json: the raw
    numbers stay available to anyone who deliberately goes looking for them.
    """
    if not measurements:
        return measurements
    device_ids = {m.get("device_id") for m in measurements if m.get("device_id")}
    if not device_ids:
        return measurements

    times = [_aware(m["measured_at"]) for m in measurements if m.get("measured_at")]
    if not times:
        return measurements
    windows = list_inspections(device_ids, start_at=min(times), end_at=max(times))
    # An OPEN window masks everything after its start, so a hub that died
    # mid-inspection and never came back would blank its own hive forever. The
    # timeout sweep is what closes those, and it runs here rather than on every
    # read: the extra pass costs two queries and only happens while a window is
    # actually open, which is the one case that can go wrong.
    if any(w.ended_at is None for w in windows):
        for device_id in device_ids:
            expire_timed_out_inspections(device_id)
        windows = list_inspections(device_ids, start_at=min(times), end_at=max(times))
    if not windows:
        return measurements

    by_device: dict[str, list[DeviceInspection]] = {}
    for w in windows:
        # An API request is only pending until the sleeping hub reports that it
        # entered inspection mode.  Do not hide measurements that the device
        # took before that acknowledgement.
        if w.acknowledged_at is None:
            continue
        by_device.setdefault(w.device_id, []).append(w)

    for m in measurements:
        at = _aware(m.get("measured_at"))
        if at is None:
            continue
        for w in by_device.get(m.get("device_id"), ()):
            if at < _aware(w.started_at):
                continue
            if w.ended_at is not None and at > _aware(w.ended_at):
                continue
            m["inspection"] = True
            m["inspection_id"] = w.id
            m["inspection_hives"] = w.hives
            _mask_row(m, set(w.hives) if w.hives else None)
            break
        else:
            m.setdefault("inspection", False)
    return measurements


def mqtt_inspection_state(
    device_id: str, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Return the inspection fields carried by the retained MQTT state.

    Pending is deliberately distinct from active: an API request can sit in the
    command queue while a hub sleeps, and consumers must not infer that the
    physical hive is open until the device acknowledges it.
    """
    expire_timed_out_inspections(device_id)
    inspection = get_open_inspection(device_id)
    if inspection is None:
        return {
            "inspection_state": "off",
            "inspection_active": False,
            "inspection_remaining_seconds": 0,
        }
    if inspection.acknowledged_at is None:
        return {
            "inspection_state": "pending",
            "inspection_active": False,
            "inspection_remaining_seconds": 0,
        }

    config = fetch_device_config(device_id)
    deadline = _aware(inspection.started_at) + timedelta(
        minutes=config.inspection_timeout_minutes
    )
    remaining = max(0, int((deadline - (_aware(now) or _now())).total_seconds()))
    return {
        "inspection_state": "active",
        "inspection_active": True,
        "inspection_remaining_seconds": remaining,
    }


# ── HTTP API ───────────────────────────────────────────────────────────────────


def _queue_command(device_id: str, command_type: str) -> None:
    """Queue the matching command so the hub agrees with the record.

    Imported lazily: commands.py imports devices.py which imports schemas.py, and
    a module-level import here would close the loop through main.py's router
    registration.
    """
    from commands import create_command

    create_command(device_id, DeviceCommandIn(command_type=command_type, payload={}))


def start_inspection_for_device(
    device_id: str,
    body: InspectionStartIn,
    *,
    source: str,
    created_by: Optional[str] = None,
) -> DeviceInspection:
    now = _now()
    # Asked before opening, because "did this call open the window?" is what
    # decides whether a command is queued — and open_inspection() deliberately
    # returns the existing row rather than distinguishing itself from it.
    already_open = get_open_inspection(device_id) is not None
    inspection = open_inspection(
        device_id,
        hives=body.hives,
        note=body.note,
        started_at=_aware(body.started_at) or now,
        source=source,
        created_by=created_by,
        requested_at=now,
    )
    # Re-queueing for an already-open inspection would leave a second
    # start_inspection in the queue for the hub to run after the beekeeper has
    # closed the hive — which is how a hive goes quiet for an hour for no
    # visible reason.
    if not already_open:
        _queue_command(device_id, "start_inspection")
    return inspection


def stop_inspection_for_device(
    device_id: str,
    body: InspectionStopIn,
    *,
    source: str,
) -> Optional[DeviceInspection]:
    closed = close_inspection(
        device_id,
        ended_at=_aware(body.ended_at),
        end_reason=source,
        note=body.note,
    )
    # Queue the stop even when no window was open: the hub may be mid-inspection
    # with a record that a timeout already closed, and leaving it flagged is the
    # one failure mode that silently blanks a hive.
    _queue_command(device_id, "stop_inspection")
    return closed


def inspection_status(device_id: str) -> DeviceInspectionStatus:
    expire_timed_out_inspections(device_id)
    inspection = get_open_inspection(device_id)
    config = fetch_device_config(device_id)
    return DeviceInspectionStatus(
        device_id=device_id,
        active=inspection is not None and inspection.acknowledged_at is not None,
        # Requested but not yet picked up. The hub deep-sleeps between cycles, so
        # this state can last a whole send interval and a caller that renders it
        # as "on" is showing something that has not happened yet.
        pending=inspection is not None and inspection.acknowledged_at is None,
        inspection=inspection,
        timeout_minutes=config.inspection_timeout_minutes,
    )


@router.post(
    "/api/v1/devices/{device_id}/inspections/start",
    response_model=DeviceInspection,
    dependencies=[Depends(require_api_key)],
)
def api_start_inspection(device_id: str, body: InspectionStartIn):
    return start_inspection_for_device(device_id, body, source="api")


@router.post(
    "/api/v1/devices/{device_id}/inspections/stop",
    response_model=Optional[DeviceInspection],
    dependencies=[Depends(require_api_key)],
)
def api_stop_inspection(device_id: str, body: InspectionStopIn):
    return stop_inspection_for_device(device_id, body, source="api")


@router.get(
    "/api/v1/devices/{device_id}/inspections/status",
    response_model=DeviceInspectionStatus,
    dependencies=[Depends(require_api_key)],
)
def api_inspection_status(device_id: str):
    return inspection_status(device_id)


@router.get(
    "/api/v1/devices/{device_id}/inspections",
    response_model=list[DeviceInspection],
    dependencies=[Depends(require_api_key)],
)
def api_list_inspections(
    device_id: str,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    limit: int = Query(default=200, ge=1, le=2000),
):
    expire_timed_out_inspections(device_id)
    return list_inspections([device_id], start_at, end_at, limit)


@router.patch(
    "/api/v1/devices/{device_id}/inspections/{inspection_id}",
    response_model=DeviceInspection,
    dependencies=[Depends(require_api_key)],
)
def api_update_inspection(device_id: str, inspection_id: int, body: InspectionUpdateIn):
    return update_inspection(device_id, inspection_id, body.note)


# ── HivePal app API ────────────────────────────────────────────────────────────
# The use case from issue #173: a beekeeper in the apiary taps "start inspection"
# in HivePal instead of walking to the hub. Same handlers, per-user device
# authorization instead of the shared API key.


@router.post(
    "/api/v1/app/devices/{device_id}/inspections/start",
    response_model=DeviceInspection,
    dependencies=[Depends(require_hivepal_service_key)],
)
def app_start_inspection(
    device_id: str, body: InspectionStartIn, user_id: str = Depends(require_user_id)
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return start_inspection_for_device(device_id, body, source="api", created_by=user_id)


@router.post(
    "/api/v1/app/devices/{device_id}/inspections/stop",
    response_model=Optional[DeviceInspection],
    dependencies=[Depends(require_hivepal_service_key)],
)
def app_stop_inspection(
    device_id: str, body: InspectionStopIn, user_id: str = Depends(require_user_id)
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return stop_inspection_for_device(device_id, body, source="api")


@router.get(
    "/api/v1/app/devices/{device_id}/inspections/status",
    response_model=DeviceInspectionStatus,
    dependencies=[Depends(require_hivepal_service_key)],
)
def app_inspection_status(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return inspection_status(device_id)


@router.get(
    "/api/v1/app/devices/{device_id}/inspections",
    response_model=list[DeviceInspection],
    dependencies=[Depends(require_hivepal_service_key)],
)
def app_list_inspections(
    device_id: str,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    limit: int = Query(default=200, ge=1, le=2000),
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    expire_timed_out_inspections(device_id)
    return list_inspections([device_id], start_at, end_at, limit)


@router.patch(
    "/api/v1/app/devices/{device_id}/inspections/{inspection_id}",
    response_model=DeviceInspection,
    dependencies=[Depends(require_hivepal_service_key)],
)
def app_update_inspection(
    device_id: str,
    inspection_id: int,
    body: InspectionUpdateIn,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return update_inspection(device_id, inspection_id, body.note)
