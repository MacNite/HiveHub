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

const AXIS = "#8a9088";
const GRID = "rgba(31,36,33,0.08)";
const FONT = "11px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif";

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
  canvas.width = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
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

  ctx.font = FONT;
  ctx.textBaseline = "middle";

  // Y gridlines + labels
  const yTicks = niceTicks(yMin, yMax, 5);
  ctx.strokeStyle = GRID;
  ctx.fillStyle = AXIS;
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
    ctx.fillStyle = AXIS;
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
    ctx.strokeStyle = "rgba(31,36,33,0.35)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(cx, padT); ctx.lineTo(cx, cssH - padB); ctx.stroke();
    ctx.setLineDash([]);
    for (const s of series) {
      const p = valueAt(s.points, ct);
      if (!p) continue;
      const py = yOf(p.y);
      ctx.beginPath();
      ctx.arc(cx, py, 3.5, 0, Math.PI * 2);
      ctx.fillStyle = s.color;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#fff";
      ctx.stroke();
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
