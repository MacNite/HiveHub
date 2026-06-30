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

// Cycle the (6-colour) palette so up to MAX_HIVES (18) series stay distinct.
function paletteColor(i) {
  return PALETTE[((i % PALETTE.length) + PALETTE.length) % PALETTE.length];
}

// Hive indices a device exposes. A single ESP32 now carries up to 18 hives, so
// the set is derived live from the latest reading's hives[] array, any custom
// channel names, and the legacy flat scale_N keys. Falls back to the classic
// two-hive shape only when nothing else is known.
export function availableHives(state) {
  const set = new Set();
  for (const h of state.latest?.hives || []) {
    if (h && h.index != null) set.add(Number(h.index));
  }
  const names = state.channels?.names || state.device?.channels?.names || {};
  for (const k of Object.keys(names)) { const n = Number(k); if (n) set.add(n); }
  if (state.latest) {
    for (const key of Object.keys(state.latest)) {
      const mm = /^scale_(\d+)_weight_kg(?:_compensated)?$/.exec(key);
      if (mm && state.latest[key] != null) set.add(Number(mm[1]));
    }
  }
  if (!set.size) { set.add(1); set.add(2); }
  return [...set].filter((n) => n >= 1).sort((a, b) => a - b);
}

// Best display name for hive n: a custom channel name first, then the firmware-
// reported name from the hives[] array, then the legacy single-channel fields,
// then a generic "Hive n".
export function hiveLabel(state, n) {
  const names = state.channels?.names || {};
  if (names[n] != null && names[n] !== "") return names[n];
  const hv = (state.latest?.hives || []).find((h) => Number(h?.index) === Number(n));
  if (hv && hv.name) return hv.name;
  const c = state.channels || {};
  const legacy = n === 1 ? c.scale_1_display_name : n === 2 ? c.scale_2_display_name : null;
  const dev = state.device?.channels || {};
  const fromDevice = (dev.names && dev.names[n]) || (n === 1 ? dev.scale_1 : n === 2 ? dev.scale_2 : null);
  return legacy || fromDevice || `Hive ${n}`;
}

// Weight key for hive n: the temperature-compensated column when present (hives
// 1–2), otherwise the raw per-hive weight synthesized for hives 3–18.
function weightKey(m, n) {
  const comp = `scale_${n}_weight_kg_compensated`;
  return m && m[comp] != null ? comp : `scale_${n}_weight_kg`;
}

// hives shown given the top-bar hive selector ("all" | a specific index)
function selectedHives(state) {
  return state.hive === "all" ? availableHives(state) : [Number(state.hive)];
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
  const hives = availableHives(state);
  let totalWeight = 0, anyWeight = false;
  for (const n of hives) {
    const v = m[weightKey(m, n)];
    if (isNum(v)) { totalWeight += v; anyWeight = true; }
  }
  const w24 = changeOver(state.measurements, weightKey(m, hives[0]), 24);

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
        hives.map((n, i) =>
          seriesFrom(state.measurements, weightKey(m, n), hiveLabel(state, n), paletteColor(i))),
        { unit: "kg", yDigits: 1 })));
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
    seriesFrom(state.measurements, `hive_${n}_temp_c`, `${hiveLabel(state, n)}`, paletteColor(i)));
  series.push(seriesFrom(state.measurements, "ambient_temp_c", "Ambient", paletteColor(hives.length)));

  root.append(tsView("Temperature", "Inside and ambient temperature", state,
    { cards, charts: [chartCard("Temperature", null, series, { unit: "°C", yDigits: 1 })] }));
}

function renderWeight(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const cards = hives.map((n) => {
    const key = weightKey(m, n);
    const c24 = changeOver(state.measurements, key, 24);
    return metricCard(`${hiveLabel(state, n)} weight`, fmt(m[key], 2), "kg",
      c24 != null ? `24h ${signed(c24, 2, "kg")}` : "Compensated");
  });
  const series = hives.map((n, i) =>
    seriesFrom(state.measurements, weightKey(m, n), hiveLabel(state, n), paletteColor(i)));

  root.append(tsView("Weight", "Mass changes and harvest trend", state,
    { cards, charts: [chartCard("Weight", null, series, { unit: "kg", yDigits: 1 })] }));
}

function renderEnvironment(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const pressureKeys = ["ble_1_pressure_hpa", "ble_2_pressure_hpa", "hivescale_1_pressure_hpa", "hivescale_2_pressure_hpa"];
  const inHiveHumidity = latestCoalesce([m], hives.map((n) => `hive_${n}_humidity_percent`));
  const cards = [
    metricCard("Ambient humidity", fmt(m.ambient_humidity_percent, 1), "%", "Outside the hive"),
    metricCard("In-hive humidity", fmt(inHiveHumidity, 1), "%", "Brood area"),
    metricCard("Pressure", fmt(latestCoalesce([m], pressureKeys), 0), "hPa", "Barometric"),
  ];
  const charts = [
    chartCard("Humidity", "Ambient and in-hive relative humidity",
      [seriesFrom(state.measurements, "ambient_humidity_percent", "Ambient", paletteColor(hives.length)),
       ...hives.map((n, i) =>
         seriesFrom(state.measurements, `hive_${n}_humidity_percent`, hiveLabel(state, n), paletteColor(i)))],
      { unit: "%", yDigits: 0 }),
    chartCard("Pressure", "Barometric pressure around the hive",
      [seriesCoalesce(state.measurements, pressureKeys, "Pressure", PALETTE[3])], { unit: "hPa", yDigits: 0 }),
  ];
  root.append(tsView("Environment", "Humidity and air pressure", state, { cards, charts }));
}

// The stereo microphone's left / right channels map to hive 1 / hive 2, so the
// audio + frequency views label them with the hive names like every other view.
function micSide(n) {
  return n === 1 ? "left" : "right";
}

function renderAudio(root, state) {
  const m = state.latest || {};
  const hives = selectedHives(state);
  const cards = hives.map((n) => {
    const s = micSide(n);
    return metricCard(`${hiveLabel(state, n)} RMS`, fmt(m[`mic_${s}_rms_dbfs`], 1), "dBFS",
      isNum(m[`mic_${s}_peak_dbfs`]) ? `Peak ${fmt(m[`mic_${s}_peak_dbfs`], 1)}` : "Sound level");
  });
  cards.push(metricCard("Sample rate", fmtInt(m.mic_sample_rate_hz), "Hz",
    isNum(m.mic_sample_frames) ? `${fmtInt(m.mic_sample_frames)} frames` : "Microphone"));

  const rms = hives.map((n, i) => seriesFrom(state.measurements, `mic_${micSide(n)}_rms_dbfs`, hiveLabel(state, n), PALETTE[i]));
  const peak = hives.map((n, i) => seriesFrom(state.measurements, `mic_${micSide(n)}_peak_dbfs`, hiveLabel(state, n), PALETTE[i]));
  const charts = [
    chartCard("Sound level (RMS)", "Per-hive microphone RMS", rms, { unit: "dBFS", yDigits: 0 }),
    chartCard("Peak level", "Per-hive microphone peak", peak, { unit: "dBFS", yDigits: 0 }),
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
  const hives = selectedHives(state);
  const charts = [];
  for (const n of hives) {
    const items = BANDS.map(([k, label]) => ({ label, value: m[`mic_${micSide(n)}_band_${k}_dbfs`] }));
    if (items.some((i) => isNum(i.value))) {
      charts.push(barChart(`Frequency bands — ${hiveLabel(state, n)}`, "Latest per-band FFT energy (dBFS)", items));
    }
  }
  if (!charts.length) {
    charts.push(el("div", { class: "card" }, el("p", { class: "muted-text" }, "No frequency-band data reported by this device.")));
  }
  root.append(tsView("Frequency bands", "FFT energy by acoustic band", state, { charts }));
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

  // Editable configuration form
  const cfgInputs = {};
  const numRow = (label, key, isInt) => {
    const input = el("input", {
      type: "number", step: isInt ? "1" : "any",
      value: cfg[key] != null ? String(cfg[key]) : "",
    });
    cfgInputs[key] = { input, int: !!isInt };
    return el("div", { class: "form-row" }, el("label", {}, label), input);
  };
  const tcEnabled = el("input", { type: "checkbox" });
  tcEnabled.checked = !!cfg.tempco_enabled;
  const tcSource = el("select", { class: "full" },
    ...["ambient", "hive_1", "hive_2"].map((v) =>
      el("option", { value: v, selected: cfg.tempco_source === v ? true : null }, v)));
  const cfgSaveBtn = el("button", { class: "btn", type: "submit" }, "Save configuration");

  const cfgForm = el("form", {},
    el("div", { class: "rows" },
      el("div", { class: "row" }, el("span", { class: "k" }, "Device ID"), el("span", { class: "v" }, state.device?.device_id || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Config version"), el("span", { class: "v" }, cfg.config_version ?? DASH))),
    numRow("Send interval (s)", "send_interval_seconds", true),
    numRow("Scale 1 offset", "scale1_offset", true),
    numRow("Scale 1 factor", "scale1_factor"),
    numRow("Scale 2 offset", "scale2_offset", true),
    numRow("Scale 2 factor", "scale2_factor"),
    el("div", { class: "form-row" }, el("label", {}, el("span", {}, "Enable temperature compensation "), tcEnabled)),
    el("div", { class: "form-row" }, el("label", {}, "Tempco source"), tcSource),
    numRow("Tempco ref temp (°C)", "tempco_ref_temp_c"),
    numRow("Scale 1 tempco (kg/°C)", "scale1_tempco_kg_per_c"),
    numRow("Scale 2 tempco (kg/°C)", "scale2_tempco_kg_per_c"),
    el("p", { class: "note" }, "Saving bumps the config version; the device applies it on its next check-in."),
    el("div", { class: "form-actions" }, cfgSaveBtn));

  cfgForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const patch = {};
    for (const [key, { input, int }] of Object.entries(cfgInputs)) {
      const raw = input.value.trim();
      if (raw === "") continue;
      const v = int ? parseInt(raw, 10) : parseFloat(raw);
      if (!Number.isFinite(v) || v === cfg[key]) continue;
      patch[key] = v;
    }
    if (tcEnabled.checked !== !!cfg.tempco_enabled) patch.tempco_enabled = tcEnabled.checked;
    if (tcSource.value !== cfg.tempco_source) patch.tempco_source = tcSource.value;
    if (!Object.keys(patch).length) { state.toast("No changes to save"); return; }
    cfgSaveBtn.disabled = true;
    try { await state.actions.updateConfig(patch); state.toast("Configuration saved", "success"); state.reload(); }
    catch (err) { state.toast(err.message, "error"); cfgSaveBtn.disabled = false; }
  });
  const configCard = el("div", { class: "card" }, el("h2", {}, "Configuration"), cfgForm);

  // Hive (scale-channel) names — one input per hive the device reports (up to 18).
  const chData = state.channels || {};
  const chNames = chData.names || {};
  const curName = (n) =>
    chNames[n] ??
    (n === 1 ? chData.scale_1_display_name : n === 2 ? chData.scale_2_display_name : null) ??
    "";
  const chInputs = availableHives(state).map((n) => {
    const fw = (state.latest?.hives || []).find((h) => Number(h?.index) === n);
    const initial = curName(n);
    return {
      n,
      initial,
      input: el("input", { type: "text", value: initial, placeholder: (fw && fw.name) || `Hive ${n}` }),
    };
  });
  const chBtn = el("button", { class: "btn", type: "submit" }, "Save names");
  const chForm = el("form", {},
    ...chInputs.map(({ n, input }) =>
      el("div", { class: "form-row" }, el("label", {}, `Hive ${n} name`), input)),
    el("p", { class: "note" }, "Shown as the hive labels across every chart and card."),
    el("div", { class: "form-actions" }, chBtn));
  chForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const names = {};
    for (const { n, input, initial } of chInputs) {
      if (input.value !== initial) names[String(n)] = input.value;
    }
    if (!Object.keys(names).length) { state.toast("No changes to save"); return; }
    chBtn.disabled = true;
    try { await state.actions.updateChannels({ names }); state.toast("Hive names saved", "success"); state.reload(); }
    catch (err) { state.toast(err.message, "error"); chBtn.disabled = false; }
  });
  const channelsCard = el("div", { class: "card" }, el("h2", {}, "Hive names"), chForm);

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
    el("p", { class: "note" }, "Uploading registers a new firmware release for this target. To install it, approve the update in the Firmware panel above — the device flashes on its next check-in."),
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

  // Dashboard account cards: change-your-password for everyone, plus user
  // management for admins.
  const accountCards = [accountCard(state)];
  if (state.authUser?.role === "admin") accountCards.push(usersCard(state));

  node.append(
    el("div", { class: "grid wide" }, configCard, channelsCard),
    el("div", { class: "grid wide", style: "margin-top:1rem" }, fwPanel, uploadCard),
    el("div", { class: "grid wide", style: "margin-top:1rem" }, calCard, fitCard),
    el("div", { class: "grid wide", style: "margin-top:1rem" }, ...accountCards));
  root.append(node);
}

// ── dashboard account cards ──────────────────────────────────────────────────
// "Your account": let the logged-in user change their own password.
function accountCard(state) {
  const u = state.authUser || {};
  const curPw = el("input", { type: "password", autocomplete: "current-password" });
  const newPw = el("input", { type: "password", autocomplete: "new-password" });
  const newPw2 = el("input", { type: "password", autocomplete: "new-password" });
  const btn = el("button", { class: "btn", type: "submit" }, "Change password");
  const form = el("form", {},
    el("div", { class: "rows" },
      el("div", { class: "row" }, el("span", { class: "k" }, "Signed in as"), el("span", { class: "v" }, u.username || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Role"), el("span", { class: "v" }, u.role || DASH))),
    el("div", { class: "form-row" }, el("label", {}, "Current password"), curPw),
    el("div", { class: "form-row" }, el("label", {}, "New password"), newPw),
    el("div", { class: "form-row" }, el("label", {}, "Confirm new password"), newPw2),
    el("div", { class: "form-actions" }, btn));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (newPw.value.length < 8) { state.toast("New password must be at least 8 characters", "error"); return; }
    if (newPw.value !== newPw2.value) { state.toast("New passwords do not match", "error"); return; }
    btn.disabled = true;
    try {
      await state.actions.changePassword(curPw.value, newPw.value);
      state.toast("Password changed", "success");
      curPw.value = newPw.value = newPw2.value = "";
    } catch (err) { state.toast(err.message, "error"); }
    finally { btn.disabled = false; }
  });
  return el("div", { class: "card" }, el("h2", {}, "Your account"), form);
}

// "Dashboard users" (admin only): list / add / remove accounts. The list is
// fetched lazily because views render synchronously.
function usersCard(state) {
  const listEl = el("div", { class: "rows" }, el("p", { class: "muted-text" }, "Loading users…"));

  const refresh = async () => {
    try {
      const users = await state.actions.listUsers();
      listEl.replaceChildren(...users.map((usr) => {
        const del = el("button", { class: "btn small ghost", type: "button" }, "Remove");
        del.addEventListener("click", async () => {
          if (usr.username === state.authUser?.username) { state.toast("You cannot remove your own account", "error"); return; }
          del.disabled = true;
          try { await state.actions.deleteUser(usr.id); state.toast("User removed", "success"); refresh(); }
          catch (err) { state.toast(err.message, "error"); del.disabled = false; }
        });
        return el("div", { class: "row" },
          el("span", { class: "k" }, `${usr.username} · ${usr.role}`),
          el("span", { class: "v" }, del));
      }));
      if (!users.length) listEl.replaceChildren(el("p", { class: "muted-text" }, "No users yet."));
    } catch (err) {
      listEl.replaceChildren(el("p", { class: "auth-error" }, err.message || "Failed to load users"));
    }
  };

  const nu = el("input", { type: "text", autocomplete: "off", placeholder: "username" });
  const np = el("input", { type: "password", autocomplete: "new-password", placeholder: "password (min 8)" });
  const nr = el("select", { class: "full" },
    el("option", { value: "viewer" }, "Viewer (read-only)"),
    el("option", { value: "admin" }, "Admin (full control)"));
  const addBtn = el("button", { class: "btn", type: "submit" }, "Add user");
  const addForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Username"), nu),
    el("div", { class: "form-row" }, el("label", {}, "Password"), np),
    el("div", { class: "form-row" }, el("label", {}, "Role"), nr),
    el("div", { class: "form-actions" }, addBtn));
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (np.value.length < 8) { state.toast("Password must be at least 8 characters", "error"); return; }
    if (nu.value.trim().length < 3) { state.toast("Username must be at least 3 characters", "error"); return; }
    addBtn.disabled = true;
    try {
      await state.actions.createUser(nu.value.trim(), np.value, nr.value);
      state.toast("User created", "success");
      nu.value = np.value = "";
      refresh();
    } catch (err) { state.toast(err.message, "error"); }
    finally { addBtn.disabled = false; }
  });

  refresh();
  return el("div", { class: "card" }, el("h2", {}, "Dashboard users"),
    el("p", { class: "note" }, "Viewers can see all data; admins can also change configuration, calibration, firmware and users."),
    listEl,
    el("h3", { style: "margin:.8rem 0 .2rem" }, "Add a user"),
    addForm);
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
  { id: "frequency", label: "Frequency bands", icon: "📊", render: renderFrequency },
  { id: "battery", label: "Battery & power", icon: "🔋", render: renderBattery },
  { id: "connectivity", label: "Connectivity", icon: "📶", render: renderConnectivity },
  { id: "counter", label: "Counter", icon: "🐝", render: renderCounter },
  { id: "insights", label: "Insights", icon: "💡", render: renderInsights },
  { id: "device", label: "Device & admin", icon: "⚙️", render: renderDevice },
];
