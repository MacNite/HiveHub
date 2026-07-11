#!/usr/bin/env python3
"""Generate a VAPID key pair for HiveHub Web Push notifications.

Run once, then copy the printed values into your server environment
(``server/.env``)::

    python gen_vapid.py

Outputs:

* ``VAPID_PUBLIC_KEY``  — the application server key the browser subscribes with.
* ``VAPID_PRIVATE_KEY`` — the raw private key the backend signs with (keep secret).
* ``VAPID_SUBJECT``     — a mailto:/https: URL identifying you; edit to your own.

Both keys are URL-safe base64 (no padding), the format ``pywebpush`` and the
browser ``PushManager.subscribe`` API expect. Requires the ``cryptography``
package (already a HiveHub dependency).
"""

import base64

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())

    # Raw 32-byte private scalar, big-endian — the form py_vapid/pywebpush load
    # from a base64url string.
    priv_value = private_key.private_numbers().private_value
    priv_raw = priv_value.to_bytes(32, "big")

    # Uncompressed public point (65 bytes: 0x04 || X || Y) — the applicationServerKey.
    pub_raw = private_key.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )

    print("# Add these to server/.env and set WEB_PUSH_ENABLED=true")
    print(f"VAPID_PUBLIC_KEY={_b64url(pub_raw)}")
    print(f"VAPID_PRIVATE_KEY={_b64url(priv_raw)}")
    print("VAPID_SUBJECT=mailto:you@example.com  # <- change to your address")


if __name__ == "__main__":
    main()
