// HiveHub dashboard controller: wires the top-bar selectors + sidebar to the
// local API and renders the active data-group view.

import { api } from "./api.js";
import { auth } from "./auth.js";
import { el } from "./format.js";
import { GROUPS, clearCharts, drawCharts, availableHives, hiveLabel } from "./views.js";

const RANGES = {
  "1d":    { days: 1,    limit: 2000 },
  "7d":    { days: 7,    limit: 4000 },
  "30d":   { days: 30,   limit: 8000 },
  "365d":  { days: 365,  limit: 15000 },
  "1825d": { days: 1825, limit: 20000 },
};

const ui = {
  topbarControls: document.querySelector(".topbar-controls"),
  deviceSelect: document.getElementById("device-select"),
  hiveSelect: document.getElementById("hive-select"),
  rangeGroup: document.getElementById("range-group"),
  refreshBtn: document.getElementById("refresh-btn"),
  userArea: document.getElementById("user-area"),
  userChip: document.getElementById("user-chip"),
  logoutBtn: document.getElementById("logout-btn"),
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
  authUser: null,   // { username, role } once logged in
};

// ── toast ────────────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, kind = "") {
  // Replace any toast still on screen: with a single shared timer, appending a
  // second toast used to cancel the first one's removal, leaking it forever.
  for (const old of document.querySelectorAll(".toast")) old.remove();
  // role=alert/status makes screen readers announce the transient message
  const node = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" }, msg);
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 3500);
}

// ── data loading ──────────────────────────────────────────────────────────────
// Monotonic id per loadData call: every await point compares its own id against
// the newest one, so a slow response from a superseded request (device OR range
// switch) can never overwrite fresher data. The old guard only covered device
// switches, letting a stale range response win the race.
let loadSeq = 0;

async function loadData(opts = {}) {
  if (!state.deviceId || !state.authUser) return;
  const seq = ++loadSeq;
  // A "light" refresh (60s timer / range switch) refetches only what changes
  // with time or range — measurements, latest reading, insights. Config,
  // channels and firmware status are carried over from the previous load; a
  // full load runs on first load, device switch and the manual Refresh button.
  const full = !!opts.full || !state.data;
  const prev = state.data;
  state.loading = true;
  setStatus("Loading…");
  const { days, limit } = RANGES[state.range];
  const start = new Date(Date.now() - days * 86400000).toISOString();
  const id = state.deviceId;

  // Use allSettled so one failing call can't blank the whole dashboard, but —
  // unlike a silent `.catch(() => [])` — we keep the rejection so the chart/data
  // errors surface in the status line and console instead of masquerading as
  // "0 points / no data".
  const measurementsP = api.measurements(id, { start, limit });
  const latestP = api.latest(id, 1);
  const insightsP = api.insightsSummary(id);
  const configP = full ? api.config(id) : Promise.resolve(prev?.config ?? null);
  const channelsP = full ? api.channels(id) : Promise.resolve(prev?.channels ?? null);
  const firmwareP = full ? api.firmwareStatus(id) : Promise.resolve(prev?.firmware ?? null);

  const val = (r, fallback) => (r.status === "fulfilled" ? r.value : fallback);
  const errOf = (r, label) => {
    if (r.status !== "rejected") return null;
    const status = r.reason?.status ? ` (${r.reason.status})` : "";
    console.error(`HiveHub: ${label} request failed${status}:`, r.reason);
    return `${label}${status}: ${r.reason?.message || "request failed"}`;
  };

  // Phase 1: paint the charts as soon as the series + latest reading are in,
  // instead of also waiting on the slower panels (the insights summary can
  // trigger a full server-side recompute after a cold start).
  const [measurements, latest] = await Promise.allSettled([measurementsP, latestP]);
  if (seq !== loadSeq) return; // superseded by a newer load

  const chartError = errOf(measurements, "chart data");
  state.data = {
    measurements: val(measurements, []) || [],
    latest: (val(latest, null) && val(latest, [])[0]) || null,
    config: prev?.config ?? null,
    channels: prev?.channels ?? null,
    insights: prev?.insights ?? null,
    firmware: prev?.firmware ?? null,
  };
  state.dataError = chartError;
  // A failed chart query is the actionable one for empty diagrams; toast it.
  if (chartError) toast(chartError, "error");
  setStatus(chartError ? `⚠ ${chartError}` : chartStatus(state.data));
  // Early paint only when nothing is on screen yet; during a refresh the single
  // render below avoids rebuilding the view (and its forms) twice.
  if (!prev) render();

  // Phase 2: the remaining panels (placeholders resolve instantly on a light
  // refresh, so errOf only ever reports real requests).
  const [config, channels, insights, firmware] = await Promise.allSettled([
    configP, channelsP, insightsP, firmwareP,
  ]);
  if (seq !== loadSeq) return;

  const errors = [
    chartError,
    errOf(latest, "latest reading"),
    errOf(config, "config"),
    errOf(channels, "channels"),
    errOf(insights, "insights"),
    errOf(firmware, "firmware"),
  ].filter(Boolean);

  state.data = {
    ...state.data,
    config: val(config, prev?.config ?? null),
    channels: val(channels, prev?.channels ?? null),
    insights: val(insights, prev?.insights ?? null),
    firmware: val(firmware, prev?.firmware ?? null),
  };
  state.loading = false;

  if (errors.length) setStatus(`⚠ ${errors.join(" · ")}`);
  else setStatus(chartStatus(state.data));
  render();
}

// Human-readable chart status: point count plus, when the charts would be empty
// only because the latest reading predates the selected range, a hint to widen
// it (so an out-of-range device reads as "widen the range", not "no data").
function chartStatus(data) {
  const n = data.measurements.length;
  const stamp = `updated ${new Date().toLocaleTimeString()}`;
  if (n === 0 && data.latest?.measured_at) {
    const { days } = RANGES[state.range];
    const cutoff = Date.now() - days * 86400000;
    const latestMs = new Date(data.latest.measured_at).getTime();
    if (Number.isFinite(latestMs) && latestMs < cutoff) {
      return `No readings in the last ${days}d — latest is ${new Date(latestMs).toLocaleString()}. Widen the range. · ${stamp}`;
    }
  }
  return `${n} points · ${stamp}`;
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
    channels: d.channels,
    insights: d.insights,
    firmware: d.firmware,
    authUser: state.authUser,
    toast,
    reload: loadData,
    actions: {
      uploadFirmware: (fd) => api.uploadFirmware(state.deviceId, fd),
      importSdData: (fd) => api.importSdData(state.deviceId, fd),
      approveFirmware: () => api.approveFirmware(state.deviceId),
      startCalibration: (p) => api.startCalibration(state.deviceId, p),
      stopCalibration: () => api.stopCalibration(state.deviceId),
      fitTempComp: (p) => api.fitTempCompensation(state.deviceId, p),
      updateConfig: (p) => api.updateConfig(state.deviceId, p),
      updateChannels: (p) => api.updateChannels(state.deviceId, p),
      insightsHistory: (opts) => api.insightsHistory(state.deviceId, opts),
      // Dashboard account management (auth API). User-management calls require
      // the admin role server-side.
      listUsers: () => auth.listUsers(),
      createUser: (u, p, r, email) => auth.createUser(u, p, r, email),
      deleteUser: (id) => auth.deleteUser(id),
      changePassword: (cur, next) => auth.changePassword(cur, next),
      updateEmail: (email) => auth.updateEmail(email),
    },
  };
}

function renderSidebar() {
  ui.sidebar.replaceChildren(
    ...GROUPS.map((g) => {
      const active = g.id === state.group;
      const btn = el("button", {
        class: `nav-item ${active ? "active" : ""}`,
        type: "button",
        "aria-current": active ? "page" : null,
      }, el("span", { class: "nav-ico" }, g.icon), g.label);
      btn.addEventListener("click", () => { state.group = g.id; render(); });
      return btn;
    }));
}

// Rebuild the hive selector from the hives the current device actually reports
// (up to 18), labelled with their names. Preserves the current selection, or
// falls back to "All hives" when that hive is no longer present (device switch).
function populateHiveSelect(vstate) {
  const hives = availableHives(vstate);
  if (state.hive !== "all" && !hives.includes(Number(state.hive))) state.hive = "all";
  const options = [el("option", { value: "all" }, "All hives")];
  for (const n of hives) options.push(el("option", { value: String(n) }, hiveLabel(vstate, n)));
  ui.hiveSelect.replaceChildren(...options);
  ui.hiveSelect.value = state.hive;
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
    const vstate = buildState();
    populateHiveSelect(vstate);
    try {
      group.render(container, vstate);
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
    for (const b of ui.rangeGroup.querySelectorAll("button")) {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-pressed", String(b === btn));
    }
    loadData();
  });
  // The manual Refresh always refetches everything, including config/firmware.
  ui.refreshBtn.addEventListener("click", () => loadData({ full: true }));
  if (ui.logoutBtn) {
    ui.logoutBtn.addEventListener("click", async () => {
      try { await auth.logout(); } catch (_) { /* ignore — clear locally anyway */ }
      state.authUser = null;
      state.data = null;
      state.devices = [];
      renderLogin({ message: "You have been signed out." });
    });
  }
  // Session expired or missing (a data call returned 401): drop back to login.
  window.addEventListener("dashboard-unauthorized", () => {
    if (!state.authUser) return;
    state.authUser = null;
    state.data = null;
    renderLogin({ message: "Your session has expired. Please sign in again." });
  });
  window.addEventListener("resize", () => requestAnimationFrame(drawCharts));
  // Canvas charts don't inherit CSS colours, so redraw them when the theme flips.
  window.addEventListener("themechange", () => requestAnimationFrame(drawCharts));
  // auto-refresh every 60s
  setInterval(() => { if (!document.hidden && !state.loading) loadData(); }, 60000);
}

// ── auth gate (login / first-run setup) ───────────────────────────────────────
function showAuthScreen(node) {
  if (ui.topbarControls) ui.topbarControls.hidden = true;
  if (ui.userArea) ui.userArea.hidden = true;
  ui.sidebar.replaceChildren();
  ui.status.hidden = true;
  ui.content.replaceChildren(el("div", { class: "auth-wrap" }, node));
}

function renderUserArea() {
  if (!ui.userArea) return;
  if (!state.authUser) { ui.userArea.hidden = true; return; }
  ui.userChip.textContent = `${state.authUser.username} · ${state.authUser.role}`;
  ui.userArea.hidden = false;
}

function authField(labelText, input) {
  return el("label", { class: "auth-field" }, el("span", {}, labelText), input);
}

function renderLogin(opts = {}) {
  const u = el("input", { type: "text", autocomplete: "username", required: true });
  const p = el("input", { type: "password", autocomplete: "current-password", required: true });
  const btn = el("button", { class: "btn", type: "submit" }, "Sign in");
  const errLine = el("p", { class: "auth-error" }, opts.message || "");
  const form = el("form", { class: "auth-card" },
    el("h1", {}, "HiveHub Dashboard"),
    el("p", { class: "auth-sub" }, "Sign in to view and manage your hives."),
    authField("Username", u),
    authField("Password", p),
    errLine,
    el("div", { class: "form-actions" }, btn));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    btn.disabled = true; errLine.textContent = "";
    try {
      const r = await auth.login(u.value, p.value);
      state.authUser = r.user;
      await startApp();
    } catch (err) {
      errLine.textContent = err.message || "Sign in failed";
      btn.disabled = false;
    }
  });
  showAuthScreen(form);
  u.focus();
}

function renderSetup() {
  const u = el("input", { type: "text", autocomplete: "username", required: true });
  const em = el("input", { type: "email", autocomplete: "email", placeholder: "you@example.com" });
  const p = el("input", { type: "password", autocomplete: "new-password", required: true });
  const p2 = el("input", { type: "password", autocomplete: "new-password", required: true });
  const btn = el("button", { class: "btn", type: "submit" }, "Create admin account");
  const errLine = el("p", { class: "auth-error" });
  const form = el("form", { class: "auth-card" },
    el("h1", {}, "Welcome to HiveHub"),
    el("p", { class: "auth-sub" },
      "Create the first administrator account to secure this dashboard. You can add more accounts later."),
    authField("Username", u),
    authField("Email (optional — for alerts)", em),
    authField("Password (min 8 characters)", p),
    authField("Confirm password", p2),
    errLine,
    el("div", { class: "form-actions" }, btn));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (p.value.length < 8) { errLine.textContent = "Password must be at least 8 characters."; return; }
    if (p.value !== p2.value) { errLine.textContent = "Passwords do not match."; return; }
    btn.disabled = true; errLine.textContent = "";
    try {
      const r = await auth.setup(u.value, p.value, em.value);
      state.authUser = r.user;
      await startApp();
    } catch (err) {
      errLine.textContent = err.message || "Setup failed";
      btn.disabled = false;
    }
  });
  showAuthScreen(form);
  u.focus();
}

// ── app start (post-login) ────────────────────────────────────────────────────
// `devices` may be handed over by /auth/status (which includes the list for an
// authenticated session, saving one round-trip); otherwise it is fetched here.
async function startApp(devices) {
  if (ui.topbarControls) ui.topbarControls.hidden = false;
  renderUserArea();
  try {
    state.devices = Array.isArray(devices) ? devices : await api.listDevices();
  } catch (err) {
    if (err.status === 401) {
      state.authUser = null;
      renderLogin({ message: "Your session has expired. Please sign in again." });
      return;
    }
    ui.content.replaceChildren(el("div", { class: "empty-state" },
      "Could not reach the HiveHub API: " + err.message));
    return;
  }
  populateDevices();
  renderSidebar();
  if (state.deviceId) await loadData();
  else render();
}

async function init() {
  wireEvents();
  let status;
  try {
    status = await auth.status();
  } catch (err) {
    if (ui.topbarControls) ui.topbarControls.hidden = true;
    ui.content.replaceChildren(el("div", { class: "empty-state" },
      err.status === 404
        ? "Local dashboard is disabled. Set ENABLE_LOCAL_DASHBOARD=true on the server."
        : "Could not reach the HiveHub API: " + err.message));
    return;
  }
  if (status.setup_required) { renderSetup(); return; }
  if (!status.authenticated) { renderLogin(); return; }
  state.authUser = status.user;
  await startApp(status.devices);
}

init();
