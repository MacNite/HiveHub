"""ASGI middleware and rate-limiting helpers."""

from fastapi import HTTPException, Request
from slowapi.util import get_remote_address

from config import TRUST_PROXY_HEADERS


class MaxBodySizeMiddleware:
    """Reject requests whose body exceeds ``max_body_bytes``.

    A single valid API key would otherwise let a client POST arbitrarily large
    JSON bodies, which are parsed into memory and (for measurements) persisted
    verbatim into ``raw_json`` — a storage/memory amplification vector. Capping
    the body closes it. The firmware-upload endpoint legitimately receives
    multi-megabyte bodies, so it is exempt here and enforces its own,
    larger ``MAX_FIRMWARE_BYTES`` while streaming to disk.
    """

    def __init__(self, app, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    @staticmethod
    def _is_exempt(scope) -> bool:
        if scope.get("method") != "POST":
            return False
        path = scope.get("path", "")
        # Firmware binary uploads are large by design and capped by the endpoint.
        # Bulk SD import is authenticated and capped by MEASUREMENT_IMPORT_MAX rows.
        return path.endswith("/firmware") or path.endswith("/measurements/import")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.max_body_bytes <= 0 or self._is_exempt(scope):
            await self.app(scope, receive, send)
            return

        # Fast path: trust a declared Content-Length when present.
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_body_bytes:
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
                if total > self.max_body_bytes:
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
