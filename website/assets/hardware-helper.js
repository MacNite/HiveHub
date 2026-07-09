// HiveHub hardware helper prototype — dependency-free questionnaire logic.
(function () {
  "use strict";

  var MAX_BLE_DEVICES = 16;

  var DEVICE_META = {
    hiveinside: {
      label: "HiveInside",
      features: ["ENABLE_BLE_SCAN", "ENABLE_BEEHIVE_GATT"],
      guide: "Pair HiveInside devices and decide whether their data overrides wired in-hive sensors."
    },
    hiveheart: {
      label: "Beehivemonitoring.com HiveHeart",
      features: ["ENABLE_BLE_SCAN", "ENABLE_BEEHIVE_GATT"],
      guide: "Pair HiveHeart devices and map each sensor to a hive."
    },
    hivescale: {
      label: "Beehivemonitoring.com HiveScale",
      features: ["ENABLE_BLE_SCAN", "ENABLE_BEEHIVE_GATT"],
      guide: "Pair wireless HiveScale devices and map each scale to a hive."
    },
    holyiot: {
      label: "HolyIOT25015",
      features: ["ENABLE_BLE_SCAN"],
      guide: "Pair HolyIOT25015 beacons and configure beacon scan timing."
    }
  };

  var SETUP_META = {
    simple: {
      title: "Simple scale system",
      subtitle: "Good first build for one or two hives, with optional BLE in-hive sensors.",
      capacity: "1–2 wired scale channels",
      defaultScaleMax: 2
    },
    multi: {
      title: "Multi-scale system",
      subtitle: "For larger apiaries that should start with multiple scales and stay expandable.",
      capacity: "up to 16 scales in this helper profile",
      defaultScaleMax: 16
    },
    bridge: {
      title: "BLE bridge without wired scales",
      subtitle: "For forwarding BLE / beehivemonitoring device data into HiveHub without building wired scales.",
      capacity: "no wired scale channels",
      defaultScaleMax: 0
    }
  };

  var form = document.getElementById("hardware-helper");
  if (!form) return;

  var scaleFieldset = document.getElementById("scale-fieldset");
  var scaleCountRow = document.getElementById("build-scale-count");
  var scaleCount = document.getElementById("scaleCount");
  var scaleNote = document.getElementById("scale-note");
  var bleTotal = document.getElementById("ble-total");
  var bleMeter = document.getElementById("ble-meter");
  var bleLimitNote = document.getElementById("ble-limit-note");
  var flowEl = document.getElementById("setup-flow");
  var kvEl = document.getElementById("profile-kv");
  var pillsEl = document.getElementById("profile-pills");
  var guideEl = document.getElementById("guide-list");
  var previewEl = document.getElementById("profile-preview");
  var titleEl = document.getElementById("profile-title");
  var subtitleEl = document.getElementById("profile-subtitle");
  var copyButton = document.getElementById("copy-profile");
  var resetButton = document.getElementById("reset-helper");
  var toast = document.getElementById("toast");
  var bleMeterBar = bleMeter ? bleMeter.querySelector("span") : null;

  function checkedValue(name) {
    var el = form.querySelector("input[name='" + name + "']:checked");
    return el ? el.value : "";
  }

  function clampNumber(value, min, max) {
    var n = parseInt(value, 10);
    if (isNaN(n)) return min;
    return Math.max(min, Math.min(max, n));
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast._timer);
    showToast._timer = window.setTimeout(function () { toast.classList.remove("show"); }, 1800);
  }

  function syncChoiceCards() {
    var groups = form.querySelectorAll("[data-choice-group]");
    for (var i = 0; i < groups.length; i++) {
      var choices = groups[i].querySelectorAll(".choice");
      for (var j = 0; j < choices.length; j++) {
        var input = choices[j].querySelector("input[type='radio']");
        choices[j].classList.toggle("is-selected", !!input && input.checked);
      }
    }
  }

  // Enable/disable, clamp and range the interactive controls. This is the only
  // place that writes back to inputs, and it runs on structural changes (radio /
  // checkbox / reset) — never on every keystroke — so number fields stay
  // editable while the user is typing.
  function normalizeControls() {
    var setup = checkedValue("setup") || "simple";
    var scaleMax = (SETUP_META[setup] || SETUP_META.simple).defaultScaleMax;
    scaleCount.max = String(Math.max(1, scaleMax || 1));
    scaleCount.value = setup === "bridge" ? "0" : String(clampNumber(scaleCount.value, 1, scaleMax || 1));

    var rows = form.querySelectorAll(".ble-device");
    for (var i = 0; i < rows.length; i++) {
      var toggle = rows[i].querySelector("[data-device-toggle]");
      var count = rows[i].querySelector("[data-device-count]");
      if (!count) continue;
      var enabled = !!toggle && toggle.checked;
      count.disabled = !enabled;
      if (!enabled) count.value = "0";
      else count.value = String(Math.max(1, clampNumber(count.value, 0, MAX_BLE_DEVICES)));
    }
  }

  // Read-only: reports the currently selected BLE devices without mutating them.
  function collectBleDevices() {
    var rows = form.querySelectorAll(".ble-device");
    var devices = [];
    var total = 0;

    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var key = row.getAttribute("data-device");
      var toggle = row.querySelector("[data-device-toggle]");
      var count = row.querySelector("[data-device-count]");
      var enabled = !!toggle && toggle.checked;

      var amount = enabled && count ? clampNumber(count.value, 0, MAX_BLE_DEVICES) : 0;
      if (amount > 0 && DEVICE_META[key]) {
        devices.push({ key: key, label: DEVICE_META[key].label, amount: amount });
        total += amount;
      }
    }

    return { devices: devices, total: total };
  }

  function selectedBleSummary(devices) {
    if (!devices.length) return "No BLE/GATT devices selected";
    return devices.map(function (d) { return d.amount + "× " + d.label; }).join(", ");
  }

  function unique(items) {
    var seen = Object.create(null);
    var out = [];
    for (var i = 0; i < items.length; i++) {
      if (!items[i] || seen[items[i]]) continue;
      seen[items[i]] = true;
      out.push(items[i]);
    }
    return out;
  }

  function deriveProfile() {
    var power = checkedValue("power") || "available";
    var setup = checkedValue("setup") || "simple";
    var scaleSource = checkedValue("scaleSource") || "build";
    var setupMeta = SETUP_META[setup] || SETUP_META.simple;
    var scaleMax = setupMeta.defaultScaleMax;

    var ownScaleCount = setup === "bridge" || scaleSource !== "build" ? 0 : clampNumber(scaleCount.value, 1, scaleMax || 1);
    var ble = collectBleDevices();
    var hasBleScales = ble.devices.some(function (d) { return d.key === "hivescale"; });
    var hasAnyBle = ble.total > 0 || scaleSource === "ble-existing";

    var board;
    var scaleInterface;
    var powerPlan;
    var profileKey;
    var recommendations = [];
    var features = ["Wi-Fi provisioning portal", "SD cache / backup", "OTA update support"];
    var warnings = [];

    if (setup === "bridge") {
      board = "XIAO ESP32-C6 or ESP32 DevKit as BLE/GATT bridge";
      scaleInterface = hasBleScales || scaleSource === "ble-existing" ? "BLE/GATT wireless scales only" : "No scale interface selected";
      profileKey = "ble-bridge";
      features.push("ENABLE_BLE_SCAN", "ENABLE_BEEHIVE_GATT");
      recommendations.push("Skip load-cell amplifier wiring and focus the guide on BLE pairing, hive mapping and backend deployment.");
    } else if (setup === "multi") {
      board = "XIAO ESP32-C6 Scale Module plus NAU7802 breakout PCB (I2C load-cell frontend)";
      scaleInterface = scaleSource === "ble-existing" ? "BLE/GATT wireless scales" : "NAU7802 channels behind the breakout's I2C mux (up to 16 wired scales)";
      profileKey = "multi-scale";
      recommendations.push("Use the multi-scale guide path with I2C address planning, per-scale calibration and expansion notes.");
    } else {
      board = "XIAO ESP32-C6 Scale Module (recommended; the 30-pin ESP32 DevKit is legacy)";
      scaleInterface = scaleSource === "ble-existing" ? "BLE/GATT wireless scales" : "NAU7802 I2C channels (2 scales on the Scale Module)";
      profileKey = "simple-scale";
      recommendations.push("Start with the simple wiring guide: NAU7802 scale channels, I2C devices, SD card and setup button.");
    }

    if (power === "offgrid") {
      powerPlan = "Battery + solar panel with charger/regulator, INA219 solar telemetry and MAX17048 battery gauge";
      features.push("ENABLE_INA219_SOLAR", "ENABLE_MAX17048_BATTERY");
      recommendations.push("Add an off-grid power section covering charger, regulator, battery gauge, solar telemetry and weatherproofing.");
    } else {
      powerPlan = "Stable grid / PoE-derived supply with a regulated 5 V or 3.3 V rail";
      recommendations.push("Add a power section for grid or PoE-derived supply, common ground and outdoor cable strain relief.");
    }

    if (scaleSource === "build") {
      recommendations.push("Add mechanical scale-building steps, load-cell wiring checks and calibration for " + ownScaleCount + " scale" + (ownScaleCount === 1 ? "" : "s") + ".");
    } else if (scaleSource === "wired-existing") {
      recommendations.push("Add an adapter checklist for existing hive scales: signal type, excitation voltage, connector pinout and calibration.");
    } else if (scaleSource === "ble-existing") {
      features.push("ENABLE_BLE_SCAN", "ENABLE_BEEHIVE_GATT");
      recommendations.push("Add BLE scale discovery, pairing and hive-to-scale mapping steps.");
    }

    for (var i = 0; i < ble.devices.length; i++) {
      var meta = DEVICE_META[ble.devices[i].key];
      if (!meta) continue;
      for (var f = 0; f < meta.features.length; f++) features.push(meta.features[f]);
      recommendations.push(meta.guide);
    }

    if (ble.total > MAX_BLE_DEVICES) {
      warnings.push("Too many BLE/GATT devices selected. Reduce the optional device total to 16 or less.");
    }

    if (setup === "bridge" && scaleSource === "build") {
      warnings.push("The 'system without wired scales' setup ignores the own-scale count. Select a scale system if you want to build wired scales.");
    }

    return {
      power: power,
      setup: setup,
      setupTitle: setupMeta.title,
      setupSubtitle: setupMeta.subtitle,
      scaleSource: scaleSource,
      ownScaleCount: ownScaleCount,
      selectedBleDevices: ble.devices,
      bleTotal: ble.total,
      board: board,
      scaleInterface: scaleInterface,
      powerPlan: powerPlan,
      profileKey: profileKey,
      capacity: setupMeta.capacity,
      features: unique(features),
      recommendations: unique(recommendations),
      warnings: warnings
    };
  }

  function renderScaleVisibility(profile) {
    var isBridge = profile.setup === "bridge";
    scaleFieldset.style.opacity = isBridge ? ".72" : "1";
    scaleCountRow.style.display = profile.scaleSource === "build" ? "grid" : "none";

    if (isBridge) {
      scaleNote.style.display = "block";
      scaleNote.textContent = "Bridge mode is intended for BLE/GATT devices without wired scale channels. You can still select BLE HiveScale devices in section 4.";
    } else if (profile.setup === "multi") {
      scaleNote.style.display = "block";
      scaleNote.textContent = "Multi-scale mode allows up to 16 scales in this helper. The later guide should choose the exact scale frontend and wiring strategy.";
    } else {
      scaleNote.style.display = "none";
      scaleNote.textContent = "";
    }
  }

  function renderBleLimit(profile) {
    bleTotal.textContent = String(profile.bleTotal);
    var percent = Math.min(100, Math.round((profile.bleTotal / MAX_BLE_DEVICES) * 100));
    if (bleMeterBar) bleMeterBar.style.width = percent + "%";
    bleMeter.classList.toggle("over", profile.bleTotal > MAX_BLE_DEVICES);
    bleMeter.setAttribute("aria-valuenow", String(profile.bleTotal));
    bleMeter.setAttribute("aria-valuetext", profile.bleTotal + " of " + MAX_BLE_DEVICES + " BLE/GATT devices selected");
    bleLimitNote.className = profile.bleTotal > MAX_BLE_DEVICES ? "note warn" : "note info";
  }

  function renderFlow(profile) {
    var steps = [
      { title: "Power", text: profile.power === "offgrid" ? "Solar + battery" : "Grid / PoE" },
      { title: "HiveHub type", text: profile.setupTitle },
      { title: "Scales", text: profile.scaleInterface },
      { title: "BLE devices", text: profile.bleTotal + " selected" },
      { title: "Generated guide", text: "BOM + wiring + firmware + deploy" }
    ];

    flowEl.innerHTML = "";
    for (var i = 0; i < steps.length; i++) {
      var step = document.createElement("div");
      step.className = "flow-step";
      var title = document.createElement("b");
      title.textContent = steps[i].title;
      var text = document.createElement("span");
      text.textContent = steps[i].text;
      step.appendChild(title);
      step.appendChild(text);
      flowEl.appendChild(step);
    }
  }

  function renderProfile(profile) {
    titleEl.textContent = profile.setupTitle;
    subtitleEl.textContent = profile.setupSubtitle;

    var kv = [
      ["Board", profile.board],
      ["Power", profile.powerPlan],
      ["Scale path", profile.scaleInterface],
      ["Capacity", profile.capacity],
      ["BLE/GATT", selectedBleSummary(profile.selectedBleDevices)]
    ];

    kvEl.innerHTML = "";
    for (var i = 0; i < kv.length; i++) {
      var row = document.createElement("div");
      var dt = document.createElement("dt");
      var dd = document.createElement("dd");
      dt.textContent = kv[i][0];
      dd.textContent = kv[i][1];
      row.appendChild(dt);
      row.appendChild(dd);
      kvEl.appendChild(row);
    }

    pillsEl.innerHTML = "";
    var pills = [profile.profileKey, profile.power === "offgrid" ? "off-grid" : "powered-apiary"];
    if (profile.scaleSource === "build") pills.push("build-own-scales");
    if (profile.scaleSource === "wired-existing") pills.push("existing-wired-scales");
    if (profile.scaleSource === "ble-existing" || profile.bleTotal > 0) pills.push("ble-enabled");
    for (var p = 0; p < pills.length; p++) {
      var pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = pills[p];
      pillsEl.appendChild(pill);
    }

    guideEl.innerHTML = "";
    var allRecommendations = profile.recommendations.slice();
    for (var w = 0; w < profile.warnings.length; w++) allRecommendations.unshift("⚠ " + profile.warnings[w]);
    for (var r = 0; r < allRecommendations.length; r++) {
      var li = document.createElement("li");
      li.textContent = allRecommendations[r];
      if (allRecommendations[r].indexOf("⚠") === 0) li.className = "invalid";
      guideEl.appendChild(li);
    }

    previewEl.textContent = buildProfileText(profile);
  }

  function buildProfileText(profile) {
    var bleLines = profile.selectedBleDevices.length
      ? profile.selectedBleDevices.map(function (d) { return "    - type: " + d.key + "\n      amount: " + d.amount; }).join("\n")
      : "    []";

    var featureLines = profile.features.map(function (f) { return "    - " + f; }).join("\n");
    var warningLines = profile.warnings.length
      ? profile.warnings.map(function (w) { return "    - " + w; }).join("\n")
      : "    []";

    return "profile_name: " + profile.profileKey + "\n" +
      "initial_setup: " + profile.setup + "\n" +
      "power: " + profile.power + "\n" +
      "board_recommendation: " + profile.board + "\n" +
      "scale_source: " + profile.scaleSource + "\n" +
      "own_scale_count: " + profile.ownScaleCount + "\n" +
      "scale_interface: " + profile.scaleInterface + "\n" +
      "ble_gatt_devices:\n" + bleLines + "\n" +
      "firmware_features:\n" + featureLines + "\n" +
      "warnings:\n" + warningLines + "\n" +
      "guide_output:\n" +
      "    - bill-of-materials\n" +
      "    - wiring-plan\n" +
      "    - firmware-config\n" +
      "    - calibration-steps\n" +
      "    - backend-deployment\n";
  }

  function render() {
    syncChoiceCards();
    var profile = deriveProfile();
    renderScaleVisibility(profile);
    renderBleLimit(profile);
    renderFlow(profile);
    renderProfile(profile);
  }

  // Structural changes (radio / checkbox toggles, committed number edits) may
  // enable, disable or re-range controls, so normalize before re-rendering.
  form.addEventListener("change", function () {
    normalizeControls();
    render();
  });

  // Live typing in number fields: re-render the preview without rewriting the
  // field value (that would fight the user mid-edit). Radios and checkboxes are
  // handled by the change listener above.
  form.addEventListener("input", function (event) {
    var t = event.target;
    if (t && (t.type === "radio" || t.type === "checkbox")) return;
    render();
  });

  if (copyButton) {
    copyButton.addEventListener("click", function () {
      var text = previewEl.textContent || "";
      if (!navigator.clipboard) {
        showToast("Copy is not available in this browser");
        return;
      }
      navigator.clipboard.writeText(text).then(function () {
        showToast("Profile copied");
      }, function () {
        showToast("Copy failed");
      });
    });
  }

  if (resetButton) {
    resetButton.addEventListener("click", function () {
      form.reset();
      normalizeControls();
      render();
      showToast("Questionnaire reset");
    });
  }

  normalizeControls();
  render();
})();
