"""Device registry and per-device state: config, channel (hive) display
names, ownership and temperature-compensation fitting."""

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from auth import require_device_key
from db import get_conn, hash_claim_code
from schemas import (
    MAX_HIVES,
    DeviceChannelsUpdateIn,
    DeviceConfig,
    DeviceConfigUpdate,
    HiveScaleCalibration,
    TempCoefficientFitIn,
)
from tempcomp import TEMP_SOURCE_FIELD, ema_temperatures, fit_temp_coefficient

router = APIRouter()


def ensure_device_config(
    device_id: str,
    claim_code: Optional[str] = None,
    firmware_version: Optional[str] = None,
    api_key: str = "",
    touch_last_seen: bool = False,
) -> bool:
    """Upsert the devices/device_configs rows for a device.

    last_seen_at is updated only when touch_last_seen is True — i.e. only for a
    genuine measurement upload. It must NOT be bumped when the HivePal app reads
    or edits config on the device's behalf (the common case here), otherwise an
    open dashboard polling config keeps a long-offline device looking "online".
    Device config/firmware polls record contact via verify_device_key instead.

    Returns True when this device is already claimed (``claimed_at`` is set). The
    firmware uses this to decide when it may stop sending its claim code: it must
    keep sending until the server confirms the claim, so that a rebuilt/restored
    backend (which has lost the claim_code_hash) re-learns it automatically
    instead of leaving the device permanently unclaimable.
    """
    claim_hash = hash_claim_code(claim_code) if claim_code else None
    key_hash = hashlib.sha256(api_key.encode()).hexdigest() if len(api_key) >= 16 else None
    # Leave last_seen_at untouched for non-device-contact calls: NULL on first
    # insert, unchanged on conflict.
    insert_last_seen = "now()" if touch_last_seen else "NULL"
    update_last_seen = "last_seen_at = now()," if touch_last_seen else ""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO devices (device_id, claim_code_hash, api_key_hash, last_seen_at, last_firmware_version)
                VALUES (%s, %s, %s, {insert_last_seen}, %s)
                ON CONFLICT (device_id) DO UPDATE
                    SET {update_last_seen}
                        last_firmware_version = COALESCE(EXCLUDED.last_firmware_version, devices.last_firmware_version),
                        claim_code_hash = COALESCE(devices.claim_code_hash, EXCLUDED.claim_code_hash),
                        api_key_hash = COALESCE(devices.api_key_hash, EXCLUDED.api_key_hash)
                RETURNING api_key_hash, claimed_at;
                """,
                (device_id, claim_hash, key_hash, firmware_version),
            )
            row = cur.fetchone()
            if key_hash and row and row[0] and row[0] != key_hash:
                raise HTTPException(status_code=401, detail="API key does not match this device")
            claimed = bool(row and row[1] is not None)
            cur.execute(
                """
                INSERT INTO device_configs (device_id) VALUES (%s)
                ON CONFLICT (device_id) DO NOTHING;
                """,
                (device_id,),
            )
            conn.commit()
    return claimed


# Column list shared by every device_configs read so the device-facing and
# app-facing config endpoints can never drift apart.
DEVICE_CONFIG_SELECT_COLUMNS = """
    device_id, send_interval_seconds, scale1_offset, scale1_factor,
    scale2_offset, scale2_factor, config_version,
    tempco_enabled, tempco_source, tempco_ref_temp_c,
    scale1_tempco_kg_per_c, scale2_tempco_kg_per_c,
    scale_offsets_by_hive
"""


def hive_scales_from_json(raw) -> list[HiveScaleCalibration]:
    """Turn the scale_offsets_by_hive JSONB map into a sorted hive_scales list.

    The column stores calibration for hives 3..MAX_HIVES as {hive_index: {offset,
    factor, scale}} (hives 1–2 live in the scale1/2 columns). Malformed / stray
    entries are skipped so a hand-edited row can never break the config read.
    """
    if not raw:
        return []
    out: list[HiveScaleCalibration] = []
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= MAX_HIVES):
            continue
        try:
            out.append(
                HiveScaleCalibration(
                    index=idx,
                    scale=int(val.get("scale", 0) or 0),
                    offset=int(val.get("offset", 0) or 0),
                    factor=float(val.get("factor", -7050.0)),
                )
            )
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda h: (h.index, h.scale))
    return out


def merge_hive_scales(current, updates: list[dict]) -> dict:
    """Merge the requested hive_scales updates into the stored JSONB map.

    ``updates`` are HiveScaleCalibrationIn dicts (from model_dump). Each fully or
    partially updates one hive entry — an omitted offset/factor keeps its current
    value — so a device reporting both fields overwrites cleanly while a partial
    edit never wipes the other field. Returns the new map to store.
    """
    merged = dict(current) if current else {}
    for hs in updates:
        idx = hs.get("index")
        if idx is None:
            continue
        key = str(int(idx))
        entry = dict(merged.get(key) or {})
        entry["scale"] = int(hs.get("scale") or entry.get("scale") or 0)
        if hs.get("offset") is not None:
            entry["offset"] = int(hs["offset"])
        if hs.get("factor") is not None:
            entry["factor"] = float(hs["factor"])
        entry.setdefault("offset", 0)
        entry.setdefault("factor", -7050.0)
        merged[key] = entry
    return merged


def device_config_row_to_model(r) -> DeviceConfig:
    return DeviceConfig(
        device_id=r[0], send_interval_seconds=r[1], scale1_offset=r[2],
        scale1_factor=r[3], scale2_offset=r[4], scale2_factor=r[5],
        config_version=r[6], tempco_enabled=r[7], tempco_source=r[8],
        tempco_ref_temp_c=r[9], scale1_tempco_kg_per_c=r[10],
        scale2_tempco_kg_per_c=r[11],
        hive_scales=hive_scales_from_json(r[12]),
    )


def fetch_device_config(device_id: str) -> DeviceConfig:
    ensure_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {DEVICE_CONFIG_SELECT_COLUMNS} "
                "FROM device_configs WHERE device_id = %s;",
                (device_id,),
            )
            r = cur.fetchone()
    return device_config_row_to_model(r)


@router.get("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_device_key)])
def get_device_config(device_id: str):
    return fetch_device_config(device_id)


@router.patch("/api/v1/devices/{device_id}/config", dependencies=[Depends(require_device_key)])
def update_device_config(device_id: str, patch: DeviceConfigUpdate):
    ensure_device_config(device_id)
    fields = patch.model_dump(exclude_unset=True)
    # hive_scales (hives 3..MAX_HIVES) is not a scalar column — it is merged into
    # the scale_offsets_by_hive JSONB map. Pull it out and handle it separately so
    # the generic column update below only sees real columns.
    hive_scales = fields.pop("hive_scales", None)
    if not fields and not hive_scales:
        return get_device_config(device_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if hive_scales:
                cur.execute(
                    "SELECT scale_offsets_by_hive FROM device_configs "
                    "WHERE device_id = %s FOR UPDATE;",
                    (device_id,),
                )
                row = cur.fetchone()
                merged = merge_hive_scales(row[0] if row else None, hive_scales)
                fields["scale_offsets_by_hive"] = psycopg.types.json.Jsonb(merged)
            assignments = [f"{k} = %({k})s" for k in fields]
            assignments.append("config_version = config_version + 1")
            assignments.append("updated_at = now()")
            fields["device_id"] = device_id
            cur.execute(
                f"UPDATE device_configs SET {', '.join(assignments)} WHERE device_id = %(device_id)s;",
                fields,
            )
            conn.commit()
    return get_device_config(device_id)


def get_device_owner_id(device_id: str) -> Optional[str]:
    """Return the HivePal user id of the device's owner, or None if unclaimed.

    A device has at most one ``owner`` membership (set at claim time); admins and
    viewers are added later via sharing. Owner-scoped firmware is keyed on this id.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id FROM device_members
                WHERE device_id = %s AND role = 'owner'
                ORDER BY created_at
                LIMIT 1;
                """,
                (device_id,),
            )
            r = cur.fetchone()
    return r[0] if r else None


def fetch_device_channels(device_id: str) -> dict:
    """Return the per-hive (scale-channel) display names for a device.

    ``names`` maps every stored hive index ("1".."18") to its custom name and is
    the canonical multi-hive shape the local dashboard consumes. ``scale_1/2_*``
    are kept for the HivePal app endpoints and older callers.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT channel_number, name FROM device_channels WHERE device_id = %s ORDER BY channel_number;",
                (device_id,),
            )
            rows = cur.fetchall()
    ch = {r[0]: r[1] for r in rows}
    return {
        "scale_1_display_name": ch.get(1),
        "scale_2_display_name": ch.get(2),
        "names": {str(num): name for num, name in ch.items() if name is not None},
    }


def apply_device_channels(device_id: str, payload: DeviceChannelsUpdateIn) -> dict:
    """Upsert the provided per-hive display names and return all of them."""
    # Collapse the legacy scale_1/2 fields and the general names[] map into one
    # {hive_index: name} set, dropping anything outside 1..MAX_HIVES.
    updates: dict[int, Optional[str]] = {}
    if payload.scale_1_display_name is not None:
        updates[1] = payload.scale_1_display_name
    if payload.scale_2_display_name is not None:
        updates[2] = payload.scale_2_display_name
    if payload.names:
        for key, name in payload.names.items():
            try:
                idx = int(key)
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= MAX_HIVES and name is not None:
                updates[idx] = name

    with get_conn() as conn:
        with conn.cursor() as cur:
            for ch_num, ch_name in updates.items():
                cur.execute(
                    """
                    INSERT INTO device_channels (device_id, channel_number, name)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (device_id, channel_number) DO UPDATE SET name = EXCLUDED.name;
                    """,
                    (device_id, ch_num, ch_name),
                )
            conn.commit()
    return fetch_device_channels(device_id)


def run_temp_compensation_fit(device_id: str, body: "TempCoefficientFitIn") -> dict:
    """Fit (and optionally apply) a load-cell temperature coefficient.

    Shared by the HivePal app endpoint and the local dashboard endpoint; callers
    are responsible for any authorization. See the app endpoint docstring above
    for the regression details.
    """
    cfg = fetch_device_config(device_id)
    source = body.temp_source or cfg.tempco_source
    temp_field = TEMP_SOURCE_FIELD[source]
    weight_field = "scale_1_weight_kg" if body.scale == 1 else "scale_2_weight_kg"

    end_at = body.end_at or datetime.now(timezone.utc)
    start_at = body.start_at or (end_at - timedelta(days=body.lookback_days))

    where = ["device_id = %s", "measured_at >= %s", "measured_at <= %s",
             f"{weight_field} IS NOT NULL", f"{temp_field} IS NOT NULL"]
    params: list[Any] = [device_id, start_at, end_at]
    if body.calibration_mode_only:
        where.append("calibration_mode IS TRUE")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {temp_field}, {weight_field} FROM measurements "
                f"WHERE {' AND '.join(where)} ORDER BY measured_at ASC;",
                params,
            )
            samples = cur.fetchall()

    # Smooth the temperature series the same way read-time compensation does, so
    # the coefficient is fitted in the regime it is applied in
    # (raw − coeff·(EMA(temp) − ref)). Rows come back ordered by measured_at ASC,
    # which is what the EMA needs.
    smoothed_temps = ema_temperatures([row[0] for row in samples])
    samples = [(t, row[1]) for t, row in zip(smoothed_temps, samples)]

    fit = fit_temp_coefficient(samples)
    fit.update(
        scale=body.scale,
        temp_source=source,
        window_start=start_at.isoformat(),
        window_end=end_at.isoformat(),
        applied=False,
    )

    if body.apply and fit["ok"]:
        coeff_field = (
            "scale1_tempco_kg_per_c" if body.scale == 1 else "scale2_tempco_kg_per_c"
        )
        patch = DeviceConfigUpdate(
            tempco_enabled=True,
            tempco_source=source,
            tempco_ref_temp_c=fit["ref_temp_c"],
            **{coeff_field: fit["coeff_kg_per_c"]},
        )
        update_device_config(device_id, patch)
        fit["applied"] = True

    return fit
