// Formatting + small DOM helpers shared across views.

export const DASH = "–";

export function isNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

// Format a number to `digits` decimals, or the em-dash placeholder when missing.
export function fmt(v, digits = 1, unit = "") {
  if (!isNum(v)) return DASH;
  const s = v.toFixed(digits);
  return unit ? `${s}${unit}` : s;
}

export function fmtInt(v) {
  return isNum(v) ? Math.round(v).toLocaleString() : DASH;
}

export function fmtDateTime(iso) {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

// Compact relative age, e.g. "3m ago", "2h ago", "5d ago".
export function relAge(iso) {
  if (!iso) return DASH;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return DASH;
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

// tiny hyperscript-ish element builder.
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}

// Latest non-null value of `key` scanning measurements newest→oldest.
// `measurements` is assumed newest-first (the API returns DESC).
export function latestOf(measurements, key) {
  for (const m of measurements) {
    if (m[key] != null) return m[key];
  }
  return null;
}

// Severity → badge class.
export function sevClass(sev) {
  switch ((sev || "").toLowerCase()) {
    case "critical":
    case "high": return "danger";
    case "warning":
    case "medium": return "warn";
    case "info":
    case "low": return "info";
    default: return "muted";
  }
}
