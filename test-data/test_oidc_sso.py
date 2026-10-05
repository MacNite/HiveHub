"""Tests for dashboard single sign-on via OpenID Connect (server/oidc.py).

Run: python3 -m pytest test-data/test_oidc_sso.py

The provider (authentik in production) is faked: discovery, JWKS and the token
endpoint are served from server/oidc.py's _http_json seam, and ID tokens are
signed with an RSA key generated here. The endpoint handlers are called directly
with a hand-built Starlette Request (no TestClient: httpx is not in CI), and the
database helpers they use are patched, so nothing connects anywhere.

What is pinned:
  * the happy path: login redirects with state / nonce / PKCE S256, the callback
    exchanges the code and issues a dashboard session marked auth=oidc;
  * users are mapped by e-mail only, to an EXISTING account — unknown or
    duplicated addresses are refused, and the role comes from the local account;
  * token validation: state, nonce, audience, issuer, signature, alg=none,
    email_verified (and its override), e-mail from userinfo when the ID token
    lacks it;
  * OIDC_DISABLE_PASSWORD_LOGIN only bites while SSO is configured, refuses
    password logins and invalidates password sessions;
  * logout of an SSO session hands back the provider's end-session URL;
  * SSO-only accounts (no password) need SSO on and an e-mail.
"""

import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))

os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")
os.environ.setdefault("API_KEY", "test-api-key")

import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from jose import jwk, jwt  # noqa: E402
from starlette.requests import Request  # noqa: E402

import auth  # noqa: E402
import local_dashboard  # noqa: E402
import oidc  # noqa: E402
from schemas import DashboardCreateUserIn, DashboardLoginIn  # noqa: E402

ISSUER = "https://auth.example.com/application/o/hivehub/"
CLIENT_ID = "hivehub-client"
CLIENT_SECRET = "s3cret-client-secret"
BASE = "https://hub.example.com"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()
_PUBLIC_PEM = _PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()
PUBLIC_JWK = {**jwk.construct(_PUBLIC_PEM, "RS256").to_dict(), "kid": "k1", "use": "sig"}

METADATA = {
    "issuer": ISSUER,
    "authorization_endpoint": "https://auth.example.com/application/o/authorize/",
    "token_endpoint": "https://auth.example.com/application/o/token/",
    "userinfo_endpoint": "https://auth.example.com/application/o/userinfo/",
    "jwks_uri": "https://auth.example.com/application/o/hivehub/jwks/",
    "end_session_endpoint": "https://auth.example.com/application/o/hivehub/end-session/",
}

ALICE = {
    "id": 7, "username": "alice", "password_hash": auth.UNUSABLE_PASSWORD, "role": "admin",
    "email": "alice@example.com", "created_at": None, "last_login_at": None,
}


def make_id_token(nonce, **overrides):
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": CLIENT_ID, "sub": "ak-user-1", "iat": now, "exp": now + 300,
        "nonce": nonce, "email": "Alice@Example.com", "email_verified": True,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, PRIVATE_PEM, algorithm="RS256", headers={"kid": "k1"})


def make_request(path, cookies=None, query=""):
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request({
        "type": "http", "method": "GET", "path": path, "root_path": "", "scheme": "https",
        "query_string": query.encode(), "headers": headers, "server": ("hub.example.com", 443),
    })


def set_cookies(response):
    out = {}
    for name, value in response.raw_headers:
        if name == b"set-cookie":
            k, _, rest = value.decode().partition("=")
            out[k] = rest.split(";", 1)[0]
    return out


class FakeProvider:
    """Answers oidc._http_json; `token_response` is built per test."""

    def __init__(self):
        self.token_response = None
        self.userinfo = None
        self.token_requests = []

    def __call__(self, url, data=None, headers=None):
        if url.endswith("/.well-known/openid-configuration"):
            return METADATA
        if url == METADATA["jwks_uri"]:
            return {"keys": [PUBLIC_JWK]}
        if url == METADATA["token_endpoint"]:
            self.token_requests.append((data, headers))
            return self.token_response
        if url == METADATA["userinfo_endpoint"]:
            return self.userinfo
        raise AssertionError(f"unexpected URL {url}")


@contextmanager
def sso(password_login_disabled=False, require_verified=True, users=(ALICE,)):
    provider = FakeProvider()
    oidc._metadata_cache.update(value=None, at=0.0)
    oidc._jwks_cache.update(value=None, at=0.0)
    with mock.patch.multiple(
        oidc,
        OIDC_ENABLED=True, OIDC_ISSUER=ISSUER, OIDC_CLIENT_ID=CLIENT_ID,
        OIDC_CLIENT_SECRET=CLIENT_SECRET, OIDC_DISABLE_PASSWORD_LOGIN=password_login_disabled,
        OIDC_REQUIRE_EMAIL_VERIFIED=require_verified, PUBLIC_BASE_URL=BASE,
        _http_json=provider,
    ), mock.patch.object(auth, "DASHBOARD_SESSION_SECRET_ENV", "test-session-secret"), \
            mock.patch.object(auth, "ENABLE_LOCAL_DASHBOARD", True), \
            mock.patch.object(local_dashboard, "touch_dashboard_user_login"), \
            mock.patch.object(
                local_dashboard, "get_dashboard_users_by_email",
                side_effect=lambda e: [u for u in users if u["email"] == e.lower()],
            ):
        yield provider


def start_flow():
    resp = local_dashboard.local_auth_oidc_login(make_request("/api/v1/local/auth/oidc/login"))
    assert resp.status_code == 303
    location = resp.headers["location"]
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(location).query))
    flow_cookie = set_cookies(resp)[oidc.FLOW_COOKIE]
    flow = jwt.decode(flow_cookie, "test-session-secret", algorithms=["HS256"])
    return location, params, flow_cookie, flow


def callback(flow_cookie, state, code="the-code", **query):
    return local_dashboard.local_auth_oidc_callback(
        make_request("/api/v1/local/auth/oidc/callback", {oidc.FLOW_COOKIE: flow_cookie}),
        code=code, state=state, error=query.get("error", ""), error_description="",
    )


def sso_error(resp):
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/dashboard/")
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(loc).query)).get("sso_error")


# ── happy path ───────────────────────────────────────────────────────────────


def test_login_redirect_carries_state_nonce_and_pkce():
    with sso():
        location, params, _, flow = start_flow()
    assert location.startswith(METADATA["authorization_endpoint"])
    assert params["response_type"] == "code"
    assert params["client_id"] == CLIENT_ID
    assert params["redirect_uri"] == BASE + "/api/v1/local/auth/oidc/callback"
    assert "openid" in params["scope"].split() and "email" in params["scope"].split()
    assert params["state"] == flow["state"] and params["nonce"] == flow["nonce"]
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(flow["verifier"].encode()).digest()
    ).rstrip(b"=").decode()
    assert params["code_challenge_method"] == "S256" and params["code_challenge"] == expected
    assert "verifier" not in location  # the verifier never leaves the server


def test_callback_maps_email_to_existing_account_and_issues_oidc_session():
    with sso() as provider:
        _, params, flow_cookie, flow = start_flow()
        provider.token_response = {"id_token": make_id_token(flow["nonce"]), "access_token": "at"}
        resp = callback(flow_cookie, params["state"])
        assert resp.status_code == 303 and resp.headers["location"] == "/dashboard/"
        cookies = set_cookies(resp)
        session = auth.decode_dashboard_session_token(cookies[auth.DASHBOARD_SESSION_COOKIE])
        local_dashboard.touch_dashboard_user_login.assert_called_once_with(7)
    assert session["sub"] == "7" and session["username"] == "alice"
    assert session["role"] == "admin"  # from the local account, never the provider
    assert session["auth"] == "oidc" and session["idt"]
    assert cookies[oidc.FLOW_COOKIE] in ('""', "")  # flow cookie cleared
    data, headers = provider.token_requests[0]
    assert data["code"] == "the-code" and data["code_verifier"] == flow["verifier"]
    assert data["redirect_uri"] == BASE + "/api/v1/local/auth/oidc/callback"
    assert base64.b64decode(headers["Authorization"][6:]).decode() == f"{CLIENT_ID}:{CLIENT_SECRET}"


# ── user mapping ─────────────────────────────────────────────────────────────


def test_unknown_email_is_refused_never_created():
    with sso(users=()) as provider:
        _, params, flow_cookie, flow = start_flow()
        provider.token_response = {"id_token": make_id_token(flow["nonce"]), "access_token": "at"}
        resp = callback(flow_cookie, params["state"])
    assert sso_error(resp) == "unknown_user"
    assert auth.DASHBOARD_SESSION_COOKIE not in set_cookies(resp)


def test_duplicate_email_is_refused():
    twin = {**ALICE, "id": 8, "username": "alice2"}
    with sso(users=(ALICE, twin)) as provider:
        _, params, flow_cookie, flow = start_flow()
        provider.token_response = {"id_token": make_id_token(flow["nonce"]), "access_token": "at"}
        assert sso_error(callback(flow_cookie, params["state"])) == "ambiguous"


# ── validation failures ──────────────────────────────────────────────────────


@pytest.mark.parametrize("overrides, error", [
    ({"nonce": "forged"}, "token"),
    ({"aud": "some-other-client"}, "token"),
    ({"iss": "https://evil.example.com/"}, "token"),
    ({"exp": int(time.time()) - 3600}, "token"),
    ({"email_verified": False}, "email_unverified"),
    ({"email_verified": None}, "email_unverified"),
])
def test_bad_id_tokens_are_refused(overrides, error):
    with sso() as provider:
        _, params, flow_cookie, flow = start_flow()
        nonce = overrides.pop("nonce", flow["nonce"])
        provider.token_response = {"id_token": make_id_token(nonce, **overrides), "access_token": "at"}
        assert sso_error(callback(flow_cookie, params["state"])) == error


def test_state_mismatch_and_missing_flow_cookie_are_refused():
    with sso():
        _, params, flow_cookie, _ = start_flow()
        assert sso_error(callback(flow_cookie, "not-the-state")) == "state"
        assert sso_error(callback("", params["state"])) == "state"


def test_unsigned_and_wrongly_signed_tokens_are_refused():
    with sso() as provider:
        _, params, flow_cookie, flow = start_flow()
        good = make_id_token(flow["nonce"])
        header, payload, _ = good.split(".")
        none_header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
        provider.token_response = {"id_token": f"{none_header}.{payload}.", "access_token": "at"}
        assert sso_error(callback(flow_cookie, params["state"])) == "token"
        provider.token_response = {"id_token": f"{header}.{payload}.AAAA", "access_token": "at"}
        assert sso_error(callback(flow_cookie, params["state"])) == "token"


def test_hs256_tokens_signed_with_the_client_secret_are_accepted():
    # authentik signs with the client secret when the provider has no signing key.
    with sso():
        claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "u", "nonce": "n",
                  "exp": int(time.time()) + 60, "email": "a@b.c"}
        token = jwt.encode(claims, CLIENT_SECRET, algorithm="HS256")
        assert oidc.validate_id_token(token, "n")["sub"] == "u"
        forged = jwt.encode(claims, "wrong-secret", algorithm="HS256")
        with pytest.raises(oidc.OIDCError):
            oidc.validate_id_token(forged, "n")


def test_unverified_email_allowed_when_requirement_is_switched_off():
    with sso(require_verified=False) as provider:
        _, params, flow_cookie, flow = start_flow()
        provider.token_response = {
            "id_token": make_id_token(flow["nonce"], email_verified=False), "access_token": "at",
        }
        resp = callback(flow_cookie, params["state"])
    assert resp.headers["location"] == "/dashboard/"


def test_email_falls_back_to_userinfo_with_matching_sub():
    with sso() as provider:
        _, params, flow_cookie, flow = start_flow()
        provider.token_response = {
            "id_token": make_id_token(flow["nonce"], email=None, email_verified=None), "access_token": "at",
        }
        provider.userinfo = {"sub": "ak-user-1", "email": "alice@example.com", "email_verified": True}
        assert callback(flow_cookie, params["state"]).headers["location"] == "/dashboard/"
        provider.userinfo = {"sub": "someone-else", "email": "alice@example.com", "email_verified": True}
        assert sso_error(callback(flow_cookie, params["state"])) == "token"


def test_provider_error_is_reported():
    with sso():
        _, params, flow_cookie, _ = start_flow()
        assert sso_error(callback(flow_cookie, params["state"], error="access_denied")) == "denied"


def test_sso_endpoints_redirect_with_disabled_when_not_configured():
    with mock.patch.object(oidc, "OIDC_ENABLED", False), \
            mock.patch.object(auth, "ENABLE_LOCAL_DASHBOARD", True):
        resp = local_dashboard.local_auth_oidc_login(make_request("/x"))
        assert sso_error(resp) == "disabled"


# ── password login toggle ────────────────────────────────────────────────────


def test_disable_password_login_needs_working_sso():
    with mock.patch.multiple(oidc, OIDC_ENABLED=False, OIDC_DISABLE_PASSWORD_LOGIN=True):
        assert oidc.password_login_enabled()  # would otherwise lock everyone out
    with sso(password_login_disabled=True):
        assert not oidc.password_login_enabled()
        assert oidc.sso_features()["password_login"] is False


def test_password_login_and_sessions_refused_when_disabled():
    with sso(password_login_disabled=True):
        with pytest.raises(HTTPException) as exc:
            local_dashboard.local_auth_login(DashboardLoginIn(username="a", password="b"), mock.Mock())
        assert exc.value.status_code == 403
        pw_token = auth.create_dashboard_session_token(ALICE)
        sso_token = auth.create_dashboard_session_token(ALICE, auth_method="oidc", id_token="x")
        cookie = auth.DASHBOARD_SESSION_COOKIE
        assert auth.current_dashboard_session(make_request("/", {cookie: pw_token})) is None
        assert auth.current_dashboard_session(make_request("/", {cookie: sso_token}))["auth"] == "oidc"
    with sso():
        assert auth.current_dashboard_session(make_request("/", {cookie: pw_token}))["auth"] == "password"


# ── logout ───────────────────────────────────────────────────────────────────


def test_logout_of_sso_session_returns_provider_end_session_url():
    with sso():
        token = auth.create_dashboard_session_token(ALICE, auth_method="oidc", id_token="the-id-token")
        resp = mock.Mock()
        r = local_dashboard.local_auth_logout(
            make_request("/", {auth.DASHBOARD_SESSION_COOKIE: token}), resp
        )
        url = urllib.parse.urlparse(r["redirect"])
        q = dict(urllib.parse.parse_qsl(url.query))
        assert r["redirect"].startswith(METADATA["end_session_endpoint"])
        assert q["id_token_hint"] == "the-id-token"
        assert q["post_logout_redirect_uri"] == BASE + "/dashboard/"
        resp.delete_cookie.assert_called_once()
        pw = auth.create_dashboard_session_token(ALICE)
        r = local_dashboard.local_auth_logout(make_request("/", {auth.DASHBOARD_SESSION_COOKIE: pw}), mock.Mock())
        assert "redirect" not in r


# ── SSO-only accounts ────────────────────────────────────────────────────────


def _create(body):
    created = {}

    def fake_create(username, password, role, email):
        created.update(password=password)
        return {**ALICE, "username": username, "role": role, "email": email,
                "password_hash": auth.hash_password(password) if password else auth.UNUSABLE_PASSWORD}

    with mock.patch.object(local_dashboard, "get_dashboard_user_by_username", return_value=None), \
            mock.patch.object(local_dashboard, "dashboard_email_taken", return_value=False), \
            mock.patch.object(local_dashboard, "create_dashboard_user", side_effect=fake_create):
        return local_dashboard.local_create_dashboard_user(body), created


def test_passwordless_account_requires_sso_and_email():
    with mock.patch.object(oidc, "OIDC_ENABLED", False):
        with pytest.raises(HTTPException) as exc:
            _create(DashboardCreateUserIn(username="bob", email="bob@example.com"))
        assert exc.value.status_code == 422
    with sso():
        with pytest.raises(HTTPException) as exc:
            _create(DashboardCreateUserIn(username="bob"))
        assert exc.value.status_code == 422
        user, created = _create(DashboardCreateUserIn(username="bob", email="Bob@Example.com"))
        assert created["password"] is None and user["has_password"] is False
        assert user["email"] == "bob@example.com"


def test_password_is_ignored_when_password_login_disabled():
    with sso(password_login_disabled=True):
        user, created = _create(DashboardCreateUserIn(
            username="carol", password="longenough", email="carol@example.com"))
    assert created["password"] is None and user["has_password"] is False


def test_unusable_password_never_verifies():
    assert not auth.verify_password("", auth.UNUSABLE_PASSWORD)
    assert not auth.verify_password("!", auth.UNUSABLE_PASSWORD)
