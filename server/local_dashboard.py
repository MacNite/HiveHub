import time
from datetime import datetime
from typing import Any, Literal, Optional

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
from commands import create_command
from config import DASHBOARD_SESSION_COOKIE
from db import get_conn
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
    execute_measurement_query,
    serialize_chart_measurements,
    serialize_measurements,
)
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
    TempCoefficientFitIn,
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
    return {
        "setup_required": dashboard_user_count() == 0,
        "authenticated": bool(payload),
        "user": user,
    }


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

    This address is where insights-based alerts will be delivered once alert
    notifications are wired up; storing it now lets beekeepers opt in early.
    """
    user = get_dashboard_user_by_username(session["username"])
    if not user:
        raise HTTPException(status_code=404, detail="Account not found")
    set_dashboard_user_email(user["id"], body.email)
    return {"ok": True, "email": body.email}


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


@router.get("/api/v1/local/devices", dependencies=LOCAL_DASHBOARD_DEP)
def local_list_devices():
    """List every device on this server with its scale-channel display names."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT device_id, display_name, claimed_at, last_seen_at,
                       last_firmware_version
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
    return {
        "device_id": device_id,
        "target": "hivescale",
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
