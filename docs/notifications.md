# Insight alert notifications

HiveHub can push [insight alerts](insights.md) (swarm, robbing, winter risk,
queenlessness…) out of the dashboard and onto your phone or inbox the moment
they fire. Two independent, opt-in channels are supported:

- **E-mail (SMTP)** — one message per device when an alert appears or escalates.
- **Web Push** — a system notification to any browser or installable dashboard
  **PWA** (Android, desktop Chrome/Firefox, and iOS 16.4+ once added to the Home
  Screen).

Both are **off by default** and fail-soft: a broken relay or push service never
affects measurement ingest, the reconciler, or the alert history. Delivery runs
on a background worker thread, so a slow send never blocks a dashboard load.

## When a notification is sent

The insight reconciler already tracks each alert's lifecycle in the
`insight_alerts` table. A notification goes out only when an alert:

1. **first fires** (a new active alert), or
2. **escalates** past the severity it was last notified at (e.g. `warning` →
   `critical`).

An alert that keeps firing unchanged is **not** re-sent, and only alerts at or
above `NOTIFY_MIN_SEVERITY` (default `warning`) are ever dispatched — so the
noisy `info` / `watch` tiers stay in the dashboard without buzzing your phone.
Delivery is *at-most-once*: an alert is marked notified in the same transaction
that persists it, so a restart mid-send can never replay and spam you.

## Where the config lives

- **Server credentials** (SMTP relay, VAPID keys) are **environment variables**
  in `server/.env` — they are server-wide infrastructure and are never entered
  in the browser.
- **Recipient addresses** are **per dashboard account**: each user sets their own
  *Alert email* under **Account → Alert notifications** in the dashboard.
  Push subscriptions are created per browser from the same panel.

## E-mail (SMTP) setup

Point the server at any SMTP relay and turn it on in `server/.env`:

```dotenv
SMTP_ENABLED=true
SMTP_HOST=smtp.example.com
SMTP_PORT=587            # 587 + STARTTLS (default) or 465 + implicit TLS
SMTP_USERNAME=apikey-or-user
SMTP_PASSWORD=•••
SMTP_STARTTLS=true       # use SMTP_TLS=true instead for implicit TLS on 465
SMTP_FROM=alerts@example.com   # defaults to SMTP_USERNAME
SMTP_FROM_NAME=HiveHub
NOTIFY_MIN_SEVERITY=warning
```

Then, in the dashboard, open **Account** and set an **Alert email**. Every
account with an alert e-mail receives the alerts (addresses are Bcc'd, so
beekeepers don't see each other). Use the **Send test notification** button to
confirm delivery.

## Web Push setup

Web Push needs a one-time [VAPID](https://datatracker.ietf.org/doc/html/rfc8292)
key pair. Generate one with the bundled helper:

```bash
cd server
python gen_vapid.py
```

It prints three values — paste them into `server/.env` and enable the channel:

```dotenv
WEB_PUSH_ENABLED=true
VAPID_PUBLIC_KEY=BOb...            # from gen_vapid.py
VAPID_PRIVATE_KEY=0rf...           # from gen_vapid.py — keep secret
VAPID_SUBJECT=mailto:you@example.com
```

Restart the backend, open the dashboard, go to **Account → Alert
notifications**, and click **Enable on this device**. The browser asks for
notification permission and registers a subscription; repeat on every device you
want alerts on.

> **Requires HTTPS.** Service workers and Web Push only work over `https://` (or
> `http://localhost`). Put the dashboard behind TLS / a reverse proxy — the same
> recommendation as for exposing the local dashboard at all.

### iOS / iPadOS

Safari only delivers Web Push to a **web app added to the Home Screen** (iOS
16.4+), not to a normal browser tab:

1. Open the dashboard in Safari.
2. **Share → Add to Home Screen.**
3. Launch HiveHub from the new Home-Screen icon, then enable notifications from
   the Account panel.

Android, desktop Chrome, and Firefox work directly from a tab — no install
required (though installing still gives you an app icon and a cleaner window).

## How it fits together

```
insight reconciler ─▶ persist_insights()  (detects new / escalated alerts)
                              │  marks notified_at in the same transaction
                              ▼
                    notifications worker (background thread)
                       ├─▶ SMTP  → dashboard accounts with an alert e-mail
                       └─▶ Web Push → rows in push_subscriptions → service worker
```

- Server code: `server/notifications.py` (dispatcher + channels),
  `server/insights_api.py` (transition detection), `server/gen_vapid.py`.
- Dashboard: **Account → Alert notifications**
  (`server/dashboard/assets/views.js`), the Web Push client
  (`server/dashboard/assets/push.js`), the service worker
  (`server/dashboard/sw.js`), and the PWA manifest
  (`server/dashboard/manifest.webmanifest`).
- Schema: `server/migrations/018_notifications.sql`.

All of `server/.env.example`'s notification keys are documented inline.
