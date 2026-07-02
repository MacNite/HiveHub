"""Authentication / authorization dependencies: master API key, device keys,
HivePal service JWTs and local-dashboard login sessions."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request, Response
from jose import jwt, JWTError

from config import (
    API_KEY,
    DASHBOARD_COOKIE_SECURE,
    DASHBOARD_SESSION_COOKIE,
    DASHBOARD_SESSION_SECRET_ENV,
    DASHBOARD_SESSION_TTL_HOURS,
    ENABLE_LOCAL_DASHBOARD,
    HIVEPAL_JWT_SECRET,
    HIVEPAL_SERVICE_API_KEY,
)
from db import get_conn


def require_api_key(x_api_key: str = Header(default="")) -> str:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


def require_hivepal_service_key(x_hivepal_service_key: str = Header(default="")):
    if not HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=500, detail="HIVEPAL_SERVICE_API_KEY is not configured")
    if x_hivepal_service_key != HIVEPAL_SERVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid HivePal service key")


def require_local_dashboard():
    """Gate the local dashboard API behind ENABLE_LOCAL_DASHBOARD.

    Returns 404 (not 403) when disabled so the endpoints are indistinguishable
    from non-existent routes on a default / multi-tenant deployment. Used on the
    auth endpoints themselves (status / setup / login), which must be reachable
    before a session exists; the data/control endpoints additionally require a
    valid login via require_dashboard_session.
    """
    if not ENABLE_LOCAL_DASHBOARD:
        raise HTTPException(status_code=404, detail="Not Found")


# ── Dashboard auth (local dashboard login) ───────────────────────────────────
# The local dashboard is protected by username + password accounts stored in
# dashboard_users. Sessions are short-lived signed JWTs carried in an HttpOnly
# cookie. The first visit (no accounts yet) runs a setup wizard that creates the
# initial admin; thereafter every data/control endpoint requires a session, and
# write/control actions require the "admin" role.

_dashboard_secret_cache: Optional[str] = None


def dashboard_session_secret() -> str:
    """Return the session signing secret, generating + persisting one on first use.

    Prefers DASHBOARD_SESSION_SECRET when set; otherwise a random secret is stored
    in dashboard_settings so sessions stay valid across restarts. Cached in-process
    to avoid a DB hit per request.
    """
    global _dashboard_secret_cache
    if DASHBOARD_SESSION_SECRET_ENV:
        return DASHBOARD_SESSION_SECRET_ENV
    if _dashboard_secret_cache:
        return _dashboard_secret_cache
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM dashboard_settings WHERE key = 'session_secret';")
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO dashboard_settings (key, value) VALUES ('session_secret', %s) "
                    "ON CONFLICT (key) DO NOTHING;",
                    (secrets.token_urlsafe(48),),
                )
                conn.commit()
                cur.execute("SELECT value FROM dashboard_settings WHERE key = 'session_secret';")
                row = cur.fetchone()
    _dashboard_secret_cache = row[0]
    return _dashboard_secret_cache


def hash_password(password: str) -> str:
    """Salted PBKDF2-HMAC-SHA256 hash, formatted algo$iters$salt$hash."""
    salt = secrets.token_bytes(16)
    iterations = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def create_dashboard_session_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user["role"],
        "scope": "dashboard",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=DASHBOARD_SESSION_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, dashboard_session_secret(), algorithm="HS256")


def decode_dashboard_session_token(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        payload = jwt.decode(token, dashboard_session_secret(), algorithms=["HS256"])
    except JWTError:
        return None
    if payload.get("scope") != "dashboard":
        return None
    return payload


def set_dashboard_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=DASHBOARD_SESSION_COOKIE,
        value=token,
        max_age=DASHBOARD_SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=DASHBOARD_COOKIE_SECURE,
        path="/",
    )


def clear_dashboard_session_cookie(response: Response) -> None:
    response.delete_cookie(DASHBOARD_SESSION_COOKIE, path="/")


def _public_dashboard_user(user: dict) -> dict:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "email": user.get("email"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
    }


def dashboard_user_count() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dashboard_users;")
            return int(cur.fetchone()[0])


def dashboard_admin_count(exclude_id: Optional[int] = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            if exclude_id is None:
                cur.execute("SELECT COUNT(*) FROM dashboard_users WHERE role = 'admin';")
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM dashboard_users WHERE role = 'admin' AND id <> %s;",
                    (exclude_id,),
                )
            return int(cur.fetchone()[0])


def _dashboard_user_row(r) -> dict:
    return {
        "id": r[0], "username": r[1], "password_hash": r[2], "role": r[3],
        "email": r[4], "created_at": r[5], "last_login_at": r[6],
    }


def get_dashboard_user_by_username(username: str) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, role, email, created_at, last_login_at "
                "FROM dashboard_users WHERE lower(username) = lower(%s);",
                (username,),
            )
            row = cur.fetchone()
    return _dashboard_user_row(row) if row else None


def list_dashboard_users() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, password_hash, role, email, created_at, last_login_at "
                "FROM dashboard_users ORDER BY created_at;"
            )
            rows = cur.fetchall()
    return [_public_dashboard_user(_dashboard_user_row(r)) for r in rows]


def create_dashboard_user(
    username: str, password: str, role: str, email: Optional[str] = None
) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO dashboard_users (username, password_hash, role, email) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING id, username, password_hash, role, email, created_at, last_login_at;",
                (username.strip(), hash_password(password), role, email),
            )
            row = cur.fetchone()
            conn.commit()
    return _dashboard_user_row(row)


def set_dashboard_user_email(user_id: int, email: Optional[str]) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dashboard_users SET email = %s WHERE id = %s;",
                (email, user_id),
            )
            conn.commit()


def delete_dashboard_user(user_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM dashboard_users WHERE id = %s;", (user_id,))
            deleted = cur.rowcount
            conn.commit()
    return deleted > 0


def set_dashboard_user_password(user_id: int, password: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dashboard_users SET password_hash = %s WHERE id = %s;",
                (hash_password(password), user_id),
            )
            conn.commit()


def touch_dashboard_user_login(user_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE dashboard_users SET last_login_at = now() WHERE id = %s;",
                (user_id,),
            )
            conn.commit()


def require_dashboard_session(request: Request) -> dict:
    """Gate dashboard data/control endpoints behind a valid login session.

    404s when the dashboard is disabled (same masking as require_local_dashboard),
    otherwise 401s when no valid session cookie is present. Returns the decoded
    session payload (sub / username / role).
    """
    if not ENABLE_LOCAL_DASHBOARD:
        raise HTTPException(status_code=404, detail="Not Found")
    payload = decode_dashboard_session_token(request.cookies.get(DASHBOARD_SESSION_COOKIE, ""))
    if not payload:
        raise HTTPException(status_code=401, detail="Authentication required")
    return payload


def require_dashboard_admin(user: dict = Depends(require_dashboard_session)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Administrator access required")
    return user


def require_user_id(authorization: str = Header(default="")) -> str:
    if not HIVEPAL_JWT_SECRET:
        raise HTTPException(status_code=500, detail="HIVEPAL_JWT_SECRET is not configured")
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> header required")
    token = authorization[7:]
    try:
        payload = jwt.decode(token, HIVEPAL_JWT_SECRET, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing sub claim")
    return str(user_id)


def verify_device_key(device_id: str, api_key: str):
    """Register a device's API key on first contact; reject mismatches thereafter.

    This runs only for device-authenticated endpoints (config/firmware/command
    polls), so it is also where we record genuine device contact: last_seen_at
    is bumped here, never by the HivePal app reading config on the device's
    behalf (see ensure_device_config / touch_last_seen).
    """
    if len(api_key) < 16:
        raise HTTPException(status_code=401, detail="Invalid API key")
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devices
                SET api_key_hash = COALESCE(api_key_hash, %s),
                    last_seen_at = now()
                WHERE device_id = %s
                RETURNING api_key_hash
                """,
                (key_hash, device_id),
            )
            row = cur.fetchone()
            conn.commit()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if row[0] != key_hash:
        raise HTTPException(status_code=401, detail="API key does not match this device")


class DeviceKeyGuard:
    """FastAPI dependency for device-scoped endpoints. Reads device_id from the
    path and X-API-Key from the header, then delegates to verify_device_key."""
    def __call__(self, device_id: str, x_api_key: str = Header(default="")):
        verify_device_key(device_id, x_api_key)

require_device_key = DeviceKeyGuard()


def require_device_role(user_id: str, device_id: str, allowed_roles: list[str]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM device_members WHERE device_id = %s AND user_id = %s;",
                (device_id, user_id),
            )
            r = cur.fetchone()
    if not r or r[0] not in allowed_roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions for this device")
