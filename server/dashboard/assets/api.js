// Thin client for the HiveHub local dashboard API (/api/v1/local/*).
// Served from the same origin as the API, so all paths are relative and no
// credentials/headers are needed — the local API is auth-free by design.

const BASE = "/api/v1/local";

async function req(path, opts = {}) {
  const res = await fetch(BASE + path, opts);
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
  // 204 / empty body tolerated
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

export const api = {
  listDevices: () => req("/devices"),

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

  config: (deviceId) => req(`/devices/${encodeURIComponent(deviceId)}/config`),

  insightsSummary: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/insights/summary`),

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
