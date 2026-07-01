// DEMO data source for the HiveHub dashboard.
//
// Drop-in replacement for the real server/dashboard/assets/api.js: it exposes the
// same `api` surface but serves deterministic, generated sample data entirely in
// the browser — no backend, no network. Used by the public "frontend demo" on the
// website so people can click through the dashboard without installing anything.
//
// Read methods return representative curves; the write methods (firmware /
// calibration) are intentionally disabled and reject with a friendly message so
// the UI shows "this is a read-only demo" instead of pretending to act.

const DEVICES = [
  {
    device_id: "demo-garden-01", display_name: "Garden Apiary 01",
    claimed_at: "2026-03-01T08:00:00Z",
    last_firmware_version: "0.20.1",
    channels: { scale_1: "North hive", scale_2: "South hive" },
    twoHives: true, solar: true,
  },
  {
    device_id: "demo-rooftop-02", display_name: "Rooftop 02",
    claimed_at: "2026-04-15T08:00:00Z",
    last_firmware_version: "0.20.1",
    channels: { scale_1: "Rooftop hive", scale_2: null },
    twoHives: false, solar: false,
  },
];

// deterministic pseudo-noise in [-1, 1] from an integer seed
function noise(seed) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return (x - Math.floor(x)) * 2 - 1;
}

const DAY = 86400000;

function point(dev, t, i) {
  const dayPhase = 2 * Math.PI * ((t % DAY) / DAY); // 0..2π over a day
  const monthSwell = Math.sin((2 * Math.PI * t) / (DAY * 30));
  const seed = Math.floor(t / 600000) + (dev.device_id === "demo-rooftop-02" ? 9000 : 0);

  const hive1 = 35.1 + 0.55 * Math.sin(dayPhase) + 0.15 * noise(seed);
  const ambient = 16 + 9 * Math.sin(dayPhase - 0.6) + 3 * monthSwell + 0.6 * noise(seed + 1);
  const w1 = 42 + 3 * monthSwell + 0.8 * Math.sin(dayPhase) + 0.15 * noise(seed + 2);
  const w2 = 38 + 2.6 * monthSwell + 0.7 * Math.cos(dayPhase) + 0.15 * noise(seed + 3);
  const soc = Math.max(28, Math.min(100, 72 + 26 * Math.sin(dayPhase - 1.0)));
  const solarP = dev.solar ? Math.max(0, 1500 * Math.sin(dayPhase - 1.2) + 80 * noise(seed + 4)) : null;
  const traffic = Math.max(0, 42 * Math.sin(dayPhase - 0.8));

  const m = {
    id: 1_000_000 - i,
    device_id: dev.device_id,
    measured_at: new Date(t).toISOString(),
    scale_1_weight_kg: w1, scale_1_weight_kg_compensated: w1,
    hive_1_temp_c: hive1,
    hive_1_humidity_percent: 55 + 5 * Math.sin(dayPhase + Math.PI) + 0.5 * noise(seed + 5),
    ambient_temp_c: ambient,
    ambient_humidity_percent: 60 + 12 * Math.sin(dayPhase + Math.PI) + 0.6 * noise(seed + 6),
    ble_1_pressure_hpa: 1013 + 5 * monthSwell + 0.5 * noise(seed + 7),
    battery_soc_percent: soc,
    battery_voltage: 3.6 + (soc / 100) * 0.6,
    battery_alert: false,
    rssi_dbm: -62 + 6 * Math.sin(dayPhase) + noise(seed + 8),
    network_transport: "wifi", cellular_ok: null, cellular_csq: null,
    time_source: "ntp", rtc_ok: true, calibration_mode: false,
    firmware_version: dev.last_firmware_version, boot_count: 14,
    mic_left_rms_dbfs: -43 + 4 * Math.sin(dayPhase) + 0.4 * noise(seed + 9),
    mic_right_rms_dbfs: -44 + 4 * Math.cos(dayPhase) + 0.4 * noise(seed + 10),
    mic_left_peak_dbfs: -20 + 3 * Math.sin(dayPhase),
    mic_right_peak_dbfs: -21 + 3 * Math.cos(dayPhase),
    mic_sample_rate_hz: 16000, mic_sample_frames: 1024,
    mic_left_band_sub_bass_dbfs: -55 + 2 * Math.sin(dayPhase),
    mic_left_band_hum_dbfs: -37 + 3 * Math.sin(dayPhase),
    mic_left_band_piping_dbfs: -60 + 2 * noise(seed + 11),
    mic_left_band_stress_dbfs: -50 + 2 * Math.sin(dayPhase),
    mic_left_band_high_dbfs: -70 + 2 * noise(seed + 12),
    mic_right_band_sub_bass_dbfs: -56 + 2 * Math.cos(dayPhase),
    mic_right_band_hum_dbfs: -38 + 3 * Math.cos(dayPhase),
    mic_right_band_piping_dbfs: -61 + 2 * noise(seed + 13),
    mic_right_band_stress_dbfs: -51 + 2 * Math.cos(dayPhase),
    mic_right_band_high_dbfs: -71 + 2 * noise(seed + 14),
    bee_counter_1_total_in: 12000 + Math.round((t / DAY) % 1000) * 4,
    bee_counter_1_total_out: 11850 + Math.round((t / DAY) % 1000) * 4,
    bee_counter_1_interval_in: Math.round(traffic + 4 * noise(seed + 15) + 4),
    bee_counter_1_interval_out: Math.round(traffic * 0.95 + 4 * noise(seed + 16) + 4),
  };

  if (dev.twoHives) {
    m.scale_2_weight_kg = w2; m.scale_2_weight_kg_compensated = w2;
    m.hive_2_temp_c = hive1 - 0.4 + 0.2 * noise(seed + 17);
    m.hive_2_humidity_percent = 54 + 5 * Math.sin(dayPhase + Math.PI);
    m.bee_counter_2_total_in = 9800 + Math.round((t / DAY) % 1000) * 3;
    m.bee_counter_2_total_out = 9700 + Math.round((t / DAY) % 1000) * 3;
    m.bee_counter_2_interval_in = Math.round(traffic * 0.8 + 4 * noise(seed + 18) + 3);
    m.bee_counter_2_interval_out = Math.round(traffic * 0.78 + 4 * noise(seed + 19) + 3);
  }
  if (dev.solar) {
    m.solar_power_mw = solarP;
    m.solar_current_ma = solarP != null ? solarP / 5 : null;
    m.solar_bus_voltage_v = 5.0 + 0.3 * Math.sin(dayPhase - 1.2);
  }
  return m;
}

function findDevice(id) {
  return DEVICES.find((d) => d.device_id === id) || DEVICES[0];
}

// Generate a newest-first series spanning [start, now] (default 7 days).
function series(deviceId, startIso) {
  const dev = findDevice(deviceId);
  const now = Date.now();
  const start = startIso ? new Date(startIso).getTime() : now - 7 * DAY;
  const span = Math.max(DAY, now - start);
  const count = 320;
  const out = [];
  for (let i = 0; i <= count; i++) {
    const t = start + (span * i) / count;
    out.push(point(dev, t, i));
  }
  out.reverse(); // newest first, matching the real API
  return out;
}

const demoErr = () =>
  Promise.reject(new Error("This is a read-only demo — firmware and calibration actions are disabled."));

const wrap = (v) => new Promise((r) => setTimeout(() => r(v), 120)); // tiny latency for realism

export const api = {
  listDevices: () => wrap(DEVICES.map(({ twoHives, solar, ...d }) => ({
    ...d, last_seen_at: new Date().toISOString(),
  }))),

  measurements: (deviceId, { start } = {}) => wrap(series(deviceId, start)),

  latest: (deviceId) => wrap([series(deviceId)[0]]),

  config: (deviceId) => wrap({
    device_id: deviceId, send_interval_seconds: 900,
    scale1_offset: 12345, scale1_factor: 21.3,
    scale2_offset: 12010, scale2_factor: 21.7,
    config_version: 4, tempco_enabled: true, tempco_source: "hive_1",
    tempco_ref_temp_c: 25.0, scale1_tempco_kg_per_c: -0.012, scale2_tempco_kg_per_c: -0.011,
  }),

  channels: (deviceId) => {
    const dev = findDevice(deviceId);
    return wrap({
      scale_1_display_name: dev.channels.scale_1,
      scale_2_display_name: dev.channels.scale_2,
    });
  },

  insightsSummary: (deviceId) => wrap({
    device_id: deviceId, computed_at: new Date().toISOString(),
    alert_count: 1, highest_severity: "warning",
    highest_alert: {
      severity: "warning", title: "Possible nectar dearth",
      message: "Weight has been roughly flat for 3 days during the expected flow — consider checking forage.",
    },
    categories: { swarming: 0, activity: 1, overwintering: 0 },
  }),

  insightsHistory: (deviceId, { status = "all" } = {}) => {
    const now = Date.now();
    const alerts = [
      {
        alert_key: "activity.nectar_dearth", status: "active", severity: "warning",
        peak_severity: "warning", title: "Possible nectar dearth",
        description: "Weight has been roughly flat for 3 days during the expected flow — consider checking forage.",
        first_seen_at: new Date(now - 3 * DAY).toISOString(),
        last_seen_at: new Date(now).toISOString(), resolved_at: null,
      },
      {
        alert_key: "power.battery_low", status: "resolved", severity: "info",
        peak_severity: "warning", title: "Battery running low",
        description: "State of charge dipped below 35 % during a cloudy spell.",
        first_seen_at: new Date(now - 12 * DAY).toISOString(),
        last_seen_at: new Date(now - 9 * DAY).toISOString(),
        resolved_at: new Date(now - 9 * DAY).toISOString(),
      },
    ].filter((a) => status === "all" || a.status === status);
    return wrap({ device_id: deviceId, alerts });
  },

  firmwareStatus: (deviceId) => wrap({
    device_id: deviceId, target: "hivescale", current_version: "0.20.1",
    latest_version: "0.21.0", latest_is_official: true,
    approved_version: null, update_available: true, pending_approval: true,
  }),

  // write actions are disabled in the demo
  uploadFirmware: demoErr,
  approveFirmware: demoErr,
  startCalibration: demoErr,
  stopCalibration: demoErr,
  fitTempCompensation: demoErr,
  updateConfig: demoErr,
  updateChannels: demoErr,
  // account management is auth-backed in the real dashboard; disabled here
  listUsers: demoErr,
  createUser: demoErr,
  deleteUser: demoErr,
  changePassword: demoErr,
  updateEmail: demoErr,
};
