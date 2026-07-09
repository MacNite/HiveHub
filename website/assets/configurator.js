/* HiveHub secrets.h configurator
 * Pure client-side: reads the form, emits a firmware/include/secrets.h file.
 * Nothing is ever sent over the network. */
(function () {
  "use strict";

  var $ = function (sel, root) { return (root || document).querySelector(sel); };
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

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

  // ── Hive registry ────────────────────────────────────────────────────────────
  // Mirrors the on-device provisioning portal's model (firmware/src/portal.cpp):
  // each hive owns at most one scale channel (HX711 pin pair, a NAU7802 channel —
  // main bus or behind a TCA9548A mux — or a wireless beehivemonitoring.com
  // HiveScale) and at most one in-hive sensor (a wired DS18B20 ROM or one
  // wireless BLE/GATT pairing). Up to MAX_HIVES (matches firmware's default).
  var MAX_HIVES = 18;
  var SENSOR_TYPES = [
    ["holyiot", "HolyIot 25015 — beacon"],
    ["ruuvitag", "RuuviTag — beacon"],
    ["hiveinside_nrf54", "HiveInside (nRF54LM20A) — beacon"],
    ["hiveinside", "Legacy HiveInside (ESP32-C6) — GATT"],
    ["hiveheart", "HiveHeart — GATT (beehivemonitoring.com)"],
    ["beecounter", "HiveTraffic counter — GATT (polled on hives 1-2 only)"]
  ];
  // Passively-scanned beacon types (one shared scan window, no GATT connection).
  // The nRF54 HiveInside advertises continuously, so it joins the beacon set;
  // only the legacy ESP32-C6 HiveInside ("hiveinside") is read over GATT.
  var SCAN_TYPES = { holyiot: 1, ruuvitag: 1, hiveinside_nrf54: 1, hiveinside: 1, hiveheart: 1 };

  var hiveList = $("#hive-list");
  var HIVES = []; // {i, n, sk, ds: string|null, bl: {t,m}|null, wm}

  function isC6() { return val("#mcu") === "xiao_esp32c6"; }

  // Every theoretical wired scale channel; HX711 only exists on the classic
  // board (config.h force-disables it on the XIAO C6).
  function allWiredScales() {
    var out = [];
    if (!isC6()) {
      out.push({ b: "hx", hx: 0, label: "HX711 #1 (pins)" });
      out.push({ b: "hx", hx: 1, label: "HX711 #2 (pins)" });
    }
    out.push({ b: "nau", mux: -1, adc: 1, label: "NAU7802 main bus CH1" });
    out.push({ b: "nau", mux: -1, adc: 2, label: "NAU7802 main bus CH2" });
    for (var ch = 0; ch < 8; ch++) {
      out.push({ b: "nau", mux: ch, adc: 1, label: "NAU7802 mux ch" + ch + " CH1" });
      out.push({ b: "nau", mux: ch, adc: 2, label: "NAU7802 mux ch" + ch + " CH2" });
    }
    return out;
  }
  function scaleKey(o) { return o.b === "nau" ? ("nau:" + o.mux + ":" + o.adc) : ("hx:" + o.hx); }
  function findWiredScale(k) {
    var scales = allWiredScales();
    for (var i = 0; i < scales.length; i++) if (scaleKey(scales[i]) === k) return scales[i];
    return null;
  }
  function usedScaleKeys(except) {
    var u = {};
    HIVES.forEach(function (h) { if (h !== except && h.sk !== "none" && h.sk !== "ble") u[h.sk] = 1; });
    return u;
  }
  function nextFreeScaleKey() {
    var used = usedScaleKeys();
    var scales = allWiredScales();
    for (var i = 0; i < scales.length; i++) if (!used[scaleKey(scales[i])]) return scaleKey(scales[i]);
    return "none";
  }
  function nextHiveIndex() {
    var used = {};
    HIVES.forEach(function (h) { used[h.i] = 1; });
    for (var n = 1; n <= MAX_HIVES; n++) if (!used[n]) return n;
    return MAX_HIVES;
  }

  function scaleSelectHtml(h) {
    var used = usedScaleKeys(h);
    var scales = allWiredScales().filter(function (o) { return !used[scaleKey(o)]; });
    var sel = h.sk || "none";
    var html = '<option value="none"' + (sel === "none" ? " selected" : "") + '>(no scale)</option>';
    var seenSel = (sel === "none" || sel === "ble");
    scales.forEach(function (o) {
      var k = scaleKey(o);
      if (k === sel) seenSel = true;
      html += '<option value="' + k + '"' + (k === sel ? " selected" : "") + '>' + o.label + '</option>';
    });
    if (!seenSel) {
      var saved = findWiredScale(sel);
      html = '<option value="' + sel + '" selected>' + (saved ? saved.label : sel) + ' (unavailable on this board)</option>' + html;
    }
    html += '<option value="ble"' + (sel === "ble" ? " selected" : "") + '>BLE HiveScale from Beehivemonitoring</option>';
    return html;
  }

  function sensorTypeSelectHtml(selected) {
    return SENSOR_TYPES.map(function (t) {
      return '<option value="' + t[0] + '"' + (t[0] === selected ? " selected" : "") + '>' + t[1] + '</option>';
    }).join("");
  }

  function addHive() {
    if (HIVES.length >= MAX_HIVES) { toast("⚠ Max " + MAX_HIVES + " hives"); return; }
    HIVES.push({ i: nextHiveIndex(), n: "", sk: nextFreeScaleKey(), ds: null, bl: null, wm: "" });
    renderHives();
    render();
  }
  function removeHive(h) {
    var idx = HIVES.indexOf(h);
    if (idx >= 0) HIVES.splice(idx, 1);
    renderHives();
    render();
  }

  function renderHives() {
    hiveList.innerHTML = "";
    HIVES.forEach(function (h) {
      var card = document.createElement("div");
      card.className = "sensor on";
      card.setAttribute("data-hive", h.i);

      var scaleExtra = h.sk === "ble"
        ? '<div class="field" style="margin-top:.4rem"><label>HiveScale MAC address</label>' +
          '<input type="text" data-hwm placeholder="AA:BB:CC:DD:EE:FF" /></div>'
        : "";

      var sensorHtml;
      if (h.ds !== null) {
        sensorHtml =
          '<div class="field row" style="align-items:flex-end">' +
            '<div class="field" style="margin:0;flex:1"><label>DS18B20 ROM <span class="hint">— optional, 16 hex chars; blank falls back to probe order</span></label>' +
              '<input type="text" data-hds pattern="[0-9A-Fa-f]{16}" maxlength="16" placeholder="28FF64B2711304A3" /></div>' +
            '<div style="flex:0 0 auto"><button type="button" class="btn ghost small" data-hsensor-remove>✕</button></div>' +
          '</div>';
      } else if (h.bl !== null) {
        sensorHtml =
          '<div class="field row" style="align-items:flex-end">' +
            '<div class="field" style="margin:0"><label>Sensor type</label><select data-hbt>' + sensorTypeSelectHtml(h.bl.t) + '</select></div>' +
            '<div class="field" style="margin:0;flex:1"><label>MAC address</label><input type="text" data-hbm placeholder="AA:BB:CC:DD:EE:FF" /></div>' +
            '<div style="flex:0 0 auto"><button type="button" class="btn ghost small" data-hsensor-remove>✕</button></div>' +
          '</div>';
      } else {
        sensorHtml =
          '<p class="muted" style="font-size:.85rem;margin:.2rem 0">No in-hive sensor.</p>' +
          '<button type="button" class="btn ghost small" data-add-ds style="margin-right:.4rem">➕ Add DS18B20</button>' +
          '<button type="button" class="btn ghost small" data-add-ble>➕ Add BLE sensor</button>';
      }

      card.innerHTML =
        '<div class="row" style="align-items:flex-end">' +
          '<div class="field" style="margin:0;flex:1"><label>Hive ' + h.i + ' name <span class="hint">— optional</span></label>' +
            '<input type="text" data-hn placeholder="e.g. Apiary A #3" /></div>' +
          '<div style="flex:0 0 auto"><button type="button" class="btn ghost small" data-hremove>Remove hive</button></div>' +
        '</div>' +
        '<div class="field" style="margin-top:.4rem"><label>Scale</label><select data-hscale>' + scaleSelectHtml(h) + '</select></div>' +
        scaleExtra +
        '<div style="margin-top:.6rem"><span class="hint" style="display:block;margin-bottom:.3rem">In-hive sensor</span>' + sensorHtml + '</div>';
      hiveList.appendChild(card);

      $('[data-hn]', card).value = h.n || "";
      $('[data-hn]', card).addEventListener("input", function () { h.n = this.value; render(); });

      $('[data-hscale]', card).addEventListener("change", function () {
        h.sk = this.value;
        if (h.sk !== "ble") h.wm = "";
        renderHives(); render();
      });
      var wm = $('[data-hwm]', card);
      if (wm) {
        wm.value = h.wm || "";
        wm.addEventListener("input", function () { h.wm = this.value.trim(); render(); });
      }
      $('[data-hremove]', card).addEventListener("click", function () { removeHive(h); });

      var dsInput = $('[data-hds]', card);
      if (dsInput) {
        dsInput.value = h.ds || "";
        dsInput.addEventListener("input", function () { h.ds = this.value.trim().toUpperCase(); render(); });
      }
      var btSel = $('[data-hbt]', card);
      if (btSel) btSel.addEventListener("change", function () { h.bl.t = this.value; render(); });
      var bmInput = $('[data-hbm]', card);
      if (bmInput) {
        bmInput.value = (h.bl && h.bl.m) || "";
        bmInput.addEventListener("input", function () { h.bl.m = this.value.trim(); render(); });
      }
      var rmSensor = $('[data-hsensor-remove]', card);
      if (rmSensor) rmSensor.addEventListener("click", function () { h.ds = null; h.bl = null; renderHives(); render(); });
      var addDs = $('[data-add-ds]', card);
      if (addDs) addDs.addEventListener("click", function () { h.ds = ""; h.bl = null; renderHives(); render(); });
      var addBle = $('[data-add-ble]', card);
      if (addBle) addBle.addEventListener("click", function () { h.bl = { t: "holyiot", m: "" }; h.ds = null; renderHives(); render(); });
    });

    $("#hive-empty").style.display = HIVES.length ? "none" : "block";
    var full = HIVES.length >= MAX_HIVES;
    $("#add-hive").disabled = full;
    $("#hive-full").style.display = full ? "block" : "none";

    var anyInHiveBle = HIVES.some(function (h) { return h.bl && SCAN_TYPES[h.bl.t]; }) ||
                        HIVES.some(function (h) { return h.bl && h.bl.t === "beecounter"; }) ||
                        HIVES.some(function (h) { return h.sk === "ble"; });
    $("#inhive-global").style.display = anyInHiveBle ? "block" : "none";
  }

  $("#add-hive").addEventListener("click", addHive);

  // ── MCU selection ─────────────────────────────────────────────────────────
  function onMcuChange() {
    var isC6v = isC6();
    $("#antenna-row").style.display = isC6v ? "block" : "none";
    $("#c6-wired-notice").style.display = isC6v ? "block" : "none";
    ["mics", "accel"].forEach(function (name) {
      var s = $('.sensor[data-sensor="' + name + '"]');
      if (!s) return;
      var cb = $('[data-toggle]', s);
      cb.disabled = isC6v;
      s.style.opacity = isC6v ? "0.4" : "";
      s.style.pointerEvents = isC6v ? "none" : "";
      if (isC6v) s.classList.remove("on");
      else if (cb.checked) s.classList.add("on");
    });
    // HX711 doesn't exist on the C6 — drop any hive currently pinned to it
    // rather than silently emitting a channel the firmware force-disables.
    if (isC6v) {
      HIVES.forEach(function (h) { if (h.sk && h.sk.indexOf("hx:") === 0) h.sk = "none"; });
    }
    var envSpan = $("#pio-env");
    if (envSpan) envSpan.textContent = isC6v ? "xiao_esp32c6" : "esp32dev";
    renderHives();
    render();
  }
  $("#mcu").addEventListener("change", onMcuChange);

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
    var isC6v = val("#mcu") === "xiao_esp32c6";

    p("#ifndef SECRETS_H");
    p("#define SECRETS_H");
    p("");
    p("// Generated by the HiveHub secrets.h configurator.");
    p("// Save as firmware/include/secrets.h — this file is gitignored, never commit it.");
    p("// Build: pio run -e " + val("#mcu") + " --target upload");
    p("");

    if (isC6v) {
      p("// ==============================");
      p("// XIAO ESP32-C6 HARDWARE");
      p("// ==============================");
      p("// 0 = built-in ceramic patch antenna (GPIO3 LOW, default).");
      p("// 1 = external u.FL connector (GPIO3 HIGH).");
      p(def("XIAO_C6_USE_EXTERNAL_ANTENNA", $("#ext_antenna").checked ? "1" : "0"));
      p("");
    }

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

    // DS18B20 — a global bus enable; which hives actually have a probe mapped
    // lives in each hive's HIVE_i_JSON "ds" field (see the HIVES section below).
    var anyDs = HIVES.some(function (h) { return h.ds !== null; });
    p("// DS18B20 wired in-hive temperature (single 1-Wire bus, ROM-addressed per hive).");
    p(def("ENABLE_DS18B20_HIVE_TEMP", anyDs ? "1" : "0"));
    p("");

    // INMP441 mics
    p("// INMP441 stereo I2S microphones with per-band FFT.");
    if (isC6v) p("// Not available on XIAO ESP32-C6.");
    var micsOn = enabled("mics") && !isC6v;
    p(def("ENABLE_INMP441_MICS", micsOn ? "1" : "0"));
    if (micsOn) {
      p(def("INMP441_BCLK_PIN", opt("mics", "bclk")));
      p(def("INMP441_WS_PIN", opt("mics", "ws")));
      p(def("INMP441_SD_PIN", opt("mics", "sd")));
      p(def("INMP441_SAMPLE_RATE", opt("mics", "rate")));
      p(def("INMP441_SAMPLE_FRAMES", opt("mics", "frames")));
    }
    p("");

    // Accelerometer (legacy — genuinely capped at 2 by I2C address space: the
    // LIS3DH/LIS2DH12 SDO/SA0 pin only selects between 0x18 and 0x19).
    p("// LIS3DH / LIS2DH12 vibration accelerometers (I2C) — legacy, 2 fixed I2C");
    p("// addresses, so at most 2 regardless of hive count. Prefer a BLE in-hive");
    p("// sensor for vibration on any other hive.");
    if (isC6v) p("// Not available on XIAO ESP32-C6.");
    var accelOn = enabled("accel") && !isC6v;
    p(def("ENABLE_LIS3DH_ACCEL", accelOn ? "1" : "0"));
    if (accelOn) {
      p(def("LIS3DH_ADDR_SLOT_1", opt("accel", "addr1")));
      p(def("LIS3DH_ADDR_SLOT_2", opt("accel", "addr2")));
      p(def("LIS3DH_RANGE_G", opt("accel", "range")));
      p(def("LIS3DH_ODR_HZ", opt("accel", "odr")));
      p(def("LIS3DH_SAMPLE_COUNT", opt("accel", "count")));
    }
    p("");

    buildHives(p);

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

  // Build a hive's JSON blob in the exact shape hiveToJson()/hiveFromJson()
  // (firmware/src/hive_config.cpp) use, so HIVE_i_JSON round-trips through the
  // same parser the on-device portal's save handler feeds.
  function hiveJson(h) {
    var o = { i: h.i };
    if (h.n && h.n.trim()) o.n = h.n.trim();

    o.s = [];
    if (h.sk && h.sk !== "none" && h.sk !== "ble") {
      var wired = findWiredScale(h.sk);
      if (wired) {
        o.s.push(wired.b === "nau"
          ? { b: "nau", mux: wired.mux, adc: wired.adc, off: 0, fac: -7050.0 }
          : { b: "hx", hx: wired.hx, off: 0, fac: -7050.0 });
      }
    }

    if (h.ds !== null) o.ds = h.ds || "";

    o.bl = [];
    if (h.bl !== null && h.bl.m && h.bl.m.trim()) o.bl.push({ t: h.bl.t, m: h.bl.m.trim() });
    if (h.sk === "ble" && h.wm && h.wm.trim()) o.bl.push({ t: "hivescale", m: h.wm.trim() });

    return JSON.stringify(o);
  }

  // ── HIVES + the global flags their sensor choices imply ─────────────────────
  function buildHives(p) {
    var anyBeacon = HIVES.some(function (h) { return h.bl && SCAN_TYPES[h.bl.t]; });
    var anyHiveInsideGatt = HIVES.some(function (h) { return h.bl && h.bl.t === "hiveinside"; });
    var anyBeehive = HIVES.some(function (h) { return (h.bl && h.bl.t === "hiveheart") || h.sk === "ble"; });
    var anyBeecounter = HIVES.some(function (h) { return h.bl && h.bl.t === "beecounter"; });

    p("// ==============================");
    p("// HIVES (up to 18 — matches the on-device provisioning portal's registry)");
    p("// ==============================");
    p("// Each HIVE_i_JSON pre-seeds hive i's scale source and in-hive sensor on");
    p("// FIRST BOOT, in the exact blob shape the portal itself saves to NVS (see");
    p("// hive_config.cpp / docs/multi-hive.md). Entirely optional — skip this");
    p("// section and configure hives from the on-device portal instead.");
    p(def("HIVE_COUNT", String(HIVES.length)));
    HIVES.forEach(function (h) {
      p(defStr("HIVE_" + h.i + "_JSON", hiveJson(h)));
    });
    p("");

    p("// In-hive BLE bridge: one shared passive scan per cycle for beacon sensors");
    p("// (HolyIot / RuuviTag / HiveInside beacon mode) and to locate GATT sensors");
    p("// by MAC before connecting. Needed whenever any hive uses one of these.");
    p(def("ENABLE_BLE_SCAN", anyBeacon ? "1" : "0"));
    if (anyBeacon) {
      p(def("HOLYIOT_BLE_SCAN_SECONDS", val("#ble_scan") || "6"));
      p(def("HOLYIOT_BLE_ACTIVE_SCAN", $("#ble_active").checked ? "1" : "0"));
      p(def("HOLYIOT_COMPANY_ID", val("#ble_company") || "0xFFFF"));
    }
    if (anyHiveInsideGatt) p(def("HIVEINSIDE_USE_GATT", "1"));
    p("");

    p("// beehivemonitoring.com GATT: HiveHeart (in-hive sensor) and/or a wireless");
    p("// HiveScale (scale source). Both share one service/characteristic UUID.");
    p(def("ENABLE_BEEHIVE_GATT", anyBeehive ? "1" : "0"));
    p("");

    p("// HiveTraffic wireless entrance bee counter (GATT). Currently only polled");
    p("// on hives 1-2 regardless of which hive(s) a counter is paired to.");
    p(def("ENABLE_WIRELESS_BEECOUNTER", anyBeecounter ? "1" : "0"));
    p("");

    if (anyBeacon || anyBeehive) {
      p("// Collision avoidance: a hive's BLE in-hive sensor overrides its wired");
      p("// sensor for the same quantity, per hive.");
      p(def("BLE_OVERRIDE_DS18B20", $("#ovr_temp").checked ? "1" : "0"));
      p(def("BLE_OVERRIDE_MICS", $("#ovr_mics").checked ? "1" : "0"));
      p(def("BLE_OVERRIDE_ACCEL", $("#ovr_accel").checked ? "1" : "0"));
      p("");
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

  // Initial paint — seed the historical default of 2 hives (HX711 #1 / #2 on
  // the classic board), matching the pre-multi-hive tool's starting point.
  addHive();
  addHive();
  onMcuChange();
})();
