"""ASGI middleware and rate-limiting helpers."""

from typing import Optional

from fastapi import HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from slowapi.util import get_remote_address

from config import (
    MAX_FIRMWARE_BYTES,
    SD_IMPORT_MAX_FILE_SIZE,
    TRUST_PROXY_HEADERS,
    UPLOAD_ENVELOPE_BYTES,
)


# Paths whose responses must never be compressed. See SelectiveGZipMiddleware.
# Audio joins firmware here for the same reason and one more of its own. PCM is
# not meaningfully compressible, the WAV endpoint sets an explicit
# Content-Length that gzip would invalidate, and the live player reads exact
# byte offsets out of the PCM endpoint — a re-framed body would misalign every
# sample after the first poll.
NO_COMPRESS_PREFIXES = (
    "/firmware/",
    "/api/v1/recordings/",
    "/api/v1/local/recordings/",
    "/api/v1/app/recordings/",
)


class SelectiveGZipMiddleware:
    """GZip every response except firmware binary downloads.

    Compressing the measurement JSON is a large win, but applying it to
    ``GET /firmware/{filename}`` actively breaks OTA. A gzipped response is
    streamed without a ``Content-Length``, and the ESP32 sizes its download from
    that header: ``HTTPClient::getSize()`` returns -1, so the HiveInside relay
    fails with "invalid firmware content length -1" before it ever opens the BLE
    session, and the hub's own self-update falls back to the backend-supplied
    size. Firmware images are already-compact binaries, so compressing them costs
    CPU on both ends and saves nothing.

    Implemented as a wrapper rather than a GZipMiddleware subclass because
    Starlette's implementation decides to compress inside its own ASGI callable,
    with no hook for excluding a path.
    """

    def __init__(self, app, minimum_size: int = 1024,
                 exclude_prefixes: tuple = NO_COMPRESS_PREFIXES):
        self.app = app
        self.exclude_prefixes = exclude_prefixes
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(self.exclude_prefixes):
            await self.app(scope, receive, send)
            return
        await self.gzip(scope, receive, send)


class MaxBodySizeMiddleware:
    """Reject requests whose body exceeds the limit for their path.

    A single valid API key would otherwise let a client POST arbitrarily large
    JSON bodies, which are parsed into memory and (for measurements) persisted
    verbatim into ``raw_json`` — a storage/memory amplification vector. Capping
    the body closes it.

    Uploads legitimately exceed ``max_body_bytes``, so they get their own,
    larger caps rather than none: firmware binaries ``MAX_FIRMWARE_BYTES`` and
    SD imports ``SD_IMPORT_MAX_FILE_SIZE``, each plus some head-room for the
    multipart framing. Those routes also authenticate before they read the
    body (see uploads.py), so this is a backstop, not the only line.
    """

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    def _limit_for(self, scope) -> Optional[int]:
        """The byte cap for this request, or None for no cap."""
        if scope.get("method") != "POST":
            return self.max_body_bytes
        path = scope.get("path", "")
        if path.endswith("/firmware"):
            return MAX_FIRMWARE_BYTES + UPLOAD_ENVELOPE_BYTES
        if path.endswith("/measurements/import"):
            return SD_IMPORT_MAX_FILE_SIZE + UPLOAD_ENVELOPE_BYTES
        # An audio stream arrives with Transfer-Encoding: chunked and no length
        # at all — a live session's size is unknown until it ends — so buffering
        # it here to measure it would defeat the point of streaming it to disk.
        # It is authenticated and capped by MAX_RECORDING_BYTES as it is written.
        if path.endswith("/stream"):
            return None
        return self.max_body_bytes

    async def __call__(self, scope, receive, send):
        limit = self._limit_for(scope) if scope["type"] == "http" and self.max_body_bytes > 0 else None
        if limit is None:
            await self.app(scope, receive, send)
            return

        # Fast path: trust a declared Content-Length when present.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        await self._send_413(send)
                        return
                except ValueError:
                    pass
                break

        # Defence in depth: enforce while streaming, covering chunked uploads or
        # a client that omits/understates Content-Length.
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        await self.app(scope, limited_receive, send)

    @staticmethod
    async def _send_413(send):
        body = b'{"detail":"Request body too large"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def _client_ip_key(request: Request) -> str:
    """Rate-limit key: the real client IP, even behind Cloudflare / a proxy.

    Proxy headers are only consulted when TRUST_PROXY_HEADERS is on (the
    default, matching the documented reverse-proxy deployment, where the proxy
    overwrites them). Turn it off when the API is exposed directly: these
    headers are client-controlled, so trusting them there lets anyone rotate
    the value to bypass the limiter or fill another client's bucket.
    """
    if TRUST_PROXY_HEADERS:
        cf = request.headers.get("cf-connecting-ip")
        if cf:
            return cf.strip()
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return get_remote_address(request)
