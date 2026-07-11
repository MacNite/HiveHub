// HiveHub dashboard service worker.
//
// Its sole job is Web Push: receive an insight-alert payload from the backend
// (server/notifications.py) and surface it as a system notification, then focus
// or open the dashboard when the user taps it. It deliberately does NOT cache /
// serve app assets — the dashboard is an online tool served from the same
// origin, and an offline cache would only risk showing stale data.

self.addEventListener("install", () => {
  // Activate immediately so a freshly deployed worker starts handling push.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (_) {
    data = { title: "HiveHub", body: event.data ? event.data.text() : "" };
  }

  const title = data.title || "HiveHub alert";
  const options = {
    body: data.body || "",
    icon: "assets/icon.svg",
    badge: "assets/icon.svg",
    // tag = device:alert_key, so a repeat/escalation of the same alert replaces
    // the previous notification instead of stacking.
    tag: data.tag || undefined,
    renotify: !!data.tag,
    // Critical alerts (e.g. a swarm departure) stay on screen until dismissed.
    requireInteraction: data.severity === "critical",
    data: { url: data.url || "/dashboard" },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/dashboard";
  event.waitUntil(
    (async () => {
      const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of wins) {
        // Focus an existing dashboard tab if one is open.
        if ("focus" in client) {
          try { if ("navigate" in client) await client.navigate(target); } catch (_) { /* cross-origin */ }
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(target);
    })()
  );
});
