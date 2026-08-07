// HiveHub public embed — renders one published chart at /embed/chart/{token}.
//
// The page shell holds nothing but the token; everything below comes from
// GET /api/v1/public/charts/{token}, the same public JSON any site can fetch.
// Drawing goes through the dashboard's own charts.js (served next to this file
// by the embed asset whitelist), so a published chart is pixel-for-pixel the
// chart it was published from — there is no second chart implementation to keep
// in sync.

import { drawLineChart } from "./charts.js";

const root = document.getElementById("embed");
const token = root.dataset.token;

// Embeds live on pages that stay open for hours (a club homepage, a shop
// window display), so refresh on a slow timer. The server caches public
// payloads for a minute, and hive data arrives every few minutes anyway.
const REFRESH_MS = 5 * 60 * 1000;

let current = null; // last successful payload — kept for redraws on resize

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

function fmt(v, digits) {
  return typeof v === "number" && Number.isFinite(v) ? v.toFixed(digits) : "–";
}

function fmtDateTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "–" : d.toLocaleString();
}

// [isoString, value] pairs → the {t, y} points charts.js draws.
function toPoints(pairs) {
  const out = [];
  for (const [at, value] of pairs || []) {
    const t = new Date(at).getTime();
    if (Number.isFinite(t) && typeof value === "number" && Number.isFinite(value)) {
      out.push({ t, y: value });
    }
  }
  return out;
}

function state(message) {
  root.replaceChildren(el("p", { class: "embed-state" }, message));
}

function header(data) {
  return [
    el("h1", { class: "embed-title" }, data.title),
    data.subtitle ? el("p", { class: "embed-sub" }, data.subtitle) : null,
  ];
}

function footer(data) {
  if (!data.show_updated) return null;
  const parts = [`Updated ${fmtDateTime(data.updated_at)}`];
  if (data.aggregate && data.aggregate !== "none") {
    parts.push({ daily_max: "daily maximum", daily_min: "daily minimum", daily_avg: "daily average" }[data.aggregate]);
  }
  parts.push(`last ${data.range_days} day${data.range_days === 1 ? "" : "s"}`);
  return el("p", { class: "embed-foot" }, parts.join(" · "));
}

// "value" publications: the current reading per series, plus its change over the
// published period — a number to drop into a page, not a diagram.
function renderValues(data) {
  const tiles = data.series.map((s) =>
    el("div", { class: "embed-value" },
      el("div", { class: "label" },
        el("span", { class: "swatch", style: `background:${s.color};width:.6rem;height:.6rem;border-radius:2px;display:inline-block` }),
        s.label),
      el("div", { class: "value" },
        fmt(s.latest ? s.latest.value : null, data.digits),
        data.unit ? el("span", { class: "unit" }, " " + data.unit) : null),
      el("div", { class: "change" },
        s.change == null
          ? (s.latest ? fmtDateTime(s.latest.at) : "No readings in this period")
          : `${s.change >= 0 ? "+" : ""}${fmt(s.change, data.digits)}${data.unit ? " " + data.unit : ""} over ${data.range_days}d`)));
  root.replaceChildren(...[
    ...header(data),
    el("div", { class: "embed-values" }, ...tiles),
    footer(data),
  ].filter(Boolean));
}

function renderChart(data) {
  const canvas = el("canvas", { role: "img", "aria-label": `${data.title} — ${data.metric_label} chart` });
  const wrap = el("div", { class: "chart-wrap", style: `height:${data.height}px` }, canvas);
  const legend = data.show_legend
    ? el("div", { class: "embed-legend" },
        ...data.series.map((s) =>
          el("span", { class: "lg" },
            el("span", { class: "swatch", style: `background:${s.color}` }),
            s.label,
            el("span", { class: "lg-value" },
              s.latest ? `${fmt(s.latest.value, data.digits)}${data.unit ? " " + data.unit : ""}` : "–"))))
    : null;
  root.replaceChildren(...[...header(data), legend, wrap, footer(data)].filter(Boolean));
  draw(data, canvas);
}

function draw(data, canvas) {
  drawLineChart(
    canvas,
    data.series.map((s) => ({ label: s.label, color: s.color, points: toPoints(s.points) })),
    { unit: data.unit, yDigits: data.digits });
}

function render(data) {
  current = data;
  // A pinned theme wins over the viewer's OS setting; "auto" leaves the
  // stylesheet's prefers-color-scheme block in charge.
  if (data.theme === "light" || data.theme === "dark") {
    document.documentElement.setAttribute("data-theme", data.theme);
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  if (data.chart_type === "value") renderValues(data);
  else renderChart(data);
}

async function load() {
  try {
    const res = await fetch(`/api/v1/public/charts/${encodeURIComponent(token)}`, { cache: "no-store" });
    if (res.status === 404 || res.status === 410) {
      state("This chart is no longer published.");
      current = null;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch (err) {
    // Keep the last good render on a transient failure — an embed that blanks
    // out because one refresh timed out looks broken to the site's visitors.
    if (!current) state("This chart could not be loaded.");
    console.error("HiveHub embed:", err);
  }
}

// Canvas charts are drawn at a fixed pixel size, so a resized iframe (or a
// phone rotating) needs a redraw, not just a re-layout.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const canvas = root.querySelector("canvas");
    if (current && canvas) draw(current, canvas);
  }, 120);
});

load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
