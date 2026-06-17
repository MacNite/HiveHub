/* HiveScale secrets.h configurator
 * Pure client-side: reads the form, emits a firmware/include/secrets.h file.
 * Nothing is ever sent over the network. */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ── Wireless sensor catalog ─────────────────────────────────────────────────
  // Each entry: human label, category (slotting + limit), transport protocol and
  // — for GATT devices — the service / characteristic UUIDs to pre-fill. The
  // `supported` flag drives the "not in firmware yet" warning shown in the UI.
  var WTYPES = {
    holyiot:    { label: "HolyIot 25015 (BLE beacon)",                       cat: "inhive",     proto: "beacon", supported: true  },
    hiveinside: { label: "HiveInside ESP32-C6 (GATT)",                       cat: "inhive",     proto: "gatt",   supported: true,
                  svc: "8e8b0001-7a1c-4b9e-9a2f-1d6e0b9c1a01", chr: "8e8b0002-7a1c-4b9e-9a2f-1d6e0b9c1a01" },
    hiveheart:  { label: "HiveHeart — beehivemonitoring.com (GATT)",         cat: "inhive",     proto: "gatt",   supported: true, bhgatt: true,
                  svc: "0d01c3b8-eff2-44bc-9260-3256eb957268", chr: "513849eb-913d-4f80-8c44-3f0685533d6e" },
    hivescale:  { label: "HiveScale — beehivemonitoring.com (GATT)",         cat: "scale",      proto: "gatt",   supported: true, bhgatt: true,
                  svc: "0d01c3b8-eff2-44bc-9260-3256eb957268", chr: "513849eb-913d-4f80-8c44-3f0685533d6e" },
    beecounter: { label: "BeeCounter — beehivemonitoring.com (GATT)",        cat: "beecounter", proto: "gatt",   supported: false },
    ruvitag:    { label: "RuuviTag 4-in-1 (BLE beacon)",                     cat: "inhive",     proto: "beacon", supported: false }
  };
  var TYPE_ORDER = ["holyiot", "hiveinside", "hiveheart", "hivescale", "beecounter", "ruvitag"];
  var CAT_LABEL = { inhive: "In-hive", scale: "Scale", beecounter: "Bee counter" };
  var CAT_SLOT  = { inhive: "hive", scale: "scale", beecounter: "counter" };
  var CAT_LIMIT = 2;
  var MAX_TOTAL = 6;

  // ── Build the Wi-Fi rows (network 1 always on, 2 & 3 optional) ──────────────
  var wifiDefs = [
    { i: 1, on: true,  ssid: "your-wifi-ssid-1", pass: "your-wifi-password-1", required: true },
    { i: 2, on: false, ssid: "your-wifi-ssid-2", pass: "your-wifi-password-2", required: false },
    { i: 3, on: false, ssid: "your-wifi-ssid-3", pass: "your-wifi-password-3", required: false }
  ];
  var wifiList = $("#wifi-list");
  wifiDefs.forEach(function (w) {
    var row = document.createElement("div");
    row.className = "sensor" + (w.on ? " on" : "");
    row.setAttribute("data-wifi", w.i);
    row.innerHTML =
      '<label class="toggle">' +
        '<input type="checkbox" data-wtoggle ' + (w.on ? "checked" : "") + (w.required ? " disabled" : "") + ' />' +
        '<span><span class="t-title">📶 Network ' + w.i + (w.required ? " (required)" : "") + '</span></span>' +
      '</label>' +
      '<div class="opts">' +
        '<div class="field"><label>SSID</label><input type="text" data-wssid value="' + w.ssid + '" /></div>' +
        '<div class="field"><label>Password</label><input type="text" data-wpass value="' + w.pass + '" /></div>' +
      '</div>';
    wifiList.appendChild(row);
  });

  // ── Toggle wiring: show/hide option blocks ──────────────────────────────────
  $$(".sensor[data-sensor]").forEach(function (s) {
    var cb = $('[data-toggle]', s);
    if (cb.checked) s.classList.add("on");
    cb.addEventListener("change", function () { s.classList.toggle("on", cb.checked); render(); });
  });
  $$(".sensor[data-wifi]").forEach(function (s) {
    var cb = $('[data-wtoggle]', s);
    cb.addEventListener("change", function () { s.classList.toggle("on", cb.checked); render(); });
  });

  // ── Wireless sensor rows (dynamic add / remove) ─────────────────────────────
  var wireList = $("#wireless-list");
  var widSeq = 0;

  function typeOptionsHtml(selected) {
    return TYPE_ORDER.map(function (k) {
      return '<option value="' + k + '"' + (k === selected ? " selected" : "") + ">" + WTYPES[k].label + "</option>";
    }).join("");
  }

  // Rows currently configured for a category (DOM order), optionally excluding one.
  function rowsInCat(cat, except) {
    return $$(".wsensor", wireList).filter(function (r) {
      if (r === except) return false;
      return WTYPES[$('[data-wtype]', r).value].cat === cat;
    });
  }

  // The first catalog type whose category still has a free slot, or null if full.
  function firstAvailableType() {
    for (var i = 0; i < TYPE_ORDER.length; i++) {
      var k = TYPE_ORDER[i];
      if (rowsInCat(WTYPES[k].cat).length < CAT_LIMIT) return k;
    }
    return null;
  }

  function addWireless(typeKey) {
    var key = typeKey || firstAvailableType();
    if (!key) { toast("⚠ Max 6 wireless sensors (2 per category)"); return; }

    var id = ++widSeq;
    var row = document.createElement("div");
    row.className = "wsensor";
    row.setAttribute("data-wid", id);
    row.innerHTML =
      '<div class="row" style="align-items:flex-end">' +
        '<div class="field" style="margin:0"><label>Sensor type</label>' +
          '<select data-wtype>' + typeOptionsHtml(key) + '</select></div>' +
        '<div style="flex:0 0 auto"><button type="button" class="btn ghost small" data-wremove title="Remove">✕</button></div>' +
      '</div>' +
      '<p class="wmeta muted" style="font-size:.82rem;margin:.4rem 0 0"></p>' +
      '<div class="wgatt" style="display:none">' +
        '<div class="field"><label>Service UUID</label><input type="text" data-wsvc placeholder="service UUID" /></div>' +
        '<div class="field"><label>Characteristic UUID</label><input type="text" data-wchr placeholder="characteristic UUID" /></div>' +
      '</div>' +
      '<div class="wmac" style="display:none">' +
        '<div class="field"><label>MAC address <span class="hint">— optional; leave blank to pair in the device portal</span></label>' +
          '<input type="text" data-wmac placeholder="AA:BB:CC:DD:EE:FF" /></div>' +
      '</div>' +
      '<div class="note warn wunsupported" style="display:none">⚠ <strong>Not supported by the firmware yet.</strong> ' +
        'Its macros are written to <code>secrets.h</code> as a placeholder so your config is ready for a future build.</div>';
    wireList.appendChild(row);

    var sel = $('[data-wtype]', row);
    sel.addEventListener("change", function () { onTypeChange(row); });
    $('[data-wremove]', row).addEventListener("click", function () {
      row.parentNode.removeChild(row); refreshWireless(); render();
    });
    fillUuids(row, key);
    refreshWireless();
    render();
  }

  // Reject a type change that would overflow its category; otherwise re-fill UUIDs.
  function onTypeChange(row) {
    var sel = $('[data-wtype]', row);
    var cat = WTYPES[sel.value].cat;
    if (rowsInCat(cat, row).length >= CAT_LIMIT) {
      toast("⚠ Only " + CAT_LIMIT + " " + CAT_LABEL[cat].toLowerCase() + " sensors allowed");
      // Revert to a type whose category still has room.
      sel.value = firstAvailableTypeFor(row);
    }
    fillUuids(row, sel.value, true);
    refreshWireless();
    render();
  }

  function firstAvailableTypeFor(row) {
    for (var i = 0; i < TYPE_ORDER.length; i++) {
      var k = TYPE_ORDER[i];
      if (rowsInCat(WTYPES[k].cat, row).length < CAT_LIMIT) return k;
    }
    return TYPE_ORDER[0];
  }

  // Pre-fill the GATT UUID inputs from the catalog. `force` (set on an explicit
  // type change) refreshes them to the new type's defaults; otherwise only empty
  // fields are filled so a user's manual edits survive.
  function fillUuids(row, key, force) {
    var def = WTYPES[key];
    var svc = $('[data-wsvc]', row), chr = $('[data-wchr]', row);
    if (def.proto !== "gatt") { svc.value = ""; chr.value = ""; return; }
    if (force || !svc.value.trim()) svc.value = def.svc || "";
    if (force || !chr.value.trim()) chr.value = def.chr || "";
  }

  // Recompute slot labels, warnings and the shared in-hive block visibility.
  function refreshWireless() {
    var rows = $$(".wsensor", wireList);
    var seen = { inhive: 0, scale: 0, beecounter: 0 };
    var haveInhive = false;
    rows.forEach(function (r) {
      var def = WTYPES[$('[data-wtype]', r).value];
      var slot = ++seen[def.cat];
      if (def.cat === "inhive") haveInhive = true;
      $('.wmeta', r).textContent = CAT_LABEL[def.cat] + " · " + CAT_SLOT[def.cat] + " " + slot +
        "  ·  " + (def.proto === "gatt" ? "GATT" : "BLE beacon");
      $('.wgatt', r).style.display = def.proto === "gatt" ? "block" : "none";
      $('.wmac', r).style.display = def.bhgatt ? "block" : "none";
      $('.wunsupported', r).style.display = def.supported ? "none" : "block";
    });
    $("#wireless-empty").style.display = rows.length ? "none" : "block";
    $("#inhive-global").style.display = haveInhive ? "block" : "none";
    var full = rows.length >= MAX_TOTAL || firstAvailableType() === null;
    $("#add-wireless").disabled = full;
    $("#wireless-full").style.display = full ? "block" : "none";
  }

  $("#add-wireless").addEventListener("click", function () { addWireless(); });

  // Re-render on any input change.
  $("#cfg").addEventListener("input", render);

  // ── API key generator (32 random bytes → 64 hex chars, like openssl) ────────
  $("#gen_key").addEventListener("click", function () {
    var bytes = new Uint8Array(32);
    (window.crypto || window.msCrypto).getRandomValues(bytes);
    var hex = Array.prototype.map.call(bytes, function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
    $("#api_key").value = hex;
    render();
    toast("🔑 Random API key generated");
  });

  // ── Helpers ─────────────────────────────────────────────────────────────────
  function val(sel) { var el = $(sel); return el ? el.value.trim() : ""; }
  function opt(sensor, name) { var el = $('[data-opt="' + name + '"]', $('.sensor[data-sensor="' + sensor + '"]')); return el ? el.value.trim() : ""; }
  function enabled(sensor) { return $('.sensor[data-sensor="' + sensor + '"] [data-toggle]').checked; }
  function esc(s) { return String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"'); }
  function def(name, value) { return "#define " + name.padEnd(26, " ") + " " + value; }
  function defStr(name, value) { return def(name, '"' + esc(value) + '"'); }

  // ── The generator ───────────────────────────────────────────────────────────
  function build() {
    var L = [];
    var p = function (s) { L.push(s === undefined ? "" : s); };

    p("#ifndef SECRETS_H");
    p("#define SECRETS_H");
    p("");
    p("// Generated by the HiveScale secrets.h configurator.");
    p("// Save as firmware/include/secrets.h — this file is gitignored, never commit it.");
    p("");

    p("// ==============================");
    p("// DEVICE IDENTITY");
    p("// ==============================");
    p(defStr("DEVICE_ID", val("#device_id")));
    p(defStr("API_KEY", val("#api_key")));
    p(defStr("CLAIM_CODE", val("#claim_code")));
    p(def("CLAIM_CODE_REVISION", val("#claim_rev") || "1"));
    p("");

    p("// ==============================");
    p("// BACKEND CONFIG");
    p("// ==============================");
    p(defStr("API_BASE_URL", val("#api_base")));
    p("");

    p("// ==============================");
    p("// WIFI FALLBACK CREDENTIALS");
    p("// ==============================");
    p("// Used on first boot to seed Preferences (or as fallback if Preferences are empty).");
    $$(".sensor[data-wifi]").forEach(function (s) {
      var i = s.getAttribute("data-wifi");
      var on = $('[data-wtoggle]', s).checked;
      var ssid = $('[data-wssid]', s).value.trim();
      var pass = $('[data-wpass]', s).value.trim();
      if (on) {
        p(defStr("WIFI" + i + "_SSID", ssid));
        p(defStr("WIFI" + i + "_PASS", pass));
      } else {
        p("// " + defStr("WIFI" + i + "_SSID", ssid));
        p("// " + defStr("WIFI" + i + "_PASS", pass));
      }
    });
    p("");

    p("// ==============================");
    p("// SENSORS");
    p("// ==============================");
    p("// Each flag is numeric 0/1 because the firmware switches on it with #if.");
    p("");

    // DS18B20
    p("// DS18B20 wired in-hive temperature probes (hive 1 + hive 2 on GPIO 4).");
    p(def("ENABLE_DS18B20_HIVE_TEMP", enabled("ds18b20") ? "1" : "0"));
    p("");

    // INMP441 mics
    p("// INMP441 stereo I2S microphones with per-band FFT.");
    p(def("ENABLE_INMP441_MICS", enabled("mics") ? "1" : "0"));
    if (enabled("mics")) {
      p(def("INMP441_BCLK_PIN", opt("mics", "bclk")));
      p(def("INMP441_WS_PIN", opt("mics", "ws")));
      p(def("INMP441_SD_PIN", opt("mics", "sd")));
      p(def("INMP441_SAMPLE_RATE", opt("mics", "rate")));
      p(def("INMP441_SAMPLE_FRAMES", opt("mics", "frames")));
    }
    p("");

    // Accelerometer
    p("// LIS3DH / LIS2DH12 per-hive vibration accelerometers (I2C).");
    p(def("ENABLE_LIS3DH_ACCEL", enabled("accel") ? "1" : "0"));
    if (enabled("accel")) {
      p(def("LIS3DH_ADDR_SLOT_1", opt("accel", "addr1")));
      p(def("LIS3DH_ADDR_SLOT_2", opt("accel", "addr2")));
      p(def("LIS3DH_RANGE_G", opt("accel", "range")));
      p(def("LIS3DH_ODR_HZ", opt("accel", "odr")));
      p(def("LIS3DH_SAMPLE_COUNT", opt("accel", "count")));
    }
    p("");

    buildWireless(p);

    // INA219 solar
    p("// INA219 solar / load power telemetry (off-grid).");
    p(def("ENABLE_INA219_SOLAR", enabled("ina219") ? "1" : "0"));
    if (enabled("ina219")) {
      p(def("INA219_I2C_ADDRESS", opt("ina219", "addr")));
    }
    p("");

    // MAX17048 battery
    p("// MAX17048 LiPo fuel gauge telemetry (off-grid).");
    p(def("ENABLE_MAX17048_BATTERY", enabled("max17048") ? "1" : "0"));
    if (enabled("max17048")) {
      p(def("MAX17048_ALERT_PERCENT", opt("max17048", "alert")));
    }
    p("");

    p("// ==============================");
    p("// OPTIONAL FLAGS");
    p("// ==============================");
    p(def("FORCE_RESEED", $("#force_reseed").checked ? "true" : "false"));
    p(def("DEBUG_MODE", $("#debug_mode").checked ? "true" : "false"));
    p("");
    p("#endif // SECRETS_H");
    p("");

    return L.join("\n");
  }

  // ── Wireless sensor macros ────────────────────────────────────────────────
  function buildWireless(p) {
    var rows = $$(".wsensor", wireList);
    var byCat = { inhive: [], scale: [], beecounter: [] };
    rows.forEach(function (r) {
      var key = $('[data-wtype]', r).value;
      byCat[WTYPES[key].cat].push({ row: r, key: key, meta: WTYPES[key] });
    });

    p("// ==============================");
    p("// WIRELESS SENSORS (BLE)");
    p("// ==============================");
    p("// Up to 6 wireless sensors: at most 2 in-hive sensors, 2 scales, 2 bee");
    p("// counters. Pair each sensor's MAC from the provisioning portal after flashing.");
    p("// The in-hive BLE bridge and the beehivemonitoring.com GATT sensors (HiveHeart");
    p("// / HiveScale) are consumed by the firmware; the bee-counter macros are written");
    p("// so the device's intended layout is captured, ready for a future build.");
    p("");

    // beehivemonitoring.com GATT (HiveHeart / HiveScale) master switch + shared UUIDs.
    var bh = rows.filter(function (r) { return WTYPES[$('[data-wtype]', r).value].bhgatt; });
    if (bh.length) {
      var first = WTYPES[$('[data-wtype]', bh[0]).value];
      p("// ---- beehivemonitoring.com GATT (HiveHeart / HiveScale) ----");
      p(def("ENABLE_BEEHIVE_GATT", "1"));
      p(defStr("BEEHIVE_GATT_SERVICE_UUID", first.svc));
      p(defStr("BEEHIVE_GATT_CHAR_UUID", first.chr));
      p("");
    }

    // --- In-hive sensors (slot 1 -> hive 1, slot 2 -> hive 2) -----------------
    var inhive = byCat.inhive;
    p("// ---- In-hive BLE sensors (slot 1 -> hive 1, slot 2 -> hive 2) ----");
    p(def("ENABLE_BLE_SCAN", inhive.length ? "1" : "0"));
    if (inhive.length) {
      p(def("HOLYIOT_BLE_SCAN_SECONDS", val("#ble_scan") || "6"));
      p(def("HOLYIOT_BLE_ACTIVE_SCAN", $("#ble_active").checked ? "1" : "0"));
      p(def("HOLYIOT_COMPANY_ID", val("#ble_company") || "0xFFFF"));
      var usesGatt = inhive.some(function (s) { return s.key === "hiveinside" && s.meta.proto === "gatt"; });
      p(def("HIVEINSIDE_USE_GATT", usesGatt ? "1" : "0"));
      inhive.forEach(function (s, i) { emitSlot(p, "INHIVE_" + (i + 1), s); });
      p("");
      p("// In-hive BLE overrides the wired sensor measuring the same quantity (per hive).");
      p(def("BLE_OVERRIDE_DS18B20", $("#ovr_temp").checked ? "1" : "0"));
      p(def("BLE_OVERRIDE_MICS", $("#ovr_mics").checked ? "1" : "0"));
      p(def("BLE_OVERRIDE_ACCEL", $("#ovr_accel").checked ? "1" : "0"));
    }
    p("");

    // --- Wireless scales ------------------------------------------------------
    var scales = byCat.scale;
    p("// ---- Wireless scales (beehivemonitoring.com HiveScale, GATT) ----");
    p(def("ENABLE_WIRELESS_SCALE", scales.length ? "1" : "0"));
    scales.forEach(function (s, i) { emitSlot(p, "WSCALE_" + (i + 1), s); });
    p("");

    // --- Wireless bee counters ------------------------------------------------
    var bc = byCat.beecounter;
    p("// ---- Wireless bee counters (GATT) ----");
    p(def("ENABLE_WIRELESS_BEECOUNTER", bc.length ? "1" : "0"));
    bc.forEach(function (s, i) { emitSlot(p, "WBEECNT_" + (i + 1), s); });
    p("");
  }

  function emitSlot(p, prefix, s) {
    var m = s.meta;
    var tail = m.supported ? "" : "   // placeholder — no firmware support yet";
    p(defStr(prefix + "_TYPE", s.key) + tail);
    p(defStr(prefix + "_PROTOCOL", m.proto));
    if (m.proto === "gatt") {
      var svcEl = $('[data-wsvc]', s.row), chrEl = $('[data-wchr]', s.row);
      var svc = (svcEl && svcEl.value.trim()) || m.svc || "";
      var chr = (chrEl && chrEl.value.trim()) || m.chr || "";
      if (svc) p(defStr(prefix + "_GATT_SERVICE_UUID", svc));
      if (chr) p(defStr(prefix + "_GATT_CHAR_UUID", chr));
    }
    // Optional MAC seeding for beehivemonitoring.com devices (blank = pair in portal).
    if (m.bhgatt) {
      var macEl = $('[data-wmac]', s.row);
      var mac = macEl && macEl.value.trim();
      if (mac) p(defStr(prefix + "_MAC", mac));
    }
  }

  function render() { $("#preview").textContent = build(); }

  // ── Copy / download ─────────────────────────────────────────────────────────
  $("#copy").addEventListener("click", function () {
    var text = build();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { toast("📋 Copied to clipboard"); },
        function () { fallbackCopy(text); });
    } else { fallbackCopy(text); }
  });

  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); toast("📋 Copied to clipboard"); }
    catch (e) { toast("Copy failed — select the text manually"); }
    document.body.removeChild(ta);
  }

  $("#download").addEventListener("click", function () {
    var blob = new Blob([build()], { type: "text/plain;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url; a.download = "secrets.h";
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    toast("⬇️ Downloaded secrets.h");
  });

  // ── Toast ───────────────────────────────────────────────────────────────────
  var toastEl = $("#toast"), toastTimer;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 1800);
  }

  // Initial paint.
  refreshWireless();
  render();
})();
