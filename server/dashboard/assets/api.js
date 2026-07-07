// Thin client for the HiveHub local dashboard API (/api/v1/local/*).
// Served from the same origin as the API, so all paths are relative. The login
// session rides in an HttpOnly cookie (credentials: same-origin); a 401 means
// the session is missing/expired, which we broadcast so the app can re-prompt.

const BASE = "/api/v1/local";

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, { credentials: "same-origin", ...opts });
  if (!res.ok) {
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("dashboard-unauthorized"));
    }
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail != null) detail = body.detail;
    } catch (_) { /* non-JSON error body */ }
    // FastAPI's detail is usually a string, but structured errors (e.g. the SD
    // import device-mismatch guard) send an object. Surface a readable message
    // either way, and keep the full payload on err.detail for callers to act on.
    const message = typeof detail === "string" ? detail : (detail.message || res.statusText);
    const err = new Error(message);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  // 204 / empty body tolerated
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

export const api = {
  listDevices: () => req("/devices"),

  // Show / hide a device in the hive picker (admin). hidden=true retires it from
  // the top-bar picker without touching its stored data.
  setDeviceVisibility: (deviceId, hidden) =>
    req(`/devices/${encodeURIComponent(deviceId)}/visibility`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden: !!hidden }),
    }),

  // Delete a device's measurements in a time range (admin). Gated server-side by
  // the device's claim code — payload carries { start_at, end_at, claim_code }.
  deleteMeasurements: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/measurements/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  measurements: (deviceId, { start, end, limit } = {}) => {
    const q = new URLSearchParams();
    if (start) q.set("start_at", start);
    if (end) q.set("end_at", end);
    if (limit) q.set("limit", String(limit));
    const qs = q.toString();
    return req(`/devices/${encodeURIComponent(deviceId)}/measurements${qs ? "?" + qs : ""}`);
  },

  latest: (deviceId, limit = 1) =>
    req(`/devices/${encodeURIComponent(deviceId)}/measurements/latest?limit=${limit}`),

  // Upload an SD-card backup (measurements.ndjson or hivescale-sd-data.tar) pulled
  // off the scale in AP mode. formData carries the file under the "file" field;
  // the browser sets the multipart boundary, so no Content-Type header here.
  importSdData: (deviceId, formData) =>
    req(`/devices/${encodeURIComponent(deviceId)}/measurements/import`, {
      method: "POST",
      body: formData,
    }),

  config: (deviceId) => req(`/devices/${encodeURIComponent(deviceId)}/config`),

  updateConfig: (deviceId, patch) =>
    req(`/devices/${encodeURIComponent(deviceId)}/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch || {}),
    }),

  channels: (deviceId) => req(`/devices/${encodeURIComponent(deviceId)}/channels`),

  updateChannels: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/channels`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  insightsSummary: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/insights/summary`),

  insightsHistory: (deviceId, { status, category, since, limit } = {}) => {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    if (category) q.set("category", category);
    if (since) q.set("since", since);
    if (limit) q.set("limit", String(limit));
    const qs = q.toString();
    return req(`/devices/${encodeURIComponent(deviceId)}/insights/history${qs ? "?" + qs : ""}`);
  },

  firmwareStatus: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware/status`),

  uploadFirmware: (deviceId, formData) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware`, { method: "POST", body: formData }),

  approveFirmware: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware/approve`, { method: "POST" }),

  startCalibration: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/calibration/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  stopCalibration: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/calibration/stop`, { method: "POST" }),

  fitTempCompensation: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/temp-compensation/fit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),
};
