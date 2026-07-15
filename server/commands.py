"""Device command queue (calibration, reboot, OTA relays to sub-devices)."""

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_api_key, require_device_key
from config import PUBLIC_BASE_URL
from db import get_conn
from devices import ensure_device_config, get_device_owner_id
from firmware import (
    latest_hiveinside_release,
    latest_release_for_owner,
    reported_hiveinside_board,
)
from schemas import DeviceCommandIn, DeviceCommandResult

router = APIRouter()


def create_command(device_id: str, payload: DeviceCommandIn) -> dict:
    ensure_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO device_commands (device_id, command_type, payload)
                VALUES (%s, %s, %s)
                RETURNING id, status;
                """,
                (device_id, payload.command_type, psycopg.types.json.Jsonb(payload.payload)),
            )
            r = cur.fetchone()
            conn.commit()
    return {"id": r[0], "status": r[1]}


@router.post("/api/v1/devices/{device_id}/commands", dependencies=[Depends(require_api_key)])
def queue_command(device_id: str, payload: DeviceCommandIn):
    result = create_command(device_id, payload)
    return {"status": result["status"], "id": result["id"]}


def queue_relay_firmware_update(device_id: str, target: str,
                                command_type: str, slot: int) -> dict:
    """Queue a command telling the HiveHub to relay the active firmware for
    ``target`` to the sub-device in the given slot.

    The image URL and its CRC-32 are looked up server-side (the latest active
    release for the target) and embedded in the command payload so the HiveHub
    can verify the download before relaying it. The CRC-32 is checked end-to-end
    on the receiving device before it swaps slots, so a corrupted relay never
    bricks it. Shared by the device-authenticated and HivePal-authenticated
    command endpoints.

    The release is resolved owner-first (the relaying HiveHub's owner), falling
    back to a global release, so a sub-device only ever receives an image its
    owner published or an official build.

    For ``hiveinside`` (two incompatible boards: the ESP32-C6 prototype and the
    nRF54LM20A) the image is additionally matched to the board the target sensor
    last reported, so a C6 image is never relayed to an nRF54 unit or vice versa;
    a legacy board-agnostic release is used only as a fallback.
    """
    owner_id = get_device_owner_id(device_id)
    if target == "hiveinside":
        board = reported_hiveinside_board(device_id, slot)
        r = latest_hiveinside_release(owner_id, board)
    else:
        r = latest_release_for_owner(target, owner_id)
    if not r:
        raise HTTPException(status_code=404, detail=f"No active {target} firmware release")
    version, filename, crc32 = r[0], r[1], r[2]
    url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
    return create_command(device_id, DeviceCommandIn(
        command_type=command_type,
        payload={"slot": slot, "url": url, "version": version, "crc32": int(crc32 or 0)},
    ))


# NOTE: the update-beecounter endpoint was removed together with the wired I2C
# BeeCounter path. The firmware cannot relay BeeCounter images anymore (it
# rejects the obsolete update_beecounter command explicitly), and a BeeCounter
# OTA over BLE/GATT is planned but NOT implemented yet — so there is currently
# no supported remote BeeCounter firmware-update path.


@router.post("/api/v1/devices/{device_id}/commands/update-hiveinside",
          dependencies=[Depends(require_api_key)])
def queue_hiveinside_update(device_id: str, slot: int = Query(1)):
    """Queue a relay of the active HiveInside firmware to the HiveInside sensor
    paired in the given slot (1 -> bleSensorMac0, 2 -> bleSensorMac1) over BLE
    GATT. The HiveHub resolves the BLE MAC locally, so only slot + image URL +
    CRC-32 are sent."""
    return queue_relay_firmware_update(device_id, "hiveinside", "update_hiveinside", slot)


@router.get("/api/v1/devices/{device_id}/commands/next", dependencies=[Depends(require_device_key)])
def next_command(device_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, command_type, payload FROM device_commands
                WHERE device_id = %s AND status = 'pending'
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED;
                """,
                (device_id,),
            )
            r = cur.fetchone()
            if not r:
                conn.commit()
                return {"command": False}
            cur.execute(
                "UPDATE device_commands SET status = 'claimed', claimed_at = now() WHERE id = %s;",
                (r[0],),
            )
            conn.commit()
    return {"command": True, "id": r[0], "command_type": r[1], "payload": r[2]}


def apply_command_result_to_config(device_id: str, result: dict[str, Any]):
    allowed = {
        "scale1_offset",
        "scale1_factor",
        "scale2_offset",
        "scale2_factor",
        "tempco_enabled",
        "tempco_source",
        "tempco_ref_temp_c",
        "scale1_tempco_kg_per_c",
        "scale2_tempco_kg_per_c",
    }
    fields = {k: v for k, v in result.items() if k in allowed and v is not None}
    if not fields:
        return
    assignments = [f"{k} = %({k})s" for k in fields]
    assignments.append("config_version = config_version + 1")
    assignments.append("updated_at = now()")
    fields["device_id"] = device_id
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE device_configs SET {', '.join(assignments)} WHERE device_id = %(device_id)s;",
                fields,
            )
            conn.commit()


@router.post("/api/v1/devices/{device_id}/commands/{command_id}/result", dependencies=[Depends(require_device_key)])
def command_result(device_id: str, command_id: int, payload: DeviceCommandResult):
    if payload.success:
        apply_command_result_to_config(device_id, payload.result)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE device_commands
                SET status = %s, result = %s, completed_at = now()
                WHERE id = %s AND device_id = %s;
                """,
                (
                    "done" if payload.success else "failed",
                    psycopg.types.json.Jsonb(payload.model_dump()),
                    command_id,
                    device_id,
                ),
            )
            conn.commit()
    return {"status": "ok"}
