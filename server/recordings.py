"""On-request audio from a hive's HiveInside node.

A HiveInside 0.6.0 node can be asked for live audio: an authenticated request
makes it capture 16 kHz PCM16 and push it out over BLE, and the HiveHub relays
that stream here. This module owns the recording's whole life:

    request  -> a queued device command + a `requested` row
    stream   -> the hub POSTs a chunked body that is written to disk as it lands
    finalize -> the hub reports byte count, CRC and quality counters
    play     -> the browser reads it live (raw PCM slices) or later (a WAV file)

Two decisions shape everything here.

**Raw PCM on disk, WAV on the way out.** The file is exactly what the node sent:
16 kHz, 16-bit little-endian, mono. A `.wav` download is that file behind a
44-byte header generated per request. Storing WAV instead would mean either
rewriting the header every time the file grows (it grows while a live session is
in flight) or storing the audio twice.

**A live session is readable before it is finished.** The dashboard polls
`/pcm?offset=` for whatever has landed so far and plays it through the Web Audio
API. That is why the upload writes and flushes incrementally rather than
buffering: a recording still being written is a normal, useful thing to read.

Recordings are kept until somebody deletes them — no retention sweep. See
docs/audio-recording.md.
"""

import struct
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from starlette.requests import ClientDisconnect

from auth import require_api_key, require_device_key
from commands import create_command
from config import MAX_RECORDING_BYTES, RECORDINGS_DIR, logger
from db import get_conn
from devices import ensure_device_config
from schemas import MAX_HIVES, DeviceCommandIn, RecordingFinalizeIn

router = APIRouter()

# The node only ever produces this format; it is recorded per row anyway so a
# future rate change cannot silently mis-play everything already stored.
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
CHANNELS = 1

# The node caps a session at 60 s and the hub caps its relay at the same. A
# request for more is a mistake somewhere, so refuse it rather than queue a
# command that cannot be honoured.
MAX_DURATION_S = 60

# How much of a live recording the player may fetch in one poll. Large enough
# that a 250 ms poll never falls behind 32 kB/s, small enough to stay a normal
# JSON-sized response.
MAX_PCM_SLICE = 256 * 1024

# When to give up on a recording the hub stopped talking about.
#
# A row only leaves "streaming" when the hub POSTs /finalize. If the hub resets
# mid-session, loses WiFi on the last hop, or the POST is simply lost, nothing
# else ever moves it — and the dashboard shows "streaming" for a session that
# ended long ago, next to audio that is sitting on disk perfectly playable. That
# is the same failure the device-command queue solves with
# expire_stale_claimed_commands(), and it is solved the same way here.
#
# The two states get different windows because they wait on different things.
# "requested" waits for the hub's next wake, which is a whole send interval and
# is legitimately long. "streaming" waits only for a session that the node caps
# at 60 s plus its upload, so several minutes of silence there means the hub is
# gone, not slow.
RECORDING_REQUESTED_TIMEOUT_MIN = 45
RECORDING_STREAMING_TIMEOUT_MIN = 5

# What expire_stale_recordings() writes when the hub never reported. These are
# guesses, and finalize_from_command_result() lets the hub's own verdict replace
# them when it turns up late — a hub that could not post its result straight
# after a session holds it and delivers it on its next cycle.
SWEEP_NO_AUDIO_MSG = "the hub started uploading but no audio arrived"
SWEEP_NEVER_PICKED_UP_MSG = ("the hub never picked this request up \u2014 offline, "
                             "or the command was dropped")


def _path_for(device_id: str, recording_id: int) -> Path:
    """Where a recording's audio lives.

    The filename is derived from ids the database issued, never from anything a
    client sent, so there is no path to traverse out of. The device id is still
    sanitised because it reaches us from a device-authenticated header and ends
    up as a directory name.
    """
    safe_device = "".join(c for c in device_id if c.isalnum() or c in "-_")
    if not safe_device:
        raise HTTPException(status_code=400, detail="invalid device id")
    return RECORDINGS_DIR / safe_device / f"{int(recording_id)}.pcm"


def _row_to_dict(r) -> dict:
    """One recording row as the API presents it.

    `confirmation` is the honest answer to the only question a listener really
    has — can I trust what I am about to hear? Three states, because there are
    genuinely three:

      "verified"  the hub sent its detailed report and the checksum matched
      "reported"  the detailed report arrived but the checksum did not match,
                  or the hub sent no checksum at all
      "confirmed" the hub reported the session succeeded, but the detailed
                  report never arrived, so nothing verified the bytes
      "unknown"   nothing came back at all; the audio is whatever landed

    Collapsing the middle case into either neighbour is what made this panel
    shout at a recording that was almost certainly fine.
    """
    (rec_id, device_id, hive_index, status, requested_at, started_at,
     completed_at, duration_ds, gain_db, sample_rate, num_bytes, crc32,
     device_bytes, device_crc32, dropped_bytes, gaps, ring_overruns,
     ring_dropped_bytes, clipped_pct, error,
     requested_by, command_status, command_message) = r
    seconds = (num_bytes or 0) / float(sample_rate * BYTES_PER_SAMPLE) if sample_rate else 0.0
    both_crcs = crc32 is not None and device_crc32 is not None
    crc_ok = both_crcs and crc32 == device_crc32
    crc_mismatch = both_crcs and crc32 != device_crc32
    if device_bytes is not None:
        confirmation = "verified" if crc_ok else "reported"
    elif command_status == "done":
        confirmation = "confirmed"
    else:
        confirmation = "unknown"
    # Absent checksums are not evidence of damage — older hubs simply do not
    # send them — so only a checksum that actually disagrees breaks `complete`.
    complete = (
        status == "ready"
        and confirmation != "unknown"
        and not crc_mismatch
        and not dropped_bytes
        and not gaps
        and not ring_overruns
    )
    return {
        "id": rec_id,
        "device_id": device_id,
        "hive_index": hive_index,
        "status": status,
        "requested_at": requested_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "requested_duration_s": (duration_ds or 0) / 10.0,
        "gain_db": gain_db,
        "sample_rate": sample_rate,
        "bytes": num_bytes,
        "seconds": round(seconds, 2),
        "dropped_bytes": dropped_bytes,
        "gaps": gaps,
        "ring_overruns": ring_overruns,
        "ring_dropped_bytes": ring_dropped_bytes,
        "clipped_pct": clipped_pct,
        "complete": complete,
        "crc_ok": crc_ok,
        "confirmation": confirmation,
        # What the hub itself said about the session. Worth surfacing verbatim:
        # when something goes wrong it is usually more specific than anything
        # this side can infer.
        "hub_message": command_message,
        "error": error,
        "requested_by": requested_by,
    }


# The command row is joined in because it is a second, independent account of
# how a session went — and a more reliable one. /finalize is a single
# fire-and-forget POST that a reset or a lost packet takes with it, while the
# command result has retries, a stale sweep and a crash-safe marker behind it.
# Reading both means a recording can say "the hub confirmed this" even when the
# detailed report never arrived, instead of implying the audio is suspect.
_SELECT = """
    SELECT r.id, r.device_id, r.hive_index, r.status, r.requested_at,
           r.started_at, r.completed_at, r.duration_ds, r.gain_db,
           r.sample_rate, r.bytes, r.crc32, r.device_bytes, r.device_crc32,
           r.dropped_bytes, r.gaps, r.ring_overruns, r.ring_dropped_bytes,
           r.clipped_pct, r.error, r.requested_by,
           c.status, c.result->>'message'
    FROM hive_recordings r
    LEFT JOIN device_commands c ON c.id = r.command_id
"""


def expire_stale_recordings() -> None:
    """Resolve recordings the hub stopped reporting on.

    Audio that arrived is still audio: a session whose ``/finalize`` never
    landed has a complete, playable file on disk and only lacks the hub's
    account of how it went. So it is marked ready and the missing report is
    stated, rather than being discarded or left looking in-flight forever.

    With no bytes at all there is nothing to keep, and the row says so instead
    of implying a recording that never existed.

    Cheap and idempotent — called before reading recordings, so no background
    job is needed. Deliberately does NOT touch crc32/device_bytes: leaving them
    NULL is what tells the reader this recording was never verified.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hive_recordings
                SET status = 'ready',
                    completed_at = now(),
                    error = COALESCE(error,
                        'the hub did not send its report for this session')
                WHERE status = 'streaming'
                  AND bytes > 0
                  AND requested_at < now() - (%s || ' minutes')::interval;
                """,
                (RECORDING_STREAMING_TIMEOUT_MIN,),
            )
            cur.execute(
                """
                UPDATE hive_recordings
                SET status = 'failed',
                    completed_at = now(),
                    error = COALESCE(error, %s)
                WHERE status = 'streaming'
                  AND bytes = 0
                  AND requested_at < now() - (%s || ' minutes')::interval;
                """,
                (SWEEP_NO_AUDIO_MSG, RECORDING_STREAMING_TIMEOUT_MIN),
            )
            cur.execute(
                """
                UPDATE hive_recordings
                SET status = 'failed',
                    completed_at = now(),
                    error = COALESCE(error, %s)
                WHERE status = 'requested'
                  AND requested_at < now() - (%s || ' minutes')::interval;
                """,
                (SWEEP_NEVER_PICKED_UP_MSG, RECORDING_REQUESTED_TIMEOUT_MIN),
            )
            conn.commit()


def finalize_from_command_result(device_id: str, command_id: int,
                                 success: bool,
                                 message: Optional[str] = None) -> None:
    """Resolve a recording from its command result.

    The hub reports on a session twice: once through ``/finalize`` with the byte
    count, CRC and quality counters, and once through the ordinary command
    result. Only the second one is dependable — it is retried, swept when it
    goes stale, and written under the same crash-safe marker as every other
    command — while ``/finalize`` is a single fire-and-forget POST that a reset
    or a dropped packet takes with it.

    So the command result closes the row, and a later ``/finalize`` only adds
    detail to it. A recording that reaches here without its detailed report is
    "confirmed" rather than "verified": the hub said the session succeeded,
    nothing checked the bytes. That is worth saying plainly, and it is much
    closer to the truth than leaving the row streaming until a sweep declares
    contact lost.

    Only touches rows still in flight, so a `/finalize` that already landed
    keeps its more specific verdict.
    """
    failed_msg = (message or "").strip() or "the hub reported this session failed"
    empty_msg = "the hub reported success but no audio arrived"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hive_recordings
                SET status = CASE WHEN %s AND bytes > 0 THEN 'ready'
                                  ELSE 'failed' END,
                    completed_at = now(),
                    error = CASE WHEN %s AND bytes > 0 THEN error
                                 WHEN %s THEN COALESCE(error, %s)
                                 ELSE COALESCE(error, %s) END
                WHERE command_id = %s AND device_id = %s
                  AND status IN ('requested', 'streaming');
                """,
                (success, success, success, empty_msg, failed_msg,
                 command_id, device_id),
            )
            if cur.rowcount:
                logger.info("recording for command %s closed by command result "
                            "(success=%s)", command_id, success)
            elif not success:
                # Late: the sweep already failed the row with a guess. The hub's
                # reason is better than the guess, so it replaces it — but only
                # a sweep message, never a verdict /finalize wrote.
                cur.execute(
                    """
                    UPDATE hive_recordings
                    SET error = %s
                    WHERE command_id = %s AND device_id = %s
                      AND status = 'failed'
                      AND error IN (%s, %s);
                    """,
                    (failed_msg, command_id, device_id,
                     SWEEP_NO_AUDIO_MSG, SWEEP_NEVER_PICKED_UP_MSG),
                )
                if cur.rowcount:
                    logger.info("recording for command %s: late command result "
                                "replaced the stale-sweep message", command_id)
            conn.commit()


def get_recording(recording_id: int) -> dict:
    expire_stale_recordings()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT + " WHERE r.id = %s;", (recording_id,))
            r = cur.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="recording not found")
    return _row_to_dict(r)


def list_recordings(device_id: str, hive_index: Optional[int] = None,
                    limit: int = 50) -> list[dict]:
    expire_stale_recordings()
    # Every column is qualified: device_commands carries id, device_id and
    # status of its own, so an unqualified name here is ambiguous SQL.
    sql = _SELECT + " WHERE r.device_id = %s"
    params: list = [device_id]
    if hive_index is not None:
        sql += " AND r.hive_index = %s"
        params.append(hive_index)
    sql += " ORDER BY r.requested_at DESC LIMIT %s;"
    params.append(max(1, min(limit, 500)))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]


def request_recording(device_id: str, hive_index: int, duration_s: float,
                      gain_db: int = 0, requested_by: Optional[str] = None) -> dict:
    """Create the row and queue the command that fills it.

    Returns immediately — the hub only picks the command up on its next wake, so
    the caller gets a `requested` recording and polls it. Live listening is a
    sequence of these: each one is capped at 60 s by the node, and "continue
    listening" queues the next.
    """
    if not 1 <= hive_index <= MAX_HIVES:
        raise HTTPException(status_code=400,
                            detail=f"hive_index must be between 1 and {MAX_HIVES}")
    if duration_s < 0 or duration_s > MAX_DURATION_S:
        raise HTTPException(
            status_code=400,
            detail=f"duration must be between 0 (live) and {MAX_DURATION_S} seconds",
        )
    gain_db = max(-20, min(20, int(gain_db)))
    ensure_device_config(device_id)

    duration_ds = int(round(duration_s * 10))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hive_recordings
                    (device_id, hive_index, duration_ds, gain_db, sample_rate,
                     requested_by)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (device_id, hive_index, duration_ds, gain_db, SAMPLE_RATE,
                 requested_by),
            )
            recording_id = cur.fetchone()[0]
            conn.commit()

    # The command carries no URL: the hub derives the upload target from its own
    # device id. A hub that posted microphone audio to whatever address a queued
    # command named would be one malformed command away from streaming a garden
    # to a stranger.
    #
    # The row exists before the command does, because the command has to carry
    # its id. If queueing then fails, that row would sit at "requested" forever
    # waiting for a hub that was never told anything — indistinguishable from a
    # hub that is merely asleep. So mark it failed and say why, rather than
    # leaving a request nothing will ever answer.
    try:
        cmd = create_command(device_id, DeviceCommandIn(
            command_type="record_audio",
            payload={
                "slot": hive_index,
                "recording_id": recording_id,
                "duration_ds": duration_ds,
                "gain_db": gain_db,
            },
        ))
    except Exception as exc:
        logger.exception("could not queue record_audio for recording %s", recording_id)
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE hive_recordings SET status = 'failed', completed_at = now(), "
                    "error = %s WHERE id = %s;",
                    (f"could not queue the command for the hub: {exc}", recording_id),
                )
                conn.commit()
        raise HTTPException(
            status_code=500,
            detail="the recording could not be queued for the hub",
        ) from exc
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE hive_recordings SET command_id = %s WHERE id = %s;",
                        (cmd["id"], recording_id))
            conn.commit()
    return {"id": recording_id, "status": "requested", "command_id": cmd["id"],
            "hive_index": hive_index, "duration_s": duration_ds / 10.0}


def delete_recording(recording_id: int) -> dict:
    """Remove the row and its audio. Idempotent enough to be safe to retry."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT device_id FROM hive_recordings WHERE id = %s;",
                        (recording_id,))
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="recording not found")
            device_id = r[0]
            cur.execute("DELETE FROM hive_recordings WHERE id = %s;", (recording_id,))
            conn.commit()
    path = _path_for(device_id, recording_id)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - disk-level failure
        logger.warning("could not delete recording file %s: %s", path, exc)
    return {"status": "deleted", "id": recording_id}


def wav_header(data_bytes: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A 44-byte canonical PCM WAV header for `data_bytes` of audio.

    Generated per request rather than stored, so the same file on disk serves
    both the live PCM reader and a .wav download, and a growing recording never
    needs its header rewritten.
    """
    byte_rate = sample_rate * CHANNELS * BYTES_PER_SAMPLE
    block_align = CHANNELS * BYTES_PER_SAMPLE
    return b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 1, CHANNELS, sample_rate, byte_rate,
                    block_align, BYTES_PER_SAMPLE * 8) + \
        b"data" + struct.pack("<I", data_bytes)


# ── Device-authenticated ingest ─────────────────────────────────────────────

@router.post("/api/v1/devices/{device_id}/recordings/{recording_id}/stream",
             dependencies=[Depends(require_device_key)])
async def stream_recording(device_id: str, recording_id: int, request: Request):
    """Receive a live audio stream from the hub and write it to disk as it lands.

    The body arrives chunked with no declared length — a live session's size is
    not known until it ends — so it is consumed incrementally and flushed after
    every chunk. Flushing matters: the dashboard reads this file *while it is
    being written*, and buffered writes would make live playback lag by however
    much the OS felt like holding.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM hive_recordings WHERE id = %s AND device_id = %s;",
                (recording_id, device_id),
            )
            r = cur.fetchone()
            if not r:
                raise HTTPException(status_code=404, detail="recording not found")
            cur.execute(
                "UPDATE hive_recordings SET status = 'streaming', started_at = now() "
                "WHERE id = %s;",
                (recording_id,),
            )
            conn.commit()

    path = _path_for(device_id, recording_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    truncated = False
    disconnected = False
    with open(path, "wb") as fh:
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if written + len(chunk) > MAX_RECORDING_BYTES:
                    # Keep what fits and stop reading. The node bounds itself at
                    # 60 s, so this only fires for a hub that has gone wrong —
                    # and half a recording plus an explicit note beats an
                    # unbounded write.
                    chunk = chunk[: max(0, MAX_RECORDING_BYTES - written)]
                    truncated = True
                if chunk:
                    fh.write(chunk)
                    fh.flush()
                    written += len(chunk)
                if truncated:
                    break
        except ClientDisconnect:
            # The hub hung up before the body ended — or, behind a reverse
            # proxy, simply did not wait for this response: Traefik cancels the
            # upstream request (logging a 499) the moment its client goes, even
            # after a complete body. A hub whose BLE session fails to start
            # sends the terminating chunk and closes at once, so this is the
            # ordinary shape of that failure, not a server fault.
            #
            # Letting it propagate cost twice: a traceback in the log for every
            # failed session, and the byte count below never written, so audio
            # that DID land before a mid-session drop was recorded as 0 bytes
            # and later swept as "no audio arrived". Keep what arrived. The
            # row's error is deliberately left alone: the hub's command result
            # carries the real reason, and finalize_from_command_result only
            # fills an error that is still empty.
            disconnected = True

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE hive_recordings SET bytes = %s, filename = %s WHERE id = %s;",
                (written, path.name, recording_id),
            )
            if truncated:
                cur.execute(
                    "UPDATE hive_recordings SET error = %s WHERE id = %s;",
                    (f"truncated at the server cap of {MAX_RECORDING_BYTES} bytes",
                     recording_id),
                )
            conn.commit()
    if disconnected:
        logger.warning("recording %s: hub disconnected after %s bytes",
                       recording_id, written)
        # Nobody is left to read this; it is returned for symmetry and tests.
        return {"status": "disconnected", "bytes": written}
    logger.info("recording %s: received %s bytes%s", recording_id, written,
                " (truncated)" if truncated else "")
    return {"status": "ok", "bytes": written}


@router.post("/api/v1/devices/{device_id}/recordings/{recording_id}/finalize",
             dependencies=[Depends(require_device_key)])
def finalize_recording(device_id: str, recording_id: int,
                       payload: RecordingFinalizeIn):
    """Record how the session actually went.

    Called even when it went badly, and that is the point: a row that never
    hears back is indistinguishable from a hub that died mid-session, and the
    dashboard would show "recording…" forever.
    """
    status = "ready" if payload.ok else "failed"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE hive_recordings
                SET status = %s,
                    completed_at = now(),
                    crc32 = %s,
                    device_bytes = %s,
                    device_crc32 = %s,
                    dropped_bytes = %s,
                    gaps = %s,
                    ring_overruns = %s,
                    ring_dropped_bytes = %s,
                    clipped_pct = %s,
                    device_error = %s,
                    sample_rate = COALESCE(%s, sample_rate),
                    error = COALESCE(%s, error)
                WHERE id = %s AND device_id = %s;
                """,
                (status, payload.crc32, payload.device_bytes, payload.device_crc32,
                 payload.dropped_bytes, payload.gaps, payload.ring_overruns,
                 payload.ring_dropped_bytes, payload.clipped_pct,
                 payload.device_error, payload.sample_rate, payload.error,
                 recording_id, device_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="recording not found")
            conn.commit()
    return {"status": status}


# ── Playback ────────────────────────────────────────────────────────────────

def recording_pcm(recording_id: int, offset: int, limit: int) -> Response:
    """Raw PCM from `offset`, for the live player.

    Returns 204 when there is nothing new yet rather than an empty 200: the
    player polls this several times a second while a session is in flight, and
    "no new audio yet" is the common case, not an error.

    `X-Recording-Status` and `X-Recording-Offset` let the player know when to
    stop polling without a second request.
    """
    rec = get_recording(recording_id)
    path = _path_for(rec["device_id"], recording_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no audio for this recording")
    size = path.stat().st_size
    offset = max(0, offset)
    limit = max(1, min(limit, MAX_PCM_SLICE))
    headers = {
        "X-Recording-Status": rec["status"],
        "X-Recording-Size": str(size),
        "X-Recording-Offset": str(offset),
        "Cache-Control": "no-store",
    }
    if offset >= size:
        return Response(status_code=204, headers=headers)
    with open(path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(limit)
    headers["X-Recording-Next-Offset"] = str(offset + len(data))
    return Response(content=data, media_type="application/octet-stream",
                    headers=headers)


def recording_wav(recording_id: int) -> StreamingResponse:
    """The finished recording as a downloadable/playable WAV.

    The header is generated here and the payload streamed from disk, so nothing
    the size of a recording is ever held in memory.
    """
    rec = get_recording(recording_id)
    path = _path_for(rec["device_id"], recording_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no audio for this recording")
    size = path.stat().st_size
    header = wav_header(size, rec["sample_rate"] or SAMPLE_RATE)

    def body():
        yield header
        with open(path, "rb") as fh:
            while True:
                block = fh.read(64 * 1024)
                if not block:
                    break
                yield block

    return StreamingResponse(
        body(),
        media_type="audio/wav",
        headers={
            "Content-Length": str(len(header) + size),
            "Content-Disposition":
                f'inline; filename="hive{rec["hive_index"]}-recording-{recording_id}.wav"',
            "Cache-Control": "no-store",
        },
    )


# ── Master-key API (server-to-server / tooling) ─────────────────────────────

@router.post("/api/v1/devices/{device_id}/commands/record-audio",
             dependencies=[Depends(require_api_key)])
def queue_recording(device_id: str, slot: int = Query(1),
                    duration: float = Query(0.0),
                    gain_db: int = Query(0)):
    """Ask a hive for audio. `duration=0` is a live session (node-capped at 60 s)."""
    return request_recording(device_id, slot, duration, gain_db)


@router.get("/api/v1/devices/{device_id}/recordings",
            dependencies=[Depends(require_api_key)])
def list_device_recordings(device_id: str, hive: Optional[int] = Query(None),
                           limit: int = Query(50)):
    return {"recordings": list_recordings(device_id, hive, limit)}


@router.get("/api/v1/recordings/{recording_id}",
            dependencies=[Depends(require_api_key)])
def read_recording(recording_id: int):
    return get_recording(recording_id)


@router.get("/api/v1/recordings/{recording_id}/pcm",
            dependencies=[Depends(require_api_key)])
def read_recording_pcm(recording_id: int, offset: int = Query(0),
                       limit: int = Query(MAX_PCM_SLICE)):
    return recording_pcm(recording_id, offset, limit)


@router.get("/api/v1/recordings/{recording_id}/audio.wav",
            dependencies=[Depends(require_api_key)])
def read_recording_wav(recording_id: int):
    return recording_wav(recording_id)


@router.delete("/api/v1/recordings/{recording_id}",
               dependencies=[Depends(require_api_key)])
def remove_recording(recording_id: int):
    return delete_recording(recording_id)
