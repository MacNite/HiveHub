// test_portal_pairing.js — the per-hive BLE pairing lanes as the PROVISIONING
// PORTAL applies them.
//
// firmware/host_test/test_ble_lanes.cpp covers the authoritative rule
// (ble_lanes.h, enforced by hiveFromJson on every load and save). This suite
// covers the other half: the setup page's own controller, which decides which
// add buttons are offered and what hives_json actually gets submitted. The
// controller is extracted verbatim from the R"HVJS(...)HVJS" block in
// firmware/src/portal.cpp and run in jsdom, so these assertions run against the
// exact JavaScript the ESP32 serves — not a copy that can drift.
//
// NOT wired into .github/workflows/ci.yml: it needs a Node toolchain the other
// suites do not, and adding one to CI was out of scope for the change that
// introduced it. Run it by hand when touching the setup page:
//
//   npm install jsdom && node test-data/test_portal_pairing.js
//
// It exits non-zero on failure, so it can be dropped into CI later as-is.
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.log('SKIP: jsdom is not installed (npm install jsdom to run this suite).');
  process.exit(0);
}

// Pull the controller out of the firmware source so the test can never drift
// from what the device serves.
const portalCpp = fs.readFileSync(
  path.join(__dirname, '..', 'firmware', 'src', 'portal.cpp'), 'utf8');
const block = portalCpp.match(/R"HVJS\(([\s\S]*?)\)HVJS"/);
if (!block) {
  console.log('FAIL: could not find the R"HVJS(...)HVJS" controller in portal.cpp');
  process.exit(1);
}
const js = block[1].replace('<script>', '').replace('</script>', '');

let checks = 0, failures = 0;
function check(label, cond) {
  checks++;
  if (!cond) { failures++; console.log('FAIL: ' + label); }
}
function eq(label, got, want) {
  const g = JSON.stringify(got), w = JSON.stringify(want);
  checks++;
  if (g !== w) { failures++; console.log(`FAIL: ${label}\n  got  ${g}\n  want ${w}`); }
}

// Build a page with the ids/containers the controller expects, seed the globals
// portal.cpp emits inline, then run the controller.
function boot(initialHives, detectedBle) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <form id="cfgform"><input id="hives_json"><div id="hives"></div>
    <span id="hempty"></span><span id="hfull"></span>
    <button id="addhive"></button><datalist id="dsprobeopts"></datalist>
    </form></body></html>`, { runScripts: 'outside-only' });
  const w = dom.window;
  w.DETECTED_SCALES = [{ b: 'hx', hx: 0 }, { b: 'hx', hx: 1 }];
  w.DETECTED_PROBES = ['28AABBCCDDEE0011'];
  w.DEFAULT_NAU_SCALE = null;
  w.BLE_SCAN = 1;
  w.BLE_SCAN_SECONDS = 6;
  w.DETECTED_BLE = detectedBle || [];
  w.INITIAL_HIVES = initialHives;
  w.MAX_HIVES = 18;
  w.MAX_BLE = 4;
  w.MAX_INHIVE_BLE = 3;
  w.fetch = () => Promise.reject(new Error('no network in test'));
  dom.window.eval(js);
  return dom;
}

// Submitting the form is what serialises hives_json.
function submit(dom) {
  const form = dom.window.document.getElementById('cfgform');
  form.dispatchEvent(new dom.window.Event('submit', { cancelable: true }));
  return JSON.parse(dom.window.document.getElementById('hives_json').value);
}

const hive = (bl, ds) => ({ i: 1, n: 'A', s: [{ b: 'hx', hx: 0, off: 0, fac: -7050 }], ds: ds || null, bl });

// ── 1. The combination this change is about survives a round-trip ────────────
{
  const dom = boot([hive([
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
  ])]);
  const out = submit(dom);
  eq('HiveInside + HiveTraffic both persist', out[0].bl, [
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
  ]);
}

// ── 2. Three in-hive lanes + a wireless HiveScale = four pairings ────────────
{
  const h = hive([
    { t: 'hivescale',        m: 'AA:BB:CC:DD:EE:00' },
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
    { t: 'hiveheart',        m: 'AA:BB:CC:DD:EE:03' },
  ]);
  h.s = [];   // wireless scale source instead of a wired channel
  const out = submit(boot([h]));
  check('four pairings stored', out[0].bl.length === 4);
  check('hivescale kept as the scale source',
        out[0].bl.some(b => b.t === 'hivescale' && b.m === 'AA:BB:CC:DD:EE:00'));
  check('all three in-hive lanes kept',
        ['hiveinside_nrf54', 'beecounter', 'hiveheart'].every(t => out[0].bl.some(b => b.t === t)));
}

// ── 3. Same-lane duplicates are dropped, first wins ─────────────────────────
{
  const out = submit(boot([hive([
    { t: 'holyiot',          m: 'AA:BB:CC:DD:EE:01' },
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:09' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
  ])]));
  eq('second beacon dropped', out[0].bl, [
    { t: 'holyiot',    m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter', m: 'AA:BB:CC:DD:EE:02' },
  ]);
}

// ── 4. A DS18B20 blocks the beacon lane only ────────────────────────────────
{
  const out = submit(boot([hive([
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
    { t: 'hiveheart',        m: 'AA:BB:CC:DD:EE:03' },
  ], '28AABBCCDDEE0011')]));
  check('probe kept', out[0].ds === '28AABBCCDDEE0011');
  eq('beacon dropped, counter + heart kept', out[0].bl, [
    { t: 'beecounter', m: 'AA:BB:CC:DD:EE:02' },
    { t: 'hiveheart',  m: 'AA:BB:CC:DD:EE:03' },
  ]);
}

// ── 5. Add buttons: per-lane enable/disable ─────────────────────────────────
function buttons(dom) {
  const c = dom.window.document.querySelector('#hives fieldset');
  return {
    ble: c.querySelector('[data-addble]'),
    ds:  c.querySelector('[data-addds]'),
    bc:  c.querySelector('[data-addbc]'),
    hh:  c.querySelector('[data-addhh]'),
  };
}
{
  const b = buttons(boot([hive([])]));
  check('empty hive: every add button enabled',
        !b.ble.disabled && !b.ds.disabled && !b.bc.disabled && !b.hh.disabled);
}
{
  const b = buttons(boot([hive([{ t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' }])]));
  check('beacon paired: beacon + DS18B20 disabled', b.ble.disabled && b.ds.disabled);
  check('beacon paired: counter + heart still offered', !b.bc.disabled && !b.hh.disabled);
}
{
  const b = buttons(boot([hive([{ t: 'beecounter', m: 'AA:BB:CC:DD:EE:02' }])]));
  check('counter paired: counter disabled', b.bc.disabled);
  check('counter paired: beacon/DS/heart still offered',
        !b.ble.disabled && !b.ds.disabled && !b.hh.disabled);
}
{
  const b = buttons(boot([hive([], '28AABBCCDDEE0011')]));
  check('probe paired: beacon + DS18B20 disabled', b.ble.disabled && b.ds.disabled);
  check('probe paired: counter + heart still offered', !b.bc.disabled && !b.hh.disabled);
}

// ── 6. Clicking "Add HiveTraffic counter" really adds a counter row ─────────
{
  const dom = boot([hive([{ t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' }])]);
  buttons(dom).bc.click();
  const rows = dom.window.document.querySelectorAll('#hives [data-sensors] [data-bm]');
  check('a second sensor row appeared', rows.length === 2);
  rows[1].value = 'AA:BB:CC:DD:EE:02';
  rows[1].dispatchEvent(new dom.window.Event('input'));
  const out = submit(dom);
  eq('added counter is submitted', out[0].bl, [
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: 'AA:BB:CC:DD:EE:02' },
  ]);
}

// ── 7. A counter row must not be re-typed by the scan dropdown ──────────────
{
  const dom = boot(
    [hive([{ t: 'beecounter', m: '' }])],
    [{ m: 'AA:BB:CC:DD:EE:07', n: 'HolyIot', r: -60, t: 'holyiot', tn: 'HolyIot' }]);
  const c = dom.window.document.querySelector('#hives fieldset');
  check('counter row has no type dropdown', c.querySelector('[data-sensors] [data-bt]') === null);
  const sel = c.querySelector('[data-sensors] [data-bsel]');
  sel.value = 'AA:BB:CC:DD:EE:07';
  sel.dispatchEvent(new dom.window.Event('change'));
  const out = submit(dom);
  eq('type stays beecounter after picking a detected beacon', out[0].bl,
     [{ t: 'beecounter', m: 'AA:BB:CC:DD:EE:07' }]);
}

// ── 8. A beacon row DOES get auto-typed from the scan ───────────────────────
{
  const dom = boot(
    [hive([{ t: 'holyiot', m: '' }])],
    [{ m: 'AA:BB:CC:DD:EE:08', n: 'HiveInside', r: -55, t: 'hiveinside_nrf54', tn: 'HiveInside' }]);
  const c = dom.window.document.querySelector('#hives fieldset');
  const sel = c.querySelector('[data-sensors] [data-bsel]');
  sel.value = 'AA:BB:CC:DD:EE:08';
  sel.dispatchEvent(new dom.window.Event('change'));
  const out = submit(dom);
  eq('beacon row auto-typed to hiveinside_nrf54', out[0].bl,
     [{ t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:08' }]);
}

// ── 9. Empty MAC rows are not submitted ────────────────────────────────────
{
  const out = submit(boot([hive([
    { t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter',       m: '' },
  ])]));
  eq('blank counter row skipped', out[0].bl, [{ t: 'hiveinside_nrf54', m: 'AA:BB:CC:DD:EE:01' }]);
}

console.log(`${failures ? 'FAIL' : 'PASS'}: ${checks} checks, ${failures} failures`);
process.exit(failures ? 1 : 0);
