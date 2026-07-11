"""Optional insight-alert notifications for HiveHub.

When a sensor-based insight alert first fires — or an existing one escalates in
severity — the reconciler in ``insights_api.persist_insights`` hands the freshly
changed alerts to this module, which fans them out over two independent,
opt-in channels:

* **E-mail (SMTP).** Sent to every dashboard account that has stored an alert
  e-mail (``dashboard_users.email``). Relay credentials come from the
  ``SMTP_*`` environment variables — they are server-wide infra, never entered
  in the browser.
* **Web Push.** Sent to every browser / installable-PWA subscription in
  ``push_subscriptions``. Requires a VAPID key pair (``VAPID_*``) and the
  optional ``pywebpush`` dependency.

Everything here is fail-soft and runs on a dedicated background worker thread,
so a slow or broken relay never blocks measurement ingest, the insight
reconciler, or the dashboard request that opportunistically triggered a
recompute. If neither channel is configured the module is an inert no-op.

Delivery is deliberately *at-most-once*: an alert is marked ``notified_at`` in
the same transaction that persists it (see ``persist_insights``), so a crash or
restart mid-send can never replay the same alert and spam a beekeeper. The
trade-off is that a genuine send failure is logged but not retried.
"""

from __future__ import annotations

import json
import logging
import queue
import smtplib
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Optional

from config import (
    PUBLIC_BASE_URL,
    SMTP_ENABLED,
    SMTP_FROM,
    SMTP_FROM_NAME,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_STARTTLS,
    SMTP_TIMEOUT_SECONDS,
    SMTP_TLS,
    SMTP_USERNAME,
    VAPID_PRIVATE_KEY,
    VAPID_PUBLIC_KEY,
    VAPID_SUBJECT,
    WEB_PUSH_ENABLED,
)
from db import get_conn

logger = logging.getLogger("hivescale.notifications")

SEVERITY_RANK = {"info": 1, "watch": 2, "warning": 3, "critical": 4}
SEVERITY_EMOJI = {"info": "ℹ️", "watch": "👀", "warning": "⚠️", "critical": "🚨"}


@dataclass
class AlertEvent:
    """One alert that changed state and warrants a notification."""

    device_id: str
    alert_key: str
    category: str
    channel: int
    severity: str
    title: str
    description: str
    reason: str  # "new" | "escalated"


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def email_enabled() -> bool:
    return SMTP_ENABLED and bool(SMTP_HOST) and bool(SMTP_FROM)


def web_push_enabled() -> bool:
    return bool(WEB_PUSH_ENABLED)


def notifications_enabled() -> bool:
    return email_enabled() or web_push_enabled()


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def _alert_email_recipients() -> list[str]:
    """Distinct, non-empty alert e-mail addresses across dashboard accounts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT email FROM dashboard_users "
                "WHERE email IS NOT NULL AND email <> '';"
            )
            return [r[0] for r in cur.fetchall() if r[0]]


def _device_label(device_id: str) -> str:
    """Friendly device name for message copy, falling back to the id."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT display_name FROM devices WHERE device_id = %s;",
                    (device_id,),
                )
                row = cur.fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:
        logger.debug("device label lookup failed for %s", device_id, exc_info=True)
    return device_id


# ---------------------------------------------------------------------------
# Push subscription storage (used by the dashboard API too)
# ---------------------------------------------------------------------------


def upsert_push_subscription(
    user_id: Optional[int],
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: Optional[str] = None,
) -> None:
    """Store (or refresh) a browser Web Push subscription, keyed on endpoint."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO push_subscriptions
                    (user_id, endpoint, p256dh, auth, user_agent, last_used_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (endpoint) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    p256dh = EXCLUDED.p256dh,
                    auth = EXCLUDED.auth,
                    user_agent = EXCLUDED.user_agent,
                    failure_count = 0,
                    last_used_at = now();
                """,
                (user_id, endpoint, p256dh, auth, user_agent),
            )
            conn.commit()


def delete_push_subscription(endpoint: str) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = %s;", (endpoint,)
            )
            deleted = cur.rowcount
            conn.commit()
    return deleted > 0


def _list_push_subscriptions() -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, endpoint, p256dh, auth FROM push_subscriptions;"
            )
            rows = cur.fetchall()
    return [
        {"id": r[0], "endpoint": r[1], "p256dh": r[2], "auth": r[3]} for r in rows
    ]


def _drop_subscription_by_id(sub_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_subscriptions WHERE id = %s;", (sub_id,))
            conn.commit()


def _bump_subscription_failure(sub_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE push_subscriptions "
                "SET failure_count = failure_count + 1 WHERE id = %s;",
                (sub_id,),
            )
            conn.commit()


def _touch_subscription(sub_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE push_subscriptions "
                "SET last_used_at = now(), failure_count = 0 WHERE id = %s;",
                (sub_id,),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------


def _dashboard_url() -> Optional[str]:
    return f"{PUBLIC_BASE_URL}/dashboard" if PUBLIC_BASE_URL else None


def _event_line(ev: AlertEvent) -> str:
    emoji = SEVERITY_EMOJI.get(ev.severity, "")
    verb = "escalated" if ev.reason == "escalated" else "new"
    return f"{emoji} [{ev.severity.upper()}] {ev.title} (hive {ev.channel}, {verb})"


def _email_for_batch(device_label: str, events: list[AlertEvent]) -> tuple[str, str]:
    """Return (subject, plaintext body) for a device's batch of alert events."""
    top = max(events, key=lambda e: SEVERITY_RANK.get(e.severity, 0))
    if len(events) == 1:
        subject = f"[HiveHub] {top.severity.upper()}: {top.title} — {device_label}"
    else:
        subject = (
            f"[HiveHub] {len(events)} alerts ({top.severity.upper()}) — {device_label}"
        )

    lines = [f"HiveHub colony alert for “{device_label}”", ""]
    for ev in events:
        lines.append(_event_line(ev))
        if ev.description:
            lines.append(f"    {ev.description}")
        lines.append("")
    url = _dashboard_url()
    if url:
        lines.append(f"Open the dashboard: {url}")
    lines.append("")
    lines.append(
        "You are receiving this because your HiveHub dashboard account has an "
        "alert e-mail set. Clear it under Account → Alert email to stop."
    )
    return subject, "\n".join(lines)


def _push_payload(ev: AlertEvent, device_label: str) -> dict[str, Any]:
    return {
        "title": f"{SEVERITY_EMOJI.get(ev.severity, '')} {ev.title}".strip(),
        "body": f"{device_label} · hive {ev.channel}\n{ev.description}".strip(),
        "severity": ev.severity,
        "category": ev.category,
        "tag": f"{ev.device_id}:{ev.alert_key}",
        "url": _dashboard_url() or "/dashboard",
    }


# ---------------------------------------------------------------------------
# Channel senders
# ---------------------------------------------------------------------------


def _send_email(recipients: list[str], subject: str, body: str) -> None:
    if not recipients:
        return
    msg = EmailMessage()
    msg["From"] = formataddr((SMTP_FROM_NAME or "HiveHub", SMTP_FROM))
    # Recipients go in Bcc so beekeepers don't see each other's addresses.
    msg["To"] = formataddr((SMTP_FROM_NAME or "HiveHub", SMTP_FROM))
    msg["Bcc"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    if SMTP_TLS:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS
        )
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_SECONDS)
    try:
        if SMTP_STARTTLS and not SMTP_TLS:
            server.starttls()
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    logger.info("sent alert e-mail to %d recipient(s)", len(recipients))


def _send_web_push_one(sub: dict[str, Any], payload: dict[str, Any]) -> None:
    """Deliver one payload to one subscription; prune it if the service 404/410s."""
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        logger.warning(
            "WEB_PUSH_ENABLED but the 'pywebpush' package is not installed; "
            "skipping Web Push. Add it to requirements to enable this channel."
        )
        raise _PushUnavailable

    subscription_info = {
        "endpoint": sub["endpoint"],
        "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
    }
    try:
        webpush(
            subscription_info=subscription_info,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
        _touch_subscription(sub["id"])
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            # Subscription is permanently gone — drop it so we stop trying.
            _drop_subscription_by_id(sub["id"])
            logger.info("pruned expired push subscription %s", sub["id"])
        else:
            _bump_subscription_failure(sub["id"])
            logger.warning("web push failed (status %s): %s", status, exc)


class _PushUnavailable(Exception):
    """Raised when pywebpush can't be imported, to stop the per-sub loop early."""


def _send_web_push(events: list[AlertEvent], device_label: str) -> None:
    subs = _list_push_subscriptions()
    if not subs:
        return
    for ev in events:
        payload = _push_payload(ev, device_label)
        for sub in subs:
            try:
                _send_web_push_one(sub, payload)
            except _PushUnavailable:
                return  # dependency missing — no point continuing this batch
            except Exception:
                logger.exception("unexpected web push error for sub %s", sub["id"])


# ---------------------------------------------------------------------------
# Dispatch + background worker
# ---------------------------------------------------------------------------


def _dispatch(events: list[AlertEvent]) -> None:
    """Send one device's batch of changed alerts over all enabled channels."""
    if not events:
        return
    device_label = _device_label(events[0].device_id)

    if email_enabled():
        try:
            recipients = _alert_email_recipients()
            if recipients:
                subject, body = _email_for_batch(device_label, events)
                _send_email(recipients, subject, body)
        except Exception:
            logger.exception("alert e-mail dispatch failed")

    if web_push_enabled():
        try:
            _send_web_push(events, device_label)
        except Exception:
            logger.exception("alert web push dispatch failed")


_queue: "queue.Queue[Optional[list[AlertEvent]]]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_worker_lock = threading.Lock()


def _worker_loop() -> None:
    while True:
        batch = _queue.get()
        try:
            if batch is None:  # shutdown sentinel
                return
            _dispatch(batch)
        except Exception:
            logger.exception("notification worker failed to dispatch a batch")
        finally:
            _queue.task_done()


def start_notification_worker() -> None:
    """Start the background dispatch thread (no-op if no channel is configured)."""
    global _worker
    if not notifications_enabled():
        logger.info(
            "insight notifications disabled (configure SMTP_* and/or VAPID_*)"
        )
        return
    with _worker_lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_worker_loop, name="notification-worker", daemon=True
        )
        _worker.start()
    logger.info(
        "insight notifications enabled (email=%s, web_push=%s)",
        email_enabled(),
        web_push_enabled(),
    )


def stop_notification_worker() -> None:
    with _worker_lock:
        worker = _worker
    if worker and worker.is_alive():
        _queue.put(None)  # unblock the get() so the thread can exit
        worker.join(timeout=5)


def enqueue_alert_events(events: list[AlertEvent]) -> None:
    """Queue a device's changed alerts for asynchronous delivery.

    Called from ``persist_insights`` after the alert rows (and their
    ``notified_at`` markers) are committed. Safe to call unconditionally: it
    returns immediately when no channel is configured.
    """
    if not events or not notifications_enabled():
        return
    _queue.put(events)


# ---------------------------------------------------------------------------
# Test notification (dashboard "send test" button)
# ---------------------------------------------------------------------------


def send_test_notification(email: Optional[str]) -> dict[str, Any]:
    """Fire a one-off test over each enabled channel. Runs synchronously so the
    dashboard can report the result. Returns a per-channel status dict."""
    result: dict[str, Any] = {"email": None, "web_push": None}

    if email_enabled() and email:
        try:
            _send_email(
                [email],
                "[HiveHub] Test notification",
                "This is a test alert from your HiveHub dashboard. "
                "If you received it, e-mail notifications are working.",
            )
            result["email"] = "sent"
        except Exception as exc:
            logger.exception("test e-mail failed")
            result["email"] = f"error: {exc}"

    if web_push_enabled():
        subs = _list_push_subscriptions()
        payload = {
            "title": "🐝 HiveHub test notification",
            "body": "Web Push is working. You'll get colony alerts here.",
            "severity": "info",
            "tag": "hivehub-test",
            "url": _dashboard_url() or "/dashboard",
        }
        sent = 0
        for sub in subs:
            try:
                _send_web_push_one(sub, payload)
                sent += 1
            except _PushUnavailable:
                result["web_push"] = "error: pywebpush not installed"
                break
            except Exception as exc:  # noqa: BLE001
                result["web_push"] = f"error: {exc}"
        else:
            result["web_push"] = f"sent to {sent} subscription(s)"

    return result
