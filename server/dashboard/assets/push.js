// Web Push (browser / installable-PWA) client helpers for the dashboard.
//
// The service worker (../sw.js) is registered from index.html; here we drive the
// subscribe / unsubscribe handshake and mirror the resulting subscription to the
// backend (POST /api/v1/local/notifications/subscribe|unsubscribe). The backend
// stores it in push_subscriptions and delivers insight alerts to it.
//
// iOS note: Safari only exposes Web Push to a PWA that has been added to the Home
// Screen (iOS 16.4+). isSupported() is true there once installed; in a plain iOS
// Safari tab PushManager is absent, which the UI surfaces as "not supported".

import { api } from "./api.js";

export function isSupported() {
  return (
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}

// VAPID public keys travel as URL-safe base64; PushManager wants a Uint8Array.
function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function registration() {
  // index.html registers sw.js on load; ready resolves once it's active.
  return navigator.serviceWorker.ready;
}

// Whether this browser already has a live push subscription.
export async function isSubscribed() {
  if (!isSupported()) return false;
  try {
    const reg = await registration();
    return !!(await reg.pushManager.getSubscription());
  } catch (_) {
    return false;
  }
}

// Request permission (if needed) and subscribe, then register with the backend.
export async function subscribe(vapidPublicKey) {
  if (!isSupported()) throw new Error("Notifications are not supported on this browser");
  if (!vapidPublicKey) throw new Error("Web Push is not configured on this server");

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    throw new Error(
      permission === "denied"
        ? "Notifications are blocked — allow them in your browser settings"
        : "Notification permission was not granted"
    );
  }

  const reg = await registration();
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    });
  }
  // Send the full subscription (endpoint + keys) to the server.
  await api.pushSubscribe(sub.toJSON());
  return sub;
}

// Unsubscribe locally and tell the backend to forget the endpoint.
export async function unsubscribe() {
  if (!isSupported()) return;
  const reg = await registration();
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return;
  try {
    await api.pushUnsubscribe(sub.endpoint);
  } finally {
    // Drop it client-side even if the server call failed, so the toggle is honest.
    await sub.unsubscribe();
  }
}
