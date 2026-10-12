// Regression checks for immediate dismissal and cancelled detail requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
function section(start, end) {
  const from = html.indexOf(start);
  const to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return html.slice(from, to);
}
function node() {
  const classes = new Set();
  return {
    style: {}, inert: false, attributes: {}, isConnected: false,
    classList: {
      add: c => classes.add(c), remove: c => classes.delete(c),
      contains: c => classes.has(c),
      toggle: (c, value) => value ? classes.add(c) : classes.delete(c)
    },
    setAttribute(key, value) { this.attributes[key] = value; },
    querySelector() { return null; }, focus() {}
  };
}
const drawer = node(), backdrop = node(), app = node(), login = node();
let modal = null, resolveRequest;
const body = {style: {}};
const $ = selector => {
  if (selector === '#drawer') return drawer;
  if (selector === '#drawer.open') return drawer.classList.contains('open') ? drawer : null;
  if (selector === '#drawerBackdrop') return backdrop;
  if (selector === '#loginBg.open') return login.classList.contains('open') ? login : null;
  if (selector === '.app') return app;
  throw new Error('Unexpected selector: ' + selector);
};
const context = vm.createContext({
  $, document: {body, activeElement: null, querySelector: () => modal, querySelectorAll: () => []},
  requestAnimationFrame: () => {},
  api: () => new Promise(resolve => { resolveRequest = resolve; }),
  companyProfile: 'general', toast: message => { throw new Error(message); }
});
vm.runInContext(section('let drawerRequest=0;', 'function setView') +
  section('let activeOverlay=null, previousFocus=null;', "document.addEventListener('keydown',e=>") +
  section('async function openCompany(cid)', 'async function openCompanyRecord'), context);
function run(code) { return vm.runInContext(code, context); }
function assertReleased() {
  assert.equal(drawer.classList.contains('open'), false);
  assert.equal(backdrop.classList.contains('open'), false);
  assert.equal(backdrop.style.display, 'none');
  assert.equal(backdrop.style.pointerEvents, 'none');
  assert.equal(body.style.overflow, '');
  assert.equal(app.inert, false);
  assert.equal(drawer.inert, true);
  assert.equal(run('activeOverlay'), null);
}
(async () => {
  for (let i = 0; i < 3; i++) {
    run('openDrawer()');
    assert.equal(backdrop.style.display, 'block');
    assert.equal(app.inert, true);
    run('closeDrawer()');
    // No observer callback or animation frame is needed to unlock the page.
    assertReleased();
  }
  backdrop.classList.add('open'); backdrop.style.display = 'block'; app.inert = true;
  run('closeDrawer()');
  assertReleased();
  modal = node();
  run('openDrawer(); closeDrawer()');
  assert.equal(app.inert, true, 'An active form must keep its own focus lock');
  assert.equal(backdrop.style.display, 'none');
  modal = null; run('syncOverlays()');
  assertReleased();
  const pending = run('openCompany(1)');
  run('closeDrawer()');
  resolveRequest({});
  await pending;
  assertReleased();
  console.log('Passed: repeated dismissals, orphan backdrop recovery, form precedence and cancelled detail request.');
})().catch(error => { console.error(error); process.exitCode = 1; });
