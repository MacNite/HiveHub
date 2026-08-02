import os
import time
from datetime import datetime
from typing import Any, Iterable, Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse

from auth import (
    create_dashboard_session_token,
    create_dashboard_user,
    dashboard_admin_count,
    dashboard_user_count,
    decode_dashboard_session_token,
    delete_dashboard_user,
    get_dashboard_user_by_username,
    list_dashboard_users,
    require_dashboard_admin,
    require_dashboard_session,
    require_local_dashboard,
    set_dashboard_session_cookie,
    clear_dashboard_session_cookie,
    set_dashboard_user_email,
    set_dashboard_user_password,
    touch_dashboard_user_login,
    verify_password,
    _public_dashboard_user,
)
from commands import (
    create_command,
    latest_beecounter_relays,
    latest_hiveinside_relays,
    queue_relay_firmware_update,
)
from config import DASHBOARD_SESSION_COOKIE, NOTIFY_MIN_SEVERITY, VAPID_PUBLIC_KEY
from data_export import (
    EXPORT_PAGE_ROWS,
    FALLBACK_COLUMNS,
    export_filename,
    iter_export_lines,
)
from notifications import (
    delete_push_subscription,
    email_enabled,
    send_test_notification,
    upsert_push_subscription,
    web_push_enabled,
)
from db import get_conn, hash_claim_code
from devices import (
    apply_device_channels,
    fetch_device_channels,
    fetch_device_config,
    get_device_owner_id,
    run_temp_compensation_fit,
    update_device_config,
)
from firmware import (
    get_approved_firmware_version,
    get_device_board,
    latest_beecounter_release,
    latest_hiveinside_release,
    latest_release_for_owner,
    other_board_releases,
    parse_version,
    set_approved_firmware_version,
    store_firmware_upload,
)
from insights_api import (
    _INSIGHTS_SUMMARY_TTL_SECONDS,
    _insights_summary_cache,
    _insights_summary_lock,
    _summary_from_persisted,
    build_insight_history,
    compute_local_insights_summary,
)
from measurements import (
    CHART_MEASUREMENT_COLUMNS,
    MEASUREMENT_SELECT_COLUMNS,
    bulk_import_measurements,
    execute_measurement_query,
    serialize_chart_measurements,
    serialize_measurements,
)
from pydantic import ValidationError
from schemas import (
    AppCalibrationModeStartIn,
    DashboardChangePasswordIn,
    DashboardCreateUserIn,
    DashboardLoginIn,
    DashboardSetupIn,
    DashboardUpdateEmailIn,
    DeviceChannelsUpdateIn,
    DeviceCommandIn,
    DeviceConfigUpdate,
    DeviceVisibilityUpdateIn,
    MAX_HIVES,
    MEASUREMENT_IMPORT_MAX,
    MeasurementDeleteIn,
    MeasurementIn,
    PushSubscriptionIn,
    PushUnsubscribeIn,
    TempCoefficientFitIn,
)
from sd_import import (
    distinct_source_device_ids,
    group_records_by_device,
    parse_sd_measurements,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Local dashboard API (single-owner self-host, login-protected)
# ---------------------------------------------------------------------------
# Everything below is gated by ENABLE_LOCAL_DASHBOARD (404 when off). Data and
# control endpoints additionally require a valid dashboard login session
# (LOCAL_DASHBOARD_DEP -> require_dashboard_session); write/control actions
# require the admin role (LOCAL_DASHBOARD_ADMIN_DEP). The auth endpoints below
# (status / setup / login / logout) use only the 404 gate so they are reachable
# before a session exists. Handlers reuse the same helpers as the app API so the
# data shapes never drift. It serves EVERY device on this server, so the login is
# the access boundary — keep ENABLE_LOCAL_DASHBOARD off on multi-tenant servers.

# Reachable pre-login (only ENABLE_LOCAL_DASHBOARD gate): the auth handshake.
LOCAL_DASHBOARD_AUTH_DEP = [Depends(require_local_dashboard)]
# Read endpoints: any logged-in user (admin or viewer).
LOCAL_DASHBOARD_DEP = [Depends(require_dashboard_session)]
# Write / control endpoints: admins only.
LOCAL_DASHBOARD_ADMIN_DEP = [Depends(require_dashboard_admin)]


@router.get("/api/v1/local/auth/status", dependencies=LOCAL_DASHBOARD_AUTH_DEP)
def local_auth_status(request: Request):
    """Tell the dashboard whether to show the setup wizard, login, or the app."""
    payload = decode_dashboard_session_token(request.cookies.get(DASHBOARD_SESSION_COOKIE, ""))
    user = None
    if payload:
        # Read the live row so the email (which can change after login) is fresh.
        record = get_dashboard_user_by_username(payload["username"])
        user = (
            _public_dashboard_user(record)
            if record
            else {"username": payload["username"], "role": payload["role"]}
        )
    result = {
        "setup_required": dashboard_user_count() == 0,
        "authenticated": bool(payload),
        "user": user,
    }
    if payload:
        # Ship the device list with the auth check so an already-signed-in
        # dashboard saves one serial round-trip before it can request data.
        result["devices"] = _list_devices_payload()
    return result


@router.post("/api/v1/local/auth/setup", dependencies=LOCAL_DASHBOARD_AUTH_DEP)
def local_auth_setup(body: DashboardSetupIn, response: Response):
    """First-run wizard: create the initial admin. No-op once any account exists."""
    if dashboard_user_count() > 0:
        raise HTTPException(status_code=409, detail="Setup has already been completed")
    user = create_dashboard_user(body.username, body.password, "admin", body.email)
    touch_dashboard_user_login(user["id"])
    set_dashboard_session_cookie(response, create_dashboard_session_token(user))
    return {"user": _public_dashboard_user(user)}


@router.post("/api/v1/local/auth/login", dependencies=LOCAL_DASHBOARD_AUTH_DEP)
def local_auth_login(body: DashboardLoginIn, response: Response):
    user = get_dashboard_user_by_username(body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    touch_dashboard_user_login(user["id"])
    set_dashboard_session_cookie(response, create_dashboard_session_token(user))
    return {"user": _public_dashboard_user(user)}


@router.post("/api/v1/local/auth/logout", dependencies=LOCAL_DASHBOARD_AUTH_DEP)
def local_auth_logout(response: Response):
    clear_dashboard_session_cookie(response)
    return {"ok": True}


@router.post("/api/v1/local/auth/password")
def local_auth_change_password(
    body: DashboardChangePasswordIn, session: dict = Depends(require_dashboard_session)
):
    """Change the logged-in user's own password (re-auth with current password)."""
    user = get_dashboard_user_by_username(session["username"])
    if not user or not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    set_dashboard_user_password(user["id"], body.new_password)
    return {"ok": True}


@router.post("/api/v1/local/auth/email")
def local_auth_update_email(
    body: DashboardUpdateEmailIn, session: dict = Depends(require_dashboard_session)
):
    """Set or clear the logged-in user's contact email.

    This is the destination for insight-alert e-mails (swarm, robbing, winter
    risk…). Delivery is active when the server has SMTP configured; leave the
    field blank to receive none.
    """
    user = get_dashboard_user_by_username(session["username"])
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    set_dashboard_user_email(user["id"], body.email)
    return {"ok": True, "email": body.email}


# ── Insight-alert notifications (e-mail + Web Push) ──────────────────────────


@router.get("/api/v1/local/notifications/config", dependencies=LOCAL_DASHBOARD_DEP)
def local_notifications_config():
    """Report which notification channels the server has enabled, plus the VAPID
    public key the browser needs to subscribe to Web Push."""
    push_on = web_push_enabled()
    return {
        "email_enabled": email_enabled(),
        "web_push_enabled": push_on,
        # Only expose the key when push is actually configured, so the frontend
        # doesn't try to subscribe against a half-configured server.
        "vapid_public_key": VAPID_PUBLIC_KEY if push_on else None,
        "min_severity": NOTIFY_MIN_SEVERITY,
    }


@router.post("/api/v1/local/notifications/subscribe")
def local_notifications_subscribe(
    body: PushSubscriptionIn, request: Request, session: dict = Depends(require_dashboard_session)
):
    """Register (or refresh) this browser's Web Push subscription for the user."""
    if not web_push_enabled():
        raise HTTPException(status_code=409, detail="Web Push is not configured on this server")
    user = get_dashboard_user_by_username(session["username"])
    upsert_push_subscription(
        user_id=user["id"] if user else None,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=request.headers.get("user-agent", "")[:512] or None,
    )
    return {"ok": True}


@router.post("/api/v1/local/notifications/unsubscribe", dependencies=LOCAL_DASHBOARD_DEP)
def local_notifications_unsubscribe(body: PushUnsubscribeIn):
    """Forget a browser's Web Push subscription (called on opt-out)."""
    removed = delete_push_subscription(body.endpoint)
    return {"ok": True, "removed": removed}


@router.post("/api/v1/local/notifications/test")
def local_notifications_test(session: dict = Depends(require_dashboard_session)):
    """Send a one-off test over every enabled channel to the current user."""
    user = get_dashboard_user_by_username(session["username"])
    email = user.get("email") if user else None
    result = send_test_notification(email)
    return {"ok": True, "result": result}


@router.get("/api/v1/local/auth/users", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_list_dashboard_users():
    """List all dashboard accounts (admin only)."""
    return list_dashboard_users()


@router.post("/api/v1/local/auth/users", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_create_dashboard_user(body: DashboardCreateUserIn):
    """Create a new dashboard account with the admin or viewer role (admin only)."""
    if get_dashboard_user_by_username(body.username):
        raise HTTPException(status_code=409, detail="That username is already taken")
    return _public_dashboard_user(
        create_dashboard_user(body.username, body.password, body.role, body.email)
    )


@router.delete("/api/v1/local/auth/users/{user_id}")
def local_delete_dashboard_user(user_id: int, admin: dict = Depends(require_dashboard_admin)):
    """Delete a dashboard account (admin only).

    Guards against locking yourself out: you cannot delete your own account, nor
    the last remaining admin.
    """
    if str(admin.get("sub")) == str(user_id):
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    target = None
    for u in list_dashboard_users():
        if u["id"] == user_id:
            target = u
            break
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["role"] == "admin" and dashboard_admin_count(exclude_id=user_id) == 0:
        raise HTTPException(status_code=400, detail="Cannot delete the last administrator")
    delete_dashboard_user(user_id)
    return {"ok": True}


def _list_devices_payload() -> list[dict]:
    """Every device on this server with its scale-channel display names.

    Shared by GET /devices and the authenticated branch of /auth/status."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT device_id, display_name, claimed_at, last_seen_at,
                       last_firmware_version, hidden
                FROM devices
                ORDER BY last_seen_at DESC NULLS LAST;
                """
            )
            rows = cur.fetchall()
            device_ids = [r[0] for r in rows]
            channels: dict[str, dict] = {}
            if device_ids:
                cur.execute(
                    "SELECT device_id, channel_number, name FROM device_channels "
                    "WHERE device_id = ANY(%s);",
                    (device_ids,),
                )
                for ch in cur.fetchall():
                    channels.setdefault(ch[0], {})[ch[1]] = ch[2]
    return [
        {
            "device_id": r[0],
            "display_name": r[1],
            "claimed_at": r[2],
            "last_seen_at": r[3],
            "last_firmware_version": r[4],
            "hidden": bool(r[5]),
            "channels": {
                "scale_1": channels.get(r[0], {}).get(1),
                "scale_2": channels.get(r[0], {}).get(2),
                # All custom hive names this device has (index "1".."18" -> name),
                # so the dashboard can label hives beyond the first two.
                "names": {
                    str(num): name
                    for num, name in channels.get(r[0], {}).items()
                    if name is not None
                },
            },
        }
        for r in rows
    ]


@router.get("/api/v1/local/devices", dependencies=LOCAL_DASHBOARD_DEP)
def local_list_devices():
    """List every device on this server with its scale-channel display names."""
    return _list_devices_payload()


@router.patch("/api/v1/local/devices/{device_id}/visibility", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_set_device_visibility(device_id: str, body: DeviceVisibilityUpdateIn):
    """Show or hide a device in the dashboard's hive picker (admin only).

    Hiding a retired device removes it from the top-bar picker without deleting
    any of its stored readings; unhiding brings it back. The device keeps
    ingesting and stays reachable through every other API path.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET hidden = %s WHERE device_id = %s;",
                (body.hidden, device_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Device not found")
            conn.commit()
    return {"device_id": device_id, "hidden": body.hidden}


@router.get("/api/v1/local/devices/{device_id}/measurements", dependencies=LOCAL_DASHBOARD_DEP)
def local_list_measurements(
    device_id: str,
    limit: int = 2000,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    max_points: int = 1500,
):
    """Time-series measurements for one device (newest first), for the charts.

    Returns the slim chart column set (see CHART_MEASUREMENT_COLUMNS) and, by
    default, down-samples to at most ~``max_points`` evenly-spaced rows so wide
    ranges (30d / 1y / 5y) stay fast and small instead of shipping every reading
    to draw a ~600px chart. Pass ``max_points=0`` to disable down-sampling.
    """
    limit = min(max(limit, 1), 20000)
    max_points = min(max(max_points, 0), 20000)
    where_parts = ["device_id = %s"]
    params: list[Any] = [device_id]
    if start_at is not None:
        where_parts.append("measured_at >= %s")
        params.append(start_at)
    if end_at is not None:
        where_parts.append("measured_at <= %s")
        params.append(end_at)
    with get_conn() as conn:
        with conn.cursor() as cur:
            execute_measurement_query(
                cur, CHART_MEASUREMENT_COLUMNS, where_parts, params, limit, max_points
            )
            return serialize_chart_measurements(cur)


@router.get("/api/v1/local/devices/{device_id}/measurements/latest", dependencies=LOCAL_DASHBOARD_DEP)
def local_latest_measurements(device_id: str, limit: int = 1):
    """Most recent measurement row(s) for the overview cards."""
    limit = min(max(limit, 1), 500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {MEASUREMENT_SELECT_COLUMNS}
                FROM measurements
                WHERE device_id = %s
                ORDER BY measured_at DESC
                LIMIT %s;
                """,
                (device_id, limit),
            )
            rows = cur.fetchall()
    return serialize_measurements(rows)


# ── Download / backup ────────────────────────────────────────────────────────
# Streams stored readings back out in the SD-card backup format, so a self-host
# can archive its history before re-deploying the stack (or hand one beekeeper
# their data to take to their own server) and load it back in through the
# existing "Import SD card data" upload. See server/data_export.py for the
# record shaping and why device_id / timestamp are re-stamped from the database.
#
# Admin-only, matching the import it feeds. A viewer can already read any single
# device's readings through the chart endpoints, so this grants no new access to
# the data — but bulk-extracting every device on the server in one file is an
# operator action, and it sits with the other operator actions on the dashboard's
# Admin panel.


def _resolve_export_devices(device_ids: list[str]) -> list[str]:
    """Validate the requested device ids; an empty request means *every* device.

    Hidden devices are included: hiding only retires a device from the hive
    picker, and a backup that quietly skipped retired devices would lose exactly
    the history nobody is watching any more.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT device_id FROM devices ORDER BY device_id;")
            known = [r[0] for r in cur.fetchall()]

    if not device_ids:
        if not known:
            raise HTTPException(status_code=404, detail="No devices on this server")
        return known

    known_set = set(known)
    unknown = [d for d in device_ids if d not in known_set]
    if unknown:
        raise HTTPException(
            status_code=404, detail=f"Unknown device(s): {', '.join(sorted(set(unknown)))}"
        )
    # Preserve the caller's order, but never export a device twice.
    seen: set[str] = set()
    return [d for d in device_ids if not (d in seen or seen.add(d))]


def _export_where(
    device_id: str, start_at: Optional[datetime], end_at: Optional[datetime]
) -> tuple[str, list[Any]]:
    """WHERE clause + params selecting one device's readings in a time range."""
    parts = ["device_id = %s"]
    params: list[Any] = [device_id]
    if start_at is not None:
        parts.append("measured_at >= %s")
        params.append(start_at)
    if end_at is not None:
        parts.append("measured_at <= %s")
        params.append(end_at)
    return " AND ".join(parts), params


def _validate_export_hives(hives: list[int]) -> list[int]:
    out = sorted({int(h) for h in hives})
    if any(h < 1 or h > MAX_HIVES for h in out):
        raise HTTPException(
            status_code=400, detail=f"Hive index must be between 1 and {MAX_HIVES}"
        )
    return out


def _iter_export_rows(
    device_ids: list[str], start_at: Optional[datetime], end_at: Optional[datetime]
):
    """Yield ``(device_id, measured_at, raw_json, fallback)`` oldest-first.

    Paged with a keyset cursor on ``(measured_at, id)`` and a fresh pooled
    connection per page, so a multi-year download neither buffers the whole
    result set nor pins a connection for the lifetime of the response.
    """
    columns = ", ".join(FALLBACK_COLUMNS)
    for device_id in device_ids:
        after: Optional[tuple[datetime, int]] = None
        while True:
            where, params = _export_where(device_id, start_at, end_at)
            if after is not None:
                where += " AND (measured_at, id) > (%s, %s)"
                params = params + [after[0], after[1]]
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT id, measured_at, raw_json, {columns}
                        FROM measurements
                        WHERE {where}
                        ORDER BY measured_at, id
                        LIMIT %s;
                        """,
                        params + [EXPORT_PAGE_ROWS],
                    )
                    rows = cur.fetchall()
            for row in rows:
                fallback = dict(zip(FALLBACK_COLUMNS, row[3:]))
                yield (device_id, row[1], row[2], fallback)
            if len(rows) < EXPORT_PAGE_ROWS:
                break
            after = (rows[-1][1], rows[-1][0])


@router.get(
    "/api/v1/local/export/measurements/summary",
    dependencies=LOCAL_DASHBOARD_ADMIN_DEP,
)
def local_export_summary(
    device_id: list[str] = Query(default=[]),
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
):
    """How much a download with these filters would contain, per device.

    The dashboard shows this before the download starts: a browser file download
    is a plain navigation, so a server-side error or an empty selection would
    otherwise be invisible until the user opened the saved file. Counts are per
    measurement row and therefore unaffected by the hive filter, which trims
    fields inside a row rather than dropping rows.
    """
    devices = _resolve_export_devices(device_id)
    per_device = []
    total = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            for did in devices:
                where, params = _export_where(did, start_at, end_at)
                cur.execute(
                    f"SELECT count(*), min(measured_at), max(measured_at) "
                    f"FROM measurements WHERE {where};",
                    params,
                )
                count, first, last = cur.fetchone()
                total += count
                per_device.append(
                    {
                        "device_id": did,
                        "measurements": count,
                        "first_measured_at": first,
                        "last_measured_at": last,
                    }
                )
    return {
        "devices": per_device,
        "total_measurements": total,
        "filename": export_filename(devices, start_at, end_at),
    }


@router.get("/api/v1/local/export/measurements", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_export_measurements(
    device_id: list[str] = Query(default=[]),
    hive: list[int] = Query(default=[]),
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
):
    """Download stored readings as an SD-style NDJSON backup.

    ``device_id`` may be repeated (omit it for every device on the server),
    ``hive`` restricts the per-hive fields written into each row (omit for all
    hives), and ``start_at`` / ``end_at`` bound the period. The response streams,
    so exporting years of readings costs a page of rows in memory at a time.

    A single-device download imports straight back through
    ``POST …/devices/{id}/measurements/import``. A download covering several
    devices carries each row's own ``device_id``, and is imported with
    ``route_by_device=true`` so every reading lands on the device that recorded
    it.
    """
    devices = _resolve_export_devices(device_id)
    hives = _validate_export_hives(hive)
    filename = export_filename(devices, start_at, end_at)
    rows = _iter_export_rows(devices, start_at, end_at)
    return StreamingResponse(
        iter_export_lines(rows, hives),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The body is generated on the fly and must not be cached by a proxy
            # sitting in front of the dashboard.
            "Cache-Control": "no-store",
        },
    )


# SD backup uploads are fully buffered in memory, so cap the upload size to
# avoid excessive memory usage / DoS from oversized files. Overridable via env
# var to allow tuning without a code change (mirrors the HivePal proxy's cap).
SD_IMPORT_MAX_FILE_SIZE = int(
    os.environ.get("HIVEHUB_SD_IMPORT_MAX_FILE_SIZE", str(250 * 1024 * 1024))
)


@router.post(
    "/api/v1/local/devices/{device_id}/measurements/import",
    dependencies=LOCAL_DASHBOARD_ADMIN_DEP,
)
async def local_import_sd_measurements(
    device_id: str,
    file: UploadFile = File(...),
    force: bool = Form(False),
    route_by_device: bool = Form(False),
):
    """Import measurements from an SD-card backup pulled off the scale in AP mode.

    Accepts either the raw ``measurements.ndjson`` or the
    ``hivescale-sd-data.tar`` download. The file is parsed server-side into
    measurement records, each row is pinned to the path ``device_id``, and the
    batch is de-duplicated on ``(device_id, measured_at)`` so re-uploading the
    same file inserts nothing. Mirrors the HivePal SD-upload feature for the
    self-hosted dashboard that ships with HiveHub.

    Because every row is re-pinned to the path ``device_id``, uploading a card
    while the *wrong* device is selected would silently attach one hive's
    readings to another. The firmware stamps each backup line with the device
    that recorded it, so we first refuse (HTTP 409) any file whose embedded
    ``device_id`` does not match the import target. ``force=true`` overrides the
    check for the legitimate edge case where a device was re-provisioned under a
    new id.

    ``route_by_device=true`` handles the other legitimate multi-device upload: a
    whole-server backup from the dashboard's download panel, restored after a
    redeploy. Instead of re-pinning, every row is imported into the device that
    recorded it — so the mismatch guard has nothing to protect against and is
    skipped. Devices the backup names but this server does not know are rejected
    rather than conjured into existence: a device row is created by the
    claim/check-in flow, and inventing one from an uploaded file would fabricate
    a device with no key and no owner.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="No SD data file provided")
    if len(data) > SD_IMPORT_MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=(
                f"SD file exceeds the {SD_IMPORT_MAX_FILE_SIZE // (1024 * 1024)} MB "
                "upload limit"
            ),
        )

    records, skipped = parse_sd_measurements(data, file.filename)

    if route_by_device:
        groups = group_records_by_device(records, device_id)
        unknown = sorted(set(groups) - _known_device_ids(groups))
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "unknown_devices",
                    "message": (
                        f"This backup holds readings for {', '.join(unknown)}, which "
                        "this server does not know yet. A device registers itself on "
                        "its first check-in — wait for it to report once, then import "
                        "again."
                    ),
                    "file_device_ids": unknown,
                },
            )
    else:
        # Refuse a backup that belongs to a different device than the one selected,
        # so its readings are not silently re-pinned onto (and mis-mapped to) this
        # device. A well-formed single-card upload carries exactly one embedded id;
        # rows without one predate the field and never trip the guard.
        sources = distinct_source_device_ids(records)
        mismatched = [d for d in sources if d != device_id]
        if mismatched and not force:
            multi = len(sources) > 1
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "device_mismatch",
                    "message": (
                        f"This file holds readings for {len(sources)} devices "
                        f"({', '.join(sources)}). Importing it into {device_id} "
                        "would attach them all to that one device. Restore each "
                        "device's readings into the device that recorded them "
                        "instead."
                        if multi else
                        f"This SD backup was recorded by {', '.join(mismatched)}, "
                        f"not {device_id}. Importing it here would attach those "
                        "readings to the wrong device. Select the matching device, "
                        "or confirm to import anyway."
                    ),
                    "target_device_id": device_id,
                    # The devices that do NOT match the import target: what a
                    # single foreign SD card has to be re-posted to.
                    "file_device_ids": mismatched,
                    # Every device the file names, target included. A backup from
                    # the download panel legitimately carries several, and it
                    # still lists several when one of them happens to be the
                    # selected device — which is exactly the case a
                    # mismatched-only list cannot distinguish from one foreign
                    # card, and which needs a routed restore rather than a
                    # re-post.
                    "source_device_ids": sources,
                },
            )
        groups = {device_id: records}

    per_device = []
    received = 0
    inserted = 0
    duplicates = 0
    for target_id, group in groups.items():
        # Validate each record independently so a single corrupt row does not sink
        # the whole upload — invalid rows join the unreadable-line "skipped" tally.
        measurements: list[MeasurementIn] = []
        for record in group:
            # Fill the required device_id when the row omits it;
            # bulk_import_measurements re-pins it to the target device anyway, so a
            # mismatching value is harmless.
            record.setdefault("device_id", target_id)
            try:
                measurements.append(MeasurementIn.model_validate(record))
            except ValidationError:
                skipped += 1
        if not measurements:
            continue

        result = {"received": 0, "inserted": 0, "duplicates": 0}
        for start in range(0, len(measurements), MEASUREMENT_IMPORT_MAX):
            chunk = measurements[start : start + MEASUREMENT_IMPORT_MAX]
            chunk_result = bulk_import_measurements(target_id, chunk)
            for key in result:
                result[key] += chunk_result[key]
        received += result["received"]
        inserted += result["inserted"]
        duplicates += result["duplicates"]
        per_device.append({"device_id": target_id, **result})

    if not per_device:
        raise HTTPException(
            status_code=400,
            detail=(
                "No measurements found in the uploaded file. Expected a HiveHub "
                ".ndjson backup or the .tar SD download."
            ),
        )

    return {
        "status": "ok",
        "device_id": device_id,
        "parsed": len(records),
        "skipped": skipped,
        "received": received,
        "inserted": inserted,
        "duplicates": duplicates,
        # Per-device breakdown — one entry for a normal SD card, one per device
        # for a routed whole-server restore.
        "devices": per_device,
    }


def _known_device_ids(candidates: Iterable[str]) -> set[str]:
    """Which of ``candidates`` are registered devices on this server."""
    ids = list(candidates)
    if not ids:
        return set()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT device_id FROM devices WHERE device_id = ANY(%s);", (ids,)
            )
            return {row[0] for row in cur.fetchall()}


@router.post(
    "/api/v1/local/devices/{device_id}/measurements/delete",
    dependencies=LOCAL_DASHBOARD_ADMIN_DEP,
)
def local_delete_measurements(device_id: str, body: MeasurementDeleteIn):
    """Delete a device's measurements within a time range (admin only).

    Meant for pruning the useless spikes a device emits on boot before it knows
    its calibration, which otherwise dominate the charts. Destructive and
    irreversible, so it is gated by the device's claim code as a second factor:
    the caller must supply the claim code the device was provisioned with,
    matched against the stored hash. Deleting the parent measurement rows also
    removes their per-hive readings via the ON DELETE CASCADE foreign key.
    """
    if body.end_at < body.start_at:
        raise HTTPException(status_code=400, detail="end_at must be at or after start_at")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT claim_code_hash FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Device not found")
            stored_hash = row[0]
            if not stored_hash:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "This device has no claim code on record, so a deletion "
                        "cannot be authenticated."
                    ),
                )
            if hash_claim_code(body.claim_code) != stored_hash:
                raise HTTPException(status_code=403, detail="Claim code does not match this device")

            cur.execute(
                """
                DELETE FROM measurements
                WHERE device_id = %s AND measured_at >= %s AND measured_at <= %s;
                """,
                (device_id, body.start_at, body.end_at),
            )
            deleted = cur.rowcount
            conn.commit()

    return {"status": "ok", "device_id": device_id, "deleted": deleted}


@router.get("/api/v1/local/devices/{device_id}/config", dependencies=LOCAL_DASHBOARD_DEP)
def local_get_config(device_id: str):
    """Read the device's send-interval / calibration / temp-compensation config."""
    return fetch_device_config(device_id)


@router.patch("/api/v1/local/devices/{device_id}/config", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_update_config(device_id: str, patch: DeviceConfigUpdate):
    """Update device config (send interval, scale offsets/factors, temp comp).

    Only the provided fields change; the write bumps config_version so the device
    picks it up on its next check-in (same path as the HivePal config edit).
    """
    return update_device_config(device_id, patch)


@router.get("/api/v1/local/devices/{device_id}/channels", dependencies=LOCAL_DASHBOARD_DEP)
def local_get_channels(device_id: str):
    """Read the scale-channel (hive) display names."""
    return fetch_device_channels(device_id)


@router.patch("/api/v1/local/devices/{device_id}/channels", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_update_channels(device_id: str, payload: DeviceChannelsUpdateIn):
    """Rename the scale channels (the hive labels shown across the dashboard)."""
    return apply_device_channels(device_id, payload)


@router.get("/api/v1/local/devices/{device_id}/insights/summary", dependencies=LOCAL_DASHBOARD_DEP)
def local_insights_summary(device_id: str, refresh: bool = False):
    """Highest-severity insight summary (14-day lookback) for the overview.

    Normally served from the reconciler-maintained ``insight_alerts`` table (a
    cheap indexed lookup), falling back to a short-lived in-memory cache and,
    only when no fresh persisted state exists, a live recompute of the full
    insight pipeline. Pass ``refresh=true`` to force a live recompute.
    """
    now = time.monotonic()
    if not refresh:
        # Fast path: persisted state kept fresh by the background reconciler.
        persisted = _summary_from_persisted(device_id)
        if persisted is not None:
            return persisted
        cached = _insights_summary_cache.get(device_id)
        if cached and now - cached[0] < _INSIGHTS_SUMMARY_TTL_SECONDS:
            return cached[1]
    result = compute_local_insights_summary(device_id)
    with _insights_summary_lock:
        _insights_summary_cache[device_id] = (time.monotonic(), result)
    return result


@router.get("/api/v1/local/devices/{device_id}/insights/history", dependencies=LOCAL_DASHBOARD_DEP)
def local_insights_history(
    device_id: str,
    status: Literal["all", "active", "resolved"] = Query("all"),
    category: Optional[str] = Query(None),
    since: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    """Persisted lifecycle of this device's insight alerts (active + resolved).

    Complements ``/insights/summary`` (the live current state) with the stored
    history the reconciler builds — including resolved warnings — newest first.
    """
    return build_insight_history(device_id, status, category, since, limit)


@router.get("/api/v1/local/devices/{device_id}/firmware/status", dependencies=LOCAL_DASHBOARD_DEP)
def local_firmware_status(device_id: str):
    """Current vs latest firmware and whether an approved update is pending."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_firmware_version FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    current_version = row[0]
    owner_id = get_device_owner_id(device_id)
    device_board = get_device_board(device_id)
    release = latest_release_for_owner("hivescale", owner_id, device_board)
    latest_version = release[0] if release else None
    latest_is_official = bool(release) and release[3] is None
    approved_version = get_approved_firmware_version(device_id)
    update_available = bool(
        latest_version is not None
        and current_version is not None
        and parse_version(latest_version) > parse_version(current_version)
    )
    # The newest HiveInside release available to this owner, so the dashboard can
    # show what a paired in-hive node could be updated TO next to the version it
    # advertises. This is release metadata only — relaying it is a separate,
    # explicit command (see docs/hiveinside-ble-sensor.md).
    hiveinside_release = latest_hiveinside_release(owner_id)
    # Same, for the HiveTraffic counters. A counter reports its version only from
    # the firmware that added the "ver" field onwards, so an older one shows no
    # running version and is never gated — see check_relay_is_newer.
    beecounter_release = latest_beecounter_release(owner_id)
    return {
        "device_id": device_id,
        "target": "hivescale",
        "hiveinside_latest_version": hiveinside_release[0] if hiveinside_release else None,
        # Last relay attempt per hive slot, so the node list can show "queued" /
        # "relaying" / the exact failure instead of silently looking unchanged.
        "hiveinside_relays": latest_hiveinside_relays(device_id),
        "beecounter_latest_version": beecounter_release[0] if beecounter_release else None,
        "beecounter_relays": latest_beecounter_relays(device_id),
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_is_official": latest_is_official,
        "approved_version": approved_version,
        "update_available": update_available,
        "pending_approval": update_available and approved_version != latest_version,
        # The board this device reports, plus any newer releases uploaded for a
        # DIFFERENT board — so a wrong-board upload is explained rather than
        # silently filtered out of "latest".
        "device_board": device_board,
        "other_board_releases": other_board_releases(
            "hivescale", owner_id, device_board, current_version
        ),
    }


@router.post("/api/v1/local/devices/{device_id}/firmware", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
async def local_upload_firmware(
    device_id: str,
    file: UploadFile = File(...),
    version: str = Form(...),
    target: str = Form("hivescale"),
    active: bool = Form(True),
    board: str = Form(""),
):
    """Upload a firmware binary and register it as a GLOBAL release.

    In single-owner local mode there is no per-user scoping, so the release is
    left owner-less (owner_user_id=None) and every device on this server can pick
    it up after it is approved.
    """
    return await store_firmware_upload(
        device_id, file, version, target, active, board, owner_user_id=None
    )


@router.post("/api/v1/local/devices/{device_id}/firmware/approve", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_approve_firmware(device_id: str):
    """Approve the latest available firmware and nudge the device to update now."""
    owner_id = get_device_owner_id(device_id)
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    if not release:
        raise HTTPException(
            status_code=404, detail="No firmware release available for this device"
        )
    latest_version = release[0]
    set_approved_firmware_version(device_id, latest_version)
    command = create_command(
        device_id, DeviceCommandIn(command_type="ota_update", payload={})
    )
    return {
        "status": "approved",
        "device_id": device_id,
        "version": latest_version,
        "command_id": command["id"],
    }


@router.post("/api/v1/local/devices/{device_id}/hiveinside/update", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_queue_hiveinside_update(
    device_id: str, slot: int = Query(...), force: bool = Query(False)
):
    """Queue a BLE relay of the active HiveInside release to the node on ``slot``.

    Uploading a HiveInside .bin only REGISTERS a release; nothing is transferred
    until a relay is queued. Until this endpoint existed the dashboard could not
    queue one at all — the only routes were the master-key
    ``/api/v1/devices/{id}/commands/update-hiveinside`` and the HivePal app
    endpoint — so an operator who uploaded an image and waited saw the node sit
    on its old version indefinitely, with the UI implying a relay was coming.

    Thin wrapper over the shared helper, so the release lookup, the slot bounds
    check and the "must be newer than what the node advertises" gate behave
    exactly as they do for the other two callers (400 / 404 / 409 respectively).
    """
    return queue_relay_firmware_update(
        device_id, "hiveinside", "update_hiveinside", slot, force
    )


@router.post("/api/v1/local/devices/{device_id}/beecounter/update", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_queue_beecounter_update(
    device_id: str, slot: int = Query(...), force: bool = Query(False)
):
    """Queue a BLE relay of the active HiveTraffic release to the counter on ``slot``.

    The HiveInside endpoint above, for the other relay target, through the same
    shared helper — so the release lookup, slot bounds check and version gate
    behave identically (400 / 404 / 409). The counter stops counting for the
    duration of the transfer, which is why the gate is not skipped by default.
    """
    return queue_relay_firmware_update(
        device_id, "beecounter", "update_beecounter", slot, force
    )


@router.post("/api/v1/local/devices/{device_id}/calibration/start", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_start_calibration(
    device_id: str,
    payload: Optional[AppCalibrationModeStartIn] = None,
):
    """Queue a start-calibration-mode command (denser sampling) for the device."""
    payload = payload or AppCalibrationModeStartIn()
    command_payload = {
        "interval_seconds": payload.interval_seconds,
        "timeout_seconds": payload.timeout_seconds,
    }
    result = create_command(
        device_id,
        DeviceCommandIn(command_type="start_calibration_mode", payload=command_payload),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "start_calibration_mode",
        "payload": command_payload,
    }


@router.post("/api/v1/local/devices/{device_id}/calibration/stop", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_stop_calibration(device_id: str):
    """Queue a stop-calibration-mode command for the device."""
    result = create_command(
        device_id,
        DeviceCommandIn(command_type="stop_calibration_mode", payload={}),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "stop_calibration_mode",
        "payload": {},
    }


@router.post("/api/v1/local/devices/{device_id}/temp-compensation/fit", dependencies=LOCAL_DASHBOARD_ADMIN_DEP)
def local_temp_compensation_fit(device_id: str, body: TempCoefficientFitIn):
    """Fit (and optionally apply) a load-cell temperature coefficient."""
    return run_temp_compensation_fit(device_id, body)
