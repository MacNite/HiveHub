// test_configurator_hives.js — the website configurator composes every hive
// sensor combination the firmware supports (audit F09).
//
// The firmware's lane rule (firmware/include/ble_lanes.h, enforced by
// hiveFromJson; the portal half is test_portal_pairing.js) lets one hive hold a
// beacon OR a DS18B20, a HiveTraffic counter and a HiveHeart at once. The
// website configurator kept a single in-hive sensor per hive, so adding one
// cleared the other and "counter + climate beacon" could not be generated.
// This drives the real page (website/configurator.html + assets/configurator.js)
// in jsdom through its buttons, and reads the generated secrets.h preview.
//
//   npm install jsdom && node test-data/test_configurator_hives.js
const fs = require('fs');
const path = require('path');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (e) {
  console.log('SKIP: jsdom is not installed (npm install jsdom to run this suite).');
  process.exit(0);
}

const root = path.join(__dirname, '..', 'website');
const html = fs.readFileSync(path.join(root, 'configurator.html'), 'utf8')
  .replace(/<script\b[^>]*\bsrc=[^>]*><\/script>/g, '');
const js = fs.readFileSync(path.join(root, 'assets', 'configurator.js'), 'utf8');

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

function boot() {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'https://example.test/' });
  dom.window.eval(js);
  return dom;
}

const $ = (dom, sel) => dom.window.document.querySelector(sel);
const card = (dom, i) => $(dom, `[data-hive="${i}"]`);

function click(dom, el) {
  if (!el) throw new Error('missing element');
  el.dispatchEvent(new dom.window.Event('click', { bubbles: true }));
}
function type(dom, el, value) {
  if (!el) throw new Error('missing input');
  el.value = value;
  el.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
}
// Add a pairing in `lane` to hive i and give it a MAC.
function addLane(dom, i, lane, mac) {
  click(dom, card(dom, i).querySelector(`[data-add-lane="${lane}"]`));
  type(dom, card(dom, i).querySelector(`[data-hbm="${lane}"]`), mac);
}

function preview(dom) { return $(dom, '#preview').textContent; }
function flag(dom, name) {
  const m = preview(dom).match(new RegExp(`#define ${name}\\s+(\\S+)`));
  return m ? m[1] : null;
}
function hiveJson(dom, i) {
  const m = preview(dom).match(new RegExp(`#define HIVE_${i}_JSON\\s+"(.*)"`));
  return m ? JSON.parse(m[1].replace(/\\"/g, '"').replace(/\\\\/g, '\\')) : null;
}
const addButtons = (dom, i) =>
  [...card(dom, i).querySelectorAll('[data-add-lane],[data-add-ds]')]
    .map(b => b.getAttribute('data-add-lane') || 'ds');

// ── 1. Counter + climate beacon on one hive ──────────────────────────────────
{
  const dom = boot();
  click(dom, $(dom, '#add-hive'));
  addLane(dom, 1, 'counter', 'AA:BB:CC:DD:EE:02');
  addLane(dom, 1, 'beacon', 'AA:BB:CC:DD:EE:01');
  eq('counter + beacon are both generated', hiveJson(dom, 1).bl, [
    { t: 'holyiot', m: 'AA:BB:CC:DD:EE:01' },
    { t: 'beecounter', m: 'AA:BB:CC:DD:EE:02' },
  ]);
  eq('counter flag on', flag(dom, 'ENABLE_WIRELESS_BEECOUNTER'), '1');
  eq('beacon scan on', flag(dom, 'ENABLE_BLE_SCAN'), '1');
  eq('only the HiveHeart lane is still offered', addButtons(dom, 1), ['heart']);

  // The beacon type is still selectable within its lane.
  const sel = card(dom, 1).querySelector('[data-hbt]');
  sel.value = 'hiveinside_nrf54';
  sel.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  eq('beacon type change keeps the counter', hiveJson(dom, 1).bl.map(b => b.t),
     ['hiveinside_nrf54', 'beecounter']);
}

// ── 2. Counter + HiveHeart, and all three lanes ──────────────────────────────
{
  const dom = boot();
  click(dom, $(dom, '#add-hive'));
  addLane(dom, 1, 'counter', 'AA:BB:CC:DD:EE:02');
  addLane(dom, 1, 'heart', 'AA:BB:CC:DD:EE:03');
  eq('counter + heart', hiveJson(dom, 1).bl.map(b => b.t), ['beecounter', 'hiveheart']);
  eq('GATT flag for the heart', flag(dom, 'ENABLE_BEEHIVE_GATT'), '1');
  eq('counter flag', flag(dom, 'ENABLE_WIRELESS_BEECOUNTER'), '1');
  addLane(dom, 1, 'beacon', 'AA:BB:CC:DD:EE:01');
  eq('all three lanes on one hive', hiveJson(dom, 1).bl.map(b => b.t),
     ['holyiot', 'beecounter', 'hiveheart']);
  eq('no lane left to add', addButtons(dom, 1), []);
}

// ── 3. Counter + wired DS18B20 ───────────────────────────────────────────────
{
  const dom = boot();
  click(dom, $(dom, '#add-hive'));
  addLane(dom, 1, 'counter', 'AA:BB:CC:DD:EE:02');
  click(dom, card(dom, 1).querySelector('[data-add-ds]'));
  type(dom, card(dom, 1).querySelector('[data-hds]'), '28ff64b2711304a3');
  const j = hiveJson(dom, 1);
  eq('the probe is generated', j.ds, '28FF64B2711304A3');
  eq('next to the counter', j.bl, [{ t: 'beecounter', m: 'AA:BB:CC:DD:EE:02' }]);
  eq('DS18B20 bus on', flag(dom, 'ENABLE_DS18B20_HIVE_TEMP'), '1');
  eq('counter flag on', flag(dom, 'ENABLE_WIRELESS_BEECOUNTER'), '1');
  // The probe and a beacon share the in-hive temperature lane.
  eq('a probe takes the beacon lane', addButtons(dom, 1), ['heart']);

  // Removing the probe frees the lane and keeps the counter.
  click(dom, card(dom, 1).querySelector('[data-hsensor-remove="beacon"]'));
  eq('removing the probe keeps the counter', hiveJson(dom, 1).bl.map(b => b.t), ['beecounter']);
  check('no probe after removal', hiveJson(dom, 1).ds === undefined);
  eq('beacon lane offered again', addButtons(dom, 1), ['ds', 'beacon', 'heart']);
}

// ── 4. Removing one lane leaves the others; a MAC-less pairing is not emitted ─
{
  const dom = boot();
  click(dom, $(dom, '#add-hive'));
  addLane(dom, 1, 'counter', 'AA:BB:CC:DD:EE:02');
  addLane(dom, 1, 'beacon', 'AA:BB:CC:DD:EE:01');
  click(dom, card(dom, 1).querySelector('[data-hsensor-remove="counter"]'));
  eq('removing the counter keeps the beacon', hiveJson(dom, 1).bl.map(b => b.t), ['holyiot']);
  eq('counter flag off with no counter', flag(dom, 'ENABLE_WIRELESS_BEECOUNTER'), '0');
  click(dom, card(dom, 1).querySelector('[data-add-lane="heart"]'));
  eq('a pairing without a MAC is not generated', hiveJson(dom, 1).bl.map(b => b.t), ['holyiot']);
}

// ── 5. The hive limit matches the firmware ───────────────────────────────────
{
  const dom = boot();
  for (let i = 0; i < 20; i++) click(dom, $(dom, '#add-hive'));
  eq('18 hives, like MAX_HIVES', flag(dom, 'HIVE_COUNT'), '18');
  check('header names the real limit', /HIVES \(up to 18/.test(preview(dom)));
}

console.log(`${failures ? 'FAIL' : 'PASS'}: ${checks} checks, ${failures} failures`);
process.exit(failures ? 1 : 0);
