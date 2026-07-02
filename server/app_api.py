"""HivePal app API (/api/v1/app/*): claiming/sharing devices, measurements,
firmware upload/approval and calibration triggers."""

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from auth import require_device_role, require_hivepal_service_key, require_user_id
from commands import create_command, queue_relay_firmware_update
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
    latest_release_for_owner,
    other_board_releases,
    parse_version,
    set_approved_firmware_version,
    store_firmware_upload,
)
from measurements import (
    MEASUREMENT_SELECT_COLUMNS,
    execute_measurement_query,
    serialize_measurements,
)
from schemas import (
    AppCalibrationModeStartIn,
    AppDeviceConfigUpdate,
    ClaimDeviceIn,
    DeviceChannelsUpdateIn,
    DeviceCommandIn,
    ShareDeviceIn,
    TempCoefficientFitIn,
)

router = APIRouter()


@router.post("/api/v1/app/devices/claim", dependencies=[Depends(require_hivepal_service_key)])
def claim_device(payload: ClaimDeviceIn, user_id: str = Depends(require_user_id)):
    claim_hash = hash_claim_code(payload.claim_code)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT device_id FROM devices
                WHERE claim_code_hash = %s AND claimed_at IS NULL
                LIMIT 1;
                """,
                (claim_hash,),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="No unclaimed device found with that claim code")
            device_id = r[0]
            cur.execute(
                "UPDATE devices SET claimed_at = now(), display_name = %s WHERE device_id = %s;",
                (payload.display_name, device_id),
            )
            cur.execute(
                """
                INSERT INTO device_members (device_id, user_id, role)
                VALUES (%s, %s, 'owner')
                ON CONFLICT (device_id, user_id) DO UPDATE SET role = 'owner';
                """,
                (device_id, user_id),
            )
            for ch_num, ch_name in [
                (1, payload.scale_1_display_name),
                (2, payload.scale_2_display_name),
            ]:
                if ch_name:
                    cur.execute(
                        """
                        INSERT INTO device_channels (device_id, channel_number, name)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (device_id, channel_number) DO UPDATE SET name = EXCLUDED.name;
                        """,
                        (device_id, ch_num, ch_name),
                    )
            conn.commit()
    return {"status": "claimed", "device_id": device_id}


@router.get("/api/v1/app/devices", dependencies=[Depends(require_hivepal_service_key)])
def list_devices(user_id: str = Depends(require_user_id)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.device_id, d.display_name, d.claimed_at, d.last_seen_at,
                       d.last_firmware_version, dm.role
                FROM devices d
                JOIN device_members dm ON dm.device_id = d.device_id
                WHERE dm.user_id = %s
                ORDER BY d.last_seen_at DESC NULLS LAST;
                """,
                (user_id,),
            )
            rows = cur.fetchall()
            device_ids = [r[0] for r in rows]
            channels: dict[str, dict] = {}
            if device_ids:
                cur.execute(
                    "SELECT device_id, channel_number, name FROM device_channels WHERE device_id = ANY(%s);",
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
            "role": r[5],
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


@router.delete("/api/v1/app/devices/{device_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_membership(device_id: str, user_id: str = Depends(require_user_id)):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Device membership not found")
            cur.execute(
                "DELETE FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            conn.commit()
    return {"status": "removed", "device_id": device_id}


@router.get("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def get_device_channels(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return fetch_device_channels(device_id)


@router.patch("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def update_device_channels(device_id: str, payload: DeviceChannelsUpdateIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return apply_device_channels(device_id, payload)


@router.get("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def list_device_members(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, role, created_at FROM device_members WHERE device_id = %s ORDER BY created_at;",
                (device_id,),
            )
            rows = cur.fetchall()
    return [{"user_id": r[0], "role": r[1], "joined_at": r[2]} for r in rows]


@router.post("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def add_device_member(device_id: str, payload: ShareDeviceIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_members (device_id, user_id, role)
                VALUES (%s, %s, %s)
                ON CONFLICT (device_id, user_id) DO UPDATE SET role = EXCLUDED.role;
                """,
                (device_id, payload.user_id, payload.role),
            )
            conn.commit()
    return {"status": "ok", "device_id": device_id, "user_id": payload.user_id, "role": payload.role}


@router.delete("/api/v1/app/devices/{device_id}/members/{member_user_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_member(device_id: str, member_user_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, member_user_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="Member not found")
            if r[0] == "owner":
                raise HTTPException(status_code=400, detail="Owner access cannot be revoked here")
            cur.execute(
                "DELETE FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, member_user_id),
            )
            conn.commit()
    return {"status": "revoked", "device_id": device_id, "user_id": member_user_id}


@router.get("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def get_device_config_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return fetch_device_config(device_id)


@router.get("/api/v1/app/devices/{device_id}/measurements", dependencies=[Depends(require_hivepal_service_key)])
def list_device_measurements(
    device_id: str,
    limit: int = 200,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    max_points: Optional[int] = None,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    limit = min(max(limit, 1), 10000)
    # Optional, opt-in down-sampling for chart consumers (e.g. HivePal wide
    # ranges). Default None keeps the existing full-resolution behaviour, so this
    # is non-breaking; the full column set is preserved either way.
    if max_points is not None:
        max_points = min(max(max_points, 0), 10000)
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
                cur, MEASUREMENT_SELECT_COLUMNS, where_parts, params, limit, max_points
            )
            rows = cur.fetchall()

    return serialize_measurements(rows)


@router.get("/api/v1/app/devices/{device_id}/measurements/latest", dependencies=[Depends(require_hivepal_service_key)])
def latest_device_measurements(device_id: str, limit: int = 50, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
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


@router.patch("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def update_device_config_from_app(device_id: str, patch: AppDeviceConfigUpdate, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return update_device_config(device_id, patch)


@router.post(
    "/api/v1/app/devices/{device_id}/temp-compensation/fit",
    dependencies=[Depends(require_hivepal_service_key)],
)
def fit_temp_compensation_from_app(
    device_id: str,
    body: TempCoefficientFitIn,
    user_id: str = Depends(require_user_id),
):
    """Derive a load-cell temperature coefficient from this device's history.

    Regresses the chosen scale's *raw* weight against an EMA-smoothed temperature
    channel over the requested window (see server/tempcomp.fit_temp_coefficient
    and ema_temperatures) — the same smoothing read-time compensation applies, so
    the coefficient is fitted in the regime it is used in — and returns the fit.
    With ``apply=true`` the coefficient, reference temperature and
    temperature source are written to the device config and compensation is
    enabled — applying ``apply`` requires owner/admin, a plain fit needs only
    viewer access.
    """
    role = ["owner", "admin"] if body.apply else ["owner", "admin", "viewer"]
    require_device_role(user_id, device_id, role)
    return run_temp_compensation_fit(device_id, body)


@router.post(
    "/api/v1/app/devices/{device_id}/firmware",
    dependencies=[Depends(require_hivepal_service_key)],
)
async def upload_firmware_from_app(
    device_id: str,
    file: UploadFile = File(...),
    version: str = Form(...),
    target: str = Form("hivescale"),
    active: bool = Form(True),
    board: str = Form(""),
    user_id: str = Depends(require_user_id),
):
    """Upload a firmware binary from HivePal and register it as a release.

    Unlike POST /api/v1/firmware/releases (which only registers a file that is
    already present in FIRMWARE_DIR and is authenticated with the device
    X-API-Key), this endpoint accepts the binary itself as multipart/form-data,
    writes it into FIRMWARE_DIR, computes its CRC-32 and upserts the
    firmware_releases row.

    Authorization is per-device: the caller must be owner or admin on the given
    device. The release is scoped to that device's OWNER (owner_user_id), so it is
    only offered to scales owned by the same user — not the whole fleet. Pushing a
    global / official build is still possible via the master-key
    POST /api/v1/firmware/releases (which leaves owner_user_id NULL).
    """
    require_device_role(user_id, device_id, ["owner", "admin"])
    # Scope the release to the device's owner so only that owner's scales can pick
    # it up. Fall back to the uploader (an admin acting on an owner-less device)
    # so a release always has an owner and never silently becomes global.
    owner_user_id = get_device_owner_id(device_id) or user_id
    return await store_firmware_upload(
        device_id, file, version, target, active, board, owner_user_id
    )


@router.get(
    "/api/v1/app/devices/{device_id}/firmware/status",
    dependencies=[Depends(require_hivepal_service_key)],
)
def firmware_status_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    """Report the device's firmware-update status for the HivePal setup panel.

    HivePal renders an "update available — apply" notice from this.
    ``current_version`` is the version the device last reported; ``latest_version``
    is the newest active release resolved owner-first (the owner's own build, else
    a global/official one). ``update_available`` means latest > current;
    ``pending_approval`` means an update is available but the owner has not approved
    it yet, so the device will NOT auto-flash until they do via the approve
    endpoint below. Any role may read.
    """
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
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
    # Resolve the latest release for this device's reported board so a C6 device is
    # never shown an esp32-only build as "available" (and vice versa). Falls back to
    # board-agnostic when the device has not yet checked in with the board param.
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


@router.post(
    "/api/v1/app/devices/{device_id}/firmware/approve",
    dependencies=[Depends(require_hivepal_service_key)],
)
def approve_firmware_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    """Approve the latest available firmware so this device may apply it.

    The accept-to-apply step: it records the approved version for this device (so
    check_firmware starts returning update=true for it) and queues an
    ``ota_update`` command to nudge the scale to update on its next check-in rather
    than waiting for its scheduled OTA poll. Requires owner or admin.
    """
    require_device_role(user_id, device_id, ["owner", "admin"])
    owner_id = get_device_owner_id(device_id)
    # Approve the latest release built for this device's board, so we never record
    # an approval for a version that only exists for the other architecture.
    release = latest_release_for_owner("hivescale", owner_id, get_device_board(device_id))
    if not release:
        raise HTTPException(
            status_code=404, detail="No firmware release available for this device"
        )
    latest_version = release[0]
    set_approved_firmware_version(device_id, latest_version)
    # Nudge the device to check now rather than waiting for its scheduled poll.
    command = create_command(
        device_id, DeviceCommandIn(command_type="ota_update", payload={})
    )
    return {
        "status": "approved",
        "device_id": device_id,
        "version": latest_version,
        "command_id": command["id"],
    }


@router.post("/api/v1/app/devices/{device_id}/calibration/start", dependencies=[Depends(require_hivepal_service_key)])
def start_calibration_mode_from_app(
    device_id: str,
    payload: Optional[AppCalibrationModeStartIn] = None,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    payload = payload or AppCalibrationModeStartIn()
    command_payload = {
        "interval_seconds": payload.interval_seconds,
        "timeout_seconds": payload.timeout_seconds,
    }
    result = create_command(
        device_id,
        DeviceCommandIn(
            command_type="start_calibration_mode",
            payload=command_payload,
        ),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "start_calibration_mode",
        "payload": command_payload,
    }


@router.post("/api/v1/app/devices/{device_id}/calibration/stop", dependencies=[Depends(require_hivepal_service_key)])
def stop_calibration_mode_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = create_command(
        device_id,
        DeviceCommandIn(
            command_type="stop_calibration_mode",
            payload={},
        ),
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "stop_calibration_mode",
        "payload": {},
    }


@router.post(
    "/api/v1/app/devices/{device_id}/commands/update-hiveinside",
    dependencies=[Depends(require_hivepal_service_key)],
)
def queue_hiveinside_update_from_app(
    device_id: str,
    slot: int = Query(1),
    user_id: str = Depends(require_user_id),
):
    """App-facing trigger for a HiveInside OTA relay.

    Uploading a HiveInside binary (POST .../firmware) only *registers* the
    release; it does not start the relay. HivePal calls this endpoint to actually
    queue the ``update_hiveinside`` command for the HiveHub to pick up. The
    caller must be owner or admin on the device.
    """
    if slot not in (1, 2):
        raise HTTPException(status_code=400, detail="slot must be 1 or 2")
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = queue_relay_firmware_update(
        device_id, "hiveinside", "update_hiveinside", slot
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "update_hiveinside",
        "payload": {"slot": slot},
    }


@router.post(
    "/api/v1/app/devices/{device_id}/commands/update-beecounter",
    dependencies=[Depends(require_hivepal_service_key)],
)
def queue_beecounter_update_from_app(
    device_id: str,
    slot: int = Query(1),
    user_id: str = Depends(require_user_id),
):
    """App-facing trigger for a BeeCounter OTA relay (see the HiveInside endpoint
    above; same upload-then-queue split). The caller must be owner or admin."""
    if slot not in (1, 2):
        raise HTTPException(status_code=400, detail="slot must be 1 or 2")
    require_device_role(user_id, device_id, ["owner", "admin"])
    result = queue_relay_firmware_update(
        device_id, "beecounter", "update_beecounter", slot
    )
    return {
        "status": result["status"],
        "id": result["id"],
        "command_type": "update_beecounter",
        "payload": {"slot": slot},
    }
