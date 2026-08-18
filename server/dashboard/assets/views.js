// View renderers, one per sidebar data group. Each group exposes
// { id, label, icon, render(root, state) }. `state` carries the loaded data and
// the admin action callbacks (see app.js buildState()).

import { el, fmt, fmtInt, isNum, relAge, latestOf, sevClass, fmtDateTime, DASH } from "./format.js";
import { drawLineChart, drawSpectrumChart, seriesFrom, dailyMaxSeries, valueAt, PALETTE, bandColor, CONTEXT_COLOR, withAlpha } from "./charts.js";
import { isSupported as pushSupported, isSubscribed as pushIsSubscribed, subscribe as pushSubscribe, unsubscribe as pushUnsubscribe } from "./push.js";

// Pull the firmware version out of a build artifact's filename so the upload
// form can pre-fill the Version field. Two naming schemes are in play:
//
//   * rename_firmware.py (hub, counter) — "<prefix>_<board>_<version>.bin",
//     e.g. hivehub_esp32_0.21.0.bin, hivetraffic_esp32-c6_0.3.1.bin: the version
//     is the trailing dotted token, optionally with a "-rc1"-style suffix.
//   * HiveInside's Zephyr build — "hiveinside-<board>-v<version>-<variant>.signed.bin",
//     e.g. hiveinside-nrf54lm20a-v0.4.7-lowpower.signed.bin: the version sits in
//     the middle, behind a "v", with the build variant and ".signed" after it.
//
// The v-stamped token is tried first because the trailing-token rule would drag
// "-lowpower.signed" into the version on a HiveInside artifact. Requiring a dot
// means the board tokens (esp32 / c6) are never mistaken for a version; returns
// "" when no version-looking token is present.
function versionFromFilename(name) {
  const base = (name || "").replace(/\.[^.]*$/, "");
  const stamped = base.match(/[-_]v(\d+\.\d+(?:\.\d+)*)(?=[-_.]|$)/i);
  if (stamped) return stamped[1];
  const m = base.match(/(\d+\.\d+(?:\.\d+)*(?:[-_][0-9A-Za-z.]+)?)$/);
  return m ? m[1] : "";
}

// Pick the upload target out of that same filename, so selecting a HiveInside or
// HiveTraffic image does not leave Target on the default main unit — an upload
// with the wrong target is rejected by the server (the filename's board token is
// not valid for it), and worse, a mis-targeted release that IS accepted would be
// offered to the wrong hardware. Keyed off the product token every build puts at
// the front of its artifact name: hiveinside-…, hivetraffic_…, hivehub_… /
// hivescale_…. Returns "" for an unrecognized name, which leaves whatever the
// operator picked alone.
const TARGET_FILENAME_HINTS = [
  [/hiveinside/, "hiveinside"],
  [/hivetraffic|beecounter/, "beecounter"],
  [/hivehub|hivescale/, "hivescale"],
];
function targetFromFilename(name) {
  const n = (name || "").toLowerCase();
  for (const [pattern, target] of TARGET_FILENAME_HINTS) {
    if (pattern.test(n)) return target;
  }
  return "";
}

// True when dotted version `a` is strictly newer than `b`, compared component by
// component as NUMBERS — "0.10.0" is newer than "0.9.9", which a string compare
// gets backwards. Mirrors server/firmware.py parse_version: non-digits are
// stripped per component and a missing component counts as 0, so "0.4" < "0.4.1".
function versionIsNewer(a, b) {
  const parse = (v) => String(v || "").split(".").map((p) => parseInt(p.replace(/\D/g, ""), 10) || 0);
  const x = parse(a), y = parse(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d) return d > 0;
  }
  return false;
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
    // A series may carry its own digits/unit (e.g. a secondary-axis voltage line
    // reads in V while the primary SoC line reads in %); fall back to the chart's.
    const digits = series.digits ?? c.opts.yDigits ?? 1;
    const unit = series.unit != null ? series.unit : c.opts.unit;
    valueEl.textContent = p ? `: ${fmt(p.y, digits, unit ? " " + unit : "")}` : ": " + DASH;
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
  // Keyboard scrubbing: the cursor readout is otherwise pointer-only. Arrow
  // keys step through the range (Shift = fine step), Home/End jump to the
  // edges, Escape clears. tabIndex makes the canvas reachable by Tab.
  canvas.tabIndex = 0;
  canvas.addEventListener("keydown", (e) => {
    const scale = canvas._xScale;
    if (!scale) return;
    const span = scale.tMax - scale.tMin;
    const step = span * (e.shiftKey ? 0.002 : 0.02);
    let t = cursorT;
    if (e.key === "ArrowLeft") t = (t == null ? scale.tMax : t) - step;
    else if (e.key === "ArrowRight") t = (t == null ? scale.tMax : t) + step;
    else if (e.key === "Home") t = scale.tMin;
    else if (e.key === "End") t = scale.tMax;
    else if (e.key === "Escape") t = null;
    else return;
    e.preventDefault();
    cursorT = t == null ? null : Math.min(scale.tMax, Math.max(scale.tMin, t));
    drawCharts();
  });
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
  // role=img + aria-label: canvas content is invisible to screen readers, so at
  // least announce what the chart shows instead of leaving an unnamed blob.
  // The label also advertises the keyboard scrubbing attachChartCursor adds.
  const canvas = el("canvas", {
    role: "img",
    "aria-label": `${title} chart — focus and use arrow keys to inspect values`,
  });
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
  // opts.hiveColor: a chart whose series are measures rather than hives (the
  // band charts) carries the hive's identity in its heading instead, with the
  // same swatch colour its chip has in the top bar.
  return el("div", { class: "card chart-card" },
    el("h2", {},
      opts.hiveColor ? el("span", { class: "hive-swatch", style: `background:${opts.hiveColor}` }) : null,
      title),
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
  const canvas = el("canvas", { role: "img", "aria-label": `${title} chart` });
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

// A folded-away block of charts (progressive disclosure: the expert view sits
// under the everyday one). A canvas inside a closed <details> has no layout, so
// it would draw at the fallback width and keep it — redraw once it opens.
function foldedCharts(title, ...cards) {
  const panel = collapsible(title, false, el("div", { class: "grid wide" }, ...cards));
  panel.addEventListener("toggle", () => { if (panel.open) requestAnimationFrame(drawCharts); });
  return panel;
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

// The five microphone FFT bands, low → high. This is an ORDERED scale, not a set
// of independent categories: swapping two entries would change what the chart
// says. So the bands are drawn in the ordinal ramp (bandColor) and the hive keeps
// the categorical palette — see the note above renderFrequency. The Hz ranges
// mirror firmware/include/mics.h and server/insights.py; keep them in sync. They
// are carried in the series label because "Piping" alone means nothing to a
// beekeeper who has not read the docs, while "Piping · 300–550 Hz" teaches the
// band every time the legend is read. The short label is used on its own for the
// spectrum chart's x-axis, where there is no room for the range.
const BANDS = [
  ["sub_bass", "Sub-bass", "50–150 Hz"],
  ["hum", "Hum", "150–300 Hz"],
  ["piping", "Piping", "300–550 Hz"],
  ["stress", "Stress", "550–1500 Hz"],
  ["high", "High", "1.5–3 kHz"],
];
const bandLabel = ([, label, hz]) => `${label} · ${hz}`;

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

// Which dimension gets a colour and which gets its own chart is the same
// decision on this page and on Vibration, and both answer it the same way: the
// bands share a unit and are read against each other at a moment ("is piping
// high relative to hum right now?"), which one shared chart does best, while
// hives are compared across a whole range, which separate charts do best. So
// every band of one hive goes on one chart, and each hive gets its own — the
// hive's identity riding on the chart title and its palette dot rather than on
// the series colours. The spectrum profile answers a different question (the
// shape of the sound, rather than its history) and is kept below, folded away.
function bandTimeChart(state, ref, hiveColor) {
  const series = BANDS.map((band, i) =>
    seriesCoalesce(ref.measurements, micKeys(ref.hive, `band_${band[0]}_dbfs`),
      bandLabel(band), bandColor(i, BANDS.length)));
  if (!series.some((s) => s.points.length)) return null;
  return chartCard(`Frequency bands — ${refLabel(state, ref)}`,
    "Loudness in each band over the selected range. Higher is louder; bands run low to high in one colour. Click a band in the legend to hide it.",
    series, { unit: "dBFS", yDigits: 0, hiveColor });
}

function renderFrequency(root, state) {
  const refs = selectedRefs(state);
  const categories = BANDS.map(([, label]) => label);
  const charts = [];
  let micCharts = 0;
  // Time first: the question a beekeeper actually arrives with is "has anything
  // changed this week", and only a time axis answers it.
  refs.forEach((ref, i) => {
    const chart = bandTimeChart(state, ref, paletteColor(i));
    if (chart) { micCharts++; charts.push(chart); }
  });
  if (!micCharts) {
    charts.push(el("div", { class: "card" }, el("p", { class: "muted-text" }, "No frequency-band data reported by this device.")));
  }

  // The spectrum profile answers the other question — the shape of the sound
  // right now, rather than how it got there — so it stays on the page, folded
  // below the time charts. It encodes age as fadedness, which carries no scale a
  // reader can decode, and that is exactly why it is the second view: a rising
  // band or a one-day spike is invisible in it.
  const profile = [];
  refs.forEach((ref, i) => {
    const keysList = BANDS.map(([key]) => micKeys(ref.hive, `band_${key}_dbfs`));
    const snapshots = spectrumSnapshots(ref.measurements, keysList);
    if (!snapshots.length) return;
    const bandStats = bandMinMax(ref.measurements, keysList);
    profile.push(spectrumChartCard(`Spectrum profile — ${refLabel(state, ref)}`,
      "FFT energy by band, like a spectrum analyzer — the bold line is the latest reading, fainter lines are earlier ones",
      categories, snapshots, bandStats, paletteColor(i), { unit: "dBFS", yDigits: 0 }));
  });

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
        profile.push(spectrumChartCard(`HiveHeart spectrum — ${refLabel(state, ref)}`,
          "HiveHeart in-hive FFT — 16 frequency ranges (Hz), relative level 0–15 (not dBFS); bold line is the latest reading, fainter lines are earlier ones. The dashed pink marker is HiveHeart's reported peak frequency; the Sub-bass/Hum/Piping/Stress headings are approximate and span several ranges",
          HIVEHEART_FFT_LABELS, snaps, bandStats, paletteColor(i), opts));
      } else {
        profile.push(el("div", { class: "card" },
          el("h2", {}, `HiveHeart spectrum — ${refLabel(state, ref)}`),
          el("p", { class: "muted-text" }, "No HiveHeart FFT data for this hive.")));
      }
    });
  }

  const node = tsView("Frequency bands",
    "Microphone FFT energy per band, in dBFS — louder is higher up the axis", state, { charts });
  if (profile.length) node.append(foldedCharts("Spectrum profile", ...profile));
  root.append(node);
}

// ── VIBRATION / ACCELERATION ─────────────────────────────────────────────────
// In-hive vibration from the paired BLE sensor, all values in milli-g and all AC
// (gravity removed). A HiveInside node computes the three bands plus broadband
// RMS/peak on-device; a passive HolyIot/RuuviTag beacon can only send a
// single-shot magnitude, so it fills rms/peak and leaves the bands null (see
// docs/accelerometer.md).
//
// Laid out exactly like the Frequency bands page, and for the same reason: the
// bands share a unit and are read against each other, so they share a chart in
// the ordinal ramp, and each hive gets its own chart tagged with its palette
// colour. The two acoustic pages are read one after the other — the vibration
// bands pick up below 50 Hz where the microphones stop — so they should not need
// two different reading habits.
//
// Ordered low → high, like the microphone bands. Ranges mirror
// server/insights.py and docs/accelerometer.md.
const ACCEL_BANDS = [
  { key: "band_swarm_mg", label: "Pre-swarm", hz: "8–30 Hz" },
  { key: "band_fanning_mg", label: "Fanning", hz: "30–100 Hz" },
  { key: "band_activity_mg", label: "Activity", hz: "100–200 Hz" },
];

// Raw per-axis values a HolyIot / RuuviTag beacon reports next to its magnitude.
// Unlike the accel_* bands these are absolute (gravity included), so they read
// as orientation rather than vibration.
const ACCEL_AXES = [["x", "X"], ["y", "Y"], ["z", "Z"]];

// Latest reported accel_{hive}_{suffix} for a ref: the standalone latest reading
// first, then the loaded history newest→oldest, so a hive whose sensor was not
// heard during the last cycle still shows its last known value.
function accelLatest(ref, suffix) {
  const key = `accel_${ref.hive}_${suffix}`;
  if (ref.latest && ref.latest[key] != null) return ref.latest[key];
  return latestOf(ref.measurements, key);
}

// Per-hive sensor health and capture settings, for the hives that report them.
// Returns null when no selected hive has any diagnostics, so the card is simply
// left out instead of rendering an empty shell.
function accelDiagnosticsCard(state, refs) {
  const rows = [];
  for (const ref of refs) {
    const ok = accelLatest(ref, "ok");
    const rate = accelLatest(ref, "sample_rate_hz");
    const count = accelLatest(ref, "sample_count");
    const range = accelLatest(ref, "range_g");
    if (ok == null && rate == null && count == null && range == null) continue;
    const bits = [];
    if (isNum(rate)) bits.push(`${fmtInt(rate)} Hz`);
    if (isNum(count)) bits.push(`${fmtInt(count)} samples`);
    if (isNum(range)) bits.push(`±${fmtInt(range)} g`);
    rows.push([refLabel(state, ref), el("span", {},
      ok == null ? null : el("span", { class: `badge ${ok ? "good" : "danger"}` }, ok ? "OK" : "Fault"),
      bits.length ? ` ${bits.join(" · ")}` : null)]);
  }
  return rows.length ? rowsCard("Vibration sensor", rows) : null;
}

// One hive's vibration chart: the three bands in the ordinal ramp plus the
// broadband RMS behind them in grey, because RMS is the total those bands are
// part of — context, not a fourth band. A hive on a passive beacon has no bands
// at all, so there RMS becomes the subject and takes a ramp colour instead of
// the grey. Returns null when the hive has reported nothing.
function vibrationChart(state, ref, hiveColor) {
  const bands = ACCEL_BANDS.map((band, i) =>
    seriesFrom(ref.measurements, `accel_${ref.hive}_${band.key}`,
      `${band.label} · ${band.hz}`, bandColor(i, ACCEL_BANDS.length)));
  const hasBands = bands.some((s) => s.points.length);
  const rms = seriesFrom(ref.measurements, `accel_${ref.hive}_rms_mg`,
    "Broadband RMS", hasBands ? CONTEXT_COLOR : bandColor(0, 1));
  const series = hasBands ? [...bands, rms] : [rms];
  if (!series.some((s) => s.points.length)) return null;
  return chartCard(`Vibration — ${refLabel(state, ref)}`,
    hasBands
      ? "Vibration strength in each band over the selected range. Bands run low to high in one colour; the grey line is the broadband total. Click a band in the legend to hide it."
      : "Broadband vibration strength over the selected range. This sensor reports no frequency bands — see the note above.",
    series, { unit: "mg", yDigits: 1, hiveColor });
}

function renderVibration(root, state) {
  const refs = selectedRefs(state);
  const hasBands = refs.some((ref) =>
    ACCEL_BANDS.some((band) => latestOf(ref.measurements, `accel_${ref.hive}_${band.key}`) != null));

  const cards = [];
  for (const ref of refs) {
    const swarm = accelLatest(ref, "band_swarm_mg");
    const rms = accelLatest(ref, "rms_mg");
    const peak = accelLatest(ref, "peak_mg");
    if (!isNum(swarm) && !isNum(rms) && !isNum(peak)) continue;
    // Headline the pre-swarm band where the sensor reports bands at all; a
    // passive beacon sends only a magnitude, so there the broadband RMS is the
    // most meaningful number to lead with.
    cards.push(isNum(swarm)
      ? metricCard(`${refLabel(state, ref)} swarm band`, fmt(swarm, 1), "mg",
          isNum(rms) ? `Broadband ${fmt(rms, 1)} mg` : "8–30 Hz")
      : metricCard(`${refLabel(state, ref)} vibration`, fmt(rms, 1), "mg",
          isNum(peak) ? `Peak ${fmt(peak, 1)} mg` : "Broadband RMS"));
  }
  const diagnostics = accelDiagnosticsCard(state, refs);
  if (diagnostics) cards.push(diagnostics);

  const charts = [];
  refs.forEach((ref, i) => {
    const chart = vibrationChart(state, ref, paletteColor(i));
    if (chart) charts.push(chart);
  });
  // Raw axes, per hive — only for the beacons that send them, and folded away
  // below: they are absolute values including gravity, on a different scale to
  // the AC bands, and they answer a different question (has the hive moved).
  // X/Y/Z is a nominal set, not an ordered one, so it keeps categorical colours.
  const axisCharts = [];
  for (const ref of refs) {
    const axes = ACCEL_AXES.map(([axis, label], i) =>
      seriesFrom(ref.measurements, `ble_${ref.hive}_accel_${axis}_mg`, `${label} axis`, paletteColor(i)));
    if (axes.some((s) => s.points.length)) {
      axisCharts.push(chartCard(`Raw acceleration axes — ${refLabel(state, ref)}`,
        "Absolute per-axis reading from the BLE sensor, gravity included — a lasting shift means the hive or the sensor moved",
        axes, { unit: "mg", yDigits: 0 }));
    }
  }

  if (!charts.length && !axisCharts.length) {
    charts.push(el("div", { class: "card" },
      el("p", { class: "muted-text" },
        "No vibration data for the selected hives. In-hive vibration comes from a paired BLE sensor: "
        + "a HiveInside node reports the three frequency bands, a HolyIot/RuuviTag beacon only the broadband level."),
      el("p", { class: "note" },
        el("a", {
          class: "doc-link",
          href: "https://github.com/MacNite/HiveHub/blob/main/docs/accelerometer.md",
          target: "_blank", rel: "noopener noreferrer",
        }, "Vibration monitoring docs"))));
  } else if (!hasBands) {
    charts.unshift(el("div", { class: "card" },
      el("p", { class: "muted-text" },
        "No frequency bands reported — a passive HolyIot/RuuviTag beacon sends a single broadband magnitude only. "
        + "Pair a HiveInside node to get the 8–30 Hz pre-swarm band.")));
  }

  const node = tsView("Vibration",
    "In-hive vibration in milli-g, from the paired BLE sensor — stronger vibration is higher up the axis",
    state, { cards, charts });
  if (axisCharts.length) node.append(foldedCharts("Raw acceleration axes", ...axisCharts));
  root.append(node);
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

  // Battery chart: SoC on the primary (left) axis, voltage on a secondary
  // (right) axis. Voltage barely moves (≈3.7–4.2 V), so sharing the 0–100 %
  // scale flattens it to a useless flat line — its own axis makes it readable.
  const socSeries = seriesFrom(state.measurements, "battery_soc_percent", "SoC %", PALETTE[2]);
  socSeries.unit = "%"; socSeries.digits = 0;
  const battVoltSeries = seriesFrom(state.measurements, "battery_voltage", "Voltage", PALETTE[0]);
  battVoltSeries.axis = "right"; battVoltSeries.unit = "V"; battVoltSeries.digits = 2;

  // The INA219 solar/current sensor is an optional legacy add-on (no longer part
  // of the recommended build — the MAX17048 covers battery state); only surface
  // its cards and chart when it has actually reported data, so a device without
  // one doesn't show empty "No data available" panels.
  const solarPowerSeries = seriesFrom(state.measurements, "solar_power_mw", "Power mW", PALETTE[0]);
  const solarCurrentSeries = seriesFrom(state.measurements, "solar_current_ma", "Current mA", PALETTE[1]);
  const solarBusSeries = seriesFrom(state.measurements, "solar_bus_voltage_v", "Bus V", PALETTE[2]);
  const hasSolar = solarPowerSeries.points.length > 0 || solarCurrentSeries.points.length > 0 ||
    solarBusSeries.points.length > 0 || isNum(m.solar_power_mw) || isNum(m.solar_current_ma) || isNum(m.solar_bus_voltage_v);

  const cards = [
    metricCard("State of charge", fmt(m.battery_soc_percent, 0), "%", isNum(m.battery_voltage) ? `${fmt(m.battery_voltage, 2)} V` : "Battery"),
  ];
  if (hasSolar) {
    cards.push(
      metricCard("Solar power", fmt(m.solar_power_mw, 0), "mW", isNum(m.solar_current_ma) ? `${fmt(m.solar_current_ma, 0)} mA` : "Solar input"),
      metricCard("Solar bus", fmt(m.solar_bus_voltage_v, 2), "V", "Panel voltage"),
    );
  }
  if (m.battery_alert) cards.push(metricCard("Battery alert", "Active", "", "Low battery warning"));
  const charts = [
    chartCard("Battery", "State of charge and voltage",
      [socSeries, battVoltSeries], { yDigits: 1, y2Digits: 2 }),
  ];
  if (hasSolar) {
    charts.push(chartCard("Solar", "Solar power input",
      [solarPowerSeries, solarCurrentSeries], { yDigits: 0 }));
  }

  const node = el("div", {});
  node.append(viewHead("Battery & power", "Collector battery and wireless-sensor batteries"));
  const note = deviceContextNote(state, "The collector battery readings below are");
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
    metricCard("Transport", m.network_transport || DASH, "", "Uplink"),
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

// A small "?" / "!" affordance that reveals `text` on hover or keyboard focus.
// The bubble is the primary surface; the native `title` and `aria-label` keep it
// reachable for touch and assistive tech. `icon` picks the glyph ("?" or "!").
// Update the text later with setTipText() (used for the board note, which
// changes with the selected target).
function infoTip(text, icon = "?") {
  const bubble = el("span", { class: "info-tip-bubble" }, text || "");
  const tip = el("span", { class: "info-tip", tabindex: "0", role: "note", "aria-label": text || "", title: text || "" },
    el("span", { class: "info-tip-icon", "aria-hidden": "true" }, icon),
    bubble);
  tip.addEventListener("pointerenter", () => fitTipBubble(bubble));
  tip.addEventListener("focus", () => fitTipBubble(bubble));
  return tip;
}

// Keep a tooltip on screen. The bubble is centred on its icon, so a tip sitting
// near a screen edge — a card heading on a phone, the left-most column on a
// laptop — opened half off-screen and lost the start of every line. Measure it
// just before it is shown and shift it back inside; the arrow follows the same
// custom property, so it keeps pointing at the icon (see style.css).
//
// Measuring works while the bubble is still invisible because it is hidden with
// visibility/opacity rather than display:none, so it is laid out either way.
const TIP_VIEWPORT_MARGIN = 8;
function fitTipBubble(bubble) {
  bubble.style.setProperty("--tip-shift", "0px");
  const box = bubble.getBoundingClientRect();
  const room = window.innerWidth - TIP_VIEWPORT_MARGIN;
  let shift = 0;
  if (box.left < TIP_VIEWPORT_MARGIN) shift = TIP_VIEWPORT_MARGIN - box.left;
  else if (box.right > room) shift = room - box.right;
  if (shift) bubble.style.setProperty("--tip-shift", `${Math.round(shift)}px`);
}

function setTipText(tip, text) {
  tip.setAttribute("aria-label", text || "");
  tip.setAttribute("title", text || "");
  const bubble = tip.querySelector(".info-tip-bubble");
  if (bubble) bubble.textContent = text || "";
}

// Sensor-health panel for the Device & admin page: one row per subsystem the
// device reports a health flag for, OK vs Fault. A subsystem whose flag is null
// (not fitted / not configured) is omitted rather than shown as a false fault.
function sensorStatusCard(state) {
  const m = state.latest || {};
  const checks = [];
  const add = (label, ok) => { if (ok != null) checks.push({ label, ok: !!ok }); };
  add("Ambient temp/humidity (SHT40)", m.sht_ok);
  add("Real-time clock", m.rtc_ok);
  add("SD card", m.sd_ok);
  add("Microphone", m.mic_ok);
  add("Microphone · left", m.mic_left_ok);
  add("Microphone · right", m.mic_right_ok);
  add("Battery monitor", m.battery_monitor_ok);
  add("Solar monitor", m.solar_monitor_ok);
  // Per-hive vibration and entrance sensors (hives 1–2 carry dedicated columns).
  for (const n of availableHives(state).filter((h) => h <= 2)) {
    add(`Accelerometer ${n}`, m[`accel_${n}_ok`]);
    add(`Bee counter ${n}`, m[`bee_counter_${n}_ok`]);
  }
  const faults = checks.filter((c) => !c.ok).length;
  const badge = !checks.length
    ? el("span", { class: "badge muted" }, "No sensor data")
    : faults
      ? el("span", { class: "badge danger" }, `${faults} fault${faults === 1 ? "" : "s"}`)
      : el("span", { class: "badge good" }, "All OK");
  const body = checks.length
    ? el("div", { class: "rows" },
        ...checks.map((c) => el("div", { class: "row" },
          el("span", { class: "k" }, c.label),
          el("span", { class: "v" },
            el("span", { class: `badge ${c.ok ? "good" : "danger"}` }, c.ok ? "OK" : "Fault")))))
    : el("p", { class: "muted-text" },
        "No sensor health reported yet — it appears once the device sends its first reading.");

  // The wireless nodes paired to this device, with the firmware each one runs.
  // Only HiveInside reports a version (it advertises board + version in its
  // scan-response identity record); every other in-hive node — HolyIot,
  // RuuviTag and the beehivemonitoring GATT devices — sends no version field at
  // all, so those rows show an em-dash instead of an invented number.
  const nodes = inHiveSensors(state);
  const unversioned = nodes.some((s) => !s.version);
  return el("div", { class: "card" },
    el("div", { class: "spread" }, el("h2", {}, "Status"), badge),
    el("p", { class: "note" },
      m.measured_at ? `Sensor health from the last check-in · ${relAge(m.measured_at)}.`
                    : "Per-sensor health as reported by the device."),
    body,
    nodes.length ? el("h3", { class: "fw-upload-head" }, "In-hive sensors") : null,
    nodes.length
      ? el("div", { class: "rows" },
          ...nodes.map((s) => el("div", { class: "row" },
            el("span", { class: "k" }, s.label),
            el("span", { class: "v" }, s.version ? `v${s.version}` : DASH))))
      : null,
    nodes.length && unversioned
      ? el("p", { class: "note" },
          "Firmware version as advertised by the node. Only HiveInside broadcasts one — " +
          "HolyIot, RuuviTag and HiveHeart/HiveScale report no version.")
      : null);
}

// The wireless in-hive nodes a device has reported, newest identity first: the
// passively-scanned beacons (HolyIot / RuuviTag / HiveInside) plus the
// beehivemonitoring GATT devices (HiveHeart / HiveScale). One row per node, with
// the advertised firmware version where the node sends one.
function inHiveSensors(state) {
  const rows = [];
  for (const n of availableHives(state)) {
    const label = hiveLabel(state, n);
    const type = bleFieldLatest(state, n, "sensor_type");
    const fw = bleFieldLatest(state, n, "firmware_version");
    const board = bleFieldLatest(state, n, "board");
    if (type || fw || board) {
      const kind = type || "In-hive BLE sensor";
      rows.push({ label: `${label} · ${board ? `${kind} (${board})` : kind}`, version: fw });
    }
    for (const [kind, name] of [["hiveheart", "HiveHeart"], ["hivescale", "HiveScale"]]) {
      if (gattSensorSeen(state, n, kind)) rows.push({ label: `${label} · ${name}`, version: null });
    }
  }
  return rows;
}

// Whether hive `n` has ever reported a reading from the beehivemonitoring GATT
// device `kind` ("hiveheart" / "hivescale"), nested or flat. Probes a handful of
// representative fields rather than every key, so scanning the whole loaded
// range stays cheap.
const GATT_PROBE_FIELDS = ["temp_c", "humidity_percent", "battery_v", "rssi_dbm", "weight_kg", "frequency_hz"];
function gattSensorSeen(state, n, kind) {
  for (const m of [state.latest, ...(state.measurements || [])]) {
    if (!m) continue;
    const hv = (m.hives || []).find((h) => Number(h?.index) === Number(n));
    const nested = hv && hv[kind];
    if (nested && GATT_PROBE_FIELDS.some((f) => nested[f] != null)) return true;
    if (GATT_PROBE_FIELDS.some((f) => m[`${kind}_${n}_${f}`] != null)) return true;
  }
  return false;
}

// Latest reported value of a per-hive BLE field ("sensor_type",
// "firmware_version", "board", …). Prefers the nested hives[] entry the
// multi-hive firmware sends and falls back to the flat ble_{n}_* keys (hives 1–2
// and older firmware). Readings are scanned newest-first, so a node that simply
// was not heard during the last scan window keeps showing its last known
// identity instead of an em-dash.
function bleFieldLatest(state, n, key) {
  for (const m of [state.latest, ...(state.measurements || [])]) {
    if (!m) continue;
    const hv = (m.hives || []).find((h) => Number(h?.index) === Number(n));
    const nested = hv && hv.ble ? hv.ble[key] : null;
    if (nested != null && nested !== "") return nested;
    const flat = m[`ble_${n}_${key}`];
    if (flat != null && flat !== "") return flat;
  }
  return null;
}

// Hives whose in-hive BLE node identifies itself as a HiveInside. Only HiveInside
// answers a scan with the identity record (the 'I' manufacturer element in its
// scan response) that carries board and firmware version — HolyIot and RuuviTag
// beacons leave both empty — so either field alone identifies one.
function hiveInsideNodes(state) {
  const nodes = [];
  for (const n of availableHives(state)) {
    const fw = bleFieldLatest(state, n, "firmware_version");
    const board = bleFieldLatest(state, n, "board");
    const type = String(bleFieldLatest(state, n, "sensor_type") || "");
    if (!fw && !board && !/hiveinside/i.test(type)) continue;
    nodes.push({ n, label: hiveLabel(state, n), fw, board });
  }
  return nodes;
}

// The latest value of `key` inside a hive's nested bee_counter object, newest
// reading first. Unlike the BLE fields there is no flat `bee_counter_N_*`
// fallback for the version — it rides in raw_json only — so this looks at the
// nested form alone.
function beeCounterFieldLatest(state, n, key) {
  for (const m of [state.latest, ...(state.measurements || [])]) {
    if (!m) continue;
    const hv = (m.hives || []).find((h) => Number(h?.index) === Number(n));
    const v = hv && hv.bee_counter ? hv.bee_counter[key] : null;
    if (v != null && v !== "") return v;
  }
  return null;
}

// Hives with a paired HiveTraffic counter. Identified by the counter having
// reported at all — a hive with no counter carries no bee_counter object, while
// one that is paired but unreachable this cycle still carries ok:false, and
// that hive should stay listed so a failed relay to it remains visible.
//
// `fw` is the counter's image version ("ver"), which only firmware from the
// version-reporting release onwards sends. An older counter therefore lists
// with no version — correct, and exactly the case the server never gates.
function beeCounterNodes(state) {
  const nodes = [];
  for (const n of availableHives(state)) {
    const seen = beeCounterFieldLatest(state, n, "ok") != null;
    const fw = beeCounterFieldLatest(state, n, "version");
    if (!seen && !fw) continue;
    nodes.push({ n, label: hiveLabel(state, n), fw, board: null });
  }
  return nodes;
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

  // Scales the device exposes — every hive it reports, not just the first two
  // (fallback [1, 2] for a device that has not reported yet). Hives 1–2 are
  // backed by dedicated device_configs columns; hives 3+ live in the per-hive
  // calibration map the config exposes as hive_scales and the PATCH endpoint
  // merges entry by entry. Both are edited through the same three fields below.
  // 18 is the server-side MAX_HIVES; anything past it would be rejected by the
  // config endpoint, so it never gets an editor.
  const reported = availableHives(state).filter((n) => n <= 18);
  const scales = reported.length ? reported : [1, 2];
  const scaleTag = (n) => {
    const label = hiveLabel(state, n);
    return label && label !== `Hive ${n}` ? ` · ${label}` : "";
  };

  // Editors for a hive_scales entry. A hive with no stored entry yet shows the
  // firmware defaults (the values it is actually running with), and since the
  // initial value is remembered per field, an untouched hive is never sent — so
  // opening the page cannot write default entries for every hive on the device.
  const HIVE_SCALE_DEFAULTS = { offset: 0, factor: -7050, tempco_kg_per_c: 0 };
  const hiveScaleInputs = new Map();   // hive index → { field: {input, int, initial} }
  const hiveScaleInput = (n, key, isInt) => {
    const stored = (cfg.hive_scales || []).find((h) => Number(h.index) === n);
    const cur = stored && stored[key] != null ? stored[key] : HIVE_SCALE_DEFAULTS[key];
    const input = el("input", {
      type: "number", step: isInt ? "1" : "any", value: String(cur),
    });
    if (!hiveScaleInputs.has(n)) hiveScaleInputs.set(n, {});
    hiveScaleInputs.get(n)[key] = { input, int: !!isInt, initial: cur };
    return input;
  };

  // One offset/factor/tempco group per scale; the Scale selector shows one group
  // at a time so the section reads as a per-scale editor rather than a flat list.
  const scaleGroups = new Map();
  for (const n of scales) {
    const [offset, factor, tempco] = n <= 2
      ? [numInput(`scale${n}_offset`, true), numInput(`scale${n}_factor`),
         numInput(`scale${n}_tempco_kg_per_c`)]
      : [hiveScaleInput(n, "offset", true), hiveScaleInput(n, "factor"),
         hiveScaleInput(n, "tempco_kg_per_c")];
    scaleGroups.set(n, el("div", { class: "scale-fields" },
      fieldRow("Offset", offset),
      fieldRow("Factor", factor),
      fieldRow("Tempco coefficient (kg/°C)", tempco)));
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
  // temperature and writes the coefficient into the field above (apply:false) so
  // it can be reviewed before Save. It deliberately leaves the reference
  // temperature alone — see the note it prints.
  const lookbackInput = el("input", { type: "number", value: "14", min: "1", max: "90" });
  const fitBtn = el("button", { class: "btn ghost", type: "button" }, "Fit coefficient from data");
  const fitOut = el("p", { class: "note" });
  fitBtn.addEventListener("click", async () => {
    const n = Number(scaleSelect.value);
    fitBtn.disabled = true; fitOut.textContent = "Fitting…";
    try {
      const r = await state.actions.fitTempComp({ scale: n, lookback_days: Number(lookbackInput.value) || 14, apply: false });
      if (!r || !r.ok) { fitOut.textContent = `Fit failed: ${(r && r.reason) || "insufficient data"}`; return; }
      const coeff = n <= 2
        ? cfgInputs[`scale${n}_tempco_kg_per_c`]
        : (hiveScaleInputs.get(n) || {}).tempco_kg_per_c;
      if (coeff) coeff.input.value = String(r.coeff_kg_per_c);
      tcEnabled.checked = true;
      if (r.temp_source) tcSource.value = r.temp_source;
      scaleSelect.value = String(n); showScale();
      // The reference temperature is the user's to set: it is the temperature at
      // which this scale reads true — the one it was tared and spanned at — and
      // no regression can recover that from the data. All the fit can offer is
      // the mean temperature of the window it looked at, so report that as
      // information instead of overwriting the field with it.
      const windowMean = fmt(r.ref_temp_c, 2);
      const refNow = cfgInputs.tempco_ref_temp_c
        ? cfgInputs.tempco_ref_temp_c.input.value.trim() : "";
      fitOut.textContent =
        `Filled Scale ${n}: coeff ${fmt(r.coeff_kg_per_c, 5)} kg/°C, R² ${fmt(r.r_squared, 3)} ` +
        `over ${r.n} readings from ${fmt(r.temp_min_c, 1)}–${fmt(r.temp_max_c, 1)} °C — review and Save. ` +
        `Reference temperature left${refNow ? ` at ${refNow} °C` : ""}: that is the temperature your ` +
        `scale reads true at — the one it was tared and spanned at — which the fit cannot know. ` +
        `(This window averaged ${windowMean} °C.)` +
        (Number(r.r_squared) < 0.5
          ? " R² below 0.5 — temperature explains little of this drift here. Fit over a stretch with a constant load and a wide day/night swing."
          : "");
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
        el("p", { class: "note" },
          "The temperature at which this scale reads true — the one it was tared and " +
          "spanned at. The correction is zero there and grows as the temperature moves " +
          "away from it, so fitting a coefficient never changes it."),
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
    // Hives 3+ patch through hive_scales instead of columns. Send an entry only
    // for a hive whose fields actually changed, with all three values, so the
    // server-side merge keeps the rest of that entry intact.
    const hiveScales = [];
    for (const [n, fields] of hiveScaleInputs) {
      const entry = { index: n };
      let changed = false;
      for (const [key, { input, int, initial }] of Object.entries(fields)) {
        const raw = input.value.trim();
        if (raw === "") continue;
        const v = int ? parseInt(raw, 10) : parseFloat(raw);
        if (!Number.isFinite(v)) continue;
        entry[key] = v;
        if (v !== initial) changed = true;
      }
      if (changed) hiveScales.push(entry);
    }
    if (hiveScales.length) patch.hive_scales = hiveScales;
    if (tcEnabled.checked !== !!cfg.tempco_enabled) patch.tempco_enabled = tcEnabled.checked;
    if (tcSource.value !== cfg.tempco_source) patch.tempco_source = tcSource.value;
    if (!Object.keys(patch).length) { state.toast("No changes to save"); return; }
    cfgSaveBtn.disabled = true;
    // full: true — a light reload carries the *previous* config over instead of
    // refetching it, which repainted the form with the pre-save values (a fitted
    // coefficient snapping back to 0, the reference temperature back to its
    // stored value) and left the config version stale, as if the save had been
    // dropped.
    try { await state.actions.updateConfig(patch); state.toast("Configuration saved", "success"); state.reload({ full: true }); }
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
    try { await state.actions.updateChannels({ names }); state.toast("Hive names saved", "success"); state.reload({ full: true }); }
    catch (err) { state.toast(err.message, "error"); chBtn.disabled = false; }
  });
  const channelsCard = el("div", { class: "card" }, el("h2", {}, "Hive names"), chForm);

  // Firmware panel — status + over-the-air approve, shown at the top of the same
  // card as the upload form below (Uploading registers a release; Approve & flash
  // installs it). Status leads with a 2×2 grid: Current / Latest, Approved / Board.
  const fwBadgeCls = fw.update_available ? (fw.pending_approval ? "warn" : "info") : "good";
  const fwStat = (label, value) =>
    el("div", { class: "fw-stat" },
      el("span", { class: "fw-stat-k" }, label),
      el("span", { class: "fw-stat-v" }, value || DASH));
  const fwInfo = el("div", { class: "fw-info" },
    el("div", { class: "spread" }, el("h2", {}, "Firmware"),
      el("span", { class: `badge ${fwBadgeCls}` },
        fw.update_available ? (fw.pending_approval ? "Update pending approval" : "Up to date soon") : "Up to date")),
    el("div", { class: "fw-grid" },
      fwStat("Current", fw.current_version),
      fwStat("Latest", fw.latest_version),
      fwStat("Approved", fw.approved_version),
      fwStat("Board", fw.device_board)));

  // Explain a wrong-board upload: releases exist for a board other than the one
  // this device reports, so they are (correctly) not offered here. Without this
  // note such an upload looks like it silently vanished.
  if (Array.isArray(fw.other_board_releases) && fw.other_board_releases.length) {
    const others = fw.other_board_releases.map((r) => `${r.board} ${r.version}`).join(", ");
    const forBoard = fw.device_board ? ` this ${fw.device_board} device` : " this device";
    fwInfo.append(el("p", { class: "note warn" },
      `Also uploaded for another board (not applied to${forBoard}): ${others}. ` +
      "A build only reaches a device whose board matches — check the Board field when uploading."));
  }

  // Shared "approve the latest release and flash it over the air" action, used by
  // the Firmware panel's button and by the inline Apply button that appears right
  // after an upload — so a fresh upload can be flashed without a manual refresh.
  // Full reload afterwards so the refetched firmware status hides the button once
  // the version is approved (a light reload keeps stale firmware and leaves it up).
  const approveAndFlash = async (btn, versionLabel) => {
    const version = versionLabel ? ` ${versionLabel}` : "";
    if (!window.confirm(
      `Approve firmware${version} and flash it over the air?\n\n` +
      "The device installs it on its next check-in and reboots. " +
      "A remote device that fails mid-update may need physical access to recover.")) return;
    btn.disabled = true;
    try {
      await state.actions.approveFirmware();
      state.toast("Firmware approved — device will update on next check-in", "success");
      state.reload({ full: true });
    } catch (e) { state.toast(e.message, "error"); btn.disabled = false; }
  };

  if (fw.update_available && fw.pending_approval) {
    const approveBtn = el("button", { class: "btn", type: "button" }, "Approve & flash latest");
    approveBtn.addEventListener("click", () => approveAndFlash(approveBtn, fw.latest_version));
    fwInfo.append(el("div", { class: "form-actions" }, approveBtn));
  }

  // HiveInside nodes run their own firmware, which the nRF54 beacon advertises
  // (board + version) in its scan-response identity record; the HiveHub forwards
  // it with every reading. Showing it next to the HiveHub's own version is what
  // makes a relayed update verifiable — an image the node fails to confirm is
  // reverted silently, so the node keeps advertising the old version. The whole
  // section stays out of the card unless a HiveInside node has actually been
  // heard, so a HolyIot/RuuviTag-only device sees nothing new.
  // Sub-devices run their own firmware, and both kinds report the version they
  // are running with every reading — HiveInside in its scan-response identity
  // record, a HiveTraffic counter in its measurement JSON. Showing that next to
  // the HiveHub's own version is what makes a relayed update verifiable: an
  // image a node fails to confirm is reverted silently, so it just keeps
  // reporting the old version. Each section stays out of the card entirely
  // unless a node of that kind has actually been heard, so a device with
  // neither sees nothing new.
  //
  // The two relays behave identically — same server gate, same command queue,
  // same failure reporting — so one builder renders both. `cfg` carries only
  // what genuinely differs.
  const buildRelaySection = (cfg) => {
    const nodes = cfg.nodes;
    if (!nodes.length) return null;
    // Newest uploaded release for this target, shown inline next to each node's
    // running version as "latest x.y.z". It is flagged as an available update
    // only when it is strictly newer than what the node reports — the same
    // comparison the server applies before it will queue a relay (see
    // check_relay_is_newer), so the badge never promises an update the backend
    // would refuse.
    const latest = cfg.latestVersion || null;
    const flag = (running) => {
      if (!latest) return null;
      const newer = running && versionIsNewer(latest, running);
      return el("span", {
        class: `badge ${newer ? "warn" : "muted"}`,
        title: newer
          ? `A newer ${cfg.name} release (${latest}) is uploaded and ready to relay`
          : `Newest uploaded ${cfg.name} release: ${latest}`,
      }, `latest ${latest}`);
    };
    // Whether a relay would be accepted for this node, mirroring the server gate:
    // strictly newer than the reported version, and ungated for a node that never
    // reported one — there is nothing to compare against, and an update may be
    // exactly what such a node needs.
    const relayable = (running) =>
      !!latest && (!running || versionIsNewer(latest, running));
    // Last relay attempt per hive slot (server: latest_relays). The relay records
    // a precise cause on failure, but until this was surfaced a node that never
    // updated looked the same whether the relay was pending, running, or failing
    // identically every time — so a broken relay could go unnoticed for days
    // while the version simply never changed.
    const relays = cfg.relays || {};
    const relayOf = (n) => relays[String(n)] || null;
    const RELAY_BADGE = {
      pending: ["info", "relay queued"],
      claimed: ["info", "relaying…"],
      failed: ["danger", "relay failed"],
    };
    // A relay already queued or in flight must not be queued again: the transfer
    // takes minutes, and a second command would simply repeat it.
    const relayInFlight = (relay) =>
      !!relay && (relay.status === "pending" || relay.status === "claimed");
    const relayBadge = (relay) => {
      const spec = relay && RELAY_BADGE[relay.status];
      if (!spec) return null;
      const when = relay.completed_at || relay.created_at;
      const detail = relay.message ? `: ${relay.message}` : "";
      return el("span", {
        class: `badge ${spec[0]}`,
        title: `Relay of ${relay.version || "firmware"} ${relay.status}${detail} (${fmtDateTime(when)})`,
      }, spec[1]);
    };
    // Start the transfer. Uploading a .bin only REGISTERS a release — this button
    // is the only thing in the dashboard that actually sends one to a node.
    // Reload afterwards so the queued command shows up as a "relay queued" badge
    // and the button disables itself; without that the button re-enables on the
    // next render and a second click stacks a duplicate relay.
    const startRelay = async (btn, node) => {
      if (!window.confirm(
        `Relay ${cfg.name} ${latest} to ${node.label}?\n\n` +
        "The HiveHub picks this up on its next upload cycle — normally within " +
        "about 10 minutes, or whatever send interval this device is set to. It " +
        "then streams the image over BLE for a few minutes and the node reboots " +
        "into it. The node must be awake and in range, or the relay fails and " +
        "has to be queued again." + (cfg.confirmExtra ? `\n\n${cfg.confirmExtra}` : ""))) return;
      btn.disabled = true;
      try {
        const res = await cfg.queue(node.n);
        const from = res && res.current_version ? `${res.current_version} → ` : "";
        state.toast(
          `Relay queued for ${node.label} (${from}${(res && res.version) || latest})`,
          "success");
        state.reload({ full: true }); // relay state rides on the firmware status
      } catch (e) { state.toast(e.message, "error"); btn.disabled = false; }
    };
    return el("div", {},
      el("h3", { class: "fw-upload-head" }, `${cfg.heading} `, infoTip(cfg.tip)),
      el("div", { class: "rows" },
        ...nodes.map((d) => {
          const relay = relayOf(d.n);
          // The running version gets its own element rather than sitting in the
          // cell as a bare text run: next to a wide relay button a text run is
          // squeezed to its smallest break point, which on a phone left the
          // HiveTraffic row reading "v0." — the one thing in the row that has
          // to stay readable. See .fw-node-ver / .fw-node-meta in style.css.
          const version = el("span", { class: "fw-node-ver" },
            [d.fw ? `v${d.fw}` : DASH, d.board].filter(Boolean).join(" · "));
          // Release flag, relay status and the relay button travel as one group
          // so they break onto a line of their own under the version instead of
          // stealing room from it.
          const meta = [flag(d.fw), relayBadge(relay)];
          if (relayable(d.fw)) {
            const busy = relayInFlight(relay);
            const relayBtn = el("button", {
              class: "btn small", type: "button",
              title: busy ? "A relay is already queued for this node" : "",
            }, cfg.buttonLabel);
            relayBtn.disabled = busy;
            relayBtn.addEventListener("click", () => startRelay(relayBtn, d));
            meta.push(relayBtn);
          }
          const cell = el("span", { class: "v fw-node-v" }, version,
            meta.some(Boolean) ? el("span", { class: "fw-node-meta" }, ...meta) : null);
          return el("div", { class: "row" }, el("span", { class: "k" }, d.label), cell);
        })),
      // Spell the failure out in full. The badge's tooltip is easy to miss,
      // and the message names the exact stage that failed — which is the
      // difference between "move the node closer" and "fix the server".
      ...nodes
        .filter((d) => (relayOf(d.n) || {}).status === "failed")
        .map((d) => {
          const relay = relayOf(d.n);
          return el("p", { class: "note" },
            `${d.label}: last relay failed — ${relay.message || "no reason reported"} ` +
            `(${relAge(relay.completed_at || relay.created_at)})`);
        }));
  };

  const insideSection = buildRelaySection({
    nodes: hiveInsideNodes(state),
    name: "HiveInside",
    heading: "HiveInside nodes",
    buttonLabel: "Relay to node",
    latestVersion: fw.hiveinside_latest_version,
    relays: fw.hiveinside_relays,
    queue: (n) => state.actions.queueHiveInsideUpdate(n),
    // What the version column means and how an image actually reaches a node. It
    // explains the section rather than reporting anything, so it hangs off a "?"
    // next to the heading instead of taking a paragraph under the node list —
    // the per-node relay failures below are the part worth keeping on screen.
    tip: "Firmware each in-hive node advertises. Uploading an image with target " +
      "“HiveInside” below only registers the release — nothing reaches a node " +
      "until you press “Relay to node”. The HiveHub picks the job up on its next " +
      "upload cycle (about 10 minutes by default) and streams it over BLE; this " +
      "version is what confirms the update took.",
  });

  const counterSection = buildRelaySection({
    nodes: beeCounterNodes(state),
    name: "HiveTraffic",
    heading: "HiveTraffic counters",
    buttonLabel: "Relay to counter",
    latestVersion: fw.beecounter_latest_version,
    relays: fw.beecounter_relays,
    queue: (n) => state.actions.queueBeeCounterUpdate(n),
    // Worth stating outright rather than burying: unlike a HiveInside relay,
    // this one costs data. The counter parks its IR emitters and stops polling
    // its gates while it writes flash, so every bee that passes during the
    // transfer goes uncounted.
    confirmExtra: "The counter stops counting bees for the whole transfer — " +
      "any traffic during those few minutes is lost.",
    tip: "Firmware each HiveTraffic counter reports. Uploading an image with " +
      "target “HiveTraffic counter” below only registers the release — nothing " +
      "reaches a counter until you press “Relay to counter”. The counter stops " +
      "counting while the image transfers, so relay when traffic is low. A " +
      "counter running firmware older than about v0.1.0 reports no version and " +
      "shows none here; it can still be updated.",
  });

  // Firmware upload form. The main-unit ("hivescale") target ships for two
  // boards, and the server refuses a release whose board it cannot determine
  // from this field or a board-stamped filename — so the board is a select of
  // the valid values, shown only for that target.
  const fileInput = el("input", { type: "file", accept: ".bin", required: true });
  const versionInput = el("input", { type: "text", placeholder: "e.g. 0.21.0", required: true });
  // What the picked file was recognized as. The Target select changing on its
  // own is easy to miss, and a silent auto-switch is worse than no auto-switch —
  // so name what was filled in, and say so when nothing was recognized.
  const filePicked = el("p", { class: "note", hidden: true });
  const targetSelect = el("select", { class: "full" },
    el("option", { value: "hivescale" }, "Main unit (HiveHub / HiveScale)"),
    el("option", { value: "hiveinside" }, "HiveInside"),
    el("option", { value: "beecounter" }, "HiveTraffic counter"));
  // Board options depend on the target: the main unit ships two architectures
  // (Xtensa ESP32 vs RISC-V ESP32-C6), so there it is a real choice. HiveInside
  // exists only for the nRF54LM20A (signed Zephyr image) — a single-entry list,
  // which syncBoardRow() renders as a disabled select so the fixed board is
  // visible without pretending there is something to pick. The server refuses a
  // release whose board disagrees with its filename, so a cross-architecture
  // image can never be published.
  const BOARDS_BY_TARGET = {
    hivescale: [
      ["", "Detect from filename (…_esp32_… / …_esp32-c6_…)"],
      ["esp32", "ESP32 (classic 30-pin)"],
      ["esp32-c6", "ESP32-C6"],
    ],
    hiveinside: [
      ["nrf54lm20a", "HiveInside (nRF54LM20A)"],
    ],
    beecounter: [
      ["esp32-c6", "HiveTraffic counter (ESP32-C6)"],
    ],
  };
  const boardSelect = el("select", { class: "full" });
  // The board guidance lives in a hover/focus tooltip next to the Board label
  // rather than a paragraph below the field. The text depends on the target, so
  // syncBoardRow() rewrites the tip whenever the target changes.
  const boardTip = infoTip("");
  const boardRow = el("div", { class: "form-row" },
    el("label", {}, el("span", {}, "Board "), boardTip), boardSelect);
  const BOARD_NOTES = {
    hivescale: "Main-unit firmware must state its board: pick one, or keep auto-detect when the file is named like hivehub_esp32_0.21.0.bin.",
    hiveinside: "HiveInside ships only for the Nordic nRF54LM20A, so the board is fixed and cannot be changed — the release is always stamped nrf54lm20a. HiveInside’s build already names its artifact hiveinside-nrf54lm20a-v<version>-<variant>.signed.bin — upload that file as-is (the version-stamped copy of zephyr.signed.bin, not zephyr.signed.bin itself, so bring-up and low-power images stay apart) and this form fills in the target and version from its name. It is relayed to the sensor with “Relay to node”.",
    beecounter: "The HiveTraffic counter is an ESP32-C6, so the board is fixed. HiveTraffic’s build already names its artifact hivetraffic_esp32-c6_<version>.bin — upload that file as-is (it is the application-only image, not a merged factory image), then send it with “Relay to counter”.",
  };
  const syncBoardRow = () => {
    const opts = BOARDS_BY_TARGET[targetSelect.value];
    boardRow.hidden = !opts;
    if (!opts) return;
    boardSelect.innerHTML = "";
    opts.forEach(([v, label]) => boardSelect.appendChild(el("option", { value: v }, label)));
    // Single-board target (HiveInside): show the one board it can be, greyed
    // out. Disabling is safe here because the submit handler reads
    // boardSelect.value directly — normal form serialization, which drops
    // disabled fields, is never used for this form.
    boardSelect.disabled = opts.length === 1;
    setTipText(boardTip, BOARD_NOTES[targetSelect.value]);
  };
  targetSelect.addEventListener("change", syncBoardRow);
  // Selecting/dropping a build artifact fills in Target and Version from its
  // filename (hivehub_esp32_0.21.0.bin → main unit / 0.21.0;
  // hiveinside-nrf54lm20a-v0.4.7-lowpower.signed.bin → HiveInside / 0.4.7), so
  // the operator rarely has to touch either field. Each is filled only when it
  // was actually detected — an unrecognized name leaves the current selection
  // and whatever was typed intact rather than clearing them. Registered after
  // syncBoardRow() exists because switching the target has to rebuild the Board
  // row (the fixed nRF54 board for HiveInside, the ESP32 pair for the main unit).
  fileInput.addEventListener("change", () => {
    const name = (fileInput.files[0] || {}).name || "";
    const version = versionFromFilename(name);
    if (version) versionInput.value = version;
    const target = targetFromFilename(name);
    if (target && BOARDS_BY_TARGET[target]) {
      targetSelect.value = target;
      syncBoardRow();
    }
    const filled = [];
    if (target) filled.push(`target “${targetSelect.selectedOptions[0].textContent}”`);
    if (version) filled.push(`version ${version}`);
    filePicked.hidden = !name;
    filePicked.textContent = !name ? ""
      : filled.length
        ? `${name} — filled in ${filled.join(" and ")}.`
        : `${name} — no target or version recognized in the name; set them below.`;
  });
  const uploadBtn = el("button", { class: "btn", type: "submit" }, "Upload firmware");

  // Result slot under the upload form. After a successful main-unit upload we drop
  // an "Approve & flash" button straight in here so the just-registered release can
  // be applied over the air right away, instead of waiting for a manual refresh to
  // surface the button in the Firmware panel above (a plain upload only reload()s
  // light, which doesn't refetch firmware status).
  const uploadResult = el("div", { hidden: true });
  const showUploadApply = (res) => {
    uploadResult.innerHTML = "";
    // Only the main unit ("hivescale") is applied from here. A sub-device release
    // (HiveInside, HiveTraffic) is NOT relayed automatically on any check-in — it
    // is sent by the "Relay to …" button in the matching section above, which is
    // per-node (the relay targets one hive index at a time), so there is nothing
    // meaningful to apply from the upload form.
    if (!res || res.target !== "hivescale") { uploadResult.hidden = true; return; }
    // Approve flashes the latest release for the DEVICE's board, so a .bin
    // registered for a different board wouldn't be the one applied — skip the
    // button there rather than offer a misleading Apply (the Firmware panel's
    // wrong-board note covers that case after a refresh).
    if (res.board && fw.device_board && res.board !== fw.device_board) {
      uploadResult.hidden = true;
      return;
    }
    const label = res.version ? ` ${res.version}` : "";
    const applyBtn = el("button", { class: "btn", type: "button" }, `Approve & flash${label}`);
    applyBtn.addEventListener("click", () => approveAndFlash(applyBtn, res.version));
    uploadResult.append(
      el("p", { class: "note" }, "Uploaded — approve now to flash it over the air on the device's next check-in."),
      el("div", { class: "form-actions" }, applyBtn));
    uploadResult.hidden = false;
  };

  // What "Upload" does lives in a tooltip next to the button rather than a
  // paragraph — the Firmware status above is where the release is installed from.
  const uploadTip = infoTip(
    "Uploading only registers a new firmware release for this target — it never installs anything on its own. For the main unit, use “Approve & flash” in the Firmware panel; it appears once the upload is newer than what the device runs, and the device flashes on its next check-in. For a sub-device, use the “Relay to …” button next to the node or counter you want to update.");
  const uploadForm = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Firmware .bin"), fileInput),
    filePicked,
    el("div", { class: "form-row" }, el("label", {}, "Version"), versionInput),
    el("div", { class: "form-row" }, el("label", {}, "Target"), targetSelect),
    boardRow,
    el("div", { class: "form-actions" }, uploadBtn, uploadTip));
  syncBoardRow();
  uploadForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!fileInput.files[0]) return;
    const fd = new FormData();
    fd.append("file", fileInput.files[0]);
    fd.append("version", versionInput.value.trim());
    fd.append("target", targetSelect.value);
    if (BOARDS_BY_TARGET[targetSelect.value] && boardSelect.value) {
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
      // Reveal the Apply button in place rather than reloading — a full reload would
      // rebuild the whole view (and drop this card) right after the upload.
      showUploadApply(res);
    }
    catch (err) { state.toast(err.message, "error"); }
    finally { uploadBtn.disabled = false; }
  });
  // One "Firmware" card: status/approve (fwInfo) on top, the versions any
  // sub-devices report, then the upload form.
  const firmwareCard = el("div", { class: "card" },
    fwInfo,
    insideSection,
    counterSection,
    el("h3", { class: "fw-upload-head" }, "Upload firmware"),
    uploadForm, uploadResult);

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
      "Accepts measurements.ndjson, the hivescale-sd-data.tar download, or a " +
      "backup saved from Admin → Download / backup data below. Re-uploading the " +
      "same file is safe — existing readings are skipped automatically."),
    el("div", { class: "form-actions" }, sdBtn),
    sdResult);
  // Post the picked file to a specific device (defaults to the selected one).
  // `route` sends the readings to the device each row names instead, which is how
  // a whole-server backup from the download panel is restored.
  function runSdImport(deviceId, route) {
    const fd = new FormData();
    fd.append("file", sdFileInput.files[0]);
    if (route) fd.append("route_by_device", "true");
    return state.actions.importSdData(fd, deviceId);
  }
  function renderSdResult(res) {
    const dupes = res.duplicates
      ? `, ${res.duplicates} duplicate${res.duplicates === 1 ? "" : "s"} skipped`
      : "";
    const unreadable = res.skipped
      ? ` · ${res.skipped} unreadable line${res.skipped === 1 ? "" : "s"} skipped`
      : "";
    // A routed restore spreads one file over several devices, so name the count
    // rather than a single target.
    const spread = res.devices || [];
    const into = spread.length > 1
      ? ` across ${spread.length} devices`
      : res.device_id ? ` into device “${res.device_id}”` : "";
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
          // Every device the file names, not just the ones that differ from the
          // upload target: a whole-server backup restored while one of its own
          // devices is selected lists that device too, and it needs routing
          // rather than a re-post to a single "source".
          const sources = err.detail.source_device_ids || err.detail.file_device_ids || [];
          const foreign = err.detail.file_device_ids || [];
          const target = err.detail.target_device_id;
          if (sources.length > 1) {
            // A whole-server backup from the download panel. Ask the server to
            // file every row under the device that recorded it, rather than
            // re-pinning the lot onto the device that happens to be selected.
            const ok = window.confirm(
              `This file holds readings for ${sources.length} devices:\n` +
              `${sources.join(", ")}\n\n` +
              `Restore each device's readings into that device?\n\n` +
              `OK — restore all devices      Cancel — stop`
            );
            if (!ok) { state.toast("Import cancelled", "error"); return; }
            sdBtn.textContent = "Restoring…";
            try {
              res = await runSdImport(undefined, true);
            } catch (err2) {
              state.toast(`Could not restore the backup: ${err2.message}`, "error");
              return;
            }
          } else if (foreign.length === 1) {
            const source = foreign[0];
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
            state.toast(err.detail.message || err.message, "error");
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
  // Top: three columns — Status (per-sensor health) · Hive names + Import SD card
  // data · Firmware (status/approve + upload). Each panel sizes to its own
  // content rather than stretching. Below: a full-width collapsible
  // "Configuration" (general + per-scale calibration/compensation + fit, plus the
  // Calibration-mode controls), collapsed by default; and at the very bottom a
  // full-width collapsible "Admin" grouping the account and admin-only panels.
  const isAdmin = state.authUser?.role === "admin";

  const topGrid = el("div", { class: "admin-cols" },
    el("div", { class: "admin-col" }, sensorStatusCard(state)),
    el("div", { class: "admin-col" }, channelsCard, sdCard),
    el("div", { class: "admin-col" }, firmwareCard));

  // Download / backup is built only for admins — the export endpoints are
  // admin-only, so rendering it for a viewer would just be a panel that 403s.
  // It sits next to "Delete readings": save the data, then prune it.
  const adminCards = [accountCard(state), notificationsCard(state)];
  if (isAdmin) {
    adminCards.push(usersCard(state), visibleDevicesCard(state),
                    downloadBackupCard(state), deleteMeasurementsCard(state),
                    deleteDeviceCard(state));
  }

  node.append(
    topGrid,
    collapsible("Configuration", false, cfgForm, calCard),
    // Publishing is optional server-side (ENABLE_PUBLIC_EMBEDS): when it is off,
    // every /publish endpoint 404s, so the panel is not built at all rather than
    // offered and then failing on open. state.features comes from the auth
    // handshake (see app.js / local_dashboard.dashboard_features).
    state.features?.publish ? publishPanel(state) : null,
    collapsible("Admin", false, el("div", { class: "admin-cols" }, ...adminCards)));
  root.append(node);
}

// "Download / backup data" (admin only): stream every stored reading back out in
// the same NDJSON shape the scale writes to its SD card, so a self-host can
// archive its history before re-deploying the stack — or hand one beekeeper their
// data when they move to their own server — and load it back through "Import SD
// card data" at the top of this page. Devices, hives and a time range are all
// optional filters; leaving them alone backs up everything.
//
// The download itself is a plain browser navigation (the session rides in the
// same-origin cookie), which means a server-side error would only show up inside
// the saved file. So the panel asks the server how much the current filters
// match and shows that first — an empty or mistaken selection is visible before
// anything is saved.
function downloadBackupCard(state) {
  const devices = state.devices || [];
  // The shape availableHives()/hiveLabel() expect. Custom hive names are only
  // loaded for the active device; the others fall back to the names carried in
  // the device list (see app.js deviceState).
  const dstate = (id) => ({
    latest: state.deviceLatest(id),
    channels: id === state.activeDeviceId ? state.channels : null,
    device: state.deviceMeta(id),
  });

  // Hive indices to offer: the union of what every device on this server
  // reports, since the filter selects by index across the whole download. A name
  // is only shown when every device that named that hive agrees on it.
  const namesByHive = new Map();
  const hiveSet = new Set();
  for (const d of devices) {
    const ds = dstate(d.device_id);
    for (const n of availableHives(ds)) {
      hiveSet.add(n);
      const label = hiveLabel(ds, n);
      if (label && label !== `Hive ${n}`) {
        if (!namesByHive.has(n)) namesByHive.set(n, new Set());
        namesByHive.get(n).add(label);
      }
    }
  }
  const hives = [...hiveSet].sort((a, b) => a - b);
  const hiveText = (n) => {
    const names = [...(namesByHive.get(n) || [])];
    return names.length === 1 ? `Hive ${n} · ${names[0]}` : `Hive ${n}`;
  };

  // A checkbox list with All / None shortcuts. Everything starts ticked, so the
  // untouched panel backs up the whole server.
  function checkList(items, keyOf, labelOf) {
    const boxes = new Map();
    const rows = items.map((it) => {
      const cb = el("input", { type: "checkbox" });
      cb.checked = true;
      cb.addEventListener("change", scheduleRefresh);
      boxes.set(keyOf(it), cb);
      return el("label", { class: "row" },
        el("span", { class: "k" }, labelOf(it)),
        el("span", { class: "v" }, cb));
    });
    const setAll = (on) => {
      for (const cb of boxes.values()) cb.checked = on;
      scheduleRefresh();
    };
    const head = el("div", { class: "form-actions" },
      el("button", { class: "btn ghost", type: "button", onclick: () => setAll(true) }, "All"),
      el("button", { class: "btn ghost", type: "button", onclick: () => setAll(false) }, "None"));
    return { node: el("div", {}, el("div", { class: "rows" }, ...rows), head), boxes };
  }

  const picked = (boxes) => [...boxes].filter(([, cb]) => cb.checked).map(([k]) => k);

  const deviceList = checkList(
    devices,
    (d) => d.device_id,
    (d) => (d.display_name ? `${d.display_name} · ${d.device_id}` : d.device_id));
  const hiveList = checkList(hives, (n) => n, hiveText);

  const startInput = el("input", { type: "datetime-local" });
  const endInput = el("input", { type: "datetime-local" });
  startInput.addEventListener("change", scheduleRefresh);
  endInput.addEventListener("change", scheduleRefresh);

  const summary = el("p", { class: "note" }, "Checking…");
  const breakdown = el("div", { class: "rows" });
  const btn = el("button", { class: "btn", type: "button", disabled: true }, "Download backup");

  // Filters in the shape api.exportQuery wants. An untouched (fully ticked) list
  // is sent as "no filter", which is what the server reads as "everything" — and
  // keeps a whole-server backup from carrying a URL full of ids.
  function currentOpts() {
    const deviceIds = picked(deviceList.boxes);
    const hiveSel = picked(hiveList.boxes);
    return {
      deviceIds: deviceIds.length === devices.length ? [] : deviceIds,
      hives: hiveSel.length === hives.length ? [] : hiveSel,
      start: startInput.value ? new Date(startInput.value).toISOString() : null,
      end: endInput.value ? new Date(endInput.value).toISOString() : null,
    };
  }

  let lastSummary = null;
  let refreshTimer = null;
  let refreshSeq = 0;

  function block(message) {
    lastSummary = null;
    btn.disabled = true;
    summary.textContent = message;
    breakdown.replaceChildren();
  }

  async function refresh() {
    const deviceIds = picked(deviceList.boxes);
    const hiveSel = picked(hiveList.boxes);
    if (!deviceIds.length) return block("Select at least one device to back up.");
    if (hives.length && !hiveSel.length) return block("Select at least one hive to back up.");
    if (startInput.value && endInput.value && new Date(endInput.value) < new Date(startInput.value)) {
      return block("The end of the range is before its start.");
    }
    // Only the newest request may paint: ticking through several devices fires
    // one call per change and they can come back out of order.
    const seq = ++refreshSeq;
    summary.textContent = "Checking…";
    try {
      const res = await state.actions.exportSummary(currentOpts());
      if (seq !== refreshSeq) return;
      lastSummary = res;
      const rows = res.devices || [];
      const total = res.total_measurements || 0;
      const span = rows
        .flatMap((r) => [r.first_measured_at, r.last_measured_at])
        .filter(Boolean)
        .sort();
      const period = span.length
        ? ` · ${fmtDateTime(span[0])} → ${fmtDateTime(span[span.length - 1])}`
        : "";
      summary.textContent = total
        ? `${fmtInt(total)} reading${total === 1 ? "" : "s"} from ` +
          `${rows.length} device${rows.length === 1 ? "" : "s"}${period}.`
        : "No readings match these filters.";
      breakdown.replaceChildren(
        ...(rows.length > 1
          ? rows.map((r) => el("div", { class: "row" },
              el("span", { class: "k" }, r.device_id),
              el("span", { class: "v" }, `${fmtInt(r.measurements)} readings`)))
          : []));
      btn.disabled = !total;
    } catch (err) {
      if (seq !== refreshSeq) return;
      block(`Could not check the selection: ${err.message}`);
    }
  }

  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 300);
  }

  btn.addEventListener("click", () => {
    const name = lastSummary?.filename || "hivehub-backup.ndjson";
    // Hand the URL to the browser as a download rather than fetching it: the
    // file can be hundreds of megabytes, and this streams it straight to disk
    // instead of through a blob in memory.
    const link = el("a", { href: state.actions.exportUrl(currentOpts()), download: name });
    document.body.append(link);
    link.click();
    link.remove();
    state.toast(`Downloading ${name}`, "success");
  });

  const card = el("div", { class: "card" }, el("h2", {}, "Download / backup data"),
    el("p", { class: "note" },
      "Saves the selected readings as a HiveHub .ndjson backup — the same format " +
      "the scale writes to its SD card, so it loads straight back in through " +
      "“Import SD card data” at the top of this page. Leave the filters untouched " +
      "to back up everything."),
    el("h3", { class: "fw-upload-head" }, "Devices"),
    devices.length
      ? deviceList.node
      : el("p", { class: "muted-text" }, "No devices on this server."),
    hives.length ? el("h3", { class: "fw-upload-head" }, "Hives") : null,
    hives.length ? hiveList.node : null,
    hives.length
      ? el("p", { class: "note" },
          "Hives are selected by number across every device in the download. " +
          "Readings that belong to the device rather than a hive — ambient " +
          "temperature, battery, solar, connectivity — are always included.")
      : null,
    el("h3", { class: "fw-upload-head" }, "Period"),
    el("div", { class: "form-row" }, el("label", {}, "From"), startInput),
    el("div", { class: "form-row" }, el("label", {}, "To"), endInput),
    el("p", { class: "note" }, "Leave both blank for the full history."),
    summary,
    breakdown,
    el("div", { class: "form-actions" }, btn));

  if (!devices.length) {
    block("No devices on this server yet.");
  } else {
    // The card sits inside the collapsed "Admin" section, so hold the first
    // summary until it is actually on screen: the count is one COUNT(*) per
    // device, and a page render that never opens Admin should not pay for it.
    // A closed <details> gives its contents no box, so this fires on expand.
    const seen = new IntersectionObserver((entries, obs) => {
      if (entries.some((e) => e.isIntersecting)) { obs.disconnect(); refresh(); }
    });
    seen.observe(card);
  }

  return card;
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
      "Its readings are kept and it can be shown again at any time. " +
      "To erase a device for good, use “Delete device” below."),
    listEl);
}

// "Delete device" (admin only): erase a device and everything belonging to it.
// Hiding only retires a device from the picker; a decommissioned device, or one
// created by a typo'd device_id, otherwise stays in the database forever and
// keeps ingesting uploads. Irreversible, so the server requires both the claim
// code and the device_id typed back (see local_delete_device).
function deleteDeviceCard(state) {
  const devices = state.devices || [];
  const select = el("select", {});
  for (const d of devices) {
    select.append(el("option", { value: d.device_id },
      d.display_name ? `${d.display_name} · ${d.device_id}` : d.device_id));
  }
  const idInput = el("input", { type: "text", autocomplete: "off", placeholder: "Type the device ID to confirm" });
  const codeInput = el("input", { type: "text", autocomplete: "off", placeholder: "e.g. ABCD-1234" });
  const out = el("p", { class: "note", hidden: true });
  const btn = el("button", { class: "btn danger", type: "submit" }, "Delete device");
  const form = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Device"), select),
    el("div", { class: "form-row" }, el("label", {}, "Confirm device ID"), idInput),
    el("div", { class: "form-row" }, el("label", {}, "Device claim code"), codeInput),
    el("div", { class: "form-actions" }, btn),
    out);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const deviceId = select.value;
    if (!deviceId) { state.toast("No device selected", "error"); return; }
    if (idInput.value.trim() !== deviceId) { state.toast("Confirm the device ID exactly as shown", "error"); return; }
    if (!codeInput.value.trim()) { state.toast("Enter the device's claim code to confirm", "error"); return; }
    if (!window.confirm(
      `Permanently delete “${deviceId}” and every reading, command and config row\n` +
      "belonging to it?\n\nThis cannot be undone.")) return;
    btn.disabled = true;
    try {
      const res = await state.actions.deleteDevice(deviceId, {
        claim_code: codeInput.value.trim(),
        confirm_device_id: deviceId,
      });
      const n = res?.measurements_deleted ?? 0;
      out.hidden = false;
      out.textContent = `Deleted “${deviceId}” and ${n} reading${n === 1 ? "" : "s"}.`;
      state.toast(`Deleted ${deviceId}`, "success");
      idInput.value = codeInput.value = "";
      state.reload();
    } catch (err) { state.toast(err.message, "error"); }
    finally { btn.disabled = false; }
  });
  if (!devices.length) {
    return el("div", { class: "card" }, el("h2", {}, "Delete device"),
      el("p", { class: "note" }, "No devices on this server."));
  }
  return el("div", { class: "card" }, el("h2", {}, "Delete device"),
    el("p", { class: "note" },
      "Erase a device and all of its readings, commands and configuration. " +
      "Use this for a device that is gone for good — to keep the history but " +
      "tidy the picker, hide it instead. Type the device ID and its claim code " +
      "to authorise the deletion."),
    el("p", { class: "note" },
      "Power the device down first if it is still running: a device that uploads " +
      "again simply re-registers itself."),
    form);
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

// "Alert notifications": lets a beekeeper turn on push notifications for this
// browser/PWA and see whether e-mail alerts are active, plus a "send test"
// button. The server tells us which channels are configured; the Web Push
// subscribe/unsubscribe handshake runs client-side via push.js. Renders
// synchronously with a placeholder, then fills in from the async config.
function notificationsCard(state) {
  const emailRow = el("div", { class: "rows" }, el("p", { class: "muted-text" }, "Loading…"));
  const pushRow = el("div", { class: "rows" });
  const sevNote = el("p", { class: "note" });
  const testBtn = el("button", { class: "btn small ghost", type: "button" }, "Send test notification");
  testBtn.disabled = true;
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    try {
      const r = await state.actions.testNotification();
      const res = (r && r.result) || {};
      const parts = [];
      if (res.email) parts.push(`email: ${res.email}`);
      if (res.web_push) parts.push(`push: ${res.web_push}`);
      state.toast(parts.length ? `Test sent — ${parts.join(" · ")}` : "Nothing to send — no channel active", parts.length ? "success" : "error");
    } catch (err) { state.toast(err.message, "error"); }
    finally { testBtn.disabled = false; }
  });

  (async () => {
    let cfg;
    try {
      cfg = await state.actions.notificationsConfig();
    } catch (err) {
      emailRow.replaceChildren(el("p", { class: "auth-error" }, err.message || "Failed to load notification settings"));
      pushRow.replaceChildren();
      return;
    }

    // Severity floor applies to every channel.
    sevNote.textContent = `Only ${cfg.min_severity || "warning"} alerts and above are sent (set NOTIFY_MIN_SEVERITY on the server to change).`;

    // ── E-mail ────────────────────────────────────────────────────────────
    emailRow.replaceChildren(
      el("div", { class: "row" },
        el("span", { class: "k" }, "E-mail delivery"),
        el("span", { class: "v" }, cfg.email_enabled ? "Active (server SMTP configured)" : "Not configured on server")),
      el("p", { class: "note" }, cfg.email_enabled
        ? "Alerts go to the “Alert email” set under Your account above. Leave it blank to receive none."
        : "An administrator must set the SMTP_* environment variables to enable e-mail alerts."));

    // ── Web Push ──────────────────────────────────────────────────────────
    if (!pushSupported()) {
      pushRow.replaceChildren(
        el("div", { class: "row" },
          el("span", { class: "k" }, "Push on this device"),
          el("span", { class: "v" }, "Not supported")),
        el("p", { class: "note" }, "This browser can't receive Web Push. On iPhone/iPad, add HiveHub to your Home Screen first (iOS 16.4+), then reopen it here."));
    } else if (!cfg.web_push_enabled) {
      pushRow.replaceChildren(
        el("div", { class: "row" },
          el("span", { class: "k" }, "Push on this device"),
          el("span", { class: "v" }, "Not configured on server")),
        el("p", { class: "note" }, "An administrator must generate a VAPID key pair (python gen_vapid.py) and set VAPID_* + WEB_PUSH_ENABLED=true."));
    } else {
      const toggle = el("button", { class: "btn small", type: "button" }, "…");
      const statusV = el("span", { class: "v" }, "Checking…");
      const paint = (subscribed) => {
        statusV.textContent = subscribed ? "Enabled" : "Disabled";
        toggle.textContent = subscribed ? "Disable on this device" : "Enable on this device";
        toggle.dataset.on = subscribed ? "1" : "";
      };
      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        const turningOn = toggle.dataset.on !== "1";
        try {
          if (turningOn) { await pushSubscribe(cfg.vapid_public_key); state.toast("Notifications enabled on this device", "success"); }
          else { await pushUnsubscribe(); state.toast("Notifications disabled on this device", "success"); }
          paint(turningOn);
        } catch (err) { state.toast(err.message, "error"); }
        finally { toggle.disabled = false; }
      });
      pushRow.replaceChildren(
        el("div", { class: "row" },
          el("span", { class: "k" }, "Push on this device"),
          statusV),
        el("div", { class: "form-actions" }, toggle));
      try { paint(await pushIsSubscribed()); } catch (_) { paint(false); }
    }

    // Enable the test button once we know at least one channel is live.
    testBtn.disabled = !(cfg.email_enabled || cfg.web_push_enabled);
  })();

  return el("div", { class: "card" }, el("h2", {}, "Alert notifications"),
    el("p", { class: "note" }, "Get insight alerts (swarm, robbing, winter risk…) by e-mail and/or push notification when they first fire or escalate."),
    el("h3", { style: "margin:.6rem 0 .2rem" }, "E-mail"),
    emailRow,
    el("h3", { style: "margin:.8rem 0 .2rem" }, "Push notifications"),
    pushRow,
    el("div", { style: "margin-top:.8rem" }, sevNote),
    el("div", { class: "form-actions" }, testBtn));
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

// ── PUBLISH DATA ─────────────────────────────────────────────────────────────
// Turn part of this (login-protected) dashboard into something a website can
// show: pick a metric and hives, publish, and paste the returned <iframe> into a
// club page, blog or shop. The server then serves exactly that slice under an
// unguessable token — see server/publish.py — and nothing else becomes public.
//
// The view is rebuilt by every render (view switch, the 60s auto-refresh), so
// the fetched data and the half-filled form live in module state rather than in
// the DOM: an auto-refresh mid-edit must not throw away what was typed.
const publishState = {
  metrics: null, charts: null, error: null, loading: false, loaded: false,
  fetchedAt: 0,   // for the periodic re-fetch: view counts and titles age
  user: null,     // whose session the list was loaded for (see needsPublishLoad)
};
// How long the fetched list stays fresh. The view repaints on every render, so
// this decides how often reopening it (or the 60s auto-refresh) re-reads the
// list rather than repainting the cached one.
const PUBLISH_TTL_MS = 30000;

function needsPublishLoad(state) {
  if (publishState.loading) return false;
  // A different account signed in on this tab: drop the previous session's list
  // instead of showing it until the next reload.
  if (publishState.user !== (state.authUser?.username || null)) return true;
  return !publishState.loaded || Date.now() - publishState.fetchedAt > PUBLISH_TTL_MS;
}

const PUBLISH_RANGES = [
  [7, "7 days"], [14, "14 days"], [30, "30 days"], [90, "90 days"],
  [180, "6 months"], [365, "1 year"],
];
const PUBLISH_AGGREGATES = [
  ["none", "Every reading"], ["daily_max", "Daily maximum"],
  ["daily_min", "Daily minimum"], ["daily_avg", "Daily average"],
];

function newPublishDraft() {
  return {
    metric: "weight",
    title: "",
    titleTouched: false,   // stop auto-filling the title once it was edited
    subtitle: "",
    range_days: 30,
    chart_type: "line",
    aggregate: "none",
    theme: "auto",
    height: 320,
    show_legend: true,
    show_updated: true,
    labels: {},            // "<deviceId>::<hive>" -> renamed public label
    excluded: new Set(),   // candidate keys the publisher unticked
  };
}
let publishDraft = newPublishDraft();

async function loadPublishData(state) {
  if (publishState.loading) return;
  publishState.loading = true;
  publishState.user = state.authUser?.username || null;
  try {
    const [metrics, charts] = await Promise.all([
      state.actions.publishMetrics(),
      state.actions.publishedCharts(),
    ]);
    publishState.metrics = metrics;
    publishState.charts = charts;
    publishState.error = null;
  } catch (err) {
    // 404 is the server saying publishing is switched off, which is a setting
    // rather than a failure — say so instead of showing a raw error.
    publishState.error = err.status === 404
      ? "Publishing is switched off on this server. Set ENABLE_PUBLIC_EMBEDS=true (and ENABLE_LOCAL_DASHBOARD=true) to enable it."
      : err.message || "Could not load published charts";
  } finally {
    publishState.loading = false;
    publishState.loaded = true;
    publishState.fetchedAt = Date.now();
  }
}

function metricSpec(id) {
  return (publishState.metrics || []).find((m) => m.id === id) || null;
}

// The hives (or, for device-level metrics, the devices) that can go into a new
// publication: whatever is ticked in the top-bar hive picker. Publishing follows
// the selection so "what I am looking at" is what gets published.
function publishCandidates(state, scope) {
  if (scope === "device") {
    const ids = [...new Set((state.selection || []).map((s) => s.deviceId))];
    if (!ids.length && state.activeDeviceId) ids.push(state.activeDeviceId);
    return ids.map((id) => ({ key: `${id}::`, device_id: id, hive: null, label: state.deviceName(id) }));
  }
  return selectedRefs(state).map((ref) => ({
    key: `${ref.deviceId}::${ref.hive}`,
    device_id: ref.deviceId,
    hive: ref.hive,
    label: refLabel(state, ref),
  }));
}

function chosenPublishSeries(state, scope) {
  return publishCandidates(state, scope)
    .filter((c) => !publishDraft.excluded.has(c.key))
    .map((c) => ({
      device_id: c.device_id,
      hive: c.hive,
      label: (publishDraft.labels[c.key] || c.label || "").trim(),
    }))
    .filter((s) => s.label);
}

function defaultPublishTitle(state) {
  const spec = metricSpec(publishDraft.metric);
  const names = chosenPublishSeries(state, spec?.scope || "hive").map((s) => s.label);
  if (!spec) return "";
  if (!names.length) return spec.label;
  return `${spec.label} — ${names.slice(0, 3).join(", ")}${names.length > 3 ? ", …" : ""}`;
}

// Copy to the clipboard, falling back to a hidden textarea where the async
// Clipboard API is unavailable (older browsers, and any page served over plain
// HTTP — which a self-hosted dashboard on a LAN often is).
async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to the legacy path */ }
  const ta = el("textarea", { style: "position:fixed;opacity:0;pointer-events:none" });
  ta.value = text;
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

// The snippet a site owner pastes. The iframe is a hair taller than the chart so
// the title, legend and "updated" line have room; loading="lazy" keeps an embed
// far down a page off the critical path.
function embedSnippet(chart) {
  const height = Number(chart.options?.height || 320) + (chart.chart_type === "value" ? 40 : 120);
  const title = String(chart.title || "HiveHub chart").replace(/"/g, "&quot;");
  return `<iframe src="${chart.embed_url}" title="${title}" width="100%" height="${height}" `
       + `style="border:0" loading="lazy"></iframe>`;
}

function copyRow(label, value, state) {
  const box = el("input", { type: "text", readonly: true, class: "copy-field", value });
  box.addEventListener("focus", () => box.select());
  const btn = el("button", { class: "btn ghost small", type: "button" }, "Copy");
  btn.addEventListener("click", async () => {
    const ok = await copyToClipboard(value);
    state.toast(ok ? `${label} copied` : "Could not copy — select the text and copy manually",
      ok ? "success" : "error");
  });
  return el("div", { class: "copy-row" },
    el("span", { class: "copy-label" }, label), box, btn);
}

// The "Publish a chart" form (admin only). Every control writes straight into
// publishDraft, so the next re-render — including the one 60 s from now — paints
// the same half-filled form back.
function publishFormCard(state, repaint) {
  const spec = metricSpec(publishDraft.metric) || (publishState.metrics || [])[0];
  if (!spec) return null;
  publishDraft.metric = spec.id;
  const candidates = publishCandidates(state, spec.scope);

  const metricSelect = el("select", { class: "full" },
    ...(publishState.metrics || []).map((m) =>
      el("option", { value: m.id, selected: m.id === publishDraft.metric ? true : null },
        m.scope === "device" ? `${m.label} (device)` : m.label)));
  metricSelect.addEventListener("change", () => {
    publishDraft.metric = metricSelect.value;
    repaint();   // the candidate list changes with the metric's scope
  });

  const titleInput = el("input", { type: "text", maxlength: "120" });
  titleInput.value = publishDraft.titleTouched ? publishDraft.title : defaultPublishTitle(state);
  titleInput.addEventListener("input", () => {
    publishDraft.titleTouched = true;
    publishDraft.title = titleInput.value;
  });

  const subtitleInput = el("input", { type: "text", maxlength: "200", placeholder: "Optional line under the title" });
  subtitleInput.value = publishDraft.subtitle;
  subtitleInput.addEventListener("input", () => { publishDraft.subtitle = subtitleInput.value; });

  // One row per selectable hive: tick to include, and rename it for the public
  // chart (the published label is all a visitor ever sees — no device ids, no
  // hive numbers travel with the data).
  const seriesRows = candidates.map((c) => {
    const cb = el("input", { type: "checkbox" });
    cb.checked = !publishDraft.excluded.has(c.key);
    cb.addEventListener("change", () => {
      if (cb.checked) publishDraft.excluded.delete(c.key);
      else publishDraft.excluded.add(c.key);
      if (!publishDraft.titleTouched) titleInput.value = defaultPublishTitle(state);
    });
    const nameInput = el("input", { type: "text", maxlength: "64", class: "series-label" });
    nameInput.value = publishDraft.labels[c.key] ?? c.label;
    nameInput.addEventListener("input", () => {
      publishDraft.labels[c.key] = nameInput.value;
      if (!publishDraft.titleTouched) titleInput.value = defaultPublishTitle(state);
    });
    return el("label", { class: "row publish-series-row" },
      el("span", { class: "k" }, cb, el("span", { class: "series-source" }, c.label)),
      el("span", { class: "v" }, nameInput));
  });

  const rangeSelect = el("select", { class: "full" },
    ...PUBLISH_RANGES.map(([d, label]) =>
      el("option", { value: String(d), selected: d === publishDraft.range_days ? true : null }, label)));
  rangeSelect.addEventListener("change", () => { publishDraft.range_days = Number(rangeSelect.value); });

  const typeSelect = el("select", { class: "full" },
    el("option", { value: "line", selected: publishDraft.chart_type === "line" ? true : null }, "Line chart"),
    el("option", { value: "value", selected: publishDraft.chart_type === "value" ? true : null }, "Current value only"));
  typeSelect.addEventListener("change", () => { publishDraft.chart_type = typeSelect.value; });

  const aggSelect = el("select", { class: "full" },
    ...PUBLISH_AGGREGATES.map(([v, label]) =>
      el("option", { value: v, selected: v === publishDraft.aggregate ? true : null }, label)));
  aggSelect.addEventListener("change", () => { publishDraft.aggregate = aggSelect.value; });

  const themeSelect = el("select", { class: "full" },
    ...[["auto", "Follow the visitor's system"], ["light", "Always light"], ["dark", "Always dark"]].map(([v, label]) =>
      el("option", { value: v, selected: v === publishDraft.theme ? true : null }, label)));
  themeSelect.addEventListener("change", () => { publishDraft.theme = themeSelect.value; });

  const heightInput = el("input", { type: "number", min: "140", max: "1200", step: "10" });
  heightInput.value = String(publishDraft.height);
  heightInput.addEventListener("input", () => { publishDraft.height = Number(heightInput.value) || 320; });

  const legendCb = el("input", { type: "checkbox" });
  legendCb.checked = publishDraft.show_legend;
  legendCb.addEventListener("change", () => { publishDraft.show_legend = legendCb.checked; });

  const updatedCb = el("input", { type: "checkbox" });
  updatedCb.checked = publishDraft.show_updated;
  updatedCb.addEventListener("change", () => { publishDraft.show_updated = updatedCb.checked; });

  const btn = el("button", { class: "btn", type: "submit" }, "Publish chart");
  const form = el("form", {},
    el("div", { class: "form-row" }, el("label", {}, "Data"), metricSelect),
    el("h3", { class: "fw-upload-head" }, spec.scope === "device" ? "Devices" : "Hives"),
    candidates.length
      ? el("div", { class: "rows" }, ...seriesRows)
      : el("p", { class: "muted-text" },
          spec.scope === "device"
            ? "No device selected — pick one in the Hives menu at the top of the page."
            : "No hives selected — pick them in the Hives menu at the top of the page."),
    el("p", { class: "note" },
      "Only the names on the right are published. Visitors never see device IDs, "
      + "hive numbers or any reading other than the one chosen above."),
    el("div", { class: "form-row" }, el("label", {}, "Title"), titleInput),
    el("div", { class: "form-row" }, el("label", {}, "Subtitle"), subtitleInput),
    el("div", { class: "form-row" }, el("label", {}, "Period shown"), rangeSelect),
    el("div", { class: "form-row" }, el("label", {}, "Display"), typeSelect),
    el("div", { class: "form-row" }, el("label", {}, "Resolution"), aggSelect),
    el("p", { class: "note" },
      "“Every reading” draws the raw series; the daily options collapse each day "
      + "to one point, which reads far better over a month or a year."),
    el("div", { class: "form-row" }, el("label", {}, "Colour scheme"), themeSelect),
    el("div", { class: "form-row" }, el("label", {}, "Chart height (px)"), heightInput),
    el("div", { class: "rows" },
      el("label", { class: "row" }, el("span", { class: "k" }, "Show the legend"), el("span", { class: "v" }, legendCb)),
      el("label", { class: "row" }, el("span", { class: "k" }, "Show “updated …”"), el("span", { class: "v" }, updatedCb))),
    el("div", { class: "form-actions" }, btn));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const series = chosenPublishSeries(state, spec.scope);
    if (!series.length) { state.toast("Select at least one hive to publish", "error"); return; }
    const title = (publishDraft.titleTouched ? publishDraft.title : defaultPublishTitle(state)).trim();
    if (!title) { state.toast("Give the chart a title", "error"); return; }
    btn.disabled = true;
    try {
      const created = await state.actions.publishChart({
        title,
        subtitle: publishDraft.subtitle.trim() || null,
        metric: publishDraft.metric,
        chart_type: publishDraft.chart_type,
        aggregate: publishDraft.aggregate,
        series,
        range_days: publishDraft.range_days,
        options: {
          theme: publishDraft.theme,
          height: publishDraft.height,
          show_legend: publishDraft.show_legend,
          show_updated: publishDraft.show_updated,
        },
      });
      publishState.charts = [created, ...(publishState.charts || [])];
      publishDraft = newPublishDraft();
      state.toast("Chart published — copy the embed code below", "success");
      repaint();
    } catch (err) {
      state.toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  return el("div", { class: "card" }, el("h2", {}, "Publish a chart"),
    el("p", { class: "note" },
      "Publishes the hives ticked below as a public chart with its own secret "
      + "link. Anyone holding the link can see that chart — and only that chart. "
      + "Revoke it at any time from the list on the right."),
    form);
}

// One published chart in the list: what it shows, the paste-ready snippet and
// the two ways to revoke it (take offline, or delete outright).
function publishedChartCard(chart, state, repaint) {
  const isAdmin = state.authUser?.role === "admin";
  const spec = metricSpec(chart.metric);
  const names = (chart.series || []).map((s) => s.label).join(", ");
  const rangeLabel = (PUBLISH_RANGES.find(([d]) => d === chart.range_days) || [])[1]
    || `${chart.range_days} days`;
  const aggLabel = (PUBLISH_AGGREGATES.find(([v]) => v === chart.aggregate) || [])[1] || chart.aggregate;

  const head = el("div", { class: "spread" },
    el("h2", {}, chart.title),
    el("span", { class: `badge ${chart.enabled ? "good" : "muted"}` }, chart.enabled ? "Live" : "Offline"));

  const meta = rowsCard(null, [
    ["Shows", `${spec ? spec.label : chart.metric}${spec && spec.unit ? ` (${spec.unit})` : ""}`],
    [chart.series?.length === 1 ? "Hive" : "Hives", names || DASH],
    ["Period", `${rangeLabel} · ${aggLabel}`],
    ["Published", fmtDateTime(chart.created_at)],
    ["Views", `${fmtInt(chart.view_count)}${chart.last_viewed_at ? ` · last ${relAge(chart.last_viewed_at)}` : ""}`],
  ]);

  const actions = el("div", { class: "form-actions" },
    el("a", { class: "btn ghost small", href: chart.embed_url, target: "_blank", rel: "noopener noreferrer" }, "Preview"));

  if (isAdmin) {
    const toggle = el("button", { class: "btn ghost small", type: "button" },
      chart.enabled ? "Take offline" : "Put back online");
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      try {
        const updated = await state.actions.updatePublishedChart(chart.id, { enabled: !chart.enabled });
        publishState.charts = (publishState.charts || []).map((c) => (c.id === chart.id ? updated : c));
        state.toast(updated.enabled ? "Chart is live again" : "Chart taken offline", "success");
        repaint();
      } catch (err) {
        state.toast(err.message, "error");
        toggle.disabled = false;
      }
    });

    const del = el("button", { class: "btn danger small", type: "button" }, "Revoke");
    del.addEventListener("click", async () => {
      if (!window.confirm(
        `Revoke “${chart.title}”?\n\n`
        + "The link stops working immediately, and any website that embeds it "
        + "will show nothing. This cannot be undone — publishing again creates a "
        + "new link.")) return;
      del.disabled = true;
      try {
        await state.actions.deletePublishedChart(chart.id);
        publishState.charts = (publishState.charts || []).filter((c) => c.id !== chart.id);
        state.toast("Publication revoked", "success");
        repaint();
      } catch (err) {
        state.toast(err.message, "error");
        del.disabled = false;
      }
    });
    actions.append(toggle, del);
  }

  return el("div", { class: "card" },
    head,
    meta,
    el("h3", { class: "fw-upload-head" }, "Embed in a website"),
    copyRow("Embed code", embedSnippet(chart), state),
    copyRow("Direct link", chart.embed_url, state),
    copyRow("JSON data", chart.data_url, state),
    copyRow("CSV data", chart.csv_url, state),
    el("p", { class: "note" },
      "Paste the embed code into any page. The JSON and CSV links serve the same "
      + "numbers for a site that would rather draw its own chart."),
    actions);
}

// Whether the "Publish data" panel on the Device & admin page is expanded.
// Module state, like publishDraft: renderDevice rebuilds the whole page on every
// load (including the 60s auto-refresh), which would otherwise fold the panel
// shut — and throw away the form — while it is being filled in.
let publishPanelOpen = false;

// The "Publish data" section of Device & admin. Only built when the server
// reports the feature as enabled (see renderDevice / state.features.publish),
// and only loaded once actually opened: a page view that leaves it collapsed
// costs no requests.
function publishPanel(state) {
  const body = el("div", {});
  const panel = collapsible("Publish data", publishPanelOpen, body);

  const paint = () => {
    const kids = [el("p", { class: "note" },
      "Share one chart publicly and embed it in a website — everything else stays "
      + "behind this login.")];

    if (publishState.error) {
      kids.push(el("div", { class: "card" },
        el("h2", {}, "Publishing unavailable"),
        el("p", { class: "note" }, publishState.error)));
      body.replaceChildren(...kids);
      return;
    }
    if (!publishState.loaded) {
      kids.push(el("p", { class: "muted-text" }, "Loading…"));
      body.replaceChildren(...kids);
      return;
    }

    const isAdmin = state.authUser?.role === "admin";
    const charts = publishState.charts || [];
    const list = charts.length
      ? charts.map((c) => publishedChartCard(c, state, paint))
      : [el("div", { class: "card" }, el("h2", {}, "Nothing published yet"),
          el("p", { class: "note" },
            isAdmin
              ? "Pick the hives you want to show in the Hives menu at the top of the page, then publish them on the left."
              : "An administrator can publish charts here for embedding in a website."))];

    // Two columns rather than the admin page's three: the form on the left, the
    // live publications (whose copy-me URLs need the room) on the right.
    kids.push(el("div", { class: "publish-cols" },
      el("div", { class: "admin-col" },
        isAdmin
          ? publishFormCard(state, paint)
          : el("div", { class: "card" }, el("h2", {}, "Publish a chart"),
              el("p", { class: "note" }, "Publishing a chart requires the administrator role."))),
      el("div", { class: "admin-col publish-list" }, ...list)));
    body.replaceChildren(...kids);
  };

  // The background refresh must not yank the caret out of a half-typed title, so
  // it skips the repaint while a field in the panel has focus; the next render
  // (or any action in the panel) paints the fresh list.
  const paintIfIdle = () => {
    const active = document.activeElement;
    if (body.contains(active) && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName)) return;
    paint();
  };

  // Paint from what we have, then refresh in the background when the list is
  // stale (or belongs to a previous session). loadPublishData refreshes
  // fetchedAt before this paint runs, so the repaint cannot start another load.
  const refresh = () => {
    paint();
    if (needsPublishLoad(state)) loadPublishData(state).then(paintIfIdle);
  };

  panel.addEventListener("toggle", () => {
    publishPanelOpen = panel.open;
    if (panel.open) refresh();
  });

  if (publishPanelOpen) refresh();
  else body.replaceChildren(el("p", { class: "note" },
    "Publish one chart — a metric, the hives you pick, a rolling period — as a "
    + "public link you can embed in a website. Open to publish or manage them."));
  return panel;
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
  { id: "vibration", label: "Vibration", icon: "〰️", render: renderVibration },
  { id: "battery", label: "Battery & power", icon: "🔋", render: renderBattery },
  { id: "connectivity", label: "Connectivity", icon: "📶", render: renderConnectivity },
  { id: "counter", label: "Counter", icon: "🐝", render: renderCounter },
  { id: "insights", label: "Insights", icon: "💡", render: renderInsights },
  // "Publish data" is not a data group: it lives as a collapsible panel on the
  // Device & admin page (see publishPanel), and only when the server has
  // publishing enabled.
  { id: "device", label: "Device & admin", icon: "⚙️", render: renderDevice },
];
