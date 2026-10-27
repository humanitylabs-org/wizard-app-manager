// Synthetic-only, loopback Appearance acceptance. No installed box, registry or Docker reads.
// Usage: WIZARD_CORE_DIR=/path/to/wizard-ux/web/core PLAYWRIGHT_MODULE=playwright node qa/connections-appearance.cjs OUTPUT_DIR
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const core = process.env.WIZARD_CORE_DIR;
assert(core, 'Mount the real shared Wizard core, never a theme-client shim');
const output = path.resolve(process.argv[2] || 'artifacts/connections');
fs.mkdirSync(output, { recursive: true });
const skinSource = fs.readFileSync(path.join(core, 'skins.js'), 'utf8');
const skins = {};
for (const id of ['nebula', 'obsidian-light']) {
  const match = skinSource.match(new RegExp("id: '" + id + "'[\\s\\S]*?t: T\\((\\{[\\s\\S]*?\\})\\)"));
  assert(match, `Missing real palette ${id}`);
  skins[id] = vm.runInNewContext('(' + match[1] + ')');
}
const app = (id, name, extra = {}) => ({ id, name, state: 'isolated', internet: true, gateway: 'network',
  can_reach: [], reached_by: [], host_access: [], drift: [], ...extra });
const normal = () => ({ schema: 1, enabled: true, live: true, ok: true, checked_at: Date.now() / 1000,
  gateway: { detail: 'reaches each app on its own gateway network' }, apps: [
    app('studio', 'Studio', { can_reach: [
      { to: 'files', state: 'link', declared: true },
      { to: 'mailer', state: 'not-approved', declared: true },
      { to: 'wizard-agent-service', state: 'core', declared: false },
    ], reached_by: [{ from: 'calendar', state: 'link' }] }),
    app('files', 'Files', { reached_by: [{ from: 'studio', state: 'link' }] }),
    app('mailer', 'Mailer'), app('calendar', 'Calendar'),
    app('server-helper', 'Server helper', { state: 'exposed', host_access: ['owner-approved host mount'],
      why: 'Published through the reverse proxy; shared-network reachability is visible here.' }),
  ] });
let state = 'normal';
const data = () => {
  const d = normal();
  if (state === 'empty') d.apps = [];
  if (state === 'unavailable') { d.live = false; d.ok = false; }
  if (state === 'drift') { d.ok = false; d.apps[0].drift = ['Unexpected network: synthetic-extra']; }
  return d;
};
const harness = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{margin:0;height:100%;font:13px system-ui;background:var(--bg);color:var(--ink)}
.preview{height:28px;box-sizing:border-box;padding:5px 12px;color:var(--ink-2);border-bottom:1px solid var(--line)}
.win{height:calc(100% - 28px);background:var(--sk-win)}iframe{width:100%;height:100%;border:0;display:block}</style>
<script>window.WizCore={apps:()=>({APPS:[{url:'/views/'}]})};
const skins=${JSON.stringify(skins)};
window.setSkin=id=>{let r=document.documentElement;r.dataset.skin=id;r.style.colorScheme=id==='obsidian-light'?'light':'dark';for(const[k,v]of Object.entries(skins[id]))r.style.setProperty('--'+k,v);window.WizAppearance?.sync()};
setSkin(new URLSearchParams(location.search).get('skin')||'nebula');</script>
<script src="/wizard-os-home/core/appearance-host.js"></script></head><body>
<div class="preview">App connections · synthetic preview</div><div class="win"><iframe title="Connections" src="/views/#connections"></iframe></div></body></html>`;
const types = { '.css': 'text/css', '.js': 'text/javascript', '.html': 'text/html' };
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1');
  if (u.pathname === '/harness') { res.setHeader('Content-Type', 'text/html'); return res.end(harness); }
  if (u.pathname === '/api/connections') { res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify(data())); }
  if (u.pathname === '/favicon.ico') { res.statusCode = 204; return res.end(); }
  let file;
  if (u.pathname.startsWith('/views/')) file = path.join(root, 'wizard-views', path.basename(u.pathname) || 'index.html');
  if (u.pathname === '/views/') file = path.join(root, 'wizard-views/index.html');
  if (['app-client.js', 'appearance-host.js'].some(n => u.pathname === '/wizard-os-home/core/' + n)) file = path.join(core, path.basename(u.pathname));
  if (!file || !fs.existsSync(file)) { res.statusCode = 404; return res.end('Not found'); }
  let text = fs.readFileSync(file);
  if (file.endsWith('index.html')) text = text.toString().replace('<body class="wux">', '<body class="wux" data-only="connections">');
  res.setHeader('Content-Type', types[path.extname(file)] || 'text/plain');
  res.end(text);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const [device, viewport] of Object.entries({ phone: { width: 390, height: 844 }, desktop: { width: 1440, height: 900 } })) {
      for (const skin of Object.keys(skins)) {
        for (const scenario of ['normal', 'drift', 'unavailable', 'empty']) {
          state = scenario;
          const context = await browser.newContext({ viewport, deviceScaleFactor: 1, isMobile: device === 'phone', hasTouch: device === 'phone' });
          const page = await context.newPage();
          const errors = [];
          page.on('pageerror', e => errors.push(String(e)));
          await page.goto(`http://127.0.0.1:${server.address().port}/harness?skin=${skin}`);
          const frame = page.frames().find(f => f.url().includes('/views/'));
          assert(frame, 'Missing Connections frame');
          await frame.waitForSelector('.wam-conn');
          await frame.waitForFunction(s => document.documentElement.dataset.skin === s, skin);
          const rows = frame.locator('[data-wam-conn]');
          assert.equal(await rows.count(), scenario === 'empty' ? 0 : 5);
          if (scenario === 'normal') {
            await rows.first().click();
            await frame.waitForSelector('[aria-expanded="true"]');
            assert.match(await frame.locator('.wam-conn-detail').innerText(), /grant revoked or not approved/);
          }
          const expected = { normal: 'Matches the declarations', drift: 'Drift found', unavailable: 'Live check unavailable', empty: 'No apps installed' }[scenario];
          assert.match(await frame.locator('.wam-conn').innerText(), new RegExp(expected));
          const geometry = await frame.evaluate(() => ({
            overflow: document.documentElement.scrollWidth > innerWidth,
            bodyOverflow: document.querySelector('#body').scrollWidth > document.querySelector('#body').clientWidth,
            rowHeights: [...document.querySelectorAll('[data-wam-conn]')].map(e => e.getBoundingClientRect().height),
            text: getComputedStyle(document.querySelector('.wam')).color,
            skin: document.documentElement.dataset.skin,
            theme: window.WizardApp.theme.get(),
          }));
          assert(!geometry.overflow && !geometry.bodyOverflow, 'Horizontal clipping');
          assert(geometry.rowHeights.every(h => h >= 44), 'Small connection touch target');
          assert.equal(geometry.text, skin === 'nebula' ? 'rgb(243, 239, 255)' : 'rgb(0, 0, 0)');
          const filename = `${skin}-${device}-${scenario}.jpg`;
          await page.screenshot({ path: path.join(output, filename), type: 'jpeg', quality: 82 });
          assert(fs.statSync(path.join(output, filename)).size < 1000000, 'Oversize JPEG');
          if (scenario === 'normal') {
            // Real host/client Appearance changes in place: expanded detail and row identity survive.
            const other = skin === 'nebula' ? 'obsidian-light' : 'nebula';
            await page.evaluate(s => window.setSkin(s), other);
            await frame.waitForFunction(s => document.documentElement.dataset.skin === s, other);
            assert.equal(await frame.locator('[data-wam-conn="studio"]').getAttribute('aria-expanded'), 'true');
            assert.match(await frame.locator('.wam-conn-detail').innerText(), /grant revoked or not approved/);
          }
          assert.deepEqual(errors, []);
          results.push({ device, skin, scenario, filename, bytes: fs.statSync(path.join(output, filename)).size, geometry, errors });
          await context.close();
        }
      }
    }
    assert.equal(results.length, 16);
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
    console.log(JSON.stringify({ passed: results.length, screenshots: results.map(r => r.filename) }));
  } finally { await browser.close(); server.close(); }
})().catch(e => { console.error(e); server.close(); process.exitCode = 1; });
