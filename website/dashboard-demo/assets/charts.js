// Minimal dependency-free canvas line chart for time series.
//
// drawLineChart(canvas, series, opts)
//   series: [{ label, color, points: [{ t: epochMillis, y: number }] }]
//   opts:   { unit, yDigits, cursorT }
//
// Handles retina scaling, auto y-range, light gridlines, time x-axis ticks and
// an empty state. Colours come from the caller (see PALETTE below).
//
// When opts.cursorT (epoch millis) is set, draws a vertical guide at that time
// plus a marker on each series at its nearest point, and stashes the pixel<->
// time mapping on canvas._xScale so callers can turn a pointer x back into a
// timestamp (see attachChartCursor in views.js).

export const PALETTE = ["#f2a900", "#2563a8", "#2e7d32", "#b00020", "#7b3fb0", "#0f8a8a"];

// "#rrggbb" -> "rgba(r,g,b,alpha)", for fading a series colour by age.
export function withAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

const AXIS = "#8a9088";
const GRID = "rgba(31,36,33,0.08)";
const FONT = "11px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";
const LATEST_COLOR = "#111"; // spectrum chart: latest snapshot, drawn near-black on light / near-white on dark

// Canvas can't inherit CSS custom properties, so read the theme's chart colours
// off :root at draw time (they change when the user toggles dark mode). Falls
// back to the light-theme constants above if the variable isn't set.
function themeColor(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function niceTicks(min, max, count = 5) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const norm = step0 / mag;
  const step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 0.001; v += step) ticks.push(v);
  return ticks;
}

export function drawLineChart(canvas, series, opts = {}) {
  const wrap = canvas.parentElement;
  let empty = wrap.querySelector(".chart-empty");
  const hasData = series.some((s) => s.points && s.points.length > 0);

  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = canvas.clientHeight || 300;
  // Only touch the bitmap size when it actually changed: assigning
  // canvas.width/height reallocates the backing store, which is the expensive
  // part of a redraw and pointless during cursor-scrub repaints.
  const bmpW = Math.round(cssW * dpr);
  const bmpH = Math.round(cssH * dpr);
  if (canvas.width !== bmpW) canvas.width = bmpW;
  if (canvas.height !== bmpH) canvas.height = bmpH;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  if (!hasData) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "No data available";
      wrap.append(empty);
    }
    return;
  }
  if (empty) empty.remove();

  const padL = 48, padR = 14, padT = 12, padB = 26;
  const plotW = cssW - padL - padR;
  const plotH = cssH - padT - padB;

  let tMin = Infinity, tMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  for (const s of series) {
    for (const p of s.points) {
      if (p.t < tMin) tMin = p.t;
      if (p.t > tMax) tMax = p.t;
      if (p.y < yMin) yMin = p.y;
      if (p.y > yMax) yMax = p.y;
    }
  }
  if (tMin === tMax) tMax = tMin + 1;
  const pad = (yMax - yMin) * 0.08 || 1;
  yMin -= pad; yMax += pad;

  const xOf = (t) => padL + ((t - tMin) / (tMax - tMin)) * plotW;
  const yOf = (y) => padT + (1 - (y - yMin) / (yMax - yMin)) * plotH;
  canvas._xScale = { padL, plotW, tMin, tMax };

  const axis = themeColor("--chart-axis", AXIS);
  const grid = themeColor("--chart-grid", GRID);

  ctx.font = FONT;
  ctx.textBaseline = "middle";

  // Y gridlines + labels
  const yTicks = niceTicks(yMin, yMax, 5);
  ctx.strokeStyle = grid;
  ctx.fillStyle = axis;
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (const t of yTicks) {
    const y = yOf(t);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(cssW - padR, y); ctx.stroke();
    ctx.fillText(t.toFixed(opts.yDigits ?? 1), padL - 6, y);
  }

  // X ticks (time)
  ctx.textAlign = "center";
  const xCount = Math.min(6, Math.max(2, Math.floor(plotW / 90)));
  const spanMs = tMax - tMin;
  const oneDay = 86400000;
  const dtOpts = spanMs > oneDay * 3
    ? { month: "short", day: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" };
  for (let i = 0; i <= xCount; i++) {
    const t = tMin + (spanMs * i) / xCount;
    const x = xOf(t);
    ctx.fillStyle = axis;
    ctx.fillText(new Date(t).toLocaleString(undefined, dtOpts), x, cssH - padB / 2);
  }

  // Series lines
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  for (const s of series) {
    if (!s.points.length) continue;
    ctx.strokeStyle = s.color;
    ctx.beginPath();
    s.points.forEach((p, i) => {
      const x = xOf(p.t), y = yOf(p.y);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // Interactive cursor: dashed vertical guide + a dot on each series at its
  // nearest sampled point, so scrubbing/hovering reads off the exact values.
  if (opts.cursorT != null) {
    const ct = Math.min(tMax, Math.max(tMin, opts.cursorT));
    const cx = xOf(ct);
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = themeColor("--chart-cursor", "rgba(31,36,33,0.35)");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, cssH - padB); ctx.stroke();
    ctx.setLineDash([]);
    const markerRing = themeColor("--chart-marker-ring", "#fff");
    for (const s of series) {
      const p = valueAt(s.points, ct);
      if (!p) continue;
      const py = yOf(p.y);
      ctx.beginPath();
      ctx.arc(cx, py, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = s.color;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = markerRing;
      ctx.stroke();
    }
  }
}

// FFT-style spectrum chart: x-axis is a fixed set of categories (e.g. frequency
// bands), y-axis is value (dB). Each snapshot is one measurement's reading
// across every category, drawn as its own line. Older snapshots share the
// base hue but fade with age (oldest = faint/thin); the latest snapshot is
// drawn separately in solid black so the current spectrum is unmistakable at
// a glance, regardless of hive colour.
//
// drawSpectrumChart(canvas, categories, snapshots, opts)
//   categories: [label, ...]
//   snapshots:  [{ t: epochMillis, values: [number|null, ...] }] oldest→newest,
//               values aligned 1:1 with categories
//   opts:       { unit, yDigits, color, cursorIndex, bandStats }
//
// When opts.cursorIndex (a category index) is set, draws a vertical guide at
// that band plus a bracket spanning bandStats[cursorIndex].min..max — the
// full range for that band across the selected time range, not just the
// downsampled lines actually drawn — and stashes the pixel<->category mapping
// on canvas._catScale so callers can turn a pointer x back into a category
// index (see attachSpectrumCursor in views.js).
export function drawSpectrumChart(canvas, categories, snapshots, opts = {}) {
  const wrap = canvas.parentElement;
  let empty = wrap.querySelector(".chart-empty");
  const hasData = snapshots.some((s) => s.values.some((v) => typeof v === "number" && Number.isFinite(v)));

  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 600;
  const cssH = canvas.clientHeight || 300;
  const bmpW = Math.round(cssW * dpr);
  const bmpH = Math.round(cssH * dpr);
  if (canvas.width !== bmpW) canvas.width = bmpW;
  if (canvas.height !== bmpH) canvas.height = bmpH;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  if (!hasData) {
    if (!empty) {
      empty = document.createElement("div");
      empty.className = "chart-empty";
      empty.textContent = "No data available";
      wrap.append(empty);
    }
    return;
  }
  if (empty) empty.remove();

  const padL = 48, padR = 14, padT = 12, padB = 26;
  const plotW = cssW - padL - padR;
  const plotH = cssH - padT - padB;
  const n = categories.length;

  let yMin = Infinity, yMax = -Infinity;
  for (const s of snapshots) {
    for (const v of s.values) {
      if (typeof v !== "number" || !Number.isFinite(v)) continue;
      if (v < yMin) yMin = v;
      if (v > yMax) yMax = v;
    }
  }
  const pad = (yMax - yMin) * 0.12 || 1;
  yMin -= pad; yMax += pad;

  const xOf = (i) => (n <= 1 ? padL + plotW / 2 : padL + (i / (n - 1)) * plotW);
  const yOf = (y) => padT + (1 - (y - yMin) / (yMax - yMin)) * plotH;
  canvas._catScale = { padL, plotW, n };

  const axis = themeColor("--chart-axis", AXIS);
  const grid = themeColor("--chart-grid", GRID);

  ctx.font = FONT;
  ctx.textBaseline = "middle";

  // Y gridlines + labels
  const yTicks = niceTicks(yMin, yMax, 5);
  ctx.strokeStyle = grid;
  ctx.fillStyle = axis;
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  for (const t of yTicks) {
    const y = yOf(t);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(cssW - padR, y); ctx.stroke();
    ctx.fillText(t.toFixed(opts.yDigits ?? 1), padL - 6, y);
  }

  // X gridlines + category labels (one per band, evenly spaced)
  ctx.textAlign = "center";
  for (let i = 0; i < n; i++) {
    const x = xOf(i);
    ctx.strokeStyle = grid;
    ctx.beginPath(); ctx.moveTo(x, padT); ctx.lineTo(x, cssH - padB); ctx.stroke();
    ctx.fillStyle = axis;
    ctx.fillText(categories[i], x, cssH - padB / 2);
  }

  // Older snapshot lines, oldest→newest, faded by age so recency reads at a
  // glance. The latest snapshot is excluded here and drawn separately below in
  // solid black so the current spectrum is unmistakable regardless of hue.
  const base = opts.color || "#f2a900";
  const older = snapshots.slice(0, -1);
  const count = older.length;
  ctx.lineJoin = "round";
  older.forEach((s, idx) => {
    const age = count <= 1 ? 1 : idx / (count - 1); // 0 = oldest, 1 = most recent of the older ones
    ctx.strokeStyle = withAlpha(base, 0.15 + age * 0.6);
    ctx.lineWidth = 1.2 + age * 1.2;
    ctx.beginPath();
    let started = false;
    s.values.forEach((v, i) => {
      if (typeof v !== "number" || !Number.isFinite(v)) return;
      const x = xOf(i), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    if (started) ctx.stroke();
  });

  // Latest snapshot: solid high-contrast line + dots, drawn last so it's on top.
  const latestColor = themeColor("--chart-latest", LATEST_COLOR);
  const latest = snapshots[snapshots.length - 1];
  if (latest) {
    ctx.strokeStyle = latestColor;
    ctx.lineWidth = 2.4;
    ctx.beginPath();
    let started = false;
    latest.values.forEach((v, i) => {
      if (typeof v !== "number" || !Number.isFinite(v)) return;
      const x = xOf(i), y = yOf(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    if (started) ctx.stroke();
    ctx.fillStyle = latestColor;
    latest.values.forEach((v, i) => {
      if (typeof v !== "number" || !Number.isFinite(v)) return;
      ctx.beginPath(); ctx.arc(xOf(i), yOf(v), 3, 0, Math.PI * 2); ctx.fill();
    });
  }

  // Interactive cursor: dashed vertical guide at the hovered band, plus a
  // bracket spanning that band's min..max across the full selected time
  // range so scrubbing reads off the spread even where the drawn lines are
  // downsampled.
  if (opts.cursorIndex != null && n) {
    const idx = Math.min(n - 1, Math.max(0, opts.cursorIndex));
    const cx = xOf(idx);
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = themeColor("--chart-cursor", "rgba(31,36,33,0.35)");
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, cssH - padB); ctx.stroke();
    ctx.setLineDash([]);
    const stats = opts.bandStats && opts.bandStats[idx];
    if (stats) {
      const yLo = yOf(stats.min), yHi = yOf(stats.max);
      ctx.strokeStyle = themeColor("--chart-cursor-strong", "rgba(31,36,33,0.5)");
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(cx, yLo); ctx.lineTo(cx, yHi); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx - 5, yLo); ctx.lineTo(cx + 5, yLo); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(cx - 5, yHi); ctx.lineTo(cx + 5, yHi); ctx.stroke();
    }
  }
}

// Nearest data point to timestamp `t` (points must be sorted ascending by t).
export function valueAt(points, t) {
  if (!points || !points.length) return null;
  let lo = 0, hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid].t < t) lo = mid + 1; else hi = mid;
  }
  if (lo === 0) return points[0];
  const before = points[lo - 1], after = points[lo];
  if (!after) return before;
  return (t - before.t) <= (after.t - t) ? before : after;
}

// Build a {label,color,points} series from measurements (newest-first) for a key.
export function seriesFrom(measurements, key, label, color) {
  const points = [];
  // iterate oldest→newest so the line draws left-to-right
  for (let i = measurements.length - 1; i >= 0; i--) {
    const m = measurements[i];
    if (m == null) continue;
    // Coerce numeric-looking strings (e.g. Postgres NUMERIC columns serialized as
    // strings) so the line still plots instead of silently dropping every point.
    const raw = m[key];
    if (raw == null || raw === "") continue;
    const y = typeof raw === "number" ? raw : Number(raw);
    if (!Number.isFinite(y)) continue;
    const t = new Date(m.measured_at).getTime();
    if (Number.isNaN(t)) continue;
    points.push({ t, y });
  }
  return { label, color, points };
}
