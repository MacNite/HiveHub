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
    """Highest-version active release for a target, owner-preferred with global fallback.

    Returns the release with the greatest version number available to this owner,
    preferring the owner's own build over a global ("official") build only as a
    tie-break at the same version. Returns a (version, filename, crc32,
    owner_user_id) tuple, or None when nothing matches.

    Version wins over recency on purpose: "latest" must mean the highest version,
    not the most recently inserted row. Ordering by ``created_at``/``id`` (as this
    did previously) let a lower version shadow a higher one — e.g. an owner-scoped
    0.22.7 outranking a newer global 0.22.8, or a re-uploaded older build jumping
    ahead — so the panel showed a stale "latest". Versions are compared with
    ``parse_version`` here rather than in SQL because the column is free-form text
    and lexical ordering mis-sorts (e.g. "0.22.10" < "0.22.9").

    When ``board`` is given (the dual-board ``hivescale`` target), only releases
    built for that exact board match — a 30-pin ESP32 (Xtensa) is never offered an
    ESP32-C6 (RISC-V) image or vice versa. ``board=None`` (single-architecture
    sub-device targets) applies no board filter.
    """
    sql = (
        "SELECT version, filename, crc32, owner_user_id, id "
        "FROM firmware_releases "
        "WHERE active = true AND target = %s "
        "  AND (owner_user_id = %s OR owner_user_id IS NULL) "
    )
    params: list = [target, owner_user_id]
    if board is not None:
        sql += "  AND board = %s "
        params.append(board)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    if not rows:
        return None
    # Highest version wins; at an equal version prefer the owner's own release
    # over the global one, with id as a final deterministic tiebreak. The unique
    # index on (owner, target, board, version) means genuine ties only span the
    # owner/global pair (or, when board is None, the two boards of one version).
    best = max(rows, key=lambda r: (parse_version(r[0]), r[3] is not None, r[4]))
    return best[:4]


def latest_release_board_null(target: str, owner_user_id: Optional[str]):
    """Highest-version active release for a target that carries NO board stamp.

    Used as the legacy fallback for board-aware relays: a deployment that
    published a single ``board = NULL`` image before board stamping existed keeps
    updating, without ever matching a release stamped for a specific (possibly
    wrong) architecture.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT version, filename, crc32, owner_user_id, id "
                "FROM firmware_releases "
                "WHERE active = true AND target = %s AND board IS NULL "
                "  AND (owner_user_id = %s OR owner_user_id IS NULL);",
                (target, owner_user_id),
            )
            rows = cur.fetchall()
    if not rows:
        return None
    best = max(rows, key=lambda r: (parse_version(r[0]), r[3] is not None, r[4]))
    return best[:4]


def latest_hiveinside_release(owner_user_id: Optional[str], board: Optional[str]):
    """Resolve the HiveInside OTA image for a sensor of the given board.

    Prefers a release stamped for that exact board; falls back to a legacy
    board-agnostic (``board IS NULL``) release so single-board deployments that
    predate board stamping keep updating. Never returns a release stamped for a
    DIFFERENT board, so a C6 image is never relayed to an nRF54 unit or vice
    versa. Returns a (version, filename, crc32, owner_user_id) tuple or None.
    """
    board = (board or "").strip().lower() or None
    if board is not None and board in HIVEINSIDE_BOARDS:
        r = latest_release_for_owner("hiveinside", owner_user_id, board)
        if r:
            return r
    # Board unknown or no image stamped for it: fall back to a legacy NULL-board
    # release only (never a release stamped for the other architecture).
    return latest_release_board_null("hiveinside", owner_user_id)


def reported_hiveinside_board(device_id: str, slot: int) -> Optional[str]:
    """The board a HiveInside sensor last reported for a HiveHub slot (1|2).

    Read from the most recent measurement's ``ble_{slot}_board`` (the HiveHub
    forwards the node's GATT "board" field). Returns a known HiveInside board or
    None when the sensor never reported one (older firmware / beacon-only node
    that never exposed the version characteristic)."""
    key = f"ble_{int(slot)}_board"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raw_json->>%s FROM measurements "
                "WHERE device_id = %s AND raw_json->>%s IS NOT NULL "
                "ORDER BY measured_at DESC LIMIT 1;",
                (key, device_id, key),
            )
            r = cur.fetchone()
    board = (r[0].strip().lower() if r and r[0] else None)
    return board if board in HIVEINSIDE_BOARDS else None


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
    # Ship the image size and CRC-32 alongside the URL. The ESP32 needs the size
    # when the download arrives without a Content-Length header — a reverse
    # proxy/CDN in front of the API (e.g. Cloudflare) may re-frame the response
    # as Transfer-Encoding: chunked — and uses the CRC to verify the received
    # image before committing it to the OTA partition. Both are best-effort:
    # older firmware ignores the extra keys, and a missing file/CRC degrades to
    # the previous behaviour on the device (size/CRC checks skipped).
    path = FIRMWARE_DIR / filename
    size = path.stat().st_size if path.is_file() else 0
    return {
        "update": True,
        "update_available": True,
        "version": latest_version,
        "url": url,
        "size": size,
        "crc32": r[2] or 0,
    }


# Allowed firmware targets, shared by the JSON registration endpoint and the
# multipart upload endpoint below.
FIRMWARE_TARGETS = ("hivescale", "beecounter", "hiveinside")

# Board/architecture labels per multi-board target. These MUST match the
# firmware's board labels (config.h HIVESCALE_BOARD_LABEL; the HiveInside JSON
# "board" field) and rename_firmware.py BOARD_LABELS so a device's ?board=... /
# reported board lines up with the release it should receive.
#
#  * hivescale is dual-architecture (Xtensa ESP32 vs RISC-V ESP32-C6).
#  * hiveinside now has two incompatible boards too: the original ESP32-C6
#    prototype and the current Nordic nRF54LM20A (a signed Zephyr/MCUboot image).
# beecounter stays single-architecture (board = NULL).
HIVESCALE_BOARDS = ("esp32", "esp32-c6")
HIVEINSIDE_BOARDS = ("esp32-c6", "nrf54lm20a")
# Kept for callers that only reason about the hivescale ?board= query.
FIRMWARE_BOARDS = HIVESCALE_BOARDS

# Targets whose releases are board-stamped, and the valid board set for each.
BOARDS_BY_TARGET = {
    "hivescale": HIVESCALE_BOARDS,
    "hiveinside": HIVEINSIDE_BOARDS,
}


def boards_for_target(target: str) -> tuple:
    """Valid board labels for a target, or () for single-architecture targets."""
    return BOARDS_BY_TARGET.get(target, ())


def board_from_filename(filename: str) -> Optional[str]:
    """Infer the board/architecture from a board-stamped firmware filename.

    rename_firmware.py names artifacts ``hivehub_esp32_<v>.bin`` /
    ``hivehub_esp32-c6_<v>.bin`` for the hub and ``hiveinside_esp32-c6_<v>.bin`` /
    ``hiveinside_nrf54lm20a_<v>.bin`` for the in-hive node; detection keys off the
    board token, so any prefix works. 'esp32-c6' contains the substring 'esp32',
    so the C6 and Nordic variants are matched before plain esp32. Returns None
    when the name carries no recognizable board token.
    """
    n = (filename or "").lower()
    if "nrf54" in n:
        return "nrf54lm20a"
    if "esp32-c6" in n or "esp32c6" in n or "xiao" in n:
        return "esp32-c6"
    if "esp32" in n:
        return "esp32"
    return None


def resolve_release_board(target: str, declared_board: Optional[str],
                          filename: str) -> Optional[str]:
    """Determine and validate the board for a release at registration time.

    Publish guard (defence against shipping a cross-architecture image):

      * ``hivescale`` (dual-board) — the board MUST be known AND consistent with
        the board-stamped filename, so a C6 binary can never register as ``esp32``.
      * ``hiveinside`` (now dual-board) — a declared/detected board is validated
        against the HiveInside board set and must agree with the filename, so an
        nRF54 Zephyr image can never register as an ``esp32-c6`` release. A board
        may be omitted (legacy single-architecture ``board = NULL`` release) for
        backward compatibility, but stamping one is strongly preferred.
      * every other (single-architecture) target carries no board.
    """
    boards = boards_for_target(target)
    if not boards:
        return None
    from_name = board_from_filename(filename)
    # A filename token that is valid for SOME target but not this one (e.g. an
    # "esp32" hub token on a hiveinside upload) must not be treated as this
    # target's board.
    if from_name not in boards:
        from_name = None
    declared = (declared_board or "").strip().lower() or None
    if declared is not None and declared not in boards:
        raise HTTPException(
            status_code=400,
            detail=f"board for {target} must be one of {', '.join(boards)}",
        )
    if declared is not None and from_name is not None and declared != from_name:
        raise HTTPException(
            status_code=400,
            detail=(f"board '{declared}' does not match the board in filename "
                    f"'{filename}' ('{from_name}') — refusing to publish a "
                    "possibly cross-architecture image"),
        )
    board = declared or from_name
    if board is None and target == "hivescale":
        # hivescale has always required a board; keep that strict.
        raise HTTPException(
            status_code=400,
            detail=("cannot determine board for a hivescale release: pass board= "
                    "or name the file like hivehub_esp32_<v>.bin / "
                    "hivehub_esp32-c6_<v>.bin"),
        )
    return board


# Backwards-compatible alias (older name).
resolve_hivescale_board = resolve_release_board

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
    # Publish guard: a multi-board release (hivescale / hiveinside) must carry a
    # board consistent with its board-stamped filename, so a cross-architecture
    # image (e.g. a C6 build registered as esp32, or an nRF54 image as esp32-c6)
    # is refused.
    board = resolve_release_board(payload.target, payload.board, payload.filename)
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
    # so a multi-board release (hivescale / hiveinside) whose declared board
    # contradicts its filename — or, for hivescale, carries no board token at all —
    # is rejected and leaves nothing behind.
    release_board = resolve_release_board(target, board, filename)

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
