// Client for the dashboard login API (/api/v1/local/auth/*). The session lives
// in an HttpOnly cookie set by the server, so these calls carry no token — they
// only need the cookie, which the browser attaches for same-origin requests.

const BASE = "/api/v1/local/auth";

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch (_) { /* non-JSON error body */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

function jsonBody(payload) {
  return { headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}) };
}

export const auth = {
  status: () => req("/status"),
  setup: (username, password, email) => req("/setup", { method: "POST", ...jsonBody({ username, password, email }) }),
  login: (username, password) => req("/login", { method: "POST", ...jsonBody({ username, password }) }),
  logout: () => req("/logout", { method: "POST" }),
  changePassword: (current_password, new_password) =>
    req("/password", { method: "POST", ...jsonBody({ current_password, new_password }) }),
  updateEmail: (email) => req("/email", { method: "POST", ...jsonBody({ email }) }),
  listUsers: () => req("/users"),
  createUser: (username, password, role, email) =>
    req("/users", { method: "POST", ...jsonBody({ username, password, role, email }) }),
  deleteUser: (id) => req(`/users/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
