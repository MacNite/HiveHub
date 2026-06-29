"""
HiveHub mock API — a lightweight, self-contained stand-in for the real
HiveHub FastAPI backend (``server/main.py``), backed by in-memory dummy data
instead of PostgreSQL.

Purpose
-------
It lets the HivePal maintainer explore and develop against the exact HiveHub
interface (request/response shapes, auth model, the ``/api/v1/app/...``
endpoints HivePal calls, and the insights engine) without provisioning a
database or real hardware. The response shapes mirror ``measurement_row_to_dict``
and the Pydantic models in ``server/main.py``, and the insights endpoints reuse
the real ``insights.py`` verbatim.

This is a demo/review tool. It is intentionally NOT a drop-in replacement for
the production server (no persistence, no real OTA file storage). For a
production-faithful backend, run ``server/`` against PostgreSQL and use
``seed.py`` to load the same dummy data — see instructions.md.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

import hive_data
from insights import compute_insights, summarize

# ---------------------------------------------------------------------------
# Configuration (env, with demo-friendly defaults)
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("API_KEY", "demo-device-key")
HIVEPAL_SERVICE_API_KEY = os.environ.get("HIVEPAL_SERVICE_API_KEY", "demo-hivepal-service-key")
DEMO_USER_ID = os.environ.get("DEMO_USER_ID", "demo-user")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))

app = FastAPI(
    title="HiveHub API (mock / demo)",
    description=(
        "Lightweight in-memory mock of the HiveHub backend for reviewing the "
        "HivePal <-> HiveHub interface. Pre-loaded with one dual-channel demo "
        "device and realistic dummy data for 2025-01-01 .. 2026-05-31.\n\n"
        "Auth: device endpoints need `X-API-Key`; HivePal app endpoints "
        "(`/api/v1/app/...`) need `X-HivePal-Service-Key` + `X-User-Id`."
    ),
    version="0.3.2-mock",
)

# ---------------------------------------------------------------------------
# In-memory data store, populated at startup
# ---------------------------------------------------------------------------

STORE: dict[str, Any] = {
    "measurements": [],          # ascending by measured_at
    "configs": {},               # device_id -> config dict
    "channels": {},              # device_id -> {scale_1_display_name, scale_2_display_name}
    "members": {},               # device_id -> [ {user_id, role, joined_at}, ... ]
    "devices": {},               # device_id -> device metadata dict
    "commands": [],              # queued device commands
    "firmware_releases": [],     # registered releases
    "restamp_cache": {},         # device_id -> dataset re-stamped for that device
    "data_end": None,            # newest measured_at (anchor for insights "now")
    "next_command_id": 1,
    "next_measurement_id": 1,
}


@app.on_event("startup")
def _startup() -> None:
    measurements = hive_data.generate_measurements(INTERVAL_MINUTES)
    STORE["measurements"] = measurements
    STORE["data_end"] = measurements[-1]["measured_at"] if measurements else datetime.now(timezone.utc)
    STORE["next_measurement_id"] = (measurements[-1]["id"] + 1) if measurements else 1

    did = hive_data.DEVICE_ID
    STORE["configs"][did] = hive_data.device_config(INTERVAL_MINUTES * 60)
    STORE["channels"][did] = hive_data.device_channels()
    STORE["members"][did] = hive_data.device_members(DEMO_USER_ID)
    STORE["devices"][did] = {
        "device_id": did,
        "display_name": hive_data.DISPLAY_NAME,
        "claim_code": hive_data.CLAIM_CODE,
        "claimed_at": hive_data.CLAIMED_AT,
        "created_at": hive_data.CREATED_AT,
        "last_seen_at": STORE["data_end"],
        "last_firmware_version": hive_data.FIRMWARE_VERSION,
    }
    # A pre-registered "newer" release so the OTA check can demonstrate an update.
    # owner_user_id None => a global / "official" release that any device may fall
    # back to; it stays gated behind owner approval (accept-to-apply) per device.
    STORE["firmware_releases"].append(
        {"version": "0.6.3", "filename": "hivescale-0.6.3.bin", "active": True,
         "target": "hivescale", "crc32": 305419896, "owner_user_id": None}
    )

    # A second device so the claim endpoint can be exercised. Like every demo
    # device it serves the same dummy dataset (see _measurements_for), so the
    # data shows up no matter which device you claim from HivePal.
    unclaimed_id = "hive_scale_dual_02"
    STORE["devices"][unclaimed_id] = {
        "device_id": unclaimed_id,
        "display_name": None,
        "claim_code": "WXYZ-5678",
        "claimed_at": None,
        "created_at": hive_data.CREATED_AT,
        "last_seen_at": STORE["data_end"],
        "last_firmware_version": hive_data.FIRMWARE_VERSION,
    }
    STORE["configs"][unclaimed_id] = hive_data.device_config(INTERVAL_MINUTES * 60)
    STORE["configs"][unclaimed_id]["device_id"] = unclaimed_id
    STORE["channels"][unclaimed_id] = {"scale_1_display_name": None, "scale_2_display_name": None}
    STORE["members"][unclaimed_id] = []


# ---------------------------------------------------------------------------
# Auth dependencies (identical contract to server/main.py)
# ---------------------------------------------------------------------------

def require_api_key(x_api_key: str = Header(default="")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def require_hivepal_service_key(x_hivepal_service_key: str = Header(default="")):
    if not HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=500, detail="HIVEPAL_SERVICE_API_KEY is not configured")
    if x_hivepal_service_key != HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid HivePal service key")


def require_user_id(x_user_id: str = Header(default="")) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header is required")
    return x_user_id


def _ensure_app_device(device_id: str) -> None:
    """Register a device on first access so any device a HivePal instance has
    claimed (with its own user/device ids) resolves to the demo data."""
    if device_id not in STORE["devices"]:
        STORE["devices"][device_id] = {
            "device_id": device_id,
            "display_name": None,
            "claim_code": None,
            "claimed_at": datetime.now(timezone.utc),
            "created_at": hive_data.CREATED_AT,
            "last_seen_at": STORE["data_end"],
            "last_firmware_version": hive_data.FIRMWARE_VERSION,
        }
    STORE["configs"].setdefault(
        device_id, {**hive_data.device_config(INTERVAL_MINUTES * 60), "device_id": device_id}
    )
    STORE["channels"].setdefault(
        device_id, {"scale_1_display_name": None, "scale_2_display_name": None}
    )
    STORE["members"].setdefault(device_id, [])


def _measurements_for(device_id: str) -> list[dict]:
    """Return the demo dataset for a device.

    The mock holds a single generated dataset (for ``hive_data.DEVICE_ID``) and
    serves it for whichever device the caller looks at - re-stamped with that
    device_id and cached - so the data is visible regardless of which device a
    HivePal instance claimed.
    """
    if device_id == hive_data.DEVICE_ID:
        return STORE["measurements"]
    cache = STORE["restamp_cache"]
    if device_id not in cache:
        cache[device_id] = [{**m, "device_id": device_id} for m in STORE["measurements"]]
    return cache[device_id]


def require_device_role(user_id: str, device_id: str, allowed_roles: list[str]):
    # Intentionally lenient for a review mock: any caller with a valid service
    # key + user id may access any demo device, so the data and interface are
    # visible without wiring up HivePal user IDs or per-device roles. The real
    # HiveHub backend enforces owner/admin/viewer roles here.
    _ensure_app_device(device_id)


def _device_owner_id(device_id: str) -> Optional[str]:
    """Owner user id from the device's members, or None (treated as global)."""
    for m in STORE["members"].get(device_id, []):
        if m.get("role") == "owner":
            return m.get("user_id")
    return None


def _latest_release_for_owner(target: str, owner_user_id: Optional[str]) -> Optional[dict]:
    """Newest active release for a target, owner-first with global fallback.

    Mirrors server/main.py: prefer the owner's own release, else the newest global
    (owner_user_id None) "official" release. Newest is last in the append-only list.
    """
    owned = [r for r in STORE["firmware_releases"]
             if r["active"] and r["target"] == target
             and r.get("owner_user_id") == owner_user_id]
    if owned:
        return owned[-1]
    glob = [r for r in STORE["firmware_releases"]
            if r["active"] and r["target"] == target and r.get("owner_user_id") is None]
    return glob[-1] if glob else None


def _ensure_config(device_id: str) -> dict:
    cfg = STORE["configs"].get(device_id)
    if cfg is None:
        cfg = hive_data.device_config(INTERVAL_MINUTES * 60)
        cfg["device_id"] = device_id
        STORE["configs"][device_id] = cfg
    return cfg


def _normalize_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Request models (mirrors of server/main.py; permissive where the device
# payload is large/optional)
# ---------------------------------------------------------------------------

class MeasurementIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    device_id: str
    claim_code: Optional[str] = None
    timestamp: Optional[datetime] = None
    scale_1_weight_kg: Optional[float] = None
    scale_2_weight_kg: Optional[float] = None
    hive_1_temp_c: Optional[float] = None
    hive_2_temp_c: Optional[float] = None
    ambient_temp_c: Optional[float] = None
    ambient_humidity_percent: Optional[float] = None
    firmware_version: Optional[str] = None


class DeviceConfigUpdate(BaseModel):
    send_interval_seconds: Optional[int] = None
    scale1_offset: Optional[int] = None
    scale1_factor: Optional[float] = None
    scale2_offset: Optional[int] = None
    scale2_factor: Optional[float] = None


class FirmwareReleaseIn(BaseModel):
    version: str
    filename: str
    active: bool = True
    target: Literal["hivescale", "beecounter"] = "hivescale"


class DeviceCommandIn(BaseModel):
    command_type: Literal[
        "tare_scale_1", "tare_scale_2", "calibrate_scale_1", "calibrate_scale_2",
        "reboot", "reset_preferences", "factory_reset", "reset_wifi", "check_ota",
        "ota_update", "update_beecounter", "start_provisioning",
        "start_calibration_mode", "stop_calibration_mode",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class DeviceCommandResult(BaseModel):
    success: bool
    message: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)


class ClaimDeviceIn(BaseModel):
    claim_code: str = Field(..., min_length=4, max_length=128)
    display_name: Optional[str] = None
    scale_1_display_name: Optional[str] = None
    scale_2_display_name: Optional[str] = None


class ShareDeviceIn(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: Literal["admin", "viewer"] = "viewer"


class DeviceChannelsUpdateIn(BaseModel):
    scale_1_display_name: Optional[str] = None
    scale_2_display_name: Optional[str] = None


class AppCalibrationModeStartIn(BaseModel):
    interval_seconds: int = Field(default=5, ge=1, le=3600)
    timeout_seconds: int = Field(default=600, ge=1, le=86400)


# ---------------------------------------------------------------------------
# Orientation endpoint (mock-only convenience)
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    measurements = STORE["measurements"]
    return {
        "service": "HiveHub mock API",
        "note": "Demo/review server with in-memory dummy data. See /docs for the interactive API.",
        "demo_device_id": hive_data.DEVICE_ID,
        "demo_claim_code": hive_data.CLAIM_CODE,
        "demo_user_id": DEMO_USER_ID,
        "auth": {
            "device_header": "X-API-Key",
            "hivepal_headers": ["X-HivePal-Service-Key", "X-User-Id"],
        },
        "data": {
            "from": measurements[0]["measured_at"].isoformat() if measurements else None,
            "to": measurements[-1]["measured_at"].isoformat() if measurements else None,
            "interval_minutes": INTERVAL_MINUTES,
            "measurement_count": len(measurements),
        },
        "docs": "/docs",
    }


# ---------------------------------------------------------------------------
# General / device endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/v1/time", dependencies=[Depends(require_api_key)])
def get_server_time():
    now = datetime.now(timezone.utc)
    return {"timestamp": now.isoformat(), "unix": int(now.timestamp()), "timezone": "UTC"}


@app.post("/api/v1/measurements", dependencies=[Depends(require_api_key)])
def create_measurement(payload: MeasurementIn):
    measured_at = _normalize_dt(payload.timestamp) or datetime.now(timezone.utc)
    record = {k: None for k in hive_data.MEASUREMENT_KEYS}
    data = payload.model_dump(exclude={"claim_code", "timestamp"})
    for k, v in data.items():
        if k in record:
            record[k] = v
    new_id = STORE["next_measurement_id"]
    STORE["next_measurement_id"] += 1
    record["id"] = new_id
    record["device_id"] = payload.device_id
    record["measured_at"] = measured_at
    record["received_at"] = datetime.now(timezone.utc)
    if payload.firmware_version is not None:
        record["firmware_version"] = payload.firmware_version
    STORE["measurements"].append(record)
    STORE["measurements"].sort(key=lambda m: m["measured_at"])
    STORE["restamp_cache"].clear()  # re-stamped views must pick up the new point
    _ensure_config(payload.device_id)
    return {"status": "ok", "id": new_id, "measured_at": measured_at.isoformat()}


@app.get("/api/v1/measurements/latest", dependencies=[Depends(require_api_key)])
def latest_measurements(limit: int = 50):
    limit = min(max(limit, 1), 500)
    rows = sorted(STORE["measurements"], key=lambda m: m["measured_at"], reverse=True)
    return rows[:limit]


@app.get("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_api_key)])
def get_device_config(device_id: str):
    return _ensure_config(device_id)


@app.patch("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_api_key)])
def update_device_config(device_id: str, patch: DeviceConfigUpdate):
    cfg = _ensure_config(device_id)
    fields = patch.model_dump(exclude_unset=True)
    if fields:
        cfg.update(fields)
        cfg["config_version"] = cfg.get("config_version", 1) + 1
    return cfg


@app.get("/api/v1/devices/{device_id}/firmware", dependencies=[Depends(require_api_key)])
def check_firmware(device_id: str, version: str = Query("0.0.0"), target: str = Query("hivescale")):
    latest = _latest_release_for_owner(target, _device_owner_id(device_id))
    if not latest:
        return {"update": False, "update_available": False}

    def _ver(v: str):
        return tuple(int("".join(c for c in p if c.isdigit()) or "0") for p in v.split("."))

    if _ver(latest["version"]) <= _ver(version):
        return {"update": False, "update_available": False}
    # Accept-to-apply gate for the ESP32 self-update: serve only once the owner has
    # approved this exact version for this device (mirrors server/main.py).
    if target == "hivescale":
        approved = STORE["devices"].get(device_id, {}).get("approved_firmware_version")
        if approved != latest["version"]:
            return {"update": False, "update_available": False}
    return {"update": True, "update_available": True, "version": latest["version"],
            "url": f"/firmware/{latest['filename']}"}


@app.post("/api/v1/firmware/releases", dependencies=[Depends(require_api_key)])
def create_firmware_release(payload: FirmwareReleaseIn):
    crc = 305419896
    # Master-key registrations are global / official releases (owner_user_id None).
    STORE["firmware_releases"].append(
        {"version": payload.version, "filename": payload.filename, "active": payload.active,
         "target": payload.target, "crc32": crc, "owner_user_id": None}
    )
    return {"status": "ok", "version": payload.version, "target": payload.target, "crc32": crc}


@app.get("/firmware/{filename}")
def download_firmware(filename: str):
    # Mock serves a tiny placeholder binary so the OTA flow is observable.
    return Response(content=b"HIVESCALE-MOCK-FIRMWARE\n", media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _create_command(device_id: str, command_type: str, payload: dict) -> dict:
    _ensure_config(device_id)
    cmd_id = STORE["next_command_id"]
    STORE["next_command_id"] += 1
    cmd = {"id": cmd_id, "device_id": device_id, "command_type": command_type,
           "payload": payload, "status": "pending", "result": None,
           "created_at": datetime.now(timezone.utc)}
    STORE["commands"].append(cmd)
    return cmd


@app.post("/api/v1/devices/{device_id}/commands", dependencies=[Depends(require_api_key)])
def queue_command(device_id: str, payload: DeviceCommandIn):
    cmd = _create_command(device_id, payload.command_type, payload.payload)
    return {"status": cmd["status"], "id": cmd["id"]}


@app.post("/api/v1/devices/{device_id}/commands/update-beecounter", dependencies=[Depends(require_api_key)])
def queue_beecounter_update(device_id: str, slot: int = Query(1)):
    r = _latest_release_for_owner("beecounter", _device_owner_id(device_id))
    if not r:
        raise HTTPException(status_code=404, detail="No active beecounter firmware release")
    cmd = _create_command(device_id, "update_beecounter",
                          {"slot": slot, "url": f"/firmware/{r['filename']}",
                           "version": r["version"], "crc32": int(r["crc32"] or 0)})
    return {"id": cmd["id"], "status": cmd["status"]}


@app.post("/api/v1/devices/{device_id}/commands/update-hiveinside", dependencies=[Depends(require_api_key)])
def queue_hiveinside_update(device_id: str, slot: int = Query(1)):
    r = _latest_release_for_owner("hiveinside", _device_owner_id(device_id))
    if not r:
        raise HTTPException(status_code=404, detail="No active hiveinside firmware release")
    cmd = _create_command(device_id, "update_hiveinside",
                          {"slot": slot, "url": f"/firmware/{r['filename']}",
                           "version": r["version"], "crc32": int(r["crc32"] or 0)})
    return {"id": cmd["id"], "status": cmd["status"]}


@app.get("/api/v1/devices/{device_id}/commands/next", dependencies=[Depends(require_api_key)])
def next_command(device_id: str):
    for cmd in STORE["commands"]:
        if cmd["device_id"] == device_id and cmd["status"] == "pending":
            cmd["status"] = "claimed"
            cmd["claimed_at"] = datetime.now(timezone.utc)
            return {"command": True, "id": cmd["id"], "command_type": cmd["command_type"],
                    "payload": cmd["payload"]}
    return {"command": False}


@app.post("/api/v1/devices/{device_id}/commands/{command_id}/result", dependencies=[Depends(require_api_key)])
def command_result(device_id: str, command_id: int, payload: DeviceCommandResult):
    if payload.success:
        cfg = _ensure_config(device_id)
        for k in ("scale1_offset", "scale1_factor", "scale2_offset", "scale2_factor"):
            if payload.result.get(k) is not None:
                cfg[k] = payload.result[k]
                cfg["config_version"] = cfg.get("config_version", 1) + 1
    for cmd in STORE["commands"]:
        if cmd["id"] == command_id and cmd["device_id"] == device_id:
            cmd["status"] = "done" if payload.success else "failed"
            cmd["result"] = payload.model_dump()
            cmd["completed_at"] = datetime.now(timezone.utc)
            break
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# App endpoints for HivePal
# ---------------------------------------------------------------------------

@app.post("/api/v1/app/devices/claim", dependencies=[Depends(require_hivepal_service_key)])
def claim_device(payload: ClaimDeviceIn, user_id: str = Depends(require_user_id)):
    code = payload.claim_code.strip().upper()
    match = None
    for did, dev in STORE["devices"].items():
        if (dev.get("claim_code") or "").strip().upper() == code:
            match = did
            break
    if match is None:
        raise HTTPException(status_code=404, detail="No unclaimed device found with that claim code")
    # Lenient mock: claiming a known code always succeeds and adds the caller as
    # owner, even if the device was already claimed (the real server only claims
    # an unclaimed device). The claimed device serves the full demo dataset.
    STORE["devices"][match]["claimed_at"] = STORE["devices"][match].get("claimed_at") or datetime.now(timezone.utc)
    if payload.display_name:
        STORE["devices"][match]["display_name"] = payload.display_name
    STORE["members"].setdefault(match, [])
    STORE["members"][match] = [m for m in STORE["members"][match] if m["user_id"] != user_id]
    STORE["members"][match].append({"user_id": user_id, "role": "owner",
                                    "joined_at": datetime.now(timezone.utc)})
    ch = STORE["channels"].setdefault(match, {"scale_1_display_name": None, "scale_2_display_name": None})
    if payload.scale_1_display_name:
        ch["scale_1_display_name"] = payload.scale_1_display_name
    if payload.scale_2_display_name:
        ch["scale_2_display_name"] = payload.scale_2_display_name
    return {"status": "claimed", "device_id": match}


@app.get("/api/v1/app/devices", dependencies=[Depends(require_hivepal_service_key)])
def list_devices(user_id: str = Depends(require_user_id)):
    out = []
    for did, dev in STORE["devices"].items():
        # Lenient mock: show every demo device to any caller (as owner unless
        # they already hold a different membership role).
        role = next((m["role"] for m in STORE["members"].get(did, []) if m["user_id"] == user_id), None) or "owner"
        ch = STORE["channels"].get(did, {})
        out.append({
            "device_id": did,
            "display_name": dev.get("display_name"),
            "claimed_at": dev.get("claimed_at"),
            "last_seen_at": dev.get("last_seen_at"),
            "last_firmware_version": dev.get("last_firmware_version"),
            "role": role,
            "channels": {
                "scale_1": ch.get("scale_1_display_name"),
                "scale_2": ch.get("scale_2_display_name"),
            },
        })
    return out


@app.delete("/api/v1/app/devices/{device_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_membership(device_id: str, user_id: str = Depends(require_user_id)):
    members = STORE["members"].get(device_id, [])
    if not any(m["user_id"] == user_id for m in members):
        raise HTTPException(status_code=404, detail="Device membership not found")
    # Mirrors server/main.py: the membership row is removed; claimed_at is left
    # untouched (the device does not automatically become claimable again).
    STORE["members"][device_id] = [m for m in members if m["user_id"] != user_id]
    return {"status": "removed", "device_id": device_id}


@app.get("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def get_device_channels(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    ch = STORE["channels"].get(device_id, {})
    return {"scale_1_display_name": ch.get("scale_1_display_name"),
            "scale_2_display_name": ch.get("scale_2_display_name")}


@app.patch("/api/v1/app/devices/{device_id}/channels", dependencies=[Depends(require_hivepal_service_key)])
def update_device_channels(device_id: str, payload: DeviceChannelsUpdateIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    ch = STORE["channels"].setdefault(device_id, {"scale_1_display_name": None, "scale_2_display_name": None})
    if payload.scale_1_display_name is not None:
        ch["scale_1_display_name"] = payload.scale_1_display_name
    if payload.scale_2_display_name is not None:
        ch["scale_2_display_name"] = payload.scale_2_display_name
    return get_device_channels(device_id, user_id)


@app.get("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def list_device_members(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return [{"user_id": m["user_id"], "role": m["role"], "joined_at": m["joined_at"]}
            for m in STORE["members"].get(device_id, [])]


@app.post("/api/v1/app/devices/{device_id}/members", dependencies=[Depends(require_hivepal_service_key)])
def add_device_member(device_id: str, payload: ShareDeviceIn, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    members = STORE["members"].setdefault(device_id, [])
    members[:] = [m for m in members if m["user_id"] != payload.user_id]
    members.append({"user_id": payload.user_id, "role": payload.role,
                    "joined_at": datetime.now(timezone.utc)})
    return {"status": "ok", "device_id": device_id, "user_id": payload.user_id, "role": payload.role}


@app.delete("/api/v1/app/devices/{device_id}/members/{member_user_id}", dependencies=[Depends(require_hivepal_service_key)])
def remove_device_member(device_id: str, member_user_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner"])
    members = STORE["members"].get(device_id, [])
    target = next((m for m in members if m["user_id"] == member_user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found")
    if target["role"] == "owner":
        raise HTTPException(status_code=400, detail="Owner access cannot be revoked here")
    STORE["members"][device_id] = [m for m in members if m["user_id"] != member_user_id]
    return {"status": "revoked", "device_id": device_id, "user_id": member_user_id}


@app.get("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def get_device_config_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    return _ensure_config(device_id)


@app.patch("/api/v1/app/devices/{device_id}/config", dependencies=[Depends(require_hivepal_service_key)])
def update_device_config_from_app(device_id: str, patch: DeviceConfigUpdate, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return update_device_config(device_id, patch)


@app.get("/api/v1/app/devices/{device_id}/measurements", dependencies=[Depends(require_hivepal_service_key)])
def list_device_measurements(
    device_id: str,
    limit: int = 200,
    start_at: Optional[datetime] = None,
    end_at: Optional[datetime] = None,
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    # The real server caps this at 10000; the mock allows the full demo dataset
    # so wide ranges ("All"/"365d") render completely for review.
    limit = min(max(limit, 1), 100000)
    start_at = _normalize_dt(start_at)
    end_at = _normalize_dt(end_at)
    rows = _measurements_for(device_id)
    if start_at is not None:
        rows = [m for m in rows if m["measured_at"] >= start_at]
    if end_at is not None:
        rows = [m for m in rows if m["measured_at"] <= end_at]
    rows = sorted(rows, key=lambda m: m["measured_at"], reverse=True)
    return rows[:limit]


@app.get("/api/v1/app/devices/{device_id}/measurements/latest", dependencies=[Depends(require_hivepal_service_key)])
def latest_device_measurements(device_id: str, limit: int = 50, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    limit = min(max(limit, 1), 500)
    rows = sorted(_measurements_for(device_id), key=lambda m: m["measured_at"], reverse=True)
    return rows[:limit]


@app.post("/api/v1/app/devices/{device_id}/firmware", dependencies=[Depends(require_hivepal_service_key)])
async def upload_firmware_from_app(
    device_id: str,
    file: UploadFile = File(...),
    version: str = Form(...),
    target: str = Form("hivescale"),
    active: bool = Form(True),
    user_id: str = Depends(require_user_id),
):
    require_device_role(user_id, device_id, ["owner", "admin"])
    if target not in ("hivescale", "beecounter"):
        raise HTTPException(status_code=400, detail="target must be one of hivescale, beecounter")
    size = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
    await file.close()
    if size == 0:
        raise HTTPException(status_code=400, detail="Uploaded firmware file is empty")
    filename = os.path.basename(file.filename or f"{target}-{version}.bin")
    crc = 305419896
    # Scope the release to the device's owner (global fallback to the uploader).
    owner_user_id = _device_owner_id(device_id) or user_id
    STORE["firmware_releases"].append(
        {"version": version.strip(), "filename": filename, "active": active, "target": target,
         "crc32": crc, "owner_user_id": owner_user_id}
    )
    return {"status": "ok", "version": version.strip(), "filename": filename, "target": target,
            "active": active, "size_bytes": size, "crc32": crc}


@app.get("/api/v1/app/devices/{device_id}/firmware/status",
         dependencies=[Depends(require_hivepal_service_key)])
def firmware_status_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    dev = STORE["devices"].get(device_id, {})
    current_version = dev.get("last_firmware_version")
    release = _latest_release_for_owner("hivescale", _device_owner_id(device_id))
    latest_version = release["version"] if release else None
    latest_is_official = bool(release) and release.get("owner_user_id") is None
    approved_version = dev.get("approved_firmware_version")

    def _ver(v: str):
        return tuple(int("".join(c for c in p if c.isdigit()) or "0") for p in v.split("."))

    update_available = bool(
        latest_version is not None and current_version is not None
        and _ver(latest_version) > _ver(current_version)
    )
    return {
        "device_id": device_id, "target": "hivescale",
        "current_version": current_version, "latest_version": latest_version,
        "latest_is_official": latest_is_official, "approved_version": approved_version,
        "update_available": update_available,
        "pending_approval": update_available and approved_version != latest_version,
    }


@app.post("/api/v1/app/devices/{device_id}/firmware/approve",
          dependencies=[Depends(require_hivepal_service_key)])
def approve_firmware_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    release = _latest_release_for_owner("hivescale", _device_owner_id(device_id))
    if not release:
        raise HTTPException(status_code=404, detail="No firmware release available for this device")
    latest_version = release["version"]
    STORE["devices"][device_id]["approved_firmware_version"] = latest_version
    cmd = _create_command(device_id, "ota_update", {})
    return {"status": "approved", "device_id": device_id, "version": latest_version,
            "command_id": cmd["id"]}


@app.post("/api/v1/app/devices/{device_id}/calibration/start", dependencies=[Depends(require_hivepal_service_key)])
def start_calibration_mode_from_app(device_id: str, payload: Optional[AppCalibrationModeStartIn] = None,
                                    user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    payload = payload or AppCalibrationModeStartIn()
    cmd_payload = {"interval_seconds": payload.interval_seconds, "timeout_seconds": payload.timeout_seconds}
    cmd = _create_command(device_id, "start_calibration_mode", cmd_payload)
    return {"status": cmd["status"], "id": cmd["id"], "command_type": "start_calibration_mode",
            "payload": cmd_payload}


@app.post("/api/v1/app/devices/{device_id}/calibration/stop", dependencies=[Depends(require_hivepal_service_key)])
def stop_calibration_mode_from_app(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    cmd = _create_command(device_id, "stop_calibration_mode", {})
    return {"status": cmd["status"], "id": cmd["id"], "command_type": "stop_calibration_mode", "payload": {}}


def _queue_relay_update_from_app(device_id: str, target: str, command_type: str, slot: int):
    if slot not in (1, 2):
        raise HTTPException(status_code=400, detail="slot must be 1 or 2")
    r = _latest_release_for_owner(target, _device_owner_id(device_id))
    if not r:
        raise HTTPException(status_code=404, detail=f"No active {target} firmware release")
    cmd = _create_command(device_id, command_type,
                          {"slot": slot, "url": f"/firmware/{r['filename']}",
                           "version": r["version"], "crc32": int(r["crc32"] or 0)})
    return {"status": cmd["status"], "id": cmd["id"], "command_type": command_type,
            "payload": {"slot": slot}}


@app.post("/api/v1/app/devices/{device_id}/commands/update-hiveinside",
          dependencies=[Depends(require_hivepal_service_key)])
def queue_hiveinside_update_from_app(device_id: str, slot: int = Query(1),
                                     user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return _queue_relay_update_from_app(device_id, "hiveinside", "update_hiveinside", slot)


@app.post("/api/v1/app/devices/{device_id}/commands/update-beecounter",
          dependencies=[Depends(require_hivepal_service_key)])
def queue_beecounter_update_from_app(device_id: str, slot: int = Query(1),
                                     user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin"])
    return _queue_relay_update_from_app(device_id, "beecounter", "update_beecounter", slot)


@app.get("/api/v1/app/devices/{device_id}/insights", dependencies=[Depends(require_hivepal_service_key)])
def get_device_insights(device_id: str, lookback_days: int = Query(14, ge=1, le=90),
                        user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    # Anchor "now" to the dataset end so the demo is meaningful regardless of
    # the container's wall clock.
    end_at = STORE["data_end"]
    start_at = end_at - timedelta(days=lookback_days)
    rows = sorted((m for m in _measurements_for(device_id) if m["measured_at"] >= start_at),
                  key=lambda m: m["measured_at"])
    alerts = compute_insights(rows, now=end_at)
    return {
        "device_id": device_id,
        "computed_at": end_at.isoformat(),
        "lookback_days": lookback_days,
        "measurement_count": len(rows),
        "alerts": [a.model_dump() for a in alerts],
    }


@app.get("/api/v1/app/devices/{device_id}/insights/summary", dependencies=[Depends(require_hivepal_service_key)])
def get_device_insights_summary(device_id: str, user_id: str = Depends(require_user_id)):
    require_device_role(user_id, device_id, ["owner", "admin", "viewer"])
    end_at = STORE["data_end"]
    start_at = end_at - timedelta(days=14)
    rows = sorted((m for m in _measurements_for(device_id) if m["measured_at"] >= start_at),
                  key=lambda m: m["measured_at"])
    alerts = compute_insights(rows, now=end_at)
    s = summarize(device_id, alerts, end_at)
    return {
        "device_id": s.device_id,
        "computed_at": s.computed_at.isoformat(),
        "alert_count": s.alert_count,
        "highest_severity": s.highest_severity,
        "highest_alert": s.highest_alert.model_dump() if s.highest_alert else None,
        "categories": s.categories,
    }
