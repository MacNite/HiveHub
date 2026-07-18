// HiveHub dashboard controller — DEMO fork.
//
// Same selector/render wiring as the real server/dashboard/assets/app.js, but
// with the login/auth flow removed: the demo has no backend to sign in to, so it
// runs a static "demo/viewer" session against the in-browser sample data in
// api.js. When the real app.js changes, port the relevant bits by hand.

import { api } from "./api.js";
import { el } from "./format.js";
import { GROUPS, clearCharts, drawCharts, configureCharts, availableHives, hiveLabel } from "./views.js";
import { PALETTE } from "./charts.js";

const RANGES = {
  "1d":    { days: 1,    limit: 2000 },
  "7d":    { days: 7,    limit: 4000 },
  "30d":   { days: 30,   limit: 8000 },
  "365d":  { days: 365,  limit: 15000 },
  "1825d": { days: 1825, limit: 20000 },
};

const ui = {
  // Cross-device hive picker
  hiveTrigger: document.getElementById("hive-trigger"),
  hiveTriggerLabel: document.getElementById("hive-trigger-label"),
  hiveCount: document.getElementById("hive-count"),
  hivePop: document.getElementById("hive-pop"),
  hivePopList: document.getElementById("hive-pop-list"),
  hiveSearch: document.getElementById("hive-search"),
  hiveClear: document.getElementById("hive-clear"),
  hiveDone: document.getElementById("hive-done"),
  hivePopHint: document.getElementById("hive-pop-hint"),
  selectionStrip: document.getElementById("selection-strip"),
  // Device-details scope selector (only shown when comparing >1 device)
  activeDeviceField: document.getElementById("active-device-field"),
  activeDeviceSelect: document.getElementById("active-device-select"),
  rangeGroup: document.getElementById("range-group"),
  refreshBtn: document.getElementById("refresh-btn"),
  sidebar: document.getElementById("sidebar"),
  content: document.getElementById("content"),
  status: document.getElementById("device-status"),
};

// The demo has no auth API, so a fixed session stands in for the real dashboard's
// logged-in user (drives the Device & admin "Your account" panel). It uses the
// admin role so the demo showcases the full Admin panel (users, visible devices,
// delete readings); the write actions themselves are stubbed in api.js.
const DEMO_USER = { username: "demo", role: "admin", email: null };

const state = {
  devices: [],
  // One latest reading per device, loaded up front so the hive picker can list
  // every device's hives without fetching each device's full history.
  deviceLatest: {},        // { [deviceId]: latestRow | null }
  // The cross-device comparison set: an ordered list of { deviceId, hive } the
  // user has picked. Colour is assigned by position (see selColor), matching the
  // chart palette cycle in views.js.
  selection: [],
  // The device whose device-scoped panels (config, firmware, calibration,
  // insights, connectivity, collector battery, ambient) are shown. Always one of
  // the selected devices; a switcher appears only when the selection spans several.
  activeDeviceId: null,
  range: "7d",
  group: "overview",
  // { measurements: {[id]:[]}, latest: {[id]:row}, config, channels, insights, firmware }
  data: null,
  loading: false,
};

// ── selection helpers ─────────────────────────────────────────────────────────
function selColor(i) { const n = PALETTE.length; return PALETTE[((i % n) + n) % n]; }
function selIndex(deviceId, hive) {
  return state.selection.findIndex((s) => s.deviceId === deviceId && Number(s.hive) === Number(hive));
}
function hasSel(deviceId, hive) { return selIndex(deviceId, hive) !== -1; }
function toggleSel(deviceId, hive) {
  const i = selIndex(deviceId, hive);
  if (i === -1) state.selection.push({ deviceId, hive: Number(hive) });
  else state.selection.splice(i, 1);
}
// Unique device ids appearing in the selection, in first-seen order.
function selectionDeviceIds() {
  const out = [], seen = new Set();
  for (const s of state.selection) if (!seen.has(s.deviceId)) { seen.add(s.deviceId); out.push(s.deviceId); }
  return out;
}
// Devices whose measurements we must fetch: everything in the selection plus the
// active device (so its device-level panels have data even with nothing ticked).
function deviceIdsForData() {
  const ids = selectionDeviceIds();
  if (state.activeDeviceId && !ids.includes(state.activeDeviceId)) ids.push(state.activeDeviceId);
  return ids;
}
function deviceMeta(id) { return state.devices.find((d) => d.device_id === id) || null; }
function deviceName(id) { const d = deviceMeta(id); return d ? (d.display_name || d.device_id) : id; }
// Latest weight for hive n on a reading (compensated column first), for the
// picker's per-hive value hint. Mirrors views.js weightKey().
function hiveWeight(row, n) {
  if (!row) return null;
  const comp = row[`scale_${n}_weight_kg_compensated`];
  return comp != null ? comp : (row[`scale_${n}_weight_kg`] ?? null);
}
// A device-shaped object for availableHives()/hiveLabel(): custom channel names
// are only loaded for the active device, so others fall back to firmware names.
function deviceState(id) {
  return { latest: state.deviceLatest[id] || null, channels: id === state.activeDeviceId ? state.data?.channels : null, device: deviceMeta(id) };
}
// Devices offered in the hive picker: everything except the ones an admin has
// retired (hidden). Hidden devices stay in state.devices so the admin "Visible
// devices" panel can still list and un-hide them.
function pickerDevices() { return state.devices.filter((d) => !d.hidden); }
// Default to the first visible device with all its hives selected — reproducing
// the old "device 1 · All hives" landing state.
function resetSelectionToFirstDevice() {
  const d = pickerDevices()[0];
  state.selection = [];
  if (!d) { state.activeDeviceId = null; return; }
  state.activeDeviceId = d.device_id;
  for (const n of availableHives(deviceState(d.device_id))) state.selection.push({ deviceId: d.device_id, hive: n });
}
// Keep the active device valid: it must be a device that's currently selected.
function normalizeActiveDevice() {
  const ids = selectionDeviceIds();
  const fallback = pickerDevices()[0];
  if (!ids.length) { if (!state.activeDeviceId && fallback) state.activeDeviceId = fallback.device_id; return; }
  if (!ids.includes(state.activeDeviceId)) state.activeDeviceId = ids[0];
}

// Apply a device visibility toggle from the admin panel: persist it, mirror it
// into local state, drop a newly-hidden device from the comparison selection,
// and repaint the picker + current view.
async function setDeviceVisibility(deviceId, hidden) {
  await api.setDeviceVisibility(deviceId, hidden);
  const d = deviceMeta(deviceId);
  if (d) d.hidden = hidden;
  if (hidden) {
    state.selection = state.selection.filter((s) => s.deviceId !== deviceId);
    if (!state.selection.length) resetSelectionToFirstDevice();
  }
  normalizeActiveDevice();
  renderPicker();
  renderSelectionStrip();
  renderActiveDeviceField();
  render();
}

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
// switch) can never overwrite fresher data.
let loadSeq = 0;

const errOf = (r, label) => {
  if (r.status !== "rejected") return null;
  const status = r.reason?.status ? ` (${r.reason.status})` : "";
  console.error(`HiveHub: ${label} request failed${status}:`, r.reason);
  return `${label}${status}: ${r.reason?.message || "request failed"}`;
};

async function loadData(opts = {}) {
  if (!state.activeDeviceId) return;
  const seq = ++loadSeq;
  // A "light" refresh (60s timer / range switch / selection change) refetches
  // only what changes with time or range — per-device measurements, latest
  // readings, insights. Config, channels and firmware status are carried over
  // from the previous load; a full load runs on first load, active-device switch
  // and the manual Refresh button.
  const full = !!opts.full || !state.data;
  const prev = state.data;
  state.loading = true;
  setStatus("Loading…");
  const { days, limit } = RANGES[state.range];
  const start = new Date(Date.now() - days * 86400000).toISOString();
  const activeId = state.activeDeviceId;
  const ids = deviceIdsForData();

  // Fetch measurements + latest for every device we chart, in parallel. allSettled
  // keeps one device's failure from blanking the others, while still surfacing it.
  const perDeviceP = ids.map((id) =>
    Promise.allSettled([api.measurements(id, { start, limit }), api.latest(id, 1)])
      .then(([m, l]) => ({ id, m, l })));

  // Device-scoped panels follow the active device only.
  const insightsP = api.insightsSummary(activeId);
  const configP = full ? api.config(activeId) : Promise.resolve(prev?.config ?? null);
  const channelsP = full ? api.channels(activeId) : Promise.resolve(prev?.channels ?? null);
  const firmwareP = full ? api.firmwareStatus(activeId) : Promise.resolve(prev?.firmware ?? null);

  // Phase 1: per-device series + latest. Paint as soon as these are in.
  const perDevice = await Promise.all(perDeviceP);
  if (seq !== loadSeq) return; // superseded by a newer load

  const measurements = {}, latest = {}, chartErrors = [];
  for (const { id, m, l } of perDevice) {
    measurements[id] = (m.status === "fulfilled" ? m.value : []) || [];
    const row = l.status === "fulfilled" ? ((l.value || [])[0] || null) : null;
    latest[id] = row;
    if (row) state.deviceLatest[id] = row; // keep the picker's hive list fresh
    const e = errOf(m, `chart data (${deviceName(id)})`);
    if (e) chartErrors.push(e);
  }

  state.data = {
    measurements, latest,
    config: prev?.config ?? null,
    channels: prev?.channels ?? null,
    insights: prev?.insights ?? null,
    firmware: prev?.firmware ?? null,
  };
  state.dataError = chartErrors[0] || null;
  if (state.dataError) toast(state.dataError, "error");
  setStatus(state.dataError ? `⚠ ${state.dataError}` : chartStatus());
  if (!prev) render();

  // Phase 2: active-device panels (placeholders resolve instantly on a light refresh).
  const [config, channels, insights, firmware] = await Promise.allSettled([
    configP, channelsP, insightsP, firmwareP,
  ]);
  if (seq !== loadSeq) return;

  const val = (r, fallback) => (r.status === "fulfilled" ? r.value : fallback);
  const errors = [
    ...chartErrors,
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
  else setStatus(chartStatus());
  render();
}

// Human-readable chart status: total point count across the charted devices,
// plus, when the charts would be empty only because every latest reading
// predates the selected range, a hint to widen it.
function chartStatus() {
  const data = state.data;
  if (!data) return "";
  const ids = deviceIdsForData();
  let n = 0;
  for (const id of ids) n += (data.measurements[id] || []).length;
  const stamp = `updated ${new Date().toLocaleTimeString()}`;
  if (n === 0) {
    const { days } = RANGES[state.range];
    const cutoff = Date.now() - days * 86400000;
    for (const id of ids) {
      const at = data.latest[id]?.measured_at;
      const ms = at ? new Date(at).getTime() : NaN;
      if (Number.isFinite(ms) && ms < cutoff) {
        return `No readings in the last ${days}d — latest is ${new Date(ms).toLocaleString()}. Widen the range. · ${stamp}`;
      }
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
  const activeId = state.activeDeviceId;
  const multiDevice = selectionDeviceIds().length > 1;
  return {
    // Active device — drives device-scoped panels + back-compat single-device access.
    device: deviceMeta(activeId),
    activeDeviceId: activeId,
    range: state.range,
    measurements: (d.measurements && d.measurements[activeId]) || [],
    latest: (d.latest && d.latest[activeId]) || state.deviceLatest[activeId] || null,
    config: d.config,
    channels: d.channels,
    insights: d.insights,
    firmware: d.firmware,
    // Cross-device comparison selection + accessors used by the hive-centric views.
    selection: state.selection.map((s) => ({ deviceId: s.deviceId, hive: Number(s.hive) })),
    multiDevice,
    devices: state.devices,
    deviceMeta,
    deviceName,
    deviceLatest: (id) => (d.latest && d.latest[id]) || state.deviceLatest[id] || null,
    deviceMeasurements: (id) => (d.measurements && d.measurements[id]) || [],
    deviceChannels: (id) => (id === activeId ? d.channels : null),
    // Static demo session — the real dashboard fills this from the auth API.
    authUser: DEMO_USER,
    toast,
    reload: loadData,
    actions: {
      uploadFirmware: (fd) => api.uploadFirmware(activeId, fd),
      importSdData: (fd) => api.importSdData(activeId, fd),
      approveFirmware: () => api.approveFirmware(activeId),
      startCalibration: (p) => api.startCalibration(activeId, p),
      stopCalibration: () => api.stopCalibration(activeId),
      fitTempComp: (p) => api.fitTempCompensation(activeId, p),
      updateConfig: (p) => api.updateConfig(activeId, p),
      updateChannels: (p) => api.updateChannels(activeId, p),
      insightsHistory: (opts) => api.insightsHistory(activeId, opts),
      // Device visibility (admin): retire/restore a device in the hive picker.
      setDeviceVisibility: (deviceId, hidden) => setDeviceVisibility(deviceId, hidden),
      // Delete a device's readings in a time range, authed by its claim code.
      deleteMeasurements: (deviceId, p) => api.deleteMeasurements(deviceId, p),
      // Account management is auth-backed in the real dashboard; disabled here.
      listUsers: () => api.listUsers(),
      createUser: (u, p, r, email) => api.createUser(u, p, r, email),
      deleteUser: (id) => api.deleteUser(id),
      changePassword: (cur, next) => api.changePassword(cur, next),
      updateEmail: (email) => api.updateEmail(email),
      // Insight-alert notifications: channel config for the "Alert notifications"
      // card + a test-send that surfaces the read-only demo notice.
      notificationsConfig: () => api.notificationsConfig(),
      testNotification: () => api.testNotification(),
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

// ── cross-device hive picker ────────────────────────────────────────────────
// A selection change (checkbox toggle) is reflected instantly from cached data,
// then a debounced light load fills in any newly-added device's history.
let selLoadTimer = null;
function onSelectionChanged() {
  normalizeActiveDevice();
  renderPicker();
  renderSelectionStrip();
  renderActiveDeviceField();
  render();
  clearTimeout(selLoadTimer);
  selLoadTimer = setTimeout(() => loadData(), 250);
}

// Build the grouped popover: one section per device, each hive a checkbox. The
// device header ticks/unticks all of that device's hives at once.
function renderPicker() {
  const q = ui.hiveSearch.value.trim().toLowerCase();
  const groups = [];
  for (const dev of pickerDevices()) {
    const dstate = deviceState(dev.device_id);
    const allHives = availableHives(dstate);
    const hives = allHives.filter((n) => {
      if (!q) return true;
      return hiveLabel(dstate, n).toLowerCase().includes(q)
        || (dev.display_name || "").toLowerCase().includes(q)
        || dev.device_id.toLowerCase().includes(q);
    });
    if (!hives.length) continue;

    const onCount = allHives.filter((n) => hasSel(dev.device_id, n)).length;
    const allOn = allHives.length > 0 && onCount === allHives.length;
    // The checkbox is purely visual — the wrapping button is the control. Hide
    // it from AT and keyboard so screen readers hear one labelled toggle
    // instead of a nested checkbox-in-button.
    const headCb = el("input", { type: "checkbox", "aria-hidden": "true", tabindex: "-1" });
    headCb.checked = allOn;
    headCb.indeterminate = onCount > 0 && !allOn;
    const head = el("button", {
      class: "hive-dev-head",
      type: "button",
      "aria-pressed": String(allOn),
      "aria-label": `${allOn ? "Deselect" : "Select"} all hives on ${dev.display_name || dev.device_id} (${onCount} of ${allHives.length} selected)`,
    },
      headCb,
      el("span", { class: "hive-dev-name" }, dev.display_name || dev.device_id),
      el("span", { class: "hive-dev-id" }, dev.device_id),
      el("span", { class: "hive-dev-meta" }, `${onCount}/${allHives.length}`));
    head.addEventListener("click", (e) => {
      e.preventDefault();
      const turnOn = !allOn;
      for (const n of allHives) {
        if (turnOn && !hasSel(dev.device_id, n)) state.selection.push({ deviceId: dev.device_id, hive: n });
        if (!turnOn && hasSel(dev.device_id, n)) state.selection.splice(selIndex(dev.device_id, n), 1);
      }
      onSelectionChanged();
    });

    const group = el("div", { class: "hive-dev-group" }, head);
    for (const n of hives) {
      const on = hasSel(dev.device_id, n);
      const cb = el("input", { type: "checkbox" });
      cb.checked = on;
      const w = hiveWeight(dstate.latest, n);
      const row = el("label", { class: `hive-opt-row${on ? " on" : ""}` },
        cb,
        el("span", { class: "hive-swatch", style: `background:${on ? selColor(selIndex(dev.device_id, n)) : "var(--ink-faint)"}` }),
        el("span", { class: "hive-opt-name" }, hiveLabel(dstate, n)),
        el("span", { class: "hive-opt-val" }, w != null ? `${w.toFixed(1)} kg` : ""));
      row.addEventListener("click", (e) => { e.preventDefault(); toggleSel(dev.device_id, n); onSelectionChanged(); });
      group.append(row);
    }
    groups.push(group);
  }
  ui.hivePopList.replaceChildren(
    ...(groups.length ? groups : [el("p", { class: "hive-pop-empty" }, q ? `No hives match “${ui.hiveSearch.value}”.` : "No hives reported yet.")]));

  // trigger summary + footer hint
  const count = state.selection.length;
  const nDev = selectionDeviceIds().length;
  ui.hiveCount.hidden = count === 0;
  ui.hiveCount.textContent = String(count);
  ui.hiveTriggerLabel.textContent = count === 0
    ? "Select hives"
    : count === 1
      ? hiveLabel(deviceState(state.selection[0].deviceId), state.selection[0].hive)
      : `${count} hives`;
  ui.hivePopHint.textContent = count === 0 ? "Nothing selected"
    : `${count} hive${count === 1 ? "" : "s"} · ${nDev} device${nDev === 1 ? "" : "s"}`;
}

// The always-visible strip below the top bar: one removable chip per selected
// hive, colour-matched to its chart series, tagged with its device.
function renderSelectionStrip() {
  const strip = ui.selectionStrip;
  strip.hidden = false;
  const kids = [el("span", { class: "strip-label" }, "Comparing")];
  if (!state.selection.length) {
    kids.push(el("span", { class: "strip-empty" }, "No hives selected — open the Hives menu to choose."));
  } else {
    state.selection.forEach((s, i) => {
      const dstate = deviceState(s.deviceId);
      const x = el("button", { class: "chip-x", type: "button", title: "Remove", "aria-label": `Remove ${hiveLabel(dstate, s.hive)}` }, "×");
      x.addEventListener("click", () => { toggleSel(s.deviceId, s.hive); onSelectionChanged(); });
      kids.push(el("span", { class: "hive-chip" },
        el("span", { class: "hive-swatch", style: `background:${selColor(i)}` }),
        hiveLabel(dstate, s.hive),
        el("span", { class: "chip-dev" }, `· ${deviceName(s.deviceId)}`),
        x));
    });
  }
  strip.replaceChildren(...kids);
}

// The "Device details" scope selector — only meaningful when several devices are
// being compared, so it stays hidden for the common single-device case.
function renderActiveDeviceField() {
  const ids = selectionDeviceIds();
  const show = ids.length > 1;
  ui.activeDeviceField.hidden = !show;
  if (!show) return;
  ui.activeDeviceSelect.replaceChildren(
    ...ids.map((id) => el("option", { value: id }, deviceName(id))));
  ui.activeDeviceSelect.value = state.activeDeviceId;
}

function render() {
  renderSidebar();
  clearCharts();
  const group = GROUPS.find((g) => g.id === state.group) || GROUPS[0];
  const container = el("div", {});
  if (!state.devices.length) {
    container.append(el("div", { class: "empty-state" }, "No devices found on this server."));
  } else if (!state.data) {
    container.append(el("div", { class: "empty-state" }, "Loading…"));
  } else {
    const vstate = buildState();
    configureCharts(vstate);
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
function openPicker() {
  ui.hivePop.hidden = false;
  ui.hiveTrigger.setAttribute("aria-expanded", "true");
  renderPicker();
  ui.hiveSearch.focus();
}
function closePicker() {
  // If focus is still inside the popover (Done button, search box, a row),
  // hiding it would drop keyboard focus on <body>; hand it back to the trigger.
  const hadFocus = ui.hivePop.contains(document.activeElement);
  ui.hivePop.hidden = true;
  ui.hiveTrigger.setAttribute("aria-expanded", "false");
  ui.hiveSearch.value = "";
  if (hadFocus) ui.hiveTrigger.focus();
}

function wireEvents() {
  // Hive picker: toggle, filter, clear, done, and outside-click / Escape to close.
  ui.hiveTrigger.addEventListener("click", () => { ui.hivePop.hidden ? openPicker() : closePicker(); });
  ui.hiveDone.addEventListener("click", closePicker);
  ui.hiveSearch.addEventListener("input", renderPicker);
  ui.hiveClear.addEventListener("click", () => { state.selection = []; onSelectionChanged(); ui.hiveSearch.focus(); });
  ui.hivePop.addEventListener("keydown", (e) => { if (e.key === "Escape") { closePicker(); ui.hiveTrigger.focus(); } });
  // Detect outside clicks on pointerdown (before any re-render): toggling a hive
  // rebuilds the list synchronously, which would detach the clicked row and make a
  // later "click" listener read it as an outside click, dismissing the menu.
  document.addEventListener("pointerdown", (e) => {
    if (ui.hivePop.hidden) return;
    if (!ui.hivePop.contains(e.target) && !ui.hiveTrigger.contains(e.target)) closePicker();
  });
  // Device-details scope switcher (shown only when comparing several devices).
  ui.activeDeviceSelect.addEventListener("change", () => {
    state.activeDeviceId = ui.activeDeviceSelect.value;
    renderActiveDeviceField();
    render();               // repaint device-scoped panels immediately
    loadData({ full: true }); // refetch this device's config/firmware/insights
  });
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
  window.addEventListener("resize", () => requestAnimationFrame(drawCharts));
  // Canvas charts don't inherit CSS colours, so redraw them when the theme flips.
  window.addEventListener("themechange", () => requestAnimationFrame(drawCharts));
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
  // Load one latest reading per device up front so the hive picker can list
  // every device's hives (cheap: a single row each) without pulling full history.
  await Promise.allSettled(state.devices.map(async (d) => {
    try { const r = await api.latest(d.device_id, 1); state.deviceLatest[d.device_id] = (r && r[0]) || null; }
    catch (_) { state.deviceLatest[d.device_id] = null; }
  }));
  resetSelectionToFirstDevice();
  renderSidebar();
  renderPicker();
  renderSelectionStrip();
  renderActiveDeviceField();
  if (state.activeDeviceId) await loadData();
  else render();
}

init();
