// HiveHub dashboard controller: wires the top-bar selectors + sidebar to the
// local API and renders the active data-group view.

import { api } from "./api.js";
import { el } from "./format.js";
import { GROUPS, clearCharts, drawCharts } from "./views.js";

const RANGES = {
  "1d":    { days: 1,    limit: 2000 },
  "7d":    { days: 7,    limit: 4000 },
  "30d":   { days: 30,   limit: 8000 },
  "365d":  { days: 365,  limit: 15000 },
  "1825d": { days: 1825, limit: 20000 },
};

const ui = {
  deviceSelect: document.getElementById("device-select"),
  hiveSelect: document.getElementById("hive-select"),
  rangeGroup: document.getElementById("range-group"),
  refreshBtn: document.getElementById("refresh-btn"),
  sidebar: document.getElementById("sidebar"),
  content: document.getElementById("content"),
  status: document.getElementById("device-status"),
};

const state = {
  devices: [],
  deviceId: null,
  hive: "all",
  range: "7d",
  group: "overview",
  data: null,       // { measurements, latest, config, insights, firmware }
  loading: false,
};

// ── toast ────────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, kind = "") {
  const node = el("div", { class: `toast ${kind}` }, msg);
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 3500);
}

// ── data loading ──────────────────────────────────────────────────────────────
async function loadData() {
  if (!state.deviceId) return;
  state.loading = true;
  setStatus("Loading…");
  const { days, limit } = RANGES[state.range];
  const start = new Date(Date.now() - days * 86400000).toISOString();
  const id = state.deviceId;

  const [measurements, latest, config, insights, firmware] = await Promise.all([
    api.measurements(id, { start, limit }).catch(() => []),
    api.latest(id, 1).catch(() => []),
    api.config(id).catch(() => null),
    api.insightsSummary(id).catch(() => null),
    api.firmwareStatus(id).catch(() => null),
  ]);

  // ignore the response if the user switched device/range while we were loading
  if (state.deviceId !== id) return;

  state.data = {
    measurements: measurements || [],
    latest: (latest && latest[0]) || null,
    config,
    insights,
    firmware,
  };
  state.loading = false;
  setStatus(`${state.data.measurements.length} points · updated ${new Date().toLocaleTimeString()}`);
  render();
}

function setStatus(text) {
  ui.status.textContent = text;
  ui.status.hidden = false;
}

// ── rendering ─────────────────────────────────────────────────────────────────
function buildState() {
  const d = state.data || {};
  return {
    device: state.devices.find((x) => x.device_id === state.deviceId) || null,
    hive: state.hive,
    range: state.range,
    measurements: d.measurements || [],
    latest: d.latest,
    config: d.config,
    insights: d.insights,
    firmware: d.firmware,
    toast,
    reload: loadData,
    actions: {
      uploadFirmware: (fd) => api.uploadFirmware(state.deviceId, fd),
      approveFirmware: () => api.approveFirmware(state.deviceId),
      startCalibration: (p) => api.startCalibration(state.deviceId, p),
      stopCalibration: () => api.stopCalibration(state.deviceId),
      fitTempComp: (p) => api.fitTempCompensation(state.deviceId, p),
    },
  };
}

function renderSidebar() {
  ui.sidebar.replaceChildren(
    ...GROUPS.map((g) => {
      const btn = el("button", { class: `nav-item ${g.id === state.group ? "active" : ""}`, type: "button" },
        el("span", { class: "nav-ico" }, g.icon), g.label);
      btn.addEventListener("click", () => { state.group = g.id; render(); });
      return btn;
    }));
}

function render() {
  renderSidebar();
  clearCharts();
  const group = GROUPS.find((g) => g.id === state.group) || GROUPS[0];
  const container = el("div", {});
  if (!state.deviceId) {
    container.append(el("div", { class: "empty-state" }, "No devices found on this server."));
  } else if (!state.data) {
    container.append(el("div", { class: "empty-state" }, "Loading…"));
  } else {
    try {
      group.render(container, buildState());
    } catch (err) {
      container.append(el("div", { class: "empty-state" }, "Failed to render this view: " + err.message));
      console.error(err);
    }
  }
  ui.content.replaceChildren(container);
  requestAnimationFrame(drawCharts);
}

// ── init / events ─────────────────────────────────────────────────────────────
function populateDevices() {
  ui.deviceSelect.replaceChildren(
    ...state.devices.map((d) =>
      el("option", { value: d.device_id }, d.display_name || d.device_id)));
  if (state.devices.length) {
    state.deviceId = state.devices[0].device_id;
    ui.deviceSelect.value = state.deviceId;
  }
}

function wireEvents() {
  ui.deviceSelect.addEventListener("change", () => {
    state.deviceId = ui.deviceSelect.value;
    state.data = null;
    render();
    loadData();
  });
  ui.hiveSelect.addEventListener("change", () => { state.hive = ui.hiveSelect.value; render(); });
  ui.rangeGroup.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-range]");
    if (!btn) return;
    state.range = btn.dataset.range;
    for (const b of ui.rangeGroup.querySelectorAll("button")) b.classList.toggle("active", b === btn);
    loadData();
  });
  ui.refreshBtn.addEventListener("click", loadData);
  window.addEventListener("resize", () => requestAnimationFrame(drawCharts));
  // auto-refresh every 60s
  setInterval(() => { if (!document.hidden && !state.loading) loadData(); }, 60000);
}

async function init() {
  wireEvents();
  try {
    state.devices = await api.listDevices();
  } catch (err) {
    ui.content.replaceChildren(el("div", { class: "empty-state" },
      err.status === 404
        ? "Local dashboard is disabled. Set ENABLE_LOCAL_DASHBOARD=true on the server."
        : "Could not reach the HiveHub API: " + err.message));
    return;
  }
  populateDevices();
  renderSidebar();
  if (state.deviceId) await loadData();
  else render();
}

init();
