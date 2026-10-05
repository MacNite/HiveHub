# Dashboard single sign-on with authentik (OpenID Connect)

The built-in dashboard (`/dashboard`) can let users sign in with
[authentik](https://goauthentik.io) instead of, or in addition to, a HiveHub
password. Any standards-compliant OpenID Connect provider works the same way;
authentik is what the defaults and this guide assume.

## How it works

- HiveHub is a **confidential OIDC client**. It runs the Authorization Code
  flow with PKCE on the backend, checks the ID token's signature against
  authentik's JWKS (plus issuer, audience, expiry and nonce), and then issues
  its usual HttpOnly session cookie. No authentik token is ever handed to the
  browser.
- **Users are matched by e-mail address.** The address authentik reports is
  compared, case-insensitively, with the **email** of the dashboard accounts.
  - **No account with that address → sign-in is refused.** SSO never creates
    accounts: an admin adds the user under **Device & admin → Dashboard users**
    first.
  - The **role** (admin / viewer) always comes from the HiveHub account.
    authentik groups are not used.
  - Each email address may belong to only one account. A unique index enforces
    this (see *Upgrading* below).
- By default the address must be marked `email_verified: true` by authentik
  (see `OIDC_REQUIRE_EMAIL_VERIFIED`).
- **Sign out** of an SSO session also signs the user out of authentik
  (RP-initiated logout), which then sends them back to the dashboard.

## 1. Configure authentik

1. **Applications → Providers → Create → OAuth2/OpenID Provider**
   - *Authorization flow*: your usual (implicit or explicit consent) flow.
   - *Client type*: **Confidential**. Note the **Client ID** and **Client Secret**.
   - *Redirect URIs*: strict,
     `https://<your-hivehub>/api/v1/local/auth/oidc/callback`.
     If your authentik version validates `post_logout_redirect_uri`, also add
     `https://<your-hivehub>/dashboard/`.
   - *Signing key*: pick a certificate (for example the self-signed
     "authentik Self-signed Certificate") so tokens are signed with RS256.
     Without one, authentik signs with the client secret (HS256). HiveHub
     accepts that too.
   - *Scopes*: keep `openid`, `email` and `profile`.
2. **Applications → Applications → Create**: name it e.g. *HiveHub*, set the
   slug (e.g. `hivehub`), and select the provider. Use policy, group or user
   bindings to limit who may use the application. HiveHub additionally admits
   only addresses that have a dashboard account.
3. The issuer URL is `https://<authentik-host>/application/o/<slug>/` (shown as
   "OpenID Configuration Issuer" on the provider page).

### email_verified

Depending on the authentik version, the default *"authentik default OAuth
Mapping: OpenID 'email'"* scope mapping may report `email_verified: false` for
every user. Then every sign-in is refused with "email address not verified".
Either:

- create a custom scope mapping (scope name `email`) that reports what you
  trust, for example
  ```python
  return {"email": request.user.email, "email_verified": True}
  ```
  and select it on the provider instead of the default one, **or**
- set `OIDC_REQUIRE_EMAIL_VERIFIED=false`. This is fine when only admins can set
  user e-mail addresses in authentik (users cannot change their own to someone
  else's).

## 2. Configure HiveHub

In `.env` (next to `docker-compose.yml`):

```dotenv
ENABLE_LOCAL_DASHBOARD=true
PUBLIC_BASE_URL=https://hub.example.com

OIDC_ENABLED=true
OIDC_ISSUER=https://auth.example.com/application/o/hivehub/
OIDC_CLIENT_ID=<client id>
OIDC_CLIENT_SECRET=<client secret>
```

| Variable | Default | Meaning |
|---|---|---|
| `OIDC_ENABLED` | `false` | Turn SSO on. It also needs issuer, client ID and secret; with any of them missing it stays off and an error is logged. |
| `OIDC_ISSUER` | — | Issuer URL. Endpoints are discovered from `<issuer>/.well-known/openid-configuration`. |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — | From the authentik provider. |
| `OIDC_PROVIDER_NAME` | `authentik` | Button label: "Sign in with …". |
| `OIDC_SCOPES` | `openid email profile` | Requested scopes. `openid` and `email` are required. |
| `OIDC_REDIRECT_URI` | `PUBLIC_BASE_URL` + `/api/v1/local/auth/oidc/callback` | Must match the redirect URI registered in authentik exactly. |
| `OIDC_POST_LOGOUT_REDIRECT_URI` | `PUBLIC_BASE_URL` + `/dashboard/` | Where authentik returns the browser after sign-out. |
| `OIDC_REQUIRE_EMAIL_VERIFIED` | `true` | Refuse addresses not marked `email_verified: true`. |
| `OIDC_DISABLE_PASSWORD_LOGIN` | `false` | `true` = SSO only (see below). |
| `OIDC_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for calls to authentik. |

Set `PUBLIC_BASE_URL` (or `OIDC_REDIRECT_URI`). Behind a reverse proxy, the URL
HiveHub derives from the request may otherwise be the internal one. Keep
`DASHBOARD_COOKIE_SECURE=true` when serving over HTTPS.

## 3. Add users

Under **Device & admin → Dashboard users**, add each person with **the e-mail
address their authentik account has**:

- Leave the **password empty** to create an **SSO-only** account. It can only
  sign in via authentik and is listed as "SSO only".
- Existing password accounts can use SSO as soon as their **email** (Device &
  admin → Your account) matches their authentik address.

## Password login: keep it or turn it off

By default the login page shows **Sign in with authentik** above the normal
username/password form. A local admin with a password remains a
break-glass account for when authentik is unreachable.

`OIDC_DISABLE_PASSWORD_LOGIN=true` makes the dashboard SSO-only:

- the password form and "Change password" disappear, and password logins are
  refused;
- existing password sessions stop working at once;
- new accounts are always SSO-only;
- on a fresh install, the setup wizard asks for a username and **e-mail**
  (no password), creates that admin, and sends you to authentik to sign in
  with that address.

This setting only takes effect while SSO is enabled **and** fully configured,
so a missing issuer or secret can never lock everybody out.

## Upgrading: unique e-mail addresses

On startup HiveHub creates a case-insensitive unique index on
`dashboard_users.email` (migration `032_dashboard_user_email_unique.sql` does
the same for manual migrations). If two accounts already share an address, the
index is **not** created and the server logs an error naming the addresses.
SSO then refuses those addresses until each account has its own, and the
standalone migration aborts with the same list. Fix the accounts, then restart.

## Troubleshooting

Failed sign-ins return to the login page with a message. The server log
(`hivescale.oidc`) has the technical detail.

| Message | Cause |
|---|---|
| "…does not belong to any dashboard user" | No account has the e-mail authentik sent. Add one, or fix the account's email. |
| "…reports your email address as not verified" | See [email_verified](#email_verified). |
| "…did not send an email address" | The `email` scope is not granted / mapped on the provider. |
| "took too long or was started in another browser" | The 10-minute flow cookie expired or is missing. With HTTP (not HTTPS) and `DASHBOARD_COOKIE_SECURE=true`, the cookie is never sent. |
| "Sign-in … failed" (generic) | Discovery, token exchange or token validation failed (wrong issuer/secret/redirect URI, clock skew > 60 s). Check the log. |
