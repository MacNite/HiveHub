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

  // Erase a device and all of its data (admin). Unlike the `hidden` flag this is
  // irreversible, so the server wants both the claim code and the device_id
  // typed back — payload carries { claim_code, confirm_device_id }.
  deleteDevice: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/delete`, {
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

  // ── Inspections ──────────────────────────────────────────────────────────
  // The windows a beekeeper had the hive open. Charts shade them; the readings
  // inside them come back with their hive fields blanked and inspection: true,
  // so a gap in a weight trace is explained rather than mysterious.
  inspections: (deviceId, { start, end, limit } = {}) => {
    const q = new URLSearchParams();
    if (start) q.set("start_at", start);
    if (end) q.set("end_at", end);
    if (limit) q.set("limit", String(limit));
    const qs = q.toString();
    return req(`/devices/${encodeURIComponent(deviceId)}/inspections${qs ? "?" + qs : ""}`);
  },

  // Is this hub inspecting, and has it picked the request up yet? `pending`
  // means queued but not yet acknowledged — a sleeping hub can take a whole
  // send interval to notice, and showing that as "on" would be a lie.
  inspectionStatus: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/inspections/status`),

  startInspection: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/inspections/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  stopInspection: (deviceId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/inspections/stop`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  // Annotate a recorded inspection — "removed 2 supers" next to the step it
  // explains is the whole point of keeping the record.
  updateInspection: (deviceId, inspectionId, payload) =>
    req(`/devices/${encodeURIComponent(deviceId)}/inspections/${inspectionId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  // Upload an SD-card backup (measurements.ndjson or hivescale-sd-data.tar) pulled
  // off the scale in AP mode, or a backup downloaded from the panel below.
  // formData carries the file under the "file" field; the browser sets the
  // multipart boundary, so no Content-Type header here.
  importSdData: (deviceId, formData) =>
    req(`/devices/${encodeURIComponent(deviceId)}/measurements/import`, {
      method: "POST",
      body: formData,
    }),

  // ── Download / backup ────────────────────────────────────────────────────
  // Query string shared by the export summary and the download itself:
  // repeated device_id / hive params plus an optional time range.
  exportQuery: ({ deviceIds, hives, start, end } = {}) => {
    const q = new URLSearchParams();
    for (const id of deviceIds || []) q.append("device_id", id);
    for (const h of hives || []) q.append("hive", String(h));
    if (start) q.set("start_at", start);
    if (end) q.set("end_at", end);
    return q.toString();
  },

  // How many readings a download with these filters would contain, per device.
  // Checked before downloading: the download itself is a plain browser
  // navigation, so an error or an empty selection would otherwise only show up
  // in the saved file.
  exportSummary: (opts) => req(`/export/measurements/summary?${api.exportQuery(opts)}`),

  // Absolute URL of the download, for an <a download> click. The session rides
  // in the same-origin cookie, so the navigation is authenticated like any
  // other dashboard request.
  exportUrl: (opts) => `${BASE}/export/measurements?${api.exportQuery(opts)}`,

  // ── Publish data (public embeds) ──────────────────────────────────────────
  // Charts an admin has published for embedding in a website. Reading the list
  // needs a session; creating, editing and revoking need the admin role. All of
  // these 404 when the server has publishing switched off
  // (ENABLE_PUBLIC_EMBEDS=false), which the Publish view reports as such.

  // The publishable metrics (id, label, unit, digits, hive/device scope), so the
  // dashboard never carries its own copy of the server-side registry.
  publishMetrics: () => req("/publish/metrics"),

  publishedCharts: () => req("/publish/charts"),

  publishChart: (payload) =>
    req("/publish/charts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    }),

  // Edit in place — the public token is kept, so an embed already pasted into a
  // website keeps working while its title, period or series change under it.
  updatePublishedChart: (id, patch) =>
    req(`/publish/charts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch || {}),
    }),

  deletePublishedChart: (id) =>
    req(`/publish/charts/${encodeURIComponent(id)}`, { method: "DELETE" }),

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

  // ── Insight-alert notifications ──────────────────────────────────────────
  // Which channels the server has enabled, plus the VAPID public key the
  // browser needs to subscribe to Web Push.
  notificationsConfig: () => req("/notifications/config"),

  pushSubscribe: (subscription) =>
    req("/notifications/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(subscription),
    }),

  pushUnsubscribe: (endpoint) =>
    req("/notifications/unsubscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint }),
    }),

  testNotification: () => req("/notifications/test", { method: "POST" }),

  firmwareStatus: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware/status`),

  uploadFirmware: (deviceId, formData) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware`, { method: "POST", body: formData }),

  approveFirmware: (deviceId) =>
    req(`/devices/${encodeURIComponent(deviceId)}/firmware/approve`, { method: "POST" }),

  // Queue the BLE relay of the latest HiveInside release to the node on `slot`
  // (a hive index). Uploading the .bin only registers it; this is what starts
  // the transfer.
  queueHiveInsideUpdate: (deviceId, slot, force) =>
    req(`/devices/${encodeURIComponent(deviceId)}/hiveinside/update` +
        `?slot=${encodeURIComponent(slot)}${force ? "&force=true" : ""}`,
      { method: "POST" }),

  // Same for the HiveTraffic counter on `slot`. The counter stops counting for
  // the duration of the transfer, so this is deliberately never automatic.
  queueBeeCounterUpdate: (deviceId, slot, force) =>
    req(`/devices/${encodeURIComponent(deviceId)}/beecounter/update` +
        `?slot=${encodeURIComponent(slot)}${force ? "&force=true" : ""}`,
      { method: "POST" }),

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
