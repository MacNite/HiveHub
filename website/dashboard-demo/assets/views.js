// View renderers, one per sidebar data group. Each group exposes
// { id, label, icon, render(root, state) }. `state` carries the loaded data and
// the admin action callbacks (see app.js buildState()).

import { el, fmt, fmtInt, isNum, relAge, latestOf, sevClass, DASH } from "./format.js";
import { drawLineChart, seriesFrom, PALETTE } from "./charts.js";

// ── chart manager: views register charts; app.js redraws them after mount ────
let activeCharts = [];
export function clearCharts() { activeCharts = []; }
export function drawCharts() {
  for (const c of activeCharts) drawLineChart(c.canvas, c.series, c.opts);
}

function chartCard(title, sub, series, opts = {}) {
  const canvas = el("canvas");
  const wrap = el("div", { class: "chart-wrap" }, canvas);
  const legend = el("div", { class: "chart-legend" });
  for (const s of series) {
    legend.append(el("span", { class: "lg" },
      el("span", { class: "swatch", style: `background:${s.color}` }), s.label));
  }
  activeCharts.push({ canvas, series, opts });
  return el("div", { class: "card chart-card" },
    el("h2", {}, title),
    sub ? el("p", { class: "card-sub" }, sub) : null,
    series.length ? legend : null,
    wrap);
}

// ── small builders ───────────────────────────────────────────────────────────
function metricCard(label, value, unit, sub) {
  return el("div", { class: "card" },
    el("div", { class: "metric" },
      el("span", { class: "label" }, label),
      el("span", { class: "value" }, value, unit ? el("span", { class: "unit" }, " " + unit) : null),
      sub ? el("span", { class: "sub" }, sub) : null));
}

function rowsCard(title, rows) {
  return el("div", { class: "card" },
    title ? el("h2", {}, title) : null,
    el("div", { class: "rows" },
      rows.map(([k, v]) => el("div", { class: "row" },
        el("span", { class: "k" }, k), el("span", { class: "v" }, v)))));
}

function viewHead(title, desc) {
  return el("div", { class: "view-head" }, el("h1", {}, title), desc ? el("p", {}, desc) : null);
}

function hiveLabel(state, n) {
  const ch = state.device?.channels || {};
  return (n === 1 ? ch.scale_1 : ch.scale_2) || `Hive ${n}`;
}

// hives shown given the top-bar hive selector ("all" | "1" | "2")
function selectedHives(state) {
  return state.hive === "all" ? [1, 2] : [Number(state.hive)];
}

// latest non-null among a list of candidate keys (first match wins)
function latestCoalesce(measurements, keys) {
  for (const m of measurements) {
    for (const k of keys) if (m[k] != null) return m[k];
  }
  return null;
}

function seriesCoalesce(measurements, keys, label, color) {
  const merged = measurements.map((m) => {
    const copy = { measured_at: m.measured_at };
    for (const k of keys) if (m[k] != null) { copy._v = m[k]; break; }
    return copy;
  });
  return seriesFrom(merged, "_v", label, color);
}

// weight delta between the latest reading and the one ~hours ago
function changeOver(measurements, key, hours) {
  if (!measurements.length) return null;
  const newest = latestOf(measurements, key);
  if (!isNum(newest)) return null;
  const cutoff = Date.now() - hours * 3600000;
  for (const m of measurements) {
    if (m[key] == null) continue;
    if (new Date(m.measured_at).getTime() <= cutoff) return newest - m[key];
  }
  return null;
}

function signed(v, digits, unit) {
  if (!isNum(v)) return DASH;
  return (v >= 0 ? "+" : "") + fmt(v, digits, unit ? " " + unit : "");
}

// ── OVERVIEW ─────────────────────────────────────────────────────────────────
function renderOverview(root, state) {
  const m = state.latest || {};
  const W = "scale_1_weight_kg_compensated", W2 = "scale_2_weight_kg_compensated";
  const totalWeight = (isNum(m[W]) ? m[W] : 0) + (isNum(m[W2]) ? m[W2] : 0);
  const anyWeight = isNum(m[W]) || isNum(m[W2]);
  const w24 = changeOver(state.measurements, W, 24);

  const ins = state.insights;
  const sev = ins?.highest_severity;
  const sevBadge = el("span", { class: `badge ${sevClass(sev)}` },
    el("span", { class: `dot ${sevClass(sev)}` }), sev ? sev : "OK");

  const cards = [
    metricCard("Weight", anyWeight ? fmt(totalWeight, 2) : DASH, "kg",
      w24 != null ? `24h ${signed(w24, 2, "kg")}` : "Total of active scales"),
    metricCard("Hive temperature", fmt(m.hive_1_temp_c ?? m.hive_2_temp_c, 1), "°C",
      isNum(m.ambient_temp_c) ? `Ambient ${fmt(m.ambient_temp_c, 1)} °C` : "Brood zone"),
    metricCard("Humidity", fmt(m.ambient_humidity_percent, 1), "%",
      isNum(m.hive_1_humidity_percent) ? `In-hive ${fmt(m.hive_1_humidity_percent, 0)} %` : "Ambient"),
    metricCard("Battery", isNum(m.battery_soc_percent) ? fmt(m.battery_soc_percent, 0) : DASH, "%",
      isNum(m.battery_voltage) ? `${fmt(m.battery_voltage, 2)} V` : "State of charge"),
    metricCard("Signal", fmt(m.rssi_dbm, 0), "dBm",
      m.network_transport ? String(m.network_transport) : "Radio"),
  ];

  const statusCard = el("div", { class: "card" },
    el("div", { class: "spread" },
      el("h2", {}, "Status"),
      el("span", { class: `badge ${m.calibration_mode ? "warn" : "good"}` },
        m.calibration_mode ? "Calibration mode" : "Live")),
    el("p", { class: "card-sub" },
      ins ? `${ins.alert_count || 0} active insight${(ins.alert_count || 0) === 1 ? "" : "s"}` : ""),
    el("div", { class: "rows" },
      el("div", { class: "row" }, el("span", { class: "k" }, "Highest severity"), el("span", { class: "v" }, sevBadge)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Last reading"), el("span", { class: "v" }, relAge(m.measured_at))),
      el("div", { class: "row" }, el("span", { class: "k" }, "Firmware"), el("span", { class: "v" }, m.firmware_version || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Boot count"), el("span", { class: "v" }, fmtInt(m.boot_count)))));

  root.append(
    viewHead("Overview", `${state.device?.display_name || state.device?.device_id} — last seen ${relAge(state.device?.last_seen_at)}`),
    el("div", { class: "grid" }, ...cards),
    el("div", { class: "grid wide", style: "margin-top:1rem" },
      statusCard,
      highestAlertCard(ins),
      chartCard("Weight trend", "Compensated mass over the selected range",
        [seriesFrom(state.measurements, W, hiveLabel(state, 1), PALETTE[0]),
         seriesFrom(state.measurements, W2, hiveLabel(state, 2), PALETTE[1])], { unit: "kg", yDigits: 1 })));
}

function highestAlertCard(ins) {
  const a = ins?.highest_alert;
  if (!a) {
    return el("div", { class: "card" }, el("h2", {}, "Insights"),
      el("p", { class: "card-sub" }, "No active alerts"),
      el("p", { class: "muted-text" }, "The colony looks stable over the last 14 days."));
  }
  return el("div", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Top insight"),
      el("span", { class: `badge ${sevClass(a.severity)}` }, a.severity || "")),
    el("h3", { style: "margin:.4rem 0 .2rem" }, a.title || a.code || "Alert"),
    el("p", { class: "muted-text" }, a.message || a.description || ""));
}

// ── generic time-series view ─────────────────────────────────────────────────
function tsView(title, desc, state, { cards = [], charts = [] }) {
  const root = el("div", {});
  root.append(viewHead(title, desc));
  if (cards.length) root.append(el("div", { class: "grid" }, ...cards));
  if (charts.length) root.append(el("div", { class: "grid wide", style: "margin-top:1rem" }, ...charts));
  return root;
}

function renderTemperature(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const cards = hives.map((n) =>
    metricCard(`${hiveLabel(state, n)} temp`, fmt(m[`hive_${n}_temp_c`], 1), "°C",
      isNum(m[`hive_${n}_humidity_percent`]) ? `Humidity ${fmt(m[`hive_${n}_humidity_percent`], 0)} %` : "In-hive"));
  cards.push(metricCard("Ambient", fmt(m.ambient_temp_c, 1), "°C", "Outside the hive"));

  const series = hives.map((n, i) =>
    seriesFrom(state.measurements, `hive_${n}_temp_c`, `${hiveLabel(state, n)}`, PALETTE[i]));
  series.push(seriesFrom(state.measurements, "ambient_temp_c", "Ambient", PALETTE[2]));

  root.append(tsView("Temperature", "Inside and ambient temperature", state,
    { cards, charts: [chartCard("Temperature", null, series, { unit: "°C", yDigits: 1 })] }));
}

function renderWeight(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const cards = hives.map((n) => {
    const key = `scale_${n}_weight_kg_compensated`;
    const c24 = changeOver(state.measurements, key, 24);
    return metricCard(`${hiveLabel(state, n)} weight`, fmt(m[key], 2), "kg",
      c24 != null ? `24h ${signed(c24, 2, "kg")}` : "Compensated");
  });
  const series = hives.map((n, i) =>
    seriesFrom(state.measurements, `scale_${n}_weight_kg_compensated`, hiveLabel(state, n), PALETTE[i]));

  root.append(tsView("Weight", "Mass changes and harvest trend", state,
    { cards, charts: [chartCard("Weight", null, series, { unit: "kg", yDigits: 1 })] }));
}

function renderEnvironment(root, state) {
  const m = state.latest || {};
  const pressureKeys = ["ble_1_pressure_hpa", "ble_2_pressure_hpa", "hivescale_1_pressure_hpa", "hivescale_2_pressure_hpa"];
  const cards = [
    metricCard("Ambient humidity", fmt(m.ambient_humidity_percent, 1), "%", "Outside the hive"),
    metricCard("In-hive humidity", fmt(m.hive_1_humidity_percent ?? m.hive_2_humidity_percent, 1), "%", "Brood area"),
    metricCard("Pressure", fmt(latestCoalesce([m], pressureKeys), 0), "hPa", "Barometric"),
  ];
  const charts = [
    chartCard("Humidity", "Ambient and in-hive relative humidity",
      [seriesFrom(state.measurements, "ambient_humidity_percent", "Ambient", PALETTE[2]),
       seriesFrom(state.measurements, "hive_1_humidity_percent", hiveLabel(state, 1), PALETTE[0]),
       seriesFrom(state.measurements, "hive_2_humidity_percent", hiveLabel(state, 2), PALETTE[1])],
      { unit: "%", yDigits: 0 }),
    chartCard("Pressure", "Barometric pressure around the hive",
      [seriesCoalesce(state.measurements, pressureKeys, "Pressure", PALETTE[3])], { unit: "hPa", yDigits: 0 }),
  ];
  root.append(tsView("Environment", "Humidity and air pressure", state, { cards, charts }));
}

function renderAudio(root, state) {
  const m = state.latest || {};
  const cards = [
    metricCard("Left RMS", fmt(m.mic_left_rms_dbfs, 1), "dBFS", isNum(m.mic_left_peak_dbfs) ? `Peak ${fmt(m.mic_left_peak_dbfs, 1)}` : "Sound level"),
    metricCard("Right RMS", fmt(m.mic_right_rms_dbfs, 1), "dBFS", isNum(m.mic_right_peak_dbfs) ? `Peak ${fmt(m.mic_right_peak_dbfs, 1)}` : "Sound level"),
    metricCard("Sample rate", fmtInt(m.mic_sample_rate_hz), "Hz", isNum(m.mic_sample_frames) ? `${fmtInt(m.mic_sample_frames)} frames` : "Microphone"),
  ];
  const charts = [
    chartCard("Sound level (RMS)", "Per-channel microphone RMS",
      [seriesFrom(state.measurements, "mic_left_rms_dbfs", "Left", PALETTE[0]),
       seriesFrom(state.measurements, "mic_right_rms_dbfs", "Right", PALETTE[1])], { unit: "dBFS", yDigits: 0 }),
    chartCard("Peak level", "Per-channel microphone peak",
      [seriesFrom(state.measurements, "mic_left_peak_dbfs", "Left", PALETTE[0]),
       seriesFrom(state.measurements, "mic_right_peak_dbfs", "Right", PALETTE[1])], { unit: "dBFS", yDigits: 0 }),
  ];
  root.append(tsView("Audio", "Hive sound levels", state, { cards, charts }));
}

const BANDS = [
  ["sub_bass", "Sub-bass"], ["hum", "Hum"], ["piping", "Piping"],
  ["stress", "Stress"], ["high", "High"],
];

function barChart(title, sub, items) {
  // items: [{label, value(dBFS, negative)}]; map -90..0 dBFS to 0..1 height.
  const cols = items.map((it) => {
    const frac = isNum(it.value) ? Math.max(0, Math.min(1, (it.value + 90) / 90)) : 0;
    return el("div", { class: "bar-col" },
      el("span", { class: "bar-val" }, isNum(it.value) ? fmt(it.value, 0) : DASH),
      el("div", { class: "bar", style: `height:${(frac * 100).toFixed(1)}%` }),
      el("span", { class: "bar-label" }, it.label));
  });
  return el("div", { class: "card chart-card" },
    el("h2", {}, title), sub ? el("p", { class: "card-sub" }, sub) : null,
    el("div", { class: "bars" }, ...cols));
}

function renderFrequency(root, state) {
  const m = state.latest || {};
  const charts = [];
  for (const side of ["left", "right"]) {
    const items = BANDS.map(([k, label]) => ({ label, value: m[`mic_${side}_band_${k}_dbfs`] }));
    if (items.some((i) => isNum(i.value))) {
      charts.push(barChart(`Frequency bands — ${side}`, "Latest per-band energy (dBFS)", items));
    }
  }
  if (!charts.length) {
    charts.push(el("div", { class: "card" }, el("p", { class: "muted-text" }, "No frequency-band data reported by this device.")));
  }
  root.append(tsView("Frequency bar", "FFT energy by acoustic band", state, { charts }));
}

function renderBattery(root, state) {
  const m = state.latest || {};
  const cards = [
    metricCard("State of charge", fmt(m.battery_soc_percent, 0), "%", isNum(m.battery_voltage) ? `${fmt(m.battery_voltage, 2)} V` : "Battery"),
    metricCard("Solar power", fmt(m.solar_power_mw, 0), "mW", isNum(m.solar_current_ma) ? `${fmt(m.solar_current_ma, 0)} mA` : "Solar input"),
    metricCard("Solar bus", fmt(m.solar_bus_voltage_v, 2), "V", "Panel voltage"),
  ];
  if (m.battery_alert) cards.push(metricCard("Battery alert", "Active", "", "Low battery warning"));
  const charts = [
    chartCard("Battery", "State of charge and voltage",
      [seriesFrom(state.measurements, "battery_soc_percent", "SoC %", PALETTE[2]),
       seriesFrom(state.measurements, "battery_voltage", "Voltage", PALETTE[0])], { yDigits: 1 }),
    chartCard("Solar", "Solar power input",
      [seriesFrom(state.measurements, "solar_power_mw", "Power mW", PALETTE[0]),
       seriesFrom(state.measurements, "solar_current_ma", "Current mA", PALETTE[1])], { yDigits: 0 }),
  ];
  root.append(tsView("Battery & power", "Battery and solar telemetry", state, { cards, charts }));
}

function renderConnectivity(root, state) {
  const m = state.latest || {};
  const cards = [
    metricCard("Signal", fmt(m.rssi_dbm, 0), "dBm", "Wi-Fi / radio RSSI"),
    metricCard("Transport", m.network_transport || DASH, "", m.cellular_ok != null ? (m.cellular_ok ? "Cellular OK" : "Cellular down") : "Uplink"),
    metricCard("Cellular CSQ", fmtInt(m.cellular_csq), "", "GSM signal quality"),
    metricCard("Time source", m.time_source || DASH, "", m.rtc_ok != null ? (m.rtc_ok ? "RTC OK" : "RTC fault") : "Clock"),
  ];
  const charts = [
    chartCard("Signal strength", "RSSI over the selected range",
      [seriesFrom(state.measurements, "rssi_dbm", "RSSI", PALETTE[1])], { unit: "dBm", yDigits: 0 }),
  ];
  root.append(tsView("Connectivity", "Network and timing health", state, { cards, charts }));
}

function renderCounter(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const cards = [];
  for (const n of hives) {
    cards.push(metricCard(`${hiveLabel(state, n)} in`, fmtInt(m[`bee_counter_${n}_total_in`]), "", "Total entrances"));
    cards.push(metricCard(`${hiveLabel(state, n)} out`, fmtInt(m[`bee_counter_${n}_total_out`]), "", "Total exits"));
  }
  const charts = [];
  for (const n of hives) {
    const inS = seriesFrom(state.measurements, `bee_counter_${n}_interval_in`, `${hiveLabel(state, n)} in`, PALETTE[2]);
    const outS = seriesFrom(state.measurements, `bee_counter_${n}_interval_out`, `${hiveLabel(state, n)} out`, PALETTE[3]);
    if (inS.points.length || outS.points.length) {
      charts.push(chartCard(`Traffic — ${hiveLabel(state, n)}`, "Bees in/out per interval", [inS, outS], { yDigits: 0 }));
    }
  }
  if (!charts.length) {
    charts.push(el("div", { class: "card" }, el("p", { class: "muted-text" }, "No bee-counter data reported by this device.")));
  }
  root.append(tsView("Counter", "Entrance bee traffic", state, { cards, charts }));
}

function renderInsights(root, state) {
  const ins = state.insights;
  const node = el("div", {});
  node.append(viewHead("Insights", "Rule-based colony alerts (14-day lookback)"));
  if (!ins) { node.append(el("div", { class: "card" }, "No insight data.")); root.append(node); return; }

  const cats = ins.categories || {};
  const summaryCards = [
    metricCard("Active alerts", fmtInt(ins.alert_count), "", `Computed ${relAge(ins.computed_at)}`),
    metricCard("Highest severity", ins.highest_severity || "OK", "", "Most urgent"),
  ];
  node.append(el("div", { class: "grid" }, ...summaryCards));

  const catRows = Object.entries(cats).map(([k, v]) =>
    [k, typeof v === "object" ? JSON.stringify(v) : String(v)]);
  if (catRows.length) node.append(el("div", { style: "margin-top:1rem" }, rowsCard("Categories", catRows)));
  node.append(el("div", { style: "margin-top:1rem" }, highestAlertCard(ins)));
  root.append(node);
}

// ── DEVICE / ADMIN ───────────────────────────────────────────────────────────
function renderDevice(root, state) {
  const cfg = state.config || {};
  const fw = state.firmware || {};
  const node = el("div", {});
  node.append(viewHead("Device & admin", "Configuration, firmware and calibration"));

  // Config (read-only)
  const cfgRows = [
    ["Device ID", state.device?.device_id || DASH],
    ["Send interval", isNum(cfg.send_interval_seconds) ? `${cfg.send_interval_seconds} s` : DASH],
    ["Config version", cfg.config_version ?? DASH],
    ["Scale 1 offset / factor", `${cfg.scale1_offset ?? DASH} / ${cfg.scale1_factor ?? DASH}`],
    ["Scale 2 offset / factor", `${cfg.scale2_offset ?? DASH} / ${cfg.scale2_factor ?? DASH}`],
    ["Temp compensation", cfg.tempco_enabled ? "Enabled" : "Disabled"],
    ["Tempco source / ref", `${cfg.tempco_source ?? DASH} / ${isNum(cfg.tempco_ref_temp_c) ? cfg.tempco_ref_temp_c + " °C" : DASH}`],
  ];

  // Firmware panel
  const fwBadgeCls = fw.update_available ? (fw.pending_approval ? "warn" : "info") : "good";
  const fwPanel = el("div", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Firmware"),
      el("span", { class: `badge ${fwBadgeCls}` },
        fw.update_available ? (fw.pending_approval ? "Update pending approval" : "Up to date soon") : "Up to date")),
    el("div", { class: "rows" },
      el("div", { class: "row" }, el("span", { class: "k" }, "Current"), el("span", { class: "v" }, fw.current_version || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Latest"), el("span", { class: "v" }, fw.latest_version || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Approved"), el("span", { class: "v" }, fw.approved_version || DASH))));

  if (fw.update_available && fw.pending_approval) {
    const approveBtn = el("button", { class: "btn", type: "button" }, "Approve & flash latest");
    approveBtn.addEventListener("click", async () => {
      approveBtn.disabled = true;
      try { await state.actions.approveFirmware(); state.toast("Firmware approved — device will update on next check-in", "success"); state.reload(); }
      catch (e) { state.toast(e.message, "error"); approveBtn.disabled = false; }
    });
    fwPanel.append(el("div", { class: "form-actions" }, approveBtn));
  }

  // Firmware upload form
  const fileInput = el("input", { type: "file", accept: ".bin", required: true });
  const versionInput = el("input", { type: "text", placeholder: "e.g. 0.21.0", required: true });
  const targetSelect = el("select", { class: "full" },
    el("option", { value: "hivescale" }, "HiveScale (hivehub)"),
    el("option", { value: "hiveinside" }, "HiveInside"),
    el("option", { value: "beecounter" }, "BeeCounter"));
  const boardInput = el("input", { type: "text", placeholder: "optional (e.g. esp32, esp32c6)" });
  const uploadBtn = el("button", { class: "btn", type: "submit" }, "Upload firmware");

  const uploadForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Firmware .bin"), fileInput),
    el("div", { class: "form-row" }, el("label", {}, "Version"), versionInput),
    el("div", { class: "form-row" }, el("label", {}, "Target"), targetSelect),
    el("div", { class: "form-row" }, el("label", {}, "Board"), boardInput),
    el("p", { class: "note" }, "Uploads register a global release. Approve it above to flash the device."),
    el("div", { class: "form-actions" }, uploadBtn));
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("version", versionInput.value.trim());
    fd.append("target", targetSelect.value);
    if (boardInput.value.trim()) fd.append("board", boardInput.value.trim());
    uploadBtn.disabled = true;
    try { await state.actions.uploadFirmware(fd); state.toast("Firmware uploaded", "success"); state.reload(); }
    catch (err) { state.toast(err.message, "error"); }
    finally { uploadBtn.disabled = false; }
  });
  const uploadCard = el("div", { class: "card" }, el("h2", {}, "Upload firmware"), uploadForm);

  // Calibration
  const calBadge = el("span", { class: `badge ${state.latest?.calibration_mode ? "warn" : "muted"}` },
    state.latest?.calibration_mode ? "Calibration mode active" : "Normal mode");
  const startBtn = el("button", { class: "btn", type: "button" }, "Start calibration mode");
  const stopBtn = el("button", { class: "btn ghost", type: "button" }, "Stop calibration mode");
  startBtn.addEventListener("click", async () => {
    startBtn.disabled = true;
    try { await state.actions.startCalibration({}); state.toast("Calibration mode requested", "success"); }
    catch (e) { state.toast(e.message, "error"); } finally { startBtn.disabled = false; }
  });
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    try { await state.actions.stopCalibration(); state.toast("Stop calibration requested", "success"); }
    catch (e) { state.toast(e.message, "error"); } finally { stopBtn.disabled = false; }
  });
  const calCard = el("div", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Calibration"), calBadge),
    el("p", { class: "note" }, "Calibration mode samples the load cell more frequently so you can place known weights and fit a temperature coefficient."),
    el("div", { class: "form-actions" }, startBtn, stopBtn));

  // Temp-compensation fit
  const scaleSel = el("select", { class: "full" }, el("option", { value: "1" }, "Scale 1"), el("option", { value: "2" }, "Scale 2"));
  const lookbackInput = el("input", { type: "number", value: "14", min: "1", max: "90" });
  const applyChk = el("input", { type: "checkbox" });
  const fitBtn = el("button", { class: "btn", type: "submit" }, "Fit coefficient");
  const fitOut = el("p", { class: "note" });
  const fitForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Scale"), scaleSel),
    el("div", { class: "form-row" }, el("label", {}, "Lookback (days)"), lookbackInput),
    el("div", { class: "form-row" }, el("label", {}, el("span", {}, "Apply to device config "), applyChk)),
    el("div", { class: "form-actions" }, fitBtn), fitOut);
  fitForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    fitBtn.disabled = true; fitOut.textContent = "Fitting…";
    try {
      const r = await state.actions.fitTempComp({
        scale: Number(scaleSel.value),
        lookback_days: Number(lookbackInput.value) || 14,
        apply: applyChk.checked,
      });
      fitOut.textContent = r.ok
        ? `coeff ${fmt(r.coeff_kg_per_c, 5)} kg/°C, ref ${fmt(r.ref_temp_c, 1)} °C, R² ${fmt(r.r_squared, 3)}${r.applied ? " — applied" : ""}`
        : `Fit failed: ${r.reason || "insufficient data"}`;
      if (r.applied) state.reload();
    } catch (err) { fitOut.textContent = ""; state.toast(err.message, "error"); }
    finally { fitBtn.disabled = false; }
  });
  const fitCard = el("div", { class: "card" }, el("h2", {}, "Temperature compensation"),
    el("p", { class: "note" }, "Regress raw weight against temperature to derive a load-cell coefficient."), fitForm);

  node.append(
    el("div", { class: "grid wide" }, rowsCard("Configuration", cfgRows), fwPanel),
    el("div", { class: "grid wide", style: "margin-top:1rem" }, uploadCard, calCard, fitCard));
  root.append(node);
}

// ── registry ─────────────────────────────────────────────────────────────────
// render(root, state): some replace `root`, some append — app.js passes a fresh
// container and reads back content.innerHTML via the returned/!replaced node.
export const GROUPS = [
  { id: "overview", label: "Overview", icon: "🗂", render: renderOverview, append: true },
  { id: "temperature", label: "Temperature", icon: "🌡", render: renderTemperature },
  { id: "weight", label: "Weight", icon: "⚖️", render: renderWeight },
  { id: "environment", label: "Environment", icon: "💧", render: renderEnvironment },
  { id: "audio", label: "Audio", icon: "🔊", render: renderAudio },
  { id: "frequency", label: "Frequency bar", icon: "📊", render: renderFrequency },
  { id: "battery", label: "Battery & power", icon: "🔋", render: renderBattery },
  { id: "connectivity", label: "Connectivity", icon: "📶", render: renderConnectivity },
  { id: "counter", label: "Counter", icon: "🐝", render: renderCounter },
  { id: "insights", label: "Insights", icon: "💡", render: renderInsights },
  { id: "device", label: "Device & admin", icon: "⚙️", render: renderDevice },
];
