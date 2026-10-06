"""OpenID Connect single sign-on for the local dashboard (authentik et al.).

The backend is a confidential OIDC client running the Authorization Code flow
with PKCE:

  1. GET /api/v1/local/auth/oidc/login     -> 302 to the provider, with state,
     nonce and the PKCE verifier parked in a short-lived signed cookie;
  2. GET /api/v1/local/auth/oidc/callback  -> code exchanged (client secret +
     verifier), ID token validated against the provider's JWKS, the e-mail
     address mapped to an existing dashboard account, and the normal dashboard
     session cookie issued.

No provider token ever reaches the browser. The dashboard role always comes
from the local account: the provider only proves who the user is. Endpoints are
discovered from the issuer's /.well-known/openid-configuration and cached, as is
the JWKS (re-fetched once when a token names an unknown key, so key rotation at
the provider needs no restart).
"""

import base64
import hashlib
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Request, Response
from jose import jwt, JWTError

from auth import dashboard_session_secret
from config import (
    DASHBOARD_COOKIE_SECURE,
    OIDC_CLIENT_ID,
    OIDC_CLIENT_SECRET,
    OIDC_DISABLE_PASSWORD_LOGIN,
    OIDC_ENABLED,
    OIDC_HTTP_TIMEOUT_SECONDS,
    OIDC_ISSUER,
    OIDC_POST_LOGOUT_REDIRECT_URI,
    OIDC_PROVIDER_NAME,
    OIDC_REDIRECT_URI,
    OIDC_REQUIRE_EMAIL_VERIFIED,
    OIDC_SCOPES,
    PUBLIC_BASE_URL,
    SERVER_VERSION,
)

logger = logging.getLogger("hivescale.oidc")

OIDC_PATH_PREFIX = "/api/v1/local/auth/oidc"
CALLBACK_PATH = OIDC_PATH_PREFIX + "/callback"
DASHBOARD_PATH = "/dashboard/"
# Carries state / nonce / PKCE verifier between /login and /callback. Scoped to
# the OIDC paths and SameSite=Lax: the callback is a top-level GET navigation
# back from the provider, for which Lax cookies are sent.
FLOW_COOKIE = "hivehub_oidc_flow"
FLOW_TTL_SECONDS = 600
_METADATA_TTL_SECONDS = 3600
# Asymmetric algorithms verified with the provider's JWKS. authentik signs with
# RS256 (or ES256) when the provider has a signing key; without one it falls
# back to HS256 keyed with the client secret, which is accepted too.
_ASYMMETRIC_ALGS = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
_SYMMETRIC_ALGS = {"HS256", "HS384", "HS512"}


class OIDCError(Exception):
    """A failed sign-in. `code` is passed to the dashboard (?sso_error=code),
    which maps it to a human message; the detail only goes to the log."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def oidc_configured() -> bool:
    return bool(OIDC_ENABLED and OIDC_ISSUER and OIDC_CLIENT_ID and OIDC_CLIENT_SECRET)


def password_login_enabled() -> bool:
    """Password login may only be switched off while SSO actually works —
    otherwise OIDC_DISABLE_PASSWORD_LOGIN alone would lock everybody out."""
    return not (OIDC_DISABLE_PASSWORD_LOGIN and oidc_configured())


def sso_features() -> dict:
    """What the login page needs to know before anybody is signed in."""
    return {
        "enabled": oidc_configured(),
        "provider_name": OIDC_PROVIDER_NAME,
        "password_login": password_login_enabled(),
        "login_url": OIDC_PATH_PREFIX + "/login",
    }


if OIDC_ENABLED and not oidc_configured():
    logger.error(
        "OIDC_ENABLED is set but OIDC_ISSUER / OIDC_CLIENT_ID / OIDC_CLIENT_SECRET "
        "are incomplete — single sign-on stays off."
    )
if OIDC_DISABLE_PASSWORD_LOGIN and not oidc_configured():
    logger.warning(
        "OIDC_DISABLE_PASSWORD_LOGIN is ignored because OIDC is not enabled and "
        "configured; password login stays available."
    )


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def redirect_uri(request: Request) -> str:
    return OIDC_REDIRECT_URI or _base_url(request) + CALLBACK_PATH


def post_logout_redirect_uri(request: Request) -> str:
    return OIDC_POST_LOGOUT_REDIRECT_URI or _base_url(request) + DASHBOARD_PATH


# ── provider HTTP ────────────────────────────────────────────────────────────


# urllib's default "Python-urllib/3.x" is blocked by Cloudflare's Browser
# Integrity Check (error 1010) when the provider sits behind Cloudflare.
_USER_AGENT = f"HiveHub/{SERVER_VERSION} (OpenID Connect client)"


def _http_json(url: str, data: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=OIDC_HTTP_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:500].decode("utf-8", "replace")
        raise OIDCError("provider", f"{url} returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OIDCError("provider", f"{url} failed: {exc}") from exc


_cache_lock = threading.Lock()
_metadata_cache: dict = {"value": None, "at": 0.0}
_jwks_cache: dict = {"value": None, "at": 0.0}


def provider_metadata() -> dict:
    with _cache_lock:
        if _metadata_cache["value"] and time.monotonic() - _metadata_cache["at"] < _METADATA_TTL_SECONDS:
            return _metadata_cache["value"]
    meta = _http_json(OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration")
    # The issuer in the document must be the one we were configured with (modulo
    # a trailing slash), otherwise iss validation below would be meaningless.
    if str(meta.get("issuer", "")).rstrip("/") != OIDC_ISSUER.rstrip("/"):
        raise OIDCError("provider", f"Discovery issuer {meta.get('issuer')!r} does not match OIDC_ISSUER")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not meta.get(key):
            raise OIDCError("provider", f"Discovery document lacks {key}")
    with _cache_lock:
        _metadata_cache.update(value=meta, at=time.monotonic())
    return meta


def _jwks(force: bool = False) -> dict:
    with _cache_lock:
        if not force and _jwks_cache["value"] and time.monotonic() - _jwks_cache["at"] < _METADATA_TTL_SECONDS:
            return _jwks_cache["value"]
    keys = _http_json(provider_metadata()["jwks_uri"])
    with _cache_lock:
        _jwks_cache.update(value=keys, at=time.monotonic())
    return keys


def _signing_key(header: dict) -> dict:
    kid = header.get("kid")
    for attempt in (False, True):
        keys = _jwks(force=attempt).get("keys", [])
        matches = [k for k in keys if kid is None or k.get("kid") == kid]
        if kid is None and len(matches) > 1:
            raise OIDCError("token", "ID token has no kid and the JWKS holds several keys")
        if matches:
            return matches[0]
    raise OIDCError("token", f"No JWKS key matches kid {kid!r}")


# ── flow state (login -> callback) ───────────────────────────────────────────


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def start_login(request: Request, response: Response) -> str:
    """Return the provider authorization URL and park the flow state in a cookie."""
    meta = provider_metadata()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    now = datetime.now(timezone.utc)
    flow = jwt.encode(
        {
            "scope": "oidc_flow",
            "state": state,
            "nonce": nonce,
            "verifier": verifier,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=FLOW_TTL_SECONDS)).timestamp()),
        },
        dashboard_session_secret(),
        algorithm="HS256",
    )
    response.set_cookie(
        key=FLOW_COOKIE,
        value=flow,
        max_age=FLOW_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=DASHBOARD_COOKIE_SECURE,
        path=OIDC_PATH_PREFIX,
    )
    params = {
        "response_type": "code",
        "client_id": OIDC_CLIENT_ID,
        "redirect_uri": redirect_uri(request),
        "scope": OIDC_SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    endpoint = meta["authorization_endpoint"]
    sep = "&" if "?" in endpoint else "?"
    return endpoint + sep + urllib.parse.urlencode(params)


def clear_flow_cookie(response: Response) -> None:
    response.delete_cookie(FLOW_COOKIE, path=OIDC_PATH_PREFIX)


def _read_flow(request: Request, state: str) -> dict:
    raw = request.cookies.get(FLOW_COOKIE, "")
    if not raw:
        raise OIDCError("state", "Flow cookie missing (expired, or callback opened in another browser)")
    try:
        flow = jwt.decode(raw, dashboard_session_secret(), algorithms=["HS256"])
    except JWTError as exc:
        raise OIDCError("state", f"Flow cookie invalid: {exc}") from exc
    if flow.get("scope") != "oidc_flow" or not secrets.compare_digest(str(flow.get("state", "")), state or ""):
        raise OIDCError("state", "State mismatch")
    return flow


# ── code exchange + token validation ─────────────────────────────────────────


def _exchange_code(code: str, verifier: str, request: Request) -> dict:
    meta = provider_metadata()
    basic = base64.b64encode(
        f"{urllib.parse.quote(OIDC_CLIENT_ID, safe='')}:{urllib.parse.quote(OIDC_CLIENT_SECRET, safe='')}".encode()
    ).decode("ascii")
    tokens = _http_json(
        meta["token_endpoint"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(request),
            "code_verifier": verifier,
        },
        headers={"Authorization": f"Basic {basic}"},
    )
    if not tokens.get("id_token"):
        raise OIDCError("token", "Token response has no id_token")
    return tokens


def validate_id_token(id_token: str, nonce: str, access_token: Optional[str] = None) -> dict:
    meta = provider_metadata()
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise OIDCError("token", f"Malformed ID token: {exc}") from exc
    alg = header.get("alg")
    if alg in _ASYMMETRIC_ALGS:
        key = _signing_key(header)
    elif alg in _SYMMETRIC_ALGS:
        key = OIDC_CLIENT_SECRET
    else:
        # Notably refuses "none".
        raise OIDCError("token", f"Unsupported ID token algorithm {alg!r}")
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=[alg],
            audience=OIDC_CLIENT_ID,
            issuer=meta["issuer"],
            access_token=access_token,
            options={"leeway": 60},
        )
    except JWTError as exc:
        raise OIDCError("token", f"ID token rejected: {exc}") from exc
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != OIDC_CLIENT_ID:
        raise OIDCError("token", "ID token azp does not match the client")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("token", "Nonce mismatch")
    if not claims.get("sub"):
        raise OIDCError("token", "ID token has no sub")
    return claims


def _verified_flag(value) -> bool:
    # Some providers send the claim as the string "true".
    return value is True or (isinstance(value, str) and value.lower() == "true")


def resolve_email(claims: dict, access_token: Optional[str]) -> str:
    """The user's e-mail address (lower-cased), from the ID token or, when the
    provider leaves it out of the ID token, from the userinfo endpoint."""
    source = claims
    if not claims.get("email"):
        endpoint = provider_metadata().get("userinfo_endpoint")
        if endpoint and access_token:
            info = _http_json(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            if info.get("sub") != claims.get("sub"):
                raise OIDCError("token", "userinfo sub does not match the ID token")
            source = info
    email = str(source.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise OIDCError("no_email", "Provider sent no e-mail address (is the 'email' scope granted?)")
    if OIDC_REQUIRE_EMAIL_VERIFIED and not _verified_flag(source.get("email_verified")):
        raise OIDCError("email_unverified", f"email_verified is not true for {email}")
    return email


def complete_login(request: Request, code: str, state: str) -> tuple[str, str]:
    """Run the callback half of the flow. Returns (email, id_token)."""
    flow = _read_flow(request, state)
    if not code:
        raise OIDCError("provider", "Callback without an authorization code")
    tokens = _exchange_code(code, flow["verifier"], request)
    access_token = tokens.get("access_token")
    claims = validate_id_token(tokens["id_token"], flow["nonce"], access_token)
    return resolve_email(claims, access_token), tokens["id_token"]


def end_session_url(request: Request, id_token: Optional[str]) -> Optional[str]:
    """RP-initiated logout URL at the provider, or None if it has none."""
    try:
        endpoint = provider_metadata().get("end_session_endpoint")
    except OIDCError as exc:
        logger.warning("SSO logout: provider discovery failed: %s", exc)
        return None
    if not endpoint:
        return None
    params = {"client_id": OIDC_CLIENT_ID, "post_logout_redirect_uri": post_logout_redirect_uri(request)}
    if id_token:
        params["id_token_hint"] = id_token
    sep = "&" if "?" in endpoint else "?"
    return endpoint + sep + urllib.parse.urlencode(params)


def dashboard_error_redirect(code: str) -> str:
    return DASHBOARD_PATH + "?" + urllib.parse.urlencode({"sso_error": code})
