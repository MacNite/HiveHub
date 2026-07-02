"""Firmware releases: OTA update checks, release registration, binary
upload/download and board/architecture resolution."""

import os
import re
import zlib
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from auth import require_api_key, require_device_key
from config import FIRMWARE_DIR, MAX_FIRMWARE_BYTES, PUBLIC_BASE_URL, logger
from db import get_conn
from devices import ensure_device_config, get_device_owner_id
from schemas import FirmwareReleaseIn, normalize_firmware_target

router = APIRouter()


def parse_version(v: str) -> tuple:
    parts = []
    for p in v.split("."):
        try:
            parts.append(int("".join(ch for ch in p if ch.isdigit()) or "0"))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def latest_release_for_owner(target: str, owner_user_id: Optional[str],
                             board: Optional[str] = None):
    """Most recent active release for a target, owner-first with global fallback.

    Returns the owner's own release when one exists, otherwise the newest global
    (owner_user_id IS NULL) "official" release. ``ORDER BY (owner_user_id IS NULL)``
    sorts owner-specific rows (false) ahead of global rows (true). Returns a
    (version, filename, crc32, owner_user_id) tuple, or None when nothing matches.

    When ``board`` is given (the dual-board ``hivescale`` target), only releases
    built for that exact board match — a 30-pin ESP32 (Xtensa) is never offered an
    ESP32-C6 (RISC-V) image or vice versa. ``board=None`` (single-architecture
    sub-device targets) applies no board filter.
    """
    sql = (
        "SELECT version, filename, crc32, owner_user_id "
        "FROM firmware_releases "
        "WHERE active = true AND target = %s "
        "  AND (owner_user_id = %s OR owner_user_id IS NULL) "
    )
    params: list = [target, owner_user_id]
    if board is not None:
        sql += "  AND board = %s "
        params.append(board)
    sql += "ORDER BY (owner_user_id IS NULL), created_at DESC, id DESC LIMIT 1;"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            return cur.fetchone()


def other_board_releases(target: str, owner_user_id: Optional[str],
                         device_board: Optional[str],
                         current_version: Optional[str]) -> list:
    """Releases available for boards OTHER than this device's, newer than what it
    runs.

    Used to explain why a just-uploaded image is not offered here: an ESP32
    device is never shown an ESP32-C6 build as "latest" (and vice versa), so
    without this hint an upload for the wrong board appears to silently vanish
    from the panel. Returns ``[{"board": ..., "version": ...}]`` with the newest
    version per non-matching board, and an empty list when nothing newer exists
    elsewhere. ``device_board=None`` (device has not reported its board yet)
    lists every board's newer release.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT board, version FROM firmware_releases "
                "WHERE active = true AND target = %s "
                "  AND (owner_user_id = %s OR owner_user_id IS NULL) "
                "  AND board IS NOT NULL "
                "  AND board IS DISTINCT FROM %s;",
                (target, owner_user_id, device_board),
            )
            rows = cur.fetchall()
    # Keep the newest version per board, dropping anything not actually newer than
    # what the device runs (version comparison is done here, not in SQL).
    best: dict = {}
    for board, version in rows:
        if current_version and parse_version(version) <= parse_version(current_version):
            continue
        if board not in best or parse_version(version) > parse_version(best[board]):
            best[board] = version
    return [{"board": b, "version": v} for b, v in sorted(best.items())]


def record_device_board(device_id: str, board: str) -> None:
    """Persist the board/architecture a device reported on its OTA check.

    No-op when the row does not exist (unclaimed device) or the value is unchanged.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET last_board = %s "
                "WHERE device_id = %s AND last_board IS DISTINCT FROM %s;",
                (board, device_id, board),
            )
            conn.commit()


def get_device_board(device_id: str) -> Optional[str]:
    """Return the board this device last reported on its OTA check, or None."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT last_board FROM devices WHERE device_id = %s;", (device_id,))
            r = cur.fetchone()
    return r[0] if r and r[0] in FIRMWARE_BOARDS else None


def get_approved_firmware_version(device_id: str) -> Optional[str]:
    """Return the firmware version the owner has approved for this device, if any."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT approved_firmware_version FROM devices WHERE device_id = %s;",
                (device_id,),
            )
            r = cur.fetchone()
    return r[0] if r and r[0] else None


def set_approved_firmware_version(device_id: str, version: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devices SET approved_firmware_version = %s WHERE device_id = %s;",
                (version, device_id),
            )
            conn.commit()


@router.get("/api/v1/devices/{device_id}/firmware", dependencies=[Depends(require_device_key)])
def check_firmware(device_id: str, version: str = Query("0.0.0"),
                   target: str = Query("hivescale"),
                   board: str = Query("")):
    ensure_device_config(device_id)
    # Deployed devices query ?target=hivescale; accept the "hivehub" alias too so
    # newer tooling can use either name. Canonical value is "hivescale".
    target = normalize_firmware_target(target)
    no_update = {"update": False, "update_available": False}
    # Per-board OTA gating for the dual-board hivescale target: the device must
    # report which architecture it is so we never serve a cross-arch (non-bootable)
    # image. A request without a recognized board is not matched against any
    # release — safer than guessing and risking a bricked flash. (Field devices on
    # firmware that predates the ?board= param therefore stop auto-updating until
    # they are reflashed once via the AP portal; the single-architecture
    # beecounter / hiveinside relays are unaffected.)
    board = (board or "").strip().lower()
    if target == "hivescale":
        if board not in FIRMWARE_BOARDS:
            logger.info("OTA check from %s without a known board (%r); no update served",
                        device_id, board)
            return no_update
        match_board: Optional[str] = board
        # Remember the board so the HivePal status/approve flow can resolve the
        # latest release for THIS device's architecture.
        record_device_board(device_id, board)
    else:
        match_board = None
    owner_id = get_device_owner_id(device_id)
    r = latest_release_for_owner(target, owner_id, match_board)
    # NOTE: the "update" and "update_available" keys carry the same value. The
    # ESP32 firmware reads doc["update"] while older clients/docs use
    # "update_available"; we emit both so a field-name mismatch can never
    # silently disable OTA again. (no_update is defined at the top of this handler.)
    if not r:
        return no_update
    latest_version, filename = r[0], r[1]
    if parse_version(latest_version) <= parse_version(version):
        return no_update
    # Accept-to-apply gate for the ESP32 self-update: a newer hivescale build is
    # only served once the owner has approved THIS exact version for THIS device
    # in HivePal (POST /api/v1/app/devices/{id}/firmware/approve). Without an
    # approval the device keeps polling but never flashes, so publishing firmware
    # no longer auto-updates every scale. Sub-device images (beecounter /
    # hiveinside) are relayed explicitly via commands, not here, so they are
    # unaffected by this gate.
    if target == "hivescale" and get_approved_firmware_version(device_id) != latest_version:
        return no_update
    url = f"{PUBLIC_BASE_URL}/firmware/{filename}" if PUBLIC_BASE_URL else f"/firmware/{filename}"
    return {
        "update": True,
        "update_available": True,
        "version": latest_version,
        "url": url,
    }


# Allowed firmware targets, shared by the JSON registration endpoint and the
# multipart upload endpoint below.
FIRMWARE_TARGETS = ("hivescale", "beecounter", "hiveinside")

# Board/architecture labels for the dual-board "hivescale" target. These MUST match
# the firmware's HIVESCALE_BOARD_LABEL (config.h) and rename_firmware.py BOARD_LABELS
# so a device's ?board=... query lines up with the release it should receive.
FIRMWARE_BOARDS = ("esp32", "esp32-c6")


def board_from_filename(filename: str) -> Optional[str]:
    """Infer the board/architecture from a board-stamped firmware filename.

    rename_firmware.py names artifacts ``hivehub_esp32_<v>.bin`` and
    ``hivehub_esp32-c6_<v>.bin`` (legacy builds used the ``hivescale_`` prefix);
    detection keys off the board token, so either prefix works. 'esp32-c6'
    contains the substring 'esp32', so the C6 variants are matched first. Returns
    None when the name carries no recognizable board token.
    """
    n = (filename or "").lower()
    if "esp32-c6" in n or "esp32c6" in n or "xiao" in n:
        return "esp32-c6"
    if "esp32" in n:
        return "esp32"
    return None


def resolve_hivescale_board(target: str, declared_board: Optional[str],
                            filename: str) -> Optional[str]:
    """Determine and validate the board for a release at registration time.

    Publish guard (defence against shipping a cross-architecture image): for the
    dual-board ``hivescale`` target the board must be known AND consistent with the
    board-stamped filename. A declared board that disagrees with the filename, an
    unknown board value, or a filename with no board token are all rejected, so a
    C6 binary can never be registered as an ``esp32`` release. Non-hivescale
    (single-architecture) targets carry no board.
    """
    if target != "hivescale":
        return None
    from_name = board_from_filename(filename)
    declared = (declared_board or "").strip().lower() or None
    if declared is not None and declared not in FIRMWARE_BOARDS:
        raise HTTPException(
            status_code=400,
            detail=f"board must be one of {', '.join(FIRMWARE_BOARDS)}",
        )
    if declared is not None and from_name is not None and declared != from_name:
        raise HTTPException(
            status_code=400,
            detail=(f"board '{declared}' does not match the board in filename "
                    f"'{filename}' ('{from_name}') — refusing to publish a "
                    "possibly cross-architecture image"),
        )
    board = declared or from_name
    if board is None:
        raise HTTPException(
            status_code=400,
            detail=("cannot determine board for a hivescale release: pass board= "
                    "or name the file like hivehub_esp32_<v>.bin / "
                    "hivehub_esp32-c6_<v>.bin"),
        )
    return board

# A conservative filename pattern. Firmware filenames are referenced verbatim in
# download URLs and joined onto FIRMWARE_DIR, so we reject anything that is not a
# plain basename with a safe character set. This prevents path traversal
# (e.g. "../../etc/passwd") and surprising URL encodings.
_SAFE_FIRMWARE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def crc32_of_file(path: Path) -> int:
    """Compute CRC-32 (IEEE 802.3) of a file as an unsigned 32-bit value.

    The HiveHub uses this to verify a firmware download before flashing it or
    relaying it to a BeeCounter over I2C. Stored in a BIGINT to stay positive.
    """
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def upsert_firmware_release(version: str, filename: str, active: bool,
                            target: str, crc: int,
                            owner_user_id: Optional[str] = None,
                            board: Optional[str] = None) -> None:
    """Insert or update a firmware_releases row keyed on
    (owner_user_id, target, board, version).

    Releases are unique per (owner_user_id, target, board, version), so the same
    version can coexist across targets (hivescale / beecounter / hiveinside),
    across the two hivescale boards (esp32 / esp32-c6), and across owners.
    Re-uploading the same (owner, target, board, version) replaces it.
    ``owner_user_id=None`` registers a global / "official" release that any device
    may fall back to. ``board`` is the architecture for the dual-board hivescale
    target (None for single-architecture sub-device targets).
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO firmware_releases (version, filename, active, target, crc32, owner_user_id, board)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (COALESCE(owner_user_id, ''), target, COALESCE(board, ''), version) DO UPDATE SET
                    filename = EXCLUDED.filename,
                    active   = EXCLUDED.active,
                    crc32    = EXCLUDED.crc32;
                """,
                (version, filename, active, target, crc, owner_user_id, board),
            )
            conn.commit()


@router.post("/api/v1/firmware/releases", dependencies=[Depends(require_api_key)])
def create_firmware_release(payload: FirmwareReleaseIn):
    path = FIRMWARE_DIR / payload.filename
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"Firmware file '{payload.filename}' not found in firmware directory")
    # Publish guard: a hivescale release must carry a board that matches its
    # board-stamped filename, so a C6 image can never be registered as esp32.
    board = resolve_hivescale_board(payload.target, payload.board, payload.filename)
    crc = crc32_of_file(path)
    upsert_firmware_release(
        payload.version, payload.filename, payload.active, payload.target, crc,
        board=board,
    )
    return {"status": "ok", "version": payload.version, "target": payload.target,
            "board": board, "crc32": crc}


@router.get("/firmware/{filename}")
def download_firmware(filename: str):
    path = FIRMWARE_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Firmware file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


async def store_firmware_upload(
    device_id: str,
    file: UploadFile,
    version: str,
    target: str,
    active: bool,
    board: str,
    owner_user_id: Optional[str],
) -> dict:
    """Validate, stream-to-disk and register an uploaded firmware binary.

    Shared by the HivePal app endpoint (owner-scoped release) and the local
    dashboard endpoint (global release, owner_user_id=None). Writes the image
    into FIRMWARE_DIR in bounded chunks, enforces the size cap, computes its
    CRC-32 and upserts the firmware_releases row.
    """
    normalized_version = version.strip()
    if not normalized_version:
        raise HTTPException(status_code=400, detail="version must not be empty")

    # Accept the "hivehub" alias for the canonical "hivescale" target, then
    # validate. Normalising here (before the filename fallback below) means a
    # HiveHub upload is stored as a hivescale release with no DB migration.
    target = normalize_firmware_target(target)
    if target not in FIRMWARE_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(f"target must be one of {', '.join(FIRMWARE_TARGETS)} "
                    f"('hivehub' is accepted as an alias for 'hivescale')"),
        )

    # Derive a safe basename. We prefer the uploaded filename but fall back to a
    # deterministic name built from target + version when it is missing or
    # unsafe, so a release always has a usable, predictable filename.
    raw_name = os.path.basename((file.filename or "").strip())
    if raw_name and _SAFE_FIRMWARE_FILENAME.match(raw_name):
        filename = raw_name
    else:
        safe_version = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized_version).strip("-") or "unversioned"
        filename = f"{target}-{safe_version}.bin"

    dest = FIRMWARE_DIR / filename
    # Resolve and confirm the destination stays inside FIRMWARE_DIR. This is a
    # second line of defence on top of the basename + regex checks above.
    firmware_root = FIRMWARE_DIR.resolve()
    if dest.resolve().parent != firmware_root:
        raise HTTPException(status_code=400, detail="Invalid firmware filename")

    # Publish guard: resolve + validate the board BEFORE writing the image to disk,
    # so a hivescale release whose declared board contradicts its filename (or which
    # carries no board token at all) is rejected and leaves nothing behind.
    release_board = resolve_hivescale_board(target, board, filename)

    FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)

    # Stream the upload to disk in bounded chunks so large images do not have to
    # be held fully in memory.
    bytes_written = 0
    too_large = False
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                # Enforce the size cap before writing so a flood of oversized
                # uploads cannot fill the disk.
                if bytes_written > MAX_FIRMWARE_BYTES:
                    too_large = True
                    break
                out.write(chunk)
    finally:
        await file.close()

    if too_large:
        # Remove the partial file so a rejected upload leaves nothing behind.
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(
            status_code=413,
            detail=f"Firmware exceeds the maximum allowed size of {MAX_FIRMWARE_BYTES} bytes",
        )

    if bytes_written == 0:
        # Don't leave an empty file behind or register a zero-byte release.
        try:
            dest.unlink()
        except FileNotFoundError:
            pass
        raise HTTPException(status_code=400, detail="Uploaded firmware file is empty")

    crc = crc32_of_file(dest)
    upsert_firmware_release(normalized_version, filename, active, target, crc,
                            owner_user_id=owner_user_id, board=release_board)

    return {
        "status": "ok",
        "version": normalized_version,
        "filename": filename,
        "target": target,
        "board": release_board,
        "active": active,
        "size_bytes": bytes_written,
        "crc32": crc,
    }
