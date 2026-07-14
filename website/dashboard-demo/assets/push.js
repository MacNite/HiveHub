// DEMO stub for the dashboard's Web Push helpers.
//
// Drop-in replacement for server/dashboard/assets/push.js so the shared
// views.js "Alert notifications" card renders in the public demo. The demo has
// no service worker and no backend, so there is nothing to subscribe to: push
// reports as supported-but-off, and toggling it surfaces the same "read-only
// demo" notice as the other write controls instead of pretending to act.

const demoErr = () =>
  Promise.reject(new Error("This is a read-only demo — notification changes are disabled."));

export function isSupported() {
  return true;
}

export async function isSubscribed() {
  return false;
}

export const subscribe = demoErr;
export const unsubscribe = demoErr;
