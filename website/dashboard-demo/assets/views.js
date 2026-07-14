// View renderers, one per sidebar data group. Each group exposes
// { id, label, icon, render(root, state) }. `state` carries the loaded data and
// the admin action callbacks (see app.js buildState()).

import { el, fmt, fmtInt, isNum, relAge, latestOf, sevClass, fmtDateTime, DASH } from "./format.js";
import { drawLineChart, drawSpectrumChart, seriesFrom, dailyMaxSeries, valueAt, PALETTE, withAlpha } from "./charts.js";

// Pull the firmware version out of a build artifact's filename so the upload
// form can pre-fill the Version field. rename_firmware.py names artifacts
// "<prefix>_<board>_<version>.bin" (e.g. hivehub_esp32_0.21.0.bin,
// hivehub_esp32-c6_0.9.2.bin), so the version is the trailing dotted token.
// Requiring a dot means the board tokens (esp32 / c6) are never mistaken for a
// version; returns "" when no version-looking token is present.
function versionFromFilename(name) {
  const base = (name || "").replace(/\.[^.]*$/, "");
  const m = base.match(/(\d+\.\d+(?:\.\d+)*(?:[-_][0-9A-Za-z.]+)?)$/);
  return m ? m[1] : "";
}

// ── chart manager: views register charts; app.js redraws them after mount ────
let activeCharts = [];

// Per-chart user preferences: series hidden via legend clicks and a manually
// pinned y-range (click a y-axis end label to edit it). Keyed by chart title so
// they survive the constant re-renders (view switches, the 60s auto-refresh,
// selection changes); entries for charts no longer on screen are harmless.
const chartPrefs = new Map();
function prefsFor(title) {
  let p = chartPrefs.get(title);
  if (!p) { p = { hidden: new Set(), yMin: null, yMax: null }; chartPrefs.set(title, p); }
  return p;
}

// Selected/hovered timestamp (epoch millis), shared across every chart on the
// current view so scrubbing one diagram lines up the readout on all of them.
// Persists across re-renders (view switches, auto-refresh) until the mouse
// leaves a chart, so a slid position survives a data reload.
let cursorT = null;

// Hovered category index per spectrum "group". A group is the set of charts
// that share the same x-axis categories (all mic 5-band charts form one group;
// all HiveHeart 16-range charts form another). Keying by group means hovering a
// mic band moves the cursor only on the other mic charts, never onto the
// HiveHeart spectrum whose categories (frequency ranges) are unrelated. Persists
// across re-renders the same way cursorT does.
let cursorBandByGroup = Object.create(null);

// Expected send cadence for the active device (ms). Charts use this only as a
// fallback expected spacing for series too short to infer their own cadence;
// the real gap test in drawLineChart is relative to each series' median point
// spacing, so it survives server-side down-sampling and SD backfill.
let sendIntervalMs = null;
const DEFAULT_SEND_INTERVAL_S = 600;

// Called by app.js before rendering a view so charts know the active device's
// send interval (see the "Send interval (s)" field on the device/admin page).
export function configureCharts(state) {
  const raw = Number(state?.config?.send_interval_seconds);
  const seconds = Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_SEND_INTERVAL_S;
  sendIntervalMs = seconds * 1000;
}

export function clearCharts() { activeCharts = []; }
export function drawCharts() {
  for (const c of activeCharts) {
    const p = c.prefs;
    if (c.kind === "spectrum") {
      const cb = cursorBandByGroup[c.group];
      drawSpectrumChart(c.canvas, c.categories, c.snapshots, {
        ...c.opts,
        cursorIndex: cb == null ? null : cb,
        bandStats: c.bandStats,
        // A user-pinned range wins over a chart's fixed scale (e.g. HiveHeart 0–15).
        yMin: p.yMin ?? c.opts.yMin,
        yMax: p.yMax ?? c.opts.yMax,
        hideOlder: p.hidden.has("older"),
        hideLatest: p.hidden.has("latest"),
      });
      updateSpectrumReadout(c);
      updateChartTools(c);
      continue;
    }
    drawLineChart(c.canvas, c.series.filter((s) => !p.hidden.has(s.label)),
      { ...c.opts, cursorT, yMin: p.yMin, yMax: p.yMax });
    updateReadout(c);
    updateChartTools(c);
  }
}

function updateReadout(c) {
  for (const { valueEl, series, item } of c.legendItems) {
    const off = c.prefs.hidden.has(series.label);
    item.classList.toggle("off", off);
    if (off || cursorT == null) { valueEl.textContent = ""; continue; }
    const p = valueAt(series.points, cursorT);
    valueEl.textContent = p ? `: ${fmt(p.y, c.opts.yDigits ?? 1, c.opts.unit ? " " + c.opts.unit : "")}` : ": " + DASH;
  }
  if (c.hint) c.hint.textContent = cursorT == null ? "Drag to inspect" : fmtDateTime(cursorT);
}

// Refresh the legend-row toolbox: the reset button only shows while a manual
// y-range is pinned, and spectrum legend toggles mirror their hidden state.
function updateChartTools(c) {
  if (c.resetBtn) c.resetBtn.hidden = c.prefs.yMin == null && c.prefs.yMax == null;
  if (c.toggleItems) {
    for (const { key, item } of c.toggleItems) item.classList.toggle("off", c.prefs.hidden.has(key));
  }
}

function updateSpectrumReadout(c) {
  if (!c.hint) return;
  const cursorBand = cursorBandByGroup[c.group];
  if (cursorBand == null) { c.hint.textContent = "Drag to inspect"; return; }
  const idx = Math.min(c.categories.length - 1, Math.max(0, cursorBand));
  const unit = c.opts.unit ? " " + c.opts.unit : "";
  const digits = c.opts.yDigits ?? 1;
  // Append an x-axis unit (e.g. "Hz" for HiveHeart's frequency ranges) to the
  // hovered category so the full range reads clearly even when the axis ticks
  // are thinned; mic bands leave xUnit unset.
  const cat = c.opts.xUnit ? `${c.categories[idx]} ${c.opts.xUnit}` : c.categories[idx];
  const stats = c.bandStats[idx];
  if (!stats) { c.hint.textContent = `${cat}: ${DASH}`; return; }
  c.hint.textContent = `${cat}: ${fmt(stats.min, digits)} to ${fmt(stats.max, digits)}${unit}`;
}

// Turn a pointer event's x position into a timestamp using the chart's last
// drawn pixel<->time mapping (stashed on the canvas by drawLineChart), then
// redraw every chart so the whole view's readout stays in sync. Redraws are
// coalesced to one per animation frame — pointermove can fire far faster than
// the display refreshes, and each redraw repaints every chart on the view.
let cursorRaf = 0;
function setCursorFromEvent(canvas, e) {
  const scale = canvas._xScale;
  if (!scale) return;
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const frac = Math.min(1, Math.max(0, (x - scale.padL) / scale.plotW));
  cursorT = scale.tMin + frac * (scale.tMax - scale.tMin);
  if (!cursorRaf) {
    cursorRaf = requestAnimationFrame(() => { cursorRaf = 0; drawCharts(); });
  }
}

// Same idea as setCursorFromEvent, but for a spectrum chart's categorical
// x-axis (stashed as canvas._catScale by drawSpectrumChart): map the pointer
// x to the nearest band index instead of a timestamp.
function setCursorBandFromEvent(canvas, e) {
  const scale = canvas._catScale;
  if (!scale) return;
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const frac = scale.n <= 1 ? 0 : Math.min(1, Math.max(0, (x - scale.padL) / scale.plotW));
  cursorBandByGroup[canvas._spectrumGroup] = Math.round(frac * (scale.n - 1));
  if (!cursorRaf) {
    cursorRaf = requestAnimationFrame(() => { cursorRaf = 0; drawCharts(); });
  }
}

// ── editable y-axis (click an end label to pin the range) ───────────────────
// Hit-test a pointer event against the two editable y-axis fields: the strip
// left of the plot, top ≈ the max label, bottom ≈ the min label. Uses the
// pixel geometry stashed on the canvas by the last draw; null when the event
// is elsewhere (or the chart is empty, which clears the stash).
function yEditZone(canvas, e) {
  const z = canvas._yEdit;
  if (!z) return null;
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left, y = e.clientY - rect.top;
  if (x > z.padL || y < z.padT - 12 || y > z.padT + z.plotH + 12) return null;
  if (y < z.padT + z.plotH * 0.35) return "max";
  if (y > z.padT + z.plotH * 0.65) return "min";
  return null;
}

// Inline editor for one end of a chart's y-axis: a small number input overlaid
// on the clicked end label. Enter or clicking away applies, Escape cancels; a
// value that would invert the axis (min ≥ max) is ignored. The pinned range
// lives in chartPrefs, so it survives re-renders until "Reset y-axis".
function openYEdit(chart, which) {
  const canvas = chart.canvas;
  const z = canvas._yEdit;
  if (!z) return;
  const wrap = canvas.parentElement;
  const prev = wrap.querySelector(".y-edit-input");
  if (prev) prev.remove();
  const digits = Math.max(chart.opts.yDigits ?? 1, 1);
  const cur = which === "max" ? z.yMax : z.yMin;
  const input = el("input", {
    type: "number", step: "any", class: "y-edit-input",
    "aria-label": which === "max" ? "Y-axis maximum" : "Y-axis minimum",
  });
  input.value = String(Number(cur.toFixed(digits)));
  input.style.top = `${Math.max(0, (which === "max" ? z.padT : z.padT + z.plotH) - 12)}px`;
  input.style.width = `${z.padL + 12}px`;
  let done = false;
  const close = (apply) => {
    if (done) return;
    done = true;
    if (apply) {
      const v = parseFloat(input.value);
      const valid = Number.isFinite(v) && (which === "max" ? v > z.yMin : v < z.yMax);
      if (valid) chart.prefs[which === "max" ? "yMax" : "yMin"] = v;
    }
    input.remove();
    drawCharts();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); close(true); }
    else if (e.key === "Escape") close(false);
  });
  input.addEventListener("blur", () => close(true));
  wrap.append(input);
  // Focus after the opening click's mousedown/mouseup defaults have run —
  // focusing synchronously would be undone by the canvas mousedown, whose
  // default focus change blurs (and thus closes) the editor immediately.
  setTimeout(() => { input.focus(); input.select(); }, 0);
}

// ── CSV export ───────────────────────────────────────────────────────────────
function csvField(v) {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function downloadCsv(filename, rows) {
  const blob = new Blob([rows.map((r) => r.map(csvField).join(",")).join("\r\n")],
    { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function csvName(title) {
  const slug = title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  return `hivehub-${slug || "chart"}.csv`;
}

// Export what the chart currently shows. Line charts: one row per distinct
// timestamp (union across the visible series), one column per visible series,
// cells empty where a series has no reading at that time. Spectrum charts: one
// row per drawn snapshot, one column per band, honouring the Older/Latest
// legend toggles.
function downloadChartCsv(chart, title) {
  let rows;
  if (chart.kind === "spectrum") {
    const cats = chart.categories.map((c) => (chart.opts.xUnit ? `${c} ${chart.opts.xUnit}` : c));
    let snaps = chart.snapshots;
    if (chart.prefs.hidden.has("older")) snaps = snaps.slice(-1);
    if (chart.prefs.hidden.has("latest")) snaps = snaps.slice(0, -1);
    rows = [["timestamp", ...cats],
      ...snaps.map((s) => [new Date(s.t).toISOString(), ...s.values.map((v) => (v == null ? "" : v))])];
  } else {
    const visible = chart.series.filter((s) => !chart.prefs.hidden.has(s.label));
    const unit = chart.opts.unit ? ` (${chart.opts.unit})` : "";
    const byT = new Map();
    visible.forEach((s, i) => {
      for (const p of s.points) {
        let row = byT.get(p.t);
        if (!row) { row = new Array(visible.length).fill(""); byT.set(p.t, row); }
        row[i] = p.y;
      }
    });
    rows = [["timestamp", ...visible.map((s) => s.label + unit)],
      ...[...byT.entries()].sort((a, b) => a[0] - b[0])
        .map(([t, vals]) => [new Date(t).toISOString(), ...vals])];
  }
  downloadCsv(csvName(title), rows);
}

// The per-chart toolbox on the legend row, left of the drag hint: CSV export
// and — only while a manual y-range is pinned — "Reset y-axis".
function chartTools(chart, title, hint) {
  const csvBtn = el("button", {
    class: "chart-tool-btn", type: "button",
    title: "Download the data currently shown in this chart as CSV",
  }, "⤓ CSV");
  csvBtn.addEventListener("click", () => downloadChartCsv(chart, title));
  const resetBtn = el("button", {
    class: "chart-tool-btn", type: "button", hidden: true,
    title: "Clear the manual y-axis range and re-fit automatically",
  }, "Reset y-axis");
  resetBtn.addEventListener("click", () => {
    chart.prefs.yMin = null;
    chart.prefs.yMax = null;
    drawCharts();
  });
  chart.resetBtn = resetBtn;
  return el("span", { class: "chart-tools" }, csvBtn, resetBtn, hint);
}

// Make a legend entry toggle something on click (or Enter/Space): used for
// per-series visibility on line charts and Older/Latest on spectrum charts.
function makeLegendToggle(item, prefs, key, what) {
  item.classList.add("clickable");
  item.setAttribute("role", "button");
  item.setAttribute("tabindex", "0");
  item.title = `Show/hide ${what}`;
  const toggle = () => {
    if (prefs.hidden.has(key)) prefs.hidden.delete(key);
    else prefs.hidden.add(key);
    drawCharts();
  };
  item.addEventListener("click", toggle);
  item.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
}

// Mouse hover scrubs live and clears on leave; touch drags (pointermove only
// fires while the finger is down) and the selection stays pinned after lift so
// the tapped/slid value remains readable. A pointerdown on a y-axis end label
// opens the inline range editor instead of scrubbing.
function attachChartCursor(chart) {
  const canvas = chart.canvas;
  canvas.addEventListener("pointerdown", (e) => {
    const zone = yEditZone(canvas, e);
    if (zone) { e.preventDefault(); openYEdit(chart, zone); return; }
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* unsupported */ }
    setCursorFromEvent(canvas, e);
  });
  canvas.addEventListener("pointermove", (e) => {
    const zone = yEditZone(canvas, e);
    canvas.style.cursor = zone ? "text" : "";
    if (!zone) setCursorFromEvent(canvas, e);
  });
  canvas.addEventListener("pointerup", (e) => {
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) { /* unsupported */ }
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return; // keep the touch-selected point visible
    cursorT = null;
    drawCharts();
  });
}

function attachSpectrumCursor(chart) {
  const canvas = chart.canvas;
  canvas.addEventListener("pointerdown", (e) => {
    const zone = yEditZone(canvas, e);
    if (zone) { e.preventDefault(); openYEdit(chart, zone); return; }
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* unsupported */ }
    setCursorBandFromEvent(canvas, e);
  });
  canvas.addEventListener("pointermove", (e) => {
    const zone = yEditZone(canvas, e);
    canvas.style.cursor = zone ? "text" : "";
    if (!zone) setCursorBandFromEvent(canvas, e);
  });
  canvas.addEventListener("pointerup", (e) => {
    try { canvas.releasePointerCapture(e.pointerId); } catch (_) { /* unsupported */ }
  });
  canvas.addEventListener("pointerleave", (e) => {
    if (e.pointerType !== "mouse") return;
    delete cursorBandByGroup[canvas._spectrumGroup];
    drawCharts();
  });
}

function chartCard(title, sub, series, opts = {}) {
  const canvas = el("canvas");
  const wrap = el("div", { class: "chart-wrap" }, canvas);
  const prefs = prefsFor(title);
  const legendItems = series.map((s) => {
    const valueEl = el("span", { class: "lg-value" });
    const item = el("span", { class: "lg" },
      el("span", { class: "swatch", style: `background:${s.color}` }), s.label, valueEl);
    makeLegendToggle(item, prefs, s.label, `the “${s.label}” series`);
    return { series: s, valueEl, item };
  });
  const hint = el("span", { class: "chart-hint" }, "Drag to inspect");
  // Dash segments that span a data gap (see drawLineChart). sendIntervalMs is
  // only a fallback cadence; the test adapts to each series' own spacing, so
  // even coarse charts (e.g. daily-max) flag only genuinely missing stretches.
  const chart = { canvas, series, opts: { ...opts, sendIntervalMs }, legendItems, hint, prefs };
  const legend = el("div", { class: "chart-legend" }, ...legendItems.map((li) => li.item),
    series.length ? chartTools(chart, title, hint) : null);
  activeCharts.push(chart);
  if (series.length) attachChartCursor(chart);
  return el("div", { class: "card chart-card" },
    el("h2", {}, title),
    sub ? el("p", { class: "card-sub" }, sub) : null,
    series.length ? legend : null,
    wrap);
}

// FFT-style spectrum card: x-axis is a fixed set of categories (bands), y-axis
// is value; each snapshot (one per sampled measurement) draws its own line,
// faded by age, so a whole time range overlays like a waterfall. `snapshots`
// is oldest→newest and `bandStats` is a {min,max} per category across the full
// selected time range (not just the downsampled snapshots), used for the
// hover cursor's range readout (see spectrumSnapshots/bandMinMax below).
function spectrumChartCard(title, sub, categories, snapshots, bandStats, color, opts = {}) {
  const canvas = el("canvas");
  const wrap = el("div", { class: "chart-wrap" }, canvas);
  const prefs = prefsFor(title);
  const oldest = snapshots[0], newest = snapshots[snapshots.length - 1];
  const hint = el("span", { class: "chart-hint" }, "Drag to inspect");
  // "Older" and "Latest" act as legend toggles, like the series entries on a
  // line chart: Older hides the faded history lines, Latest the bold newest one.
  const olderItem = el("span", { class: "lg" }, "Older",
    el("span", { class: "spectrum-gradient", style: `background:linear-gradient(90deg, ${withAlpha(color, 0.15)}, ${color})` }));
  makeLegendToggle(olderItem, prefs, "older", "the older snapshots");
  const latestItem = el("span", { class: "lg" },
    el("span", { class: "swatch", style: "background:var(--chart-latest)" }), "Latest");
  makeLegendToggle(latestItem, prefs, "latest", "the latest snapshot");
  const rangeNote = oldest && newest ? ` (${fmtDateTime(oldest.t)} – ${fmtDateTime(newest.t)})` : "";
  // Charts sharing the same categories (all mic charts, or all HiveHeart charts)
  // share a hover cursor; different category sets are independent groups.
  const group = categories.join("|");
  canvas._spectrumGroup = group;
  const chart = {
    canvas, kind: "spectrum", group, categories, snapshots, bandStats,
    opts: { ...opts, color }, hint, prefs,
    toggleItems: [{ key: "older", item: olderItem }, { key: "latest", item: latestItem }],
  };
  const legend = el("div", { class: "chart-legend" },
    olderItem,
    latestItem,
    // Marker key (HiveHeart peak frequency) — colour matches drawSpectrumChart.
    opts.marker ? el("span", { class: "lg" },
      el("span", { class: "swatch", style: "background:#d6336c" }), "Peak freq") : null,
    chartTools(chart, title, hint));
  activeCharts.push(chart);
  if (snapshots.length) attachSpectrumCursor(chart);
  return el("div", { class: "card chart-card" },
    el("h2", {}, title),
    sub ? el("p", { class: "card-sub" }, sub + rangeNote) : null,
    snapshots.length ? legend : null,
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

// A metric panel that stays readable across one or many hives. With a single
// hive it renders the classic big-number card (unchanged); with several it
// stacks a compact, smaller-font row per hive — each tagged with the hive name —
// so every selected hive is visible instead of just the first. `cellFn(n)`
// returns the formatted value string for hive n; `footer` is an optional note
// shown under the list (e.g. the shared ambient reading); `subFn(n)`, when
// given, returns a small per-row annotation (e.g. the hive's 24h delta) or
// null to omit it for that row.
function perHiveCard(state, label, refs, unit, cellFn, footer, subFn) {
  if (refs.length <= 1) {
    return metricCard(label, refs.length ? cellFn(refs[0]) : DASH, unit, footer);
  }
  const rows = refs.map((ref) => {
    const sub = subFn ? subFn(ref) : null;
    return el("div", { class: "hive-row" },
      el("span", { class: "hive-row-name" }, refLabel(state, ref)),
      el("span", { class: "hive-row-val" },
        cellFn(ref), unit ? el("span", { class: "hive-row-unit" }, " " + unit) : null,
        sub ? el("span", { class: "hive-row-delta" }, sub) : null));
  });
  return el("div", { class: "card" },
    el("div", { class: "metric" },
      el("span", { class: "label" }, label),
      el("div", { class: "hive-rows" }, ...rows),
      footer ? el("span", { class: "sub" }, footer) : null));
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
// channel names, and the legacy flat scale_N keys. Returns [] for a device that
// has never reported a hive (new/silent device) so views can say "no hives
// reported yet" instead of showing phantom default hives.
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

// The comparison selection resolved into per-hive "refs". Each ref carries its
// own device's data (latest reading, measurement history, channel names) so hives
// from different devices can be charted side by side. Colour is assigned by
// position — the same cycle as paletteColor — so a hive's swatch in the top-bar
// chips matches its series here.
function selectedRefs(state) {
  return (state.selection || []).map((s) => ({
    deviceId: s.deviceId,
    hive: Number(s.hive),
    key: `${s.deviceId}::${s.hive}`,
    device: state.deviceMeta(s.deviceId),
    latest: state.deviceLatest(s.deviceId),
    measurements: state.deviceMeasurements(s.deviceId),
    channels: state.deviceChannels(s.deviceId),
  }));
}

// A device-shaped object so hiveLabel()/availableHives() resolve names for any
// ref's device (custom channel names exist only for the active device; others
// fall back to the firmware-reported hive name).
function refState(ref) { return { latest: ref.latest, channels: ref.channels, device: ref.device }; }

// A hive's display label. Device-qualified ("Linden · Garden") only while more
// than one device is being compared, so the single-device case stays clean.
function refLabel(state, ref) {
  const base = hiveLabel(refState(ref), ref.hive);
  if (!state.multiDevice) return base;
  const dev = ref.device?.display_name || ref.device?.device_id || "";
  return dev ? `${base} · ${dev}` : base;
}

// A note naming which device a device-scoped panel reflects — shown only while
// several devices are compared, where "Battery", "Signal" etc. would otherwise be
// ambiguous. Points at the "Device details" top-bar switcher.
function deviceContextNote(state, what) {
  if (!state.multiDevice) return null;
  const name = state.device?.display_name || state.device?.device_id || "—";
  return el("div", { class: "device-context-note" },
    el("span", {}, `${what} shown for `), el("b", {}, name),
    el("span", {}, " — switch with “Device details” in the top bar."));
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
  const refs = selectedRefs(state);
  const m = state.latest || {};   // active device — for the device-level cards
  let totalWeight = 0, anyWeight = false;
  for (const ref of refs) {
    const v = ref.latest ? ref.latest[weightKey(ref.latest, ref.hive)] : null;
    if (isNum(v)) { totalWeight += v; anyWeight = true; }
  }
  // per-hive 24h weight deltas; total delta only when every hive has one, so a
  // hive with a data gap can't silently skew the apiary-wide number
  const deltas = new Map(refs.map((ref) => [ref.key, changeOver(ref.measurements, weightKey(ref.latest || {}, ref.hive), 24)]));
  const w24 = refs.length ? deltas.get(refs[0].key) : null;
  const total24 = refs.length && refs.every((ref) => deltas.get(ref.key) != null)
    ? refs.reduce((sum, ref) => sum + deltas.get(ref.key), 0)
    : null;

  const ins = state.insights;
  const sev = ins?.highest_severity;
  const sevBadge = el("span", { class: `badge ${sevClass(sev)}` },
    el("span", { class: `dot ${sevClass(sev)}` }), sev ? sev : "OK");

  const hiveTemp = (ref) => (ref.latest ? fmt(ref.latest[`hive_${ref.hive}_temp_c`], 1) : DASH);
  const hiveHum = (ref) => (ref.latest ? fmt(ref.latest[`hive_${ref.hive}_humidity_percent`], 0) : DASH);
  const cards = [
    refs.length > 1
      ? perHiveCard(state, "Weight", refs, "kg",
          (ref) => (ref.latest ? fmt(ref.latest[weightKey(ref.latest, ref.hive)], 2) : DASH),
          anyWeight
            ? `Total ${fmt(totalWeight, 2)} kg${total24 != null ? ` · 24h ${signed(total24, 2, "kg")}` : ""}`
            : "Total of active scales",
          (ref) => (deltas.get(ref.key) != null ? signed(deltas.get(ref.key), 2) : null))
      : metricCard("Weight", anyWeight ? fmt(totalWeight, 2) : DASH, "kg",
          refs.length === 0
            ? "No hives selected"
            : w24 != null ? `24h ${signed(w24, 2, "kg")}` : "Total of active scales"),
    perHiveCard(state, "Hive temperature", refs, "°C", hiveTemp,
      isNum(m.ambient_temp_c) ? `Ambient ${fmt(m.ambient_temp_c, 1)} °C${state.multiDevice ? ` (${state.device?.display_name || state.device?.device_id})` : ""}` : "Brood zone"),
    refs.length > 1
      ? perHiveCard(state, "In-hive humidity", refs, "%", hiveHum,
          isNum(m.ambient_humidity_percent) ? `Ambient ${fmt(m.ambient_humidity_percent, 1)} %` : "Brood area")
      : metricCard("Humidity", fmt(m.ambient_humidity_percent, 1), "%",
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

  // Subtitle reflects the whole comparison set, not a single device.
  const nDev = new Set(refs.map((r) => r.deviceId)).size;
  const subtitle = refs.length === 0
    ? "No hives selected — open the Hives menu in the top bar"
    : state.multiDevice
      ? `${refs.length} hives across ${nDev} devices`
      : `${state.device?.display_name || state.device?.device_id} — last seen ${relAge(state.device?.last_seen_at)}`;

  // Filter nulls: deviceContextNote() returns null on a single-device selection,
  // and raw DOM append() would otherwise stringify it into a "null" text node.
  root.append(...[
    viewHead("Overview", subtitle),
    deviceContextNote(state, "Battery, signal and status"),
    el("div", { class: "grid" }, ...cards),
    el("div", { class: "grid wide", style: "margin-top:1rem" },
      statusCard,
      highestAlertCard(ins),
      chartCard("Weight trend", "Compensated mass over the selected range",
        refs.map((ref, i) =>
          seriesFrom(ref.measurements, weightKey(ref.latest || {}, ref.hive), refLabel(state, ref), paletteColor(i))),
        { unit: "kg", yDigits: 1 })),
  ].filter(Boolean));
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
  const refs = selectedRefs(state);
  const m = state.latest || {};   // active device (for the ambient reference)
  const cards = refs.map((ref) => {
    const t = ref.latest ? ref.latest[`hive_${ref.hive}_temp_c`] : null;
    const h = ref.latest ? ref.latest[`hive_${ref.hive}_humidity_percent`] : null;
    return metricCard(`${refLabel(state, ref)} temp`, fmt(t, 1), "°C",
      isNum(h) ? `Humidity ${fmt(h, 0)} %` : "In-hive");
  });
  cards.push(metricCard("Ambient", fmt(m.ambient_temp_c, 1), "°C",
    state.multiDevice ? `${state.device?.display_name || state.device?.device_id}` : "Outside the hive"));

  const series = refs.map((ref, i) =>
    seriesFrom(ref.measurements, `hive_${ref.hive}_temp_c`, refLabel(state, ref), paletteColor(i)));
  series.push(seriesFrom(state.measurements, "ambient_temp_c",
    state.multiDevice ? `Ambient · ${state.device?.display_name || state.device?.device_id}` : "Ambient", paletteColor(refs.length)));

  const node = tsView("Temperature", "Inside and ambient temperature", state,
    { cards, charts: [chartCard("Temperature", null, series, { unit: "°C", yDigits: 1 })] });
  const note = deviceContextNote(state, "Ambient temperature");
  if (note) node.insertBefore(note, node.children[1]);
  root.append(node);
}

function renderWeight(root, state) {
  const refs = selectedRefs(state);
  const cards = refs.map((ref) => {
    const key = weightKey(ref.latest || {}, ref.hive);
    const c24 = changeOver(ref.measurements, key, 24);
    return metricCard(`${refLabel(state, ref)} weight`, fmt(ref.latest ? ref.latest[key] : null, 2), "kg",
      c24 != null ? `24h ${signed(c24, 2, "kg")}` : "Compensated");
  });
  const series = refs.map((ref, i) =>
    seriesFrom(ref.measurements, weightKey(ref.latest || {}, ref.hive), refLabel(state, ref), paletteColor(i)));
  const dailyMax = refs.map((ref, i) =>
    dailyMaxSeries(ref.measurements, weightKey(ref.latest || {}, ref.hive), refLabel(state, ref), paletteColor(i)));

  root.append(tsView("Weight", "Mass changes and harvest trend", state,
    { cards, charts: [
      chartCard("Weight", null, series, { unit: "kg", yDigits: 1 }),
      chartCard("Daily max weight", "Highest reading per day over the selected range", dailyMax, { unit: "kg", yDigits: 1 }),
    ] }));
}

function renderEnvironment(root, state) {
  const refs = selectedRefs(state);
  const m = state.latest || {};   // active device — ambient humidity + pressure
  const pressureKeys = ["ble_1_pressure_hpa", "ble_2_pressure_hpa", "hivescale_1_pressure_hpa", "hivescale_2_pressure_hpa"];
  const cards = [
    metricCard("Ambient humidity", fmt(m.ambient_humidity_percent, 1), "%",
      state.multiDevice ? `${state.device?.display_name || state.device?.device_id}` : "Outside the hive"),
    perHiveCard(state, "In-hive humidity", refs, "%",
      (ref) => fmt(ref.latest ? ref.latest[`hive_${ref.hive}_humidity_percent`] : null, 1), "Brood area"),
    metricCard("Pressure", fmt(latestCoalesce([m], pressureKeys), 0), "hPa", "Barometric"),
  ];
  const charts = [
    chartCard("Humidity", "Ambient and in-hive relative humidity",
      [seriesFrom(state.measurements, "ambient_humidity_percent",
         state.multiDevice ? `Ambient · ${state.device?.display_name || state.device?.device_id}` : "Ambient", paletteColor(refs.length)),
       ...refs.map((ref, i) =>
         seriesFrom(ref.measurements, `hive_${ref.hive}_humidity_percent`, refLabel(state, ref), paletteColor(i)))],
      { unit: "%", yDigits: 0 }),
    chartCard("Pressure", "Barometric pressure around the hive",
      [seriesCoalesce(state.measurements, pressureKeys,
        state.multiDevice ? `Pressure · ${state.device?.display_name || state.device?.device_id}` : "Pressure", PALETTE[3])], { unit: "hPa", yDigits: 0 }),
  ];
  const node = tsView("Environment", "Humidity and air pressure", state, { cards, charts });
  const note = deviceContextNote(state, "Ambient humidity and pressure");
  if (note) node.insertBefore(note, node.children[1]);
  root.append(node);
}

// Candidate mic keys for hive n, most-specific first. The multi-hive firmware
// exposes per-hive aliases (mic_{n}_rms_dbfs, …) for every hive; older stereo
// rows only carry the legacy left/right channels, where left = hive 1 and
// right = hive 2. Returning the per-hive key first with the legacy channel as a
// fallback means hives 3–18 read their own mic instead of aliasing hive 2's
// right channel, while legacy two-hive rows keep working.
function micKeys(n, suffix) {
  const keys = [`mic_${n}_${suffix}`];
  if (n === 1) keys.push(`mic_left_${suffix}`);
  else if (n === 2) keys.push(`mic_right_${suffix}`);
  return keys;
}

function renderAudio(root, state) {
  const m = state.latest || {};   // active device — for the sample-rate card
  const refs = selectedRefs(state);
  const cards = refs.map((ref) => {
    const rms = ref.latest ? latestCoalesce([ref.latest], micKeys(ref.hive, "rms_dbfs")) : null;
    const peak = ref.latest ? latestCoalesce([ref.latest], micKeys(ref.hive, "peak_dbfs")) : null;
    return metricCard(`${refLabel(state, ref)} RMS`, fmt(rms, 1), "dBFS",
      isNum(peak) ? `Peak ${fmt(peak, 1)}` : "Sound level");
  });
  cards.push(metricCard("Sample rate", fmtInt(m.mic_sample_rate_hz), "Hz",
    isNum(m.mic_sample_frames) ? `${fmtInt(m.mic_sample_frames)} frames` : "Microphone"));

  const rms = refs.map((ref, i) => seriesCoalesce(ref.measurements, micKeys(ref.hive, "rms_dbfs"), refLabel(state, ref), paletteColor(i)));
  const peak = refs.map((ref, i) => seriesCoalesce(ref.measurements, micKeys(ref.hive, "peak_dbfs"), refLabel(state, ref), paletteColor(i)));
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

// Max spectrum lines drawn per chart. Measurements can arrive every few
// seconds, so plotting one line per row over a multi-day range would be an
// unreadable smear — sample evenly across the range instead, always keeping
// the newest reading so the current spectrum is exact.
const SPECTRUM_MAX_SNAPSHOTS = 12;

// First non-null, numeric-coercible value of `keys` on row `m` (candidate
// keys are tried in priority order, e.g. a per-hive key before its legacy
// left/right alias — see micKeys).
function coalesceNumeric(m, keys) {
  for (const k of keys) {
    if (m[k] == null || m[k] === "") continue;
    const y = typeof m[k] === "number" ? m[k] : Number(m[k]);
    return Number.isFinite(y) ? y : null;
  }
  return null;
}

// Build oldest→newest {t, values} rows (one per sampled measurement, values
// aligned to `keysList`) from newest-first `measurements`, downsampled to at
// most SPECTRUM_MAX_SNAPSHOTS evenly spaced rows.
function spectrumSnapshots(measurements, keysList) {
  const rows = [];
  for (let i = measurements.length - 1; i >= 0; i--) {
    const m = measurements[i];
    if (m == null) continue;
    const values = keysList.map((keys) => coalesceNumeric(m, keys));
    if (!values.some(isNum)) continue;
    const t = new Date(m.measured_at).getTime();
    if (Number.isNaN(t)) continue;
    rows.push({ t, values });
  }
  if (rows.length <= SPECTRUM_MAX_SNAPSHOTS) return rows;
  const step = (rows.length - 1) / (SPECTRUM_MAX_SNAPSHOTS - 1);
  const out = [];
  for (let i = 0; i < SPECTRUM_MAX_SNAPSHOTS; i++) out.push(rows[Math.round(i * step)]);
  return out;
}

// Per-category {min, max} across every measurement in the selected time
// range (not just the downsampled snapshots actually drawn), so the hover
// cursor's range readout reflects the true spread even on a wide range.
function bandMinMax(measurements, keysList) {
  return keysList.map((keys) => {
    let min = Infinity, max = -Infinity;
    for (const m of measurements) {
      if (m == null) continue;
      const y = coalesceNumeric(m, keys);
      if (y == null) continue;
      if (y < min) min = y;
      if (y > max) max = y;
    }
    return min === Infinity ? null : { min, max };
  });
}

// ── HiveHeart in-hive FFT spectrum ───────────────────────────────────────────
// The 16 HiveHeart frequency ranges (Hz). Mirrors server/hiveheart_fft.py
// FFT_RANGES_HZ — keep in sync with the backend. Note the known 845–853 Hz gap
// between bins 9 and 10 (preserved verbatim per the vendor table; see the TODO
// in hiveheart_fft.py). HiveHeart values are RELATIVE levels 0–15, NOT dBFS, so
// they are charted on their own 0–15 axis, never overlaid on the mic spectrum.
const HIVEHEART_FFT_RANGES = [
  [0, 93], [94, 187], [188, 281], [282, 375], [376, 479], [480, 562],
  [563, 656], [657, 750], [751, 844], [854, 937], [938, 1031], [1032, 1125],
  [1126, 1218], [1219, 1312], [1313, 1406], [1407, 1500],
];
const HIVEHEART_FFT_LABELS = HIVEHEART_FFT_RANGES.map(([lo, hi]) => `${lo}–${hi}`);
const HIVEHEART_FFT_BIN_COUNT = 16;

// Conceptual acoustic bands shown as grouped headings over the HiveHeart x-axis.
// Display only: each spans several HiveHeart ranges (and boundaries fall mid-bin),
// so they are annotations, never a 1:1 bin→band mapping (see insights.py). HiveHeart
// tops out at 1500 Hz, so there is no High (1500–3000 Hz) band.
const HIVEHEART_SEMANTIC_BANDS = [
  ["Sub-bass", 0, 150], ["Hum", 150, 300], ["Piping", 300, 550], ["Stress", 550, 1500],
];

// Map a frequency (Hz) to a fractional HiveHeart bin index (0..15) so a marker or
// band boundary lines up with the plotted bins. Bin i occupies index [i-0.5, i+0.5];
// a frequency inside range i interpolates within that slot. Frequencies in the
// 845–853 Hz gap land on the boundary between the adjacent bins; out-of-range
// clamps to the first/last bin; null when not finite.
function hiveheartFreqToIndex(hz) {
  if (!Number.isFinite(hz)) return null;
  const R = HIVEHEART_FFT_RANGES;
  if (hz <= R[0][0]) return 0;
  if (hz >= R[R.length - 1][1]) return R.length - 1;
  for (let i = 0; i < R.length; i++) {
    const [lo, hi] = R[i];
    if (hz >= lo && hz <= hi) return (i - 0.5) + (hi > lo ? (hz - lo) / (hi - lo) : 0.5);
  }
  for (let i = 0; i < R.length - 1; i++) {
    if (hz > R[i][1] && hz < R[i + 1][0]) return i + 0.5; // in the inter-bin gap
  }
  return null;
}

// The semantic bands as {label, from, to} fractional-index spans for the chart.
function hiveheartSemanticSpans() {
  return HIVEHEART_SEMANTIC_BANDS.map(([label, lo, hi]) => ({
    label,
    from: hiveheartFreqToIndex(lo) ?? 0,
    to: hiveheartFreqToIndex(hi) ?? (HIVEHEART_FFT_BIN_COUNT - 1),
  }));
}

// The most recent finite HiveHeart peak frequency (Hz) reported for this hive —
// the independent frequency_hz value, drawn as a marker on the spectrum.
function latestHiveheartFreq(ref) {
  const key = `hiveheart_${ref.hive}_frequency_hz`;
  const rows = ref.latest ? [ref.latest, ...ref.measurements] : ref.measurements;
  for (const m of rows) {
    if (m == null || m[key] == null || m[key] === "") continue;
    const y = Number(m[key]);
    if (Number.isFinite(y)) return y;
  }
  return null;
}

// Coerce a measurement's decoded HiveHeart fft_bins into a 16-number array, or
// null when absent/malformed (legacy rows, or a hive with no HiveHeart sensor).
function hiveheartBins(m, key) {
  const v = m && m[key];
  if (!Array.isArray(v) || v.length !== HIVEHEART_FFT_BIN_COUNT) return null;
  const out = v.map((x) => (typeof x === "number" ? x : Number(x)));
  return out.every((x) => Number.isFinite(x)) ? out : null;
}

// Oldest→newest {t, values[16]} snapshots for a hive's HiveHeart spectrum,
// downsampled to at most SPECTRUM_MAX_SNAPSHOTS (mirrors spectrumSnapshots).
function hiveheartSnapshots(measurements, key) {
  const rows = [];
  for (let i = measurements.length - 1; i >= 0; i--) {
    const m = measurements[i];
    const bins = hiveheartBins(m, key);
    if (!bins) continue;
    const t = new Date(m.measured_at).getTime();
    if (Number.isNaN(t)) continue;
    rows.push({ t, values: bins });
  }
  if (rows.length <= SPECTRUM_MAX_SNAPSHOTS) return rows;
  const step = (rows.length - 1) / (SPECTRUM_MAX_SNAPSHOTS - 1);
  const out = [];
  for (let i = 0; i < SPECTRUM_MAX_SNAPSHOTS; i++) out.push(rows[Math.round(i * step)]);
  return out;
}

// Per-range {min,max} across the full selected range for the hover readout.
function hiveheartBandMinMax(measurements, key) {
  const stats = HIVEHEART_FFT_RANGES.map(() => ({ min: Infinity, max: -Infinity }));
  for (const m of measurements) {
    const bins = hiveheartBins(m, key);
    if (!bins) continue;
    bins.forEach((y, i) => {
      if (y < stats[i].min) stats[i].min = y;
      if (y > stats[i].max) stats[i].max = y;
    });
  }
  return stats.map((s) => (s.min === Infinity ? null : s));
}

function renderFrequency(root, state) {
  const refs = selectedRefs(state);
  const categories = BANDS.map(([, label]) => label);
  const charts = [];
  let micCharts = 0;
  refs.forEach((ref, i) => {
    const keysList = BANDS.map(([k]) => micKeys(ref.hive, `band_${k}_dbfs`));
    const snapshots = spectrumSnapshots(ref.measurements, keysList);
    if (snapshots.length) {
      micCharts++;
      const bandStats = bandMinMax(ref.measurements, keysList);
      charts.push(spectrumChartCard(`Frequency bands — ${refLabel(state, ref)}`,
        "FFT energy by band, like a spectrum analyzer — the bold line is the latest reading, fainter lines are earlier ones",
        categories, snapshots, bandStats, paletteColor(i), { unit: "dBFS", yDigits: 0 }));
    }
  });
  if (!micCharts) {
    charts.push(el("div", { class: "card" }, el("p", { class: "muted-text" }, "No frequency-band data reported by this device.")));
  }

  // HiveHeart in-hive spectrum. Rendered as a separate 16-range diagram on its
  // own 0–15 relative-level axis (HiveHeart levels are not dBFS). Only shown when
  // at least one selected hive reports HiveHeart FFT, then a card per hive so a
  // hive without data gets a clear empty state without cluttering non-HiveHeart
  // setups.
  const hhSnaps = refs.map((ref) => hiveheartSnapshots(ref.measurements, `hiveheart_${ref.hive}_fft_bins`));
  if (hhSnaps.some((s) => s.length)) {
    refs.forEach((ref, i) => {
      const snaps = hhSnaps[i];
      if (snaps.length) {
        const key = `hiveheart_${ref.hive}_fft_bins`;
        const bandStats = hiveheartBandMinMax(ref.measurements, key);
        const opts = {
          unit: "level", yDigits: 0, yMin: 0, yMax: 15, xUnit: "Hz",
          semanticBands: hiveheartSemanticSpans(),
        };
        const freqHz = latestHiveheartFreq(ref);
        const markerIndex = hiveheartFreqToIndex(freqHz);
        if (markerIndex != null) opts.marker = { index: markerIndex, label: `${Math.round(freqHz)} Hz` };
        charts.push(spectrumChartCard(`HiveHeart spectrum — ${refLabel(state, ref)}`,
          "HiveHeart in-hive FFT — 16 frequency ranges (Hz), relative level 0–15 (not dBFS); bold line is the latest reading, fainter lines are earlier ones. The dashed pink marker is HiveHeart's reported peak frequency; the Sub-bass/Hum/Piping/Stress headings are approximate and span several ranges",
          HIVEHEART_FFT_LABELS, snaps, bandStats, paletteColor(i), opts));
      } else {
        charts.push(el("div", { class: "card" },
          el("h2", {}, `HiveHeart spectrum — ${refLabel(state, ref)}`),
          el("p", { class: "muted-text" }, "No HiveHeart FFT data for this hive.")));
      }
    });
  }

  root.append(tsView("Frequency bands", "FFT energy by acoustic band", state, { charts }));
}

// Wireless (BLE) in-hive sensors that report their own battery, separate from
// the ESP32 collector pack. Voltage-based sensors (HiveScale/HiveHeart) and the
// percent-based BLE sensor (HolyIot/Ruuvi/HiveInside) are charted on separate
// axes because their units differ. `low` drives a low-battery hint on the card.
const WIRELESS_BATTERY = [
  { key: (n) => `hivescale_${n}_battery_v`, label: "HiveScale", unit: "V", digits: 2, low: 3.4 },
  { key: (n) => `hiveheart_${n}_battery_v`, label: "HiveHeart", unit: "V", digits: 2, low: 3.4 },
  { key: (n) => `ble_${n}_battery_percent`, label: "BLE sensor", unit: "%", digits: 0, low: 20 },
];

// One line-chart series per hive+sensor that has data, for the given unit. Reads
// each ref's own device measurements so wireless batteries compare across devices.
function wirelessBatterySeries(state, refs, unit) {
  const out = [];
  for (const ref of refs) {
    for (const src of WIRELESS_BATTERY) {
      if (src.unit !== unit) continue;
      const s = seriesFrom(ref.measurements, src.key(ref.hive), `${refLabel(state, ref)} · ${src.label}`, paletteColor(out.length));
      if (s.points.length) out.push(s);
    }
  }
  return out;
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

  const node = el("div", {});
  node.append(viewHead("Battery & power", "Collector battery, solar and wireless-sensor batteries"));
  const note = deviceContextNote(state, "The collector battery and solar readings below are");
  if (note) node.append(note);
  node.append(el("div", { class: "grid" }, ...cards));
  node.append(el("div", { class: "grid wide", style: "margin-top:1rem" }, ...charts));

  // Wireless (BLE) sensor batteries — each in-hive scale/acoustic/environment
  // sensor runs on its own cell, so surface them apart from the collector pack.
  // These are per-hive, so they follow the whole comparison selection.
  const refs = selectedRefs(state);
  const wCards = [];
  for (const ref of refs) {
    for (const src of WIRELESS_BATTERY) {
      const v = latestOf(ref.measurements, src.key(ref.hive));
      if (!isNum(v)) continue;
      wCards.push(metricCard(`${refLabel(state, ref)} · ${src.label}`, fmt(v, src.digits), src.unit,
        v <= src.low ? "Low battery" : "Wireless sensor"));
    }
  }
  const wCharts = [];
  const voltSeries = wirelessBatterySeries(state, refs, "V");
  const pctSeries = wirelessBatterySeries(state, refs, "%");
  if (voltSeries.length) wCharts.push(chartCard("Wireless sensor battery", "In-hive BLE scale & acoustic sensor voltage", voltSeries, { unit: "V", yDigits: 2 }));
  if (pctSeries.length) wCharts.push(chartCard("Wireless sensor charge", "In-hive BLE sensor state of charge", pctSeries, { unit: "%", yDigits: 0 }));

  node.append(el("div", { style: "margin-top:2rem" }, viewHead("Wireless sensors", "Battery of each wireless in-hive sensor")));
  if (wCards.length || wCharts.length) {
    if (wCards.length) node.append(el("div", { class: "grid" }, ...wCards));
    if (wCharts.length) node.append(el("div", { class: "grid wide", style: "margin-top:1rem" }, ...wCharts));
  } else {
    node.append(el("div", { class: "card" }, el("p", { class: "muted-text" },
      "No wireless-sensor batteries reported by this device.")));
  }
  root.append(node);
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
  const node = tsView("Connectivity", "Network and timing health", state, { cards, charts });
  const note = deviceContextNote(state, "Connectivity is per device and is");
  if (note) node.insertBefore(note, node.children[1]);
  root.append(node);
}

function renderCounter(root, state) {
  const refs = selectedRefs(state);
  const cards = [];
  for (const ref of refs) {
    const m = ref.latest || {};
    cards.push(metricCard(`${refLabel(state, ref)} in`, fmtInt(m[`bee_counter_${ref.hive}_total_in`]), "", "Total entrances"));
    cards.push(metricCard(`${refLabel(state, ref)} out`, fmtInt(m[`bee_counter_${ref.hive}_total_out`]), "", "Total exits"));
  }
  const charts = [];
  for (const ref of refs) {
    const inS = seriesFrom(ref.measurements, `bee_counter_${ref.hive}_interval_in`, `${refLabel(state, ref)} in`, PALETTE[2]);
    const outS = seriesFrom(ref.measurements, `bee_counter_${ref.hive}_interval_out`, `${refLabel(state, ref)} out`, PALETTE[3]);
    if (inS.points.length || outS.points.length) {
      charts.push(chartCard(`Traffic — ${refLabel(state, ref)}`, "Bees in/out per interval", [inS, outS], { yDigits: 0 }));
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
  node.append(viewHead("Insights", [
    "Rule-based colony alerts (14-day lookback) · ",
    el("a", { class: "doc-link", href: "https://github.com/MacNite/HiveHub/blob/main/docs/insights-sources-tldr.md", target: "_blank", rel: "noopener noreferrer" }, "TL;DR docs"),
    " · ",
    el("a", { class: "doc-link", href: "https://github.com/MacNite/HiveHub/blob/main/docs/insights.md", target: "_blank", rel: "noopener noreferrer" }, "Full docs"),
  ]));
  const insNote = deviceContextNote(state, "Insights are computed per device and are");
  if (insNote) node.append(insNote);
  if (!ins) { node.append(el("div", { class: "card" }, "No insight data.")); root.append(node); return; }

  // `categories` is a list of category names (both the live-compute and the
  // persisted summary paths); Object.entries() on it rendered "0 — swarm" rows.
  const cats = Array.isArray(ins.categories) ? ins.categories : Object.keys(ins.categories || {});
  const summaryCards = [
    metricCard("Active alerts", fmtInt(ins.alert_count), "", `Computed ${relAge(ins.computed_at)}`),
    metricCard("Highest severity", ins.highest_severity || "OK", "", "Most urgent"),
  ];
  node.append(el("div", { class: "grid" }, ...summaryCards));

  if (cats.length) node.append(el("div", { style: "margin-top:1rem" },
    rowsCard("Categories", [["Active alert categories", cats.join(", ")]])));
  node.append(el("div", { style: "margin-top:1rem" }, highestAlertCard(ins)));
  node.append(el("div", { style: "margin-top:1rem" }, insightsHistoryCard(state)));
  root.append(node);
}

// Persisted alert history (active + resolved). Views render synchronously, so the
// history is fetched lazily and the list is swapped in once it arrives — the same
// pattern the admin users card uses.
function insightsHistoryCard(state) {
  const listEl = el("div", { class: "rows" }, el("p", { class: "muted-text" }, "Loading history…"));
  const filter = el("select", { class: "full" },
    el("option", { value: "all" }, "All"),
    el("option", { value: "active" }, "Active only"),
    el("option", { value: "resolved" }, "Resolved only"));

  const historyRow = (a) => {
    const resolved = a.status === "resolved";
    const badge = el("span", { class: `badge ${sevClass(a.peak_severity || a.severity)}` },
      a.peak_severity || a.severity || "");
    const stateBadge = el("span", { class: `badge ${resolved ? "good" : "warn"}` },
      resolved ? "Resolved" : "Active");
    const when = resolved
      ? `${fmtDateTime(a.first_seen_at)} → resolved ${relAge(a.resolved_at)}`
      : `Since ${fmtDateTime(a.first_seen_at)} · last seen ${relAge(a.last_seen_at)}`;
    return el("div", { class: "card", style: "margin:0" },
      el("div", { class: "spread" },
        el("h3", { style: "margin:.1rem 0" }, a.title || a.alert_key || "Alert"),
        el("span", {}, stateBadge, " ", badge)),
      a.description ? el("p", { class: "muted-text", style: "margin:.2rem 0" }, a.description) : null,
      el("p", { class: "note", style: "margin:.2rem 0 0" }, when));
  };

  const refresh = async () => {
    listEl.replaceChildren(el("p", { class: "muted-text" }, "Loading history…"));
    try {
      const res = await state.actions.insightsHistory({ status: filter.value, limit: 100 });
      const alerts = (res && res.alerts) || [];
      if (!alerts.length) {
        listEl.replaceChildren(el("p", { class: "muted-text" },
          filter.value === "resolved"
            ? "No resolved insights yet."
            : "No insight history recorded yet."));
        return;
      }
      listEl.replaceChildren(...alerts.map(historyRow));
    } catch (err) {
      listEl.replaceChildren(el("p", { class: "auth-error" }, err.message || "Failed to load history"));
    }
  };
  filter.addEventListener("change", refresh);
  refresh();

  return el("div", { class: "card" },
    el("div", { class: "spread" },
      el("h2", {}, "Insights history"),
      el("label", { class: "field" }, el("span", { class: "field-label" }, "Show"), filter)),
    el("p", { class: "note" }, "Lifecycle of past and present alerts, including warnings that have since resolved."),
    listEl);
}

// ── DEVICE / ADMIN ───────────────────────────────────────────────────────────
// A full-width, native <details> collapsible section used to fold the
// Configuration and Admin panels away at the bottom of the page. `open` sets the
// default expanded state; children become the body.
function collapsible(title, open, ...children) {
  return el("details", { class: "collapsible-panel", open: open ? true : null },
    el("summary", { class: "collapsible-summary" }, title),
    el("div", { class: "collapsible-body" }, ...children));
}

function renderDevice(root, state) {
  const cfg = state.config || {};
  const fw = state.firmware || {};
  const node = el("div", {});
  node.append(viewHead("Device & admin", "Configuration, firmware and calibration"));
  const devNote = deviceContextNote(state, "These settings apply to");
  if (devNote) node.append(devNote);

  // ── Configuration form ──────────────────────────────────────────────────
  // General settings + per-scale calibration and temperature compensation (the
  // old standalone "Temperature compensation" panel is folded in here), plus a
  // Fit tool that writes its result into the compensation fields for review.
  const cfgInputs = {};
  const numInput = (key, isInt) => {
    const input = el("input", {
      type: "number", step: isInt ? "1" : "any",
      value: cfg[key] != null ? String(cfg[key]) : "",
    });
    cfgInputs[key] = { input, int: !!isInt };
    return input;
  };
  const fieldRow = (label, control) => el("div", { class: "form-row" }, el("label", {}, label), control);

  // Scales the device exposes. Per-scale calibration and temp-comp are backed by
  // dedicated columns for scales 1–2, so the selector is built from the hives the
  // device reports, capped to those two (fallback [1, 2] for a silent device).
  const reported = availableHives(state).filter((n) => n <= 2);
  const scales = reported.length ? reported : [1, 2];
  const scaleTag = (n) => {
    const label = hiveLabel(state, n);
    return label && label !== `Hive ${n}` ? ` · ${label}` : "";
  };

  // One offset/factor/tempco group per scale; the Scale selector shows one group
  // at a time so the section reads as a per-scale editor rather than a flat list.
  const scaleGroups = new Map();
  for (const n of scales) {
    scaleGroups.set(n, el("div", { class: "scale-fields" },
      fieldRow("Offset", numInput(`scale${n}_offset`, true)),
      fieldRow("Factor", numInput(`scale${n}_factor`)),
      fieldRow("Tempco coefficient (kg/°C)", numInput(`scale${n}_tempco_kg_per_c`))));
  }
  const scaleSelect = el("select", { class: "full" },
    ...scales.map((n) => el("option", { value: String(n) }, `Scale ${n}${scaleTag(n)}`)));
  const showScale = () => {
    const sel = Number(scaleSelect.value);
    for (const [n, g] of scaleGroups) g.hidden = n !== sel;
  };
  scaleSelect.addEventListener("change", showScale);

  const tcEnabled = el("input", { type: "checkbox" });
  tcEnabled.checked = !!cfg.tempco_enabled;
  const tcSource = el("select", { class: "full" },
    ...["ambient", "hive_1", "hive_2"].map((v) =>
      el("option", { value: v, selected: cfg.tempco_source === v ? true : null }, v)));

  // Fit tool, scoped to the selected scale: it regresses stored weight against
  // temperature and writes the coefficient + reference temp straight into the
  // fields above (apply:false) so they can be reviewed before Save.
  const lookbackInput = el("input", { type: "number", value: "14", min: "1", max: "90" });
  const fitBtn = el("button", { class: "btn ghost", type: "button" }, "Fit coefficient from data");
  const fitOut = el("p", { class: "note" });
  fitBtn.addEventListener("click", async () => {
    const n = Number(scaleSelect.value);
    fitBtn.disabled = true; fitOut.textContent = "Fitting…";
    try {
      const r = await state.actions.fitTempComp({ scale: n, lookback_days: Number(lookbackInput.value) || 14, apply: false });
      if (!r || !r.ok) { fitOut.textContent = `Fit failed: ${(r && r.reason) || "insufficient data"}`; return; }
      const coeff = cfgInputs[`scale${n}_tempco_kg_per_c`];
      if (coeff) coeff.input.value = String(r.coeff_kg_per_c);
      if (cfgInputs.tempco_ref_temp_c) cfgInputs.tempco_ref_temp_c.input.value = String(r.ref_temp_c);
      tcEnabled.checked = true;
      if (r.temp_source) tcSource.value = r.temp_source;
      scaleSelect.value = String(n); showScale();
      fitOut.textContent = `Filled Scale ${n}: coeff ${fmt(r.coeff_kg_per_c, 5)} kg/°C, ref ${fmt(r.ref_temp_c, 1)} °C, R² ${fmt(r.r_squared, 3)} — review and Save.`;
    } catch (err) { fitOut.textContent = ""; state.toast(err.message, "error"); }
    finally { fitBtn.disabled = false; }
  });

  const cfgSaveBtn = el("button", { class: "btn", type: "submit" }, "Save configuration");
  const metaRow = (k, v) => el("div", { class: "row" }, el("span", { class: "k" }, k), el("span", { class: "v" }, v));
  const cfgForm = el("form", {},
    el("div", { class: "config-grid" },
      el("div", { class: "config-block" },
        el("h3", {}, "General"),
        el("div", { class: "rows" },
          metaRow("Device ID", state.device?.device_id || DASH),
          metaRow("Config version", cfg.config_version ?? DASH)),
        fieldRow("Send interval (s)", numInput("send_interval_seconds", true))),
      el("div", { class: "config-block" },
        el("h3", {}, "Scale calibration & compensation"),
        fieldRow("Scale", scaleSelect),
        ...scaleGroups.values(),
        el("div", { class: "form-row" }, el("label", {}, el("span", {}, "Enable temperature compensation "), tcEnabled)),
        fieldRow("Tempco source", tcSource),
        fieldRow("Tempco ref temp (°C)", numInput("tempco_ref_temp_c")),
        el("div", { class: "fit-row" },
          fieldRow("Fit lookback (days)", lookbackInput),
          el("div", { class: "form-actions" }, fitBtn)),
        fitOut)),
    el("p", { class: "note" }, "Saving bumps the config version; the device applies it on its next check-in."),
    el("div", { class: "form-actions" }, cfgSaveBtn));
  showScale();

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
  const chForm = chInputs.length
    ? el("form", {},
        ...chInputs.map(({ n, input }) =>
          el("div", { class: "form-row" }, el("label", {}, `Hive ${n} name`), input)),
        el("p", { class: "note" }, "Shown as the hive labels across every chart and card."),
        el("div", { class: "form-actions" }, chBtn))
    : el("p", { class: "muted-text" },
        "No hives reported yet — names can be set once the device sends its first reading.");
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
      el("div", { class: "row" }, el("span", { class: "k" }, "Approved"), el("span", { class: "v" }, fw.approved_version || DASH)),
      el("div", { class: "row" }, el("span", { class: "k" }, "Board"), el("span", { class: "v" }, fw.device_board || DASH))));

  // Explain a wrong-board upload: releases exist for a board other than the one
  // this device reports, so they are (correctly) not offered here. Without this
  // note such an upload looks like it silently vanished.
  if (Array.isArray(fw.other_board_releases) && fw.other_board_releases.length) {
    const others = fw.other_board_releases.map((r) => `${r.board} ${r.version}`).join(", ");
    const forBoard = fw.device_board ? ` this ${fw.device_board} device` : " this device";
    fwPanel.append(el("p", { class: "note warn" },
      `Also uploaded for another board (not applied to${forBoard}): ${others}. ` +
      "A build only reaches a device whose board matches — check the Board field when uploading."));
  }

  if (fw.update_available && fw.pending_approval) {
    const approveBtn = el("button", { class: "btn", type: "button" }, "Approve & flash latest");
    approveBtn.addEventListener("click", async () => {
      const version = fw.latest_version ? ` ${fw.latest_version}` : "";
      if (!window.confirm(
        `Approve firmware${version} and flash it over the air?\n\n` +
        "The device installs it on its next check-in and reboots. " +
        "A remote device that fails mid-update may need physical access to recover.")) return;
      approveBtn.disabled = true;
      try { await state.actions.approveFirmware(); state.toast("Firmware approved — device will update on next check-in", "success"); state.reload(); }
      catch (e) { state.toast(e.message, "error"); approveBtn.disabled = false; }
    });
    fwPanel.append(el("div", { class: "form-actions" }, approveBtn));
  }

  // Firmware upload form. The main-unit ("hivescale") target ships for two
  // boards, and the server refuses a release whose board it cannot determine
  // from this field or a board-stamped filename — so the board is a select of
  // the valid values, shown only for that target.
  const fileInput = el("input", { type: "file", accept: ".bin", required: true });
  const versionInput = el("input", { type: "text", placeholder: "e.g. 0.21.0", required: true });
  // Selecting/dropping a board-stamped .bin pre-fills the version from its
  // filename (hivehub_esp32_0.21.0.bin → 0.21.0), so the operator rarely has to
  // retype it. Only fills when we detect a version — an unrecognized name leaves
  // whatever was typed intact rather than clearing it.
  fileInput.addEventListener("change", () => {
    const detected = versionFromFilename(fileInput.files[0] && fileInput.files[0].name);
    if (detected) versionInput.value = detected;
  });
  const targetSelect = el("select", { class: "full" },
    el("option", { value: "hivescale" }, "Main unit (HiveHub / HiveScale)"),
    el("option", { value: "hiveinside" }, "HiveInside"),
    el("option", { value: "beecounter" }, "BeeCounter"));
  // Board options depend on the target: the main unit ships two architectures
  // (Xtensa ESP32 vs RISC-V ESP32-C6), and HiveInside now ships two too — the
  // legacy ESP32-C6 prototype and the nRF54LM20A (signed Zephyr image). The
  // server refuses a release whose board disagrees with its filename, so a
  // cross-architecture image can never be published.
  const BOARDS_BY_TARGET = {
    hivescale: [
      ["", "Detect from filename (…_esp32_… / …_esp32-c6_…)"],
      ["esp32", "ESP32 (classic 30-pin)"],
      ["esp32-c6", "ESP32-C6"],
    ],
    hiveinside: [
      ["", "Detect from filename (…_esp32-c6_… / …_nrf54lm20a_…)"],
      ["esp32-c6", "Legacy HiveInside (ESP32-C6)"],
      ["nrf54lm20a", "HiveInside (nRF54LM20A)"],
    ],
  };
  const boardSelect = el("select", { class: "full" });
  const boardRow = el("div", { class: "form-row" }, el("label", {}, "Board"), boardSelect);
  const boardNote = el("p", { class: "note" });
  const BOARD_NOTES = {
    hivescale: "Main-unit firmware must state its board: pick one, or keep auto-detect when the file is named like hivehub_esp32_0.21.0.bin.",
    hiveinside: "HiveInside now has two boards — pick the one this image targets, or keep auto-detect when the file is named like hiveinside_nrf54lm20a_1.0.0.bin. A board-stamped release is only ever relayed to a matching sensor.",
  };
  const syncBoardRow = () => {
    const opts = BOARDS_BY_TARGET[targetSelect.value];
    boardRow.hidden = !opts;
    boardNote.hidden = !opts;
    if (!opts) return;
    boardSelect.innerHTML = "";
    opts.forEach(([v, label]) => boardSelect.appendChild(el("option", { value: v }, label)));
    boardNote.textContent = BOARD_NOTES[targetSelect.value];
  };
  targetSelect.addEventListener("change", syncBoardRow);
  const uploadBtn = el("button", { class: "btn", type: "submit" }, "Upload firmware");

  const uploadForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Firmware .bin"), fileInput),
    el("div", { class: "form-row" }, el("label", {}, "Version"), versionInput),
    el("div", { class: "form-row" }, el("label", {}, "Target"), targetSelect),
    boardRow,
    boardNote,
    el("p", { class: "note" }, "Uploading registers a new firmware release for this target. To install it, use the “Approve & flash” button in the Firmware panel — it appears there once the upload is a newer version than the device currently runs, and the device flashes on its next check-in."),
    el("div", { class: "form-actions" }, uploadBtn));
  syncBoardRow();
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("version", versionInput.value.trim());
    fd.append("target", targetSelect.value);
    if ((targetSelect.value === "hivescale" || targetSelect.value === "hiveinside") && boardSelect.value) {
      fd.append("board", boardSelect.value);
    }
    uploadBtn.disabled = true;
    try {
      const res = await state.actions.uploadFirmware(fd);
      // Surface the board the release actually registered under, so a
      // filename auto-detect that picked the wrong architecture is visible
      // immediately rather than after the release fails to appear as "latest".
      const parts = [res?.version, res?.target, res?.board].filter(Boolean).join(" / ");
      state.toast(parts ? `Firmware uploaded: ${parts}` : "Firmware uploaded", "success");
      state.reload();
    }
    catch (err) { state.toast(err.message, "error"); }
    finally { uploadBtn.disabled = false; }
  });
  const uploadCard = el("div", { class: "card" }, el("h2", {}, "Upload firmware"), uploadForm);

  // SD-card data import. Uploads a backup pulled off the scale in AP mode
  // (measurements.ndjson or the hivescale-sd-data.tar download) and back-fills the
  // readings the device could not deliver while offline. Re-uploading the same
  // file is safe — rows already stored are skipped by (device, timestamp).
  const sdFileInput = el("input", {
    type: "file",
    accept: ".ndjson,.tar,.json,application/x-tar,application/octet-stream",
    required: true,
  });
  const sdPicked = el("p", { class: "note", hidden: true });
  sdFileInput.addEventListener("change", () => {
    const f = sdFileInput.files[0];
    sdPicked.hidden = !f;
    if (f) sdPicked.textContent = `${f.name} (${(f.size / 1024).toFixed(0)} KB)`;
  });
  const sdBtn = el("button", { class: "btn", type: "submit" }, "Upload SD data");
  const sdResult = el("p", { class: "note", hidden: true });
  const sdForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "SD backup file"), sdFileInput),
    sdPicked,
    el("p", { class: "note" },
      "Accepts measurements.ndjson or the hivescale-sd-data.tar download. " +
      "Re-uploading the same file is safe — existing readings are skipped automatically."),
    el("div", { class: "form-actions" }, sdBtn),
    sdResult);
  // Post the picked file to a specific device (defaults to the selected one).
  function runSdImport(deviceId) {
    const fd = new FormData();
    fd.append("file", sdFileInput.files[0]);
    return state.actions.importSdData(fd, deviceId);
  }
  function renderSdResult(res) {
    const dupes = res.duplicates
      ? `, ${res.duplicates} duplicate${res.duplicates === 1 ? "" : "s"} skipped`
      : "";
    const unreadable = res.skipped
      ? ` · ${res.skipped} unreadable line${res.skipped === 1 ? "" : "s"} skipped`
      : "";
    const into = res.device_id ? ` into device “${res.device_id}”` : "";
    sdResult.hidden = false;
    sdResult.textContent =
      `Imported ${res.inserted} new reading${res.inserted === 1 ? "" : "s"}${into}${dupes}. ` +
      `Parsed ${res.parsed} record${res.parsed === 1 ? "" : "s"}${unreadable}.`;
    state.toast(`Imported ${res.inserted} new reading${res.inserted === 1 ? "" : "s"}${into}`, "success");
    sdForm.reset();
    sdPicked.hidden = true;
    state.reload();
  }
  sdForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!sdFileInput.files[0]) return;
    sdBtn.disabled = true;
    const restore = sdBtn.textContent;
    sdBtn.textContent = "Importing…";
    try {
      let res;
      try {
        res = await runSdImport();               // upload to the selected device
      } catch (err) {
        // The card was recorded by a different device than the one selected.
        // Offer to send the readings to their true source device instead of
        // mis-attaching them here.
        if (err.status === 409 && err.detail && err.detail.code === "device_mismatch") {
          const sources = err.detail.file_device_ids || [];
          const target = err.detail.target_device_id;
          if (sources.length !== 1) {
            // A single file holding several devices' data can't be auto-routed.
            state.toast(err.detail.message || err.message, "error");
            return;
          }
          const source = sources[0];
          const ok = window.confirm(
            `This SD-card data is from device “${source}”, but you are uploading ` +
            `it to device “${target}”.\n\n` +
            `Upload the data to its source device “${source}” instead?\n\n` +
            `OK — import into “${source}”      Cancel — stop`
          );
          if (!ok) { state.toast("SD import cancelled", "error"); return; }
          sdBtn.textContent = "Importing…";
          try {
            res = await runSdImport(source);       // re-post to the correct device
          } catch (err2) {
            state.toast(`Could not import into “${source}”: ${err2.message}`, "error");
            return;
          }
        } else {
          throw err;
        }
      }
      renderSdResult(res);
    }
    catch (err) { state.toast(err.message, "error"); }
    finally { sdBtn.disabled = false; sdBtn.textContent = restore; }
  });
  const sdCard = el("div", { class: "card" }, el("h2", {}, "Import SD card data"), sdForm);

  // Calibration
  const calBadge = el("span", { class: `badge ${state.latest?.calibration_mode ? "warn" : "muted"}` },
    state.latest?.calibration_mode ? "Calibration mode active" : "Normal mode");
  const startBtn = el("button", { class: "btn", type: "button" }, "Start calibration mode");
  const stopBtn = el("button", { class: "btn ghost", type: "button" }, "Stop calibration mode");
  startBtn.addEventListener("click", async () => {
    if (!window.confirm(
      "Start calibration mode?\n\n" +
      "The device switches to frequent load-cell sampling and stays in this mode " +
      "until you stop it — normal readings are affected while it is active.")) return;
    startBtn.disabled = true;
    try { await state.actions.startCalibration({}); state.toast("Calibration mode requested", "success"); }
    catch (e) { state.toast(e.message, "error"); } finally { startBtn.disabled = false; }
  });
  stopBtn.addEventListener("click", async () => {
    if (!window.confirm("Stop calibration mode and return the device to normal measuring?")) return;
    stopBtn.disabled = true;
    try { await state.actions.stopCalibration(); state.toast("Stop calibration requested", "success"); }
    catch (e) { state.toast(e.message, "error"); } finally { stopBtn.disabled = false; }
  });
  const calCard = el("div", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Calibration"), calBadge),
    el("p", { class: "note" }, "Calibration mode samples the load cell more frequently so you can place known weights and fit a temperature coefficient."),
    el("div", { class: "form-actions" }, startBtn, stopBtn));

  // ── Layout ────────────────────────────────────────────────────────────────
  // Top: five always-visible panels balanced across the three columns so none is
  // left empty — Calibration + Firmware · Hive names + Import SD card data ·
  // Upload firmware. Each panel sizes to its own content rather than stretching.
  // Below: a full-width collapsible "Configuration" (general + per-scale
  // calibration/compensation + fit), and at the very bottom a full-width
  // collapsible "Admin" grouping the account and admin-only management panels.
  const isAdmin = state.authUser?.role === "admin";

  const topGrid = el("div", { class: "admin-cols" },
    el("div", { class: "admin-col" }, calCard, fwPanel),
    el("div", { class: "admin-col" }, channelsCard, sdCard),
    el("div", { class: "admin-col" }, uploadCard));

  const adminCards = [accountCard(state)];
  if (isAdmin) adminCards.push(usersCard(state), visibleDevicesCard(state), deleteMeasurementsCard(state));

  node.append(
    topGrid,
    collapsible("Configuration", true, cfgForm),
    collapsible("Admin", false, el("div", { class: "admin-cols" }, ...adminCards)));
  root.append(node);
}

// "Visible devices" (admin only): retire a decommissioned device from the
// top-bar hive picker without deleting its history, or bring one back. Toggling
// a checkbox persists the flag and repaints the picker (see app.js
// setDeviceVisibility, wired through state.actions.setDeviceVisibility).
function visibleDevicesCard(state) {
  const listEl = el("div", { class: "rows" });
  const devices = state.devices || [];
  if (!devices.length) {
    listEl.append(el("p", { class: "muted-text" }, "No devices on this server."));
  } else {
    for (const d of devices) {
      const label = d.display_name ? `${d.display_name} · ${d.device_id}` : d.device_id;
      const cb = el("input", { type: "checkbox" });
      cb.checked = !d.hidden;
      cb.addEventListener("change", async () => {
        const hide = !cb.checked;
        cb.disabled = true;
        try {
          await state.actions.setDeviceVisibility(d.device_id, hide);
          state.toast(hide ? `${label} hidden from picker` : `${label} shown in picker`, "success");
          // setDeviceVisibility repaints the whole view, so this node is replaced.
        } catch (err) {
          cb.checked = !hide;   // revert on failure
          cb.disabled = false;
          state.toast(err.message, "error");
        }
      });
      listEl.append(el("label", { class: "row" },
        el("span", { class: "k" }, label,
          d.hidden ? el("span", { class: "badge muted", style: "margin-left:.5rem" }, "Hidden") : null),
        el("span", { class: "v" }, cb)));
    }
  }
  return el("div", { class: "card" }, el("h2", {}, "Visible devices"),
    el("p", { class: "note" },
      "Uncheck a retired device to remove it from the hive picker at the top of the page. " +
      "Its readings are kept and it can be shown again at any time."),
    listEl);
}

// "Delete readings" (admin only): remove a time range of measurements for the
// active device. Devices connect and upload on boot before they know their
// calibration, producing large spikes that swamp the charts; this prunes them.
// Destructive, so the server also requires the device's claim code (see
// local_delete_measurements) — a second factor on top of the admin session.
function deleteMeasurementsCard(state) {
  const dev = state.device;
  const deviceLabel = dev ? (dev.display_name || dev.device_id) : "—";
  const startInput = el("input", { type: "datetime-local" });
  const endInput = el("input", { type: "datetime-local" });
  const codeInput = el("input", { type: "text", autocomplete: "off", placeholder: "e.g. ABCD-1234" });
  const out = el("p", { class: "note", hidden: true });
  const btn = el("button", { class: "btn danger", type: "submit" }, "Delete readings");
  const form = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "From"), startInput),
    el("div", { class: "form-row" }, el("label", {}, "To"), endInput),
    el("div", { class: "form-row" }, el("label", {}, "Device claim code"), codeInput),
    el("div", { class: "form-actions" }, btn),
    out);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!dev) { state.toast("No device selected", "error"); return; }
    if (!startInput.value || !endInput.value) { state.toast("Choose a start and end time", "error"); return; }
    const start = new Date(startInput.value), end = new Date(endInput.value);
    if (end < start) { state.toast("End time must be at or after the start time", "error"); return; }
    if (!codeInput.value.trim()) { state.toast("Enter the device's claim code to confirm", "error"); return; }
    if (!window.confirm(
      `Permanently delete all readings for “${deviceLabel}” between\n` +
      `${start.toLocaleString()} and ${end.toLocaleString()}?\n\nThis cannot be undone.`)) return;
    btn.disabled = true;
    try {
      const res = await state.actions.deleteMeasurements(dev.device_id, {
        start_at: start.toISOString(),
        end_at: end.toISOString(),
        claim_code: codeInput.value.trim(),
      });
      const n = res?.deleted ?? 0;
      out.hidden = false;
      out.textContent = `Deleted ${n} reading${n === 1 ? "" : "s"} for “${deviceLabel}”.`;
      state.toast(`Deleted ${n} reading${n === 1 ? "" : "s"}`, "success");
      codeInput.value = "";
      state.reload();
    } catch (err) { state.toast(err.message, "error"); }
    finally { btn.disabled = false; }
  });
  return el("div", { class: "card" }, el("h2", {}, "Delete readings"),
    el("p", { class: "note" },
      `Remove a range of readings for “${deviceLabel}” — useful for clearing the ` +
      "spikes a device sends on boot, before it knows its calibration. " +
      "Enter the device's claim code to authorise the deletion."),
    form);
}

// ── dashboard account cards ──────────────────────────────────────────────────
// "Your account": let the logged-in user change their own password.
function accountCard(state) {
  const u = state.authUser || {};

  // Contact email — where insights-based alerts will be sent once notifications
  // are wired up. Optional; can be cleared by saving an empty field.
  const emailInput = el("input", { type: "email", autocomplete: "email", value: u.email || "", placeholder: "you@example.com" });
  const emailBtn = el("button", { class: "btn", type: "submit" }, "Save email");
  const emailForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Alert email"), emailInput),
    el("p", { class: "note" }, "Used to notify you about colony insights (swarm, robbing, winter risk…). Leave blank to receive none."),
    el("div", { class: "form-actions" }, emailBtn));
  emailForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    emailBtn.disabled = true;
    try {
      const r = await state.actions.updateEmail(emailInput.value.trim());
      // Reflect the server-normalised value back into the session + field.
      state.authUser.email = r && "email" in r ? r.email : emailInput.value.trim() || null;
      emailInput.value = state.authUser.email || "";
      state.toast("Email saved", "success");
    } catch (err) { state.toast(err.message, "error"); }
    finally { emailBtn.disabled = false; }
  });

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
  return el("div", { class: "card" }, el("h2", {}, "Your account"),
    emailForm,
    el("h3", { style: "margin:.8rem 0 .2rem" }, "Change password"),
    form);
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
          if (!window.confirm(`Remove the account "${usr.username}" (${usr.role})?\n\nThis cannot be undone; they are signed out and lose dashboard access.`)) return;
          del.disabled = true;
          try { await state.actions.deleteUser(usr.id); state.toast("User removed", "success"); refresh(); }
          catch (err) { state.toast(err.message, "error"); del.disabled = false; }
        });
        const label = usr.email ? `${usr.username} · ${usr.role} · ${usr.email}` : `${usr.username} · ${usr.role}`;
        return el("div", { class: "row" },
          el("span", { class: "k" }, label),
          el("span", { class: "v" }, del));
      }));
      if (!users.length) listEl.replaceChildren(el("p", { class: "muted-text" }, "No users yet."));
    } catch (err) {
      listEl.replaceChildren(el("p", { class: "auth-error" }, err.message || "Failed to load users"));
    }
  };

  const nu = el("input", { type: "text", autocomplete: "off", placeholder: "username" });
  const ne = el("input", { type: "email", autocomplete: "off", placeholder: "email (optional)" });
  const np = el("input", { type: "password", autocomplete: "new-password", placeholder: "password (min 8)" });
  const nr = el("select", { class: "full" },
    el("option", { value: "viewer" }, "Viewer (read-only)"),
    el("option", { value: "admin" }, "Admin (full control)"));
  const addBtn = el("button", { class: "btn", type: "submit" }, "Add user");
  const addForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Username"), nu),
    el("div", { class: "form-row" }, el("label", {}, "Email"), ne),
    el("div", { class: "form-row" }, el("label", {}, "Password"), np),
    el("div", { class: "form-row" }, el("label", {}, "Role"), nr),
    el("div", { class: "form-actions" }, addBtn));
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (np.value.length < 8) { state.toast("Password must be at least 8 characters", "error"); return; }
    if (nu.value.trim().length < 3) { state.toast("Username must be at least 3 characters", "error"); return; }
    addBtn.disabled = true;
    try {
      await state.actions.createUser(nu.value.trim(), np.value, nr.value, ne.value.trim() || null);
      state.toast("User created", "success");
      nu.value = np.value = ne.value = "";
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
