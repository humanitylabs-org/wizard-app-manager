// Wizard App Manager views for Wizard UX: My Apps, App Store, Resources. Vanilla ES module, no build step.
// Markup reuses Wizard UX classes (.lgroup .lgrid .lapp .lrow .st-group .st-row); pieces it adds are .wam-*.
//
//   renderMyApps(el, ctx)    installed apps as tiles: Agents / Apps / System, interface paired with what it uses
//   renderStore(el, ctx)     the App Index in the same style; chip row (tags + sort) goes into ctx.chipsEl
//   renderResources(el, ctx) a Settings group: Storage, Processor, Memory, Start/Stop/Restart all
//   renderCard(el, app, ctx) one app: summary, status, only the buttons that apply
//   renderConnections(el, ctx) a Settings group: what each app can reach (its declared connections), how the agent
//                            gateway reaches it, internet access, and the live check of Docker's networks
//
// ctx (all optional): api ('/api', Runtipi), wizard ('/wizard/v1'), connections ('/api/connections', wam), index ('/wizard/v1/index', or
// 'https://apps.aiwizards.com/v1'), chipsEl, extraTiles (html strings shown first under Apps), shell (true: Open
// buttons carry data-app and bubble to the shell's own launcher), onApp(app, from), onGetApps(), onBack().

import { runtipiDomainUrl } from './runtipi-open.js';

const STORE = 'wizardapps';
const BUSY = new Set(['installing', 'updating', 'starting', 'stopping', 'restarting', 'uninstalling', 'resetting']);
const PALETTE = [
  ['#6b46ff', '#c04dff'],
  ['#3fe0ff', '#3f6bff'],
  ['#ff5ccf', '#5c3bff'],
  ['#4ff0b0', '#2f8cff'],
  ['#ffbe55', '#ff6b86'],
  ['#ff8a5c', '#ff5ccf'],
];
const GROUPS = ['Agents', 'Apps', 'System'];
const STATUS = { running: 'Running', stopped: 'Stopped', missing: 'Not running' };

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const conf = (ctx = {}) => ({ api: '/api', wizard: '/wizard/v1', index: '/wizard/v1/index', ...ctx });
const hue = (id) => {
  let h = 0;
  for (const c of String(id)) h = (h * 31 + c.codePointAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
};
const noop = () => undefined;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function req(url, opts = {}) {
  const r = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...opts });
  const text = await r.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { message: text.slice(0, 120) };
  }
  if (!r.ok) throw new Error(body.message || body.error || `HTTP ${r.status}`);
  return body;
}
const post = (url, data, method = 'POST') => req(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });

// ---- data --------------------------------------------------------------------------------------------------------
const cache = { apps: null, at: 0, index: null, indexAt: 0 };

async function indexMeta(c) {
  if (cache.index && Date.now() - cache.indexAt < 300e3) return cache.index;
  const d = await req(`${c.index}/search?limit=100&sort=name`);
  cache.index = new Map((d.apps || []).map((a) => [a.id, a]));
  cache.indexAt = Date.now();
  return cache.index;
}

function model(x, meta, idx, c, settings = {}) {
  const info = x.info || {};
  const m = meta.get(info.id) || {};
  const i = idx.get(info.id) || {};
  const cats = info.categories || [];
  const type = m.type || i.type || (cats.includes('agent') ? 'agent' : info.no_gui ? 'service' : 'view');
  const latest = Number(x.metadata?.latestVersion || 0);
  const version = Number(x.app?.version || 0);
  return {
    id: info.id,
    urn: info.urn || `${info.id}:${STORE}`,
    name: m.name || info.name || info.id,
    summary: m.summary || i.summary || info.short_desc || '',
    type,
    category: m.category || i.category || cats[0] || 'app',
    uses: m.uses || i.uses || [],
    status: x.app?.status || 'missing',
    update: latest > version && latest !== Number(x.app?.ignoredVersion || 0),
    screen: type !== 'service' && !info.no_gui && m.view !== null,
    open: x.app?.status === 'running' && !x.app?.pendingRestart && !info.no_gui ? runtipiDomainUrl(x, settings) || m.view?.open || '' : '',
    logo: `${c.api}/marketplace/apps/${encodeURIComponent(info.urn || `${info.id}:${STORE}`)}/image`,
    installed: true,
  };
}

/** Installed apps merged with Wizard metadata (apps.json via the front) and the index. Cached 10 s. */
export async function loadInstalled(ctx, fresh = false) {
  const c = conf(ctx);
  if (!fresh && cache.apps && Date.now() - cache.at < 10e3) return cache.apps;
  const [inst, meta, idx, settings] = await Promise.all([
    req(`${c.api}/apps/installed`),
    req(`${c.wizard}/wizard-apps`).catch(() => ({ apps: [] })),
    indexMeta(c).catch(() => new Map()),
    req(`${c.api}/user-context`).catch(() => ({})),
  ]);
  const m = new Map((meta.apps || []).map((a) => [a.id, a]));
  cache.apps = (inst.installed || []).filter((x) => x.info?.id).map((x) => model(x, m, idx, c, settings));
  cache.at = Date.now();
  return cache.apps;
}

function fromIndex(a, c) {
  return {
    id: a.id,
    urn: `${a.id}:${STORE}`,
    name: a.name || a.id,
    summary: a.summary || '',
    type: a.type,
    category: a.category || 'app',
    uses: a.uses || [],
    repo: a.repo,
    installs: Number(a.installs || 0),
    screen: a.type !== 'service',
    logo: `${c.api}/marketplace/apps/${encodeURIComponent(`${a.id}:${STORE}`)}/image`,
    installed: false,
  };
}

// ---- pieces ------------------------------------------------------------------------------------------------------
const groupOf = (a) => (a.type === 'agent' || a.category === 'agent' ? 'Agents' : a.category === 'system' ? 'System' : 'Apps');
const isInterface = (a) => a.category === 'interface' || (a.type === 'view' && a.uses.length > 0);

/** Each agent/app followed by the interfaces that open it: [[app, ui, ...], ...]. */
function units(list) {
  const byId = new Map(list.map((a) => [a.id, a]));
  const pairs = new Map();
  const paired = new Set();
  for (const a of list) {
    if (!isInterface(a)) continue;
    const t = a.uses.find((u) => byId.has(u) && !isInterface(byId.get(u)));
    if (!t) continue;
    if (!pairs.has(t)) pairs.set(t, []);
    pairs.get(t).push(a);
    paired.add(a.id);
  }
  return list.filter((a) => !paired.has(a.id)).map((a) => [a, ...(pairs.get(a.id) || [])]);
}

const count = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));
const glyph = (a) =>
  esc(
    String(a.name || a.id)
      .trim()
      .charAt(0)
      .toUpperCase(),
  );
const icon = (a) => `<img src="${esc(a.logo)}" alt="" loading="lazy" data-glyph="${glyph(a)}">`;
const gstyle = (a) => {
  const [g1, g2] = hue(a.id);
  return `--g1:${g1};--g2:${g2}`;
};

function tile(a) {
  const dot = a.installed ? `<i class="wam-dot ${esc(a.status)}"></i>` : '';
  const up = a.update ? '<i class="wam-up">↑</i>' : '';
  const ok = !a.installed && a.have ? '<i class="wam-ok">✓</i>' : '';
  const n = !a.installed && a.installs > 0 ? `<small class="wam-n">↓ ${count(a.installs)}</small>` : '';
  return `<button class="lapp${a.screen ? '' : ' headless'}" data-wam-id="${esc(a.id)}" title="${esc(a.summary)}"><span class="ic" style="${gstyle(a)}">${icon(a)}${dot}${up}${ok}</span><span class="nm">${esc(a.name)}</span>${n}</button>`;
}

function grid(list, extra = {}) {
  const by = Object.fromEntries(GROUPS.map((g) => [g, []]));
  for (const u of units(list)) by[groupOf(u[0])].push(u);
  return GROUPS.map((g) => {
    const cells = by[g].map((u) => (u.length > 1 ? `<div class="wam-pair n${Math.min(u.length, 4)}">${u.map(tile).join('')}</div>` : tile(u[0])));
    const pre = extra[`pre${g}`] || [];
    const post = extra[`post${g}`] || [];
    if (!cells.length && !pre.length && !post.length) return '';
    return `<div class="lgroup"><h4>${g} <span>${by[g].reduce((n, u) => n + u.length, 0) || ''}</span></h4><div class="lgrid">${[...pre, ...cells, ...post].join('')}</div></div>`;
  }).join('');
}

function row(a) {
  const tag = a.have ? '<span class="kind wam-have">Installed</span>' : a.installs > 0 ? `<span class="kind">↓ ${count(a.installs)}</span>` : '';
  return `<button class="lrow" data-wam-id="${esc(a.id)}"><span class="ic" style="${gstyle(a)}">${icon(a)}</span><span class="nm">${esc(a.name)}<small>${esc(a.summary)}</small></span>${tag}</button>`;
}

// Broken logos fall back to the tile's gradient glyph (inline onerror is blocked by the shell's CSP).
function bindImages(el) {
  if (el.dataset.wamImg) return;
  el.dataset.wamImg = '1';
  el.addEventListener(
    'error',
    (e) => {
      const img = e.target;
      if (img.tagName === 'IMG' && img.dataset.glyph !== undefined) img.replaceWith(document.createTextNode(img.dataset.glyph));
    },
    true,
  );
}

function bindClicks(el, handler) {
  el._wam = handler;
  if (el.dataset.wamClick) return;
  el.dataset.wamClick = '1';
  el.addEventListener('click', (e) => el._wam?.(e));
}

const note = (t) => `<p class="st-note wam-note">${esc(t)}</p>`;

// ---- My Apps -----------------------------------------------------------------------------------------------------
export async function renderMyApps(el, ctx = {}) {
  el.classList.add('wam');
  bindImages(el);
  const getApps = '<button class="lapp" data-wam-get><span class="ic wam-get">+</span><span class="nm">Get apps</span></button>';
  const paint = (apps) => {
    el.innerHTML = grid(apps, { preApps: ctx.extraTiles || [], postApps: [getApps] });
  };
  bindClicks(el, (e) => {
    if (e.target.closest('[data-wam-get]')) {
      e.stopPropagation();
      return ctx.onGetApps ? ctx.onGetApps() : renderStore(el, ctx);
    }
    const t = e.target.closest('[data-wam-id]');
    if (!t) return;
    e.stopPropagation();
    const a = (cache.apps || []).find((x) => x.id === t.dataset.wamId);
    if (a) ctx.onApp ? ctx.onApp(a, 'apps') : renderCard(el, a, { ...ctx, onBack: () => renderMyApps(el, ctx) });
  });
  if (cache.apps) paint(cache.apps);
  try {
    paint(await loadInstalled(ctx));
  } catch {
    if (!cache.apps)
      el.innerHTML = `<div class="lgroup"><h4>Apps</h4><div class="lgrid">${(ctx.extraTiles || []).join('')}${getApps}</div></div>${note('Can’t reach the App Manager')}`;
  }
}

// ---- App card ----------------------------------------------------------------------------------------------------
export function renderCard(el, app, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  bindImages(el);
  let a = app;
  let busy = '';
  let msg = '';
  const paint = () => {
    const st = a.installed ? STATUS[a.status] || `${a.status.charAt(0).toUpperCase()}${a.status.slice(1)}…` : a.have ? 'Installed' : '';
    const btn = (k, label, cls = '') =>
      `<button class="wam-btn ${cls}" data-wam-do="${k}"${busy ? ' disabled' : ''}>${busy === k ? '…' : label}</button>`;
    const acts = [];
    if (a.installed) {
      if (a.screen && a.open && a.status === 'running')
        acts.push(
          `<button class="wam-btn primary" data-wam-do="open"${c.shell && new URL(a.open, location.href).origin === location.origin ? ` data-app="${esc(a.id)}"` : ''}>Open</button>`,
        );
      if (a.update) acts.push(btn('update', 'Update', 'primary'));
      if (a.status === 'running') acts.push(btn('stop', 'Stop'));
      else if (!BUSY.has(a.status)) acts.push(btn('start', 'Start'));
    } else if (!a.have) acts.push(btn('install', 'Install', 'primary'));
    el.innerHTML = `<div class="wam-card">
      <button class="wam-back" data-wam-back>‹ ${ctx.from === 'store' ? 'App Store' : 'My apps'}</button>
      <div class="wam-panel">
        <div class="st-row static"><span class="wam-ic" style="${gstyle(a)}">${icon(a)}</span><span class="st-tx"><b>${esc(a.name)}</b><small>${esc(a.summary)}</small></span></div>
        ${st ? `<div class="wam-meta"><i class="wam-dot ${esc(a.installed ? a.status : 'running')}"></i>${esc(st)}${!a.installed && a.installs > 0 ? ` · ${count(a.installs)} installs` : ''}</div>` : ''}
        ${acts.length ? `<div class="wam-acts">${acts.join('')}</div>` : ''}
        ${a.screen ? '' : note('No screen: ask your agent to use it')}
        ${msg ? note(msg) : ''}
      </div>
    </div>`;
  };
  const settle = async () => {
    for (let k = 0; k < 60; k++) {
      await sleep(2000);
      const now = (await loadInstalled(ctx, true).catch(() => [])).find((x) => x.id === a.id);
      if (now) a = now;
      paint();
      if (now && !BUSY.has(now.status)) return;
    }
  };
  const run = async (k) => {
    busy = k;
    msg = '';
    paint();
    try {
      if (k === 'install') await install(a, c);
      else if (k === 'update') await post(`${c.api}/app-lifecycle/${encodeURIComponent(a.urn)}/update`, { performBackup: true }, 'PATCH');
      else await post(`${c.api}/app-lifecycle/${encodeURIComponent(a.urn)}/${k}`);
      busy = '';
      await settle();
    } catch (e) {
      busy = '';
      msg = k === 'install' ? `Couldn’t install: ${e.message}. Ask your agent to install ${a.name}.` : `Couldn’t ${k}: ${e.message}`;
      paint();
    }
  };
  bindClicks(el, (e) => {
    if (e.target.closest('[data-wam-back]')) {
      e.stopPropagation();
      return ctx.onBack?.();
    }
    const b = e.target.closest('[data-wam-do]');
    if (!b) return;
    if (b.dataset.wamDo === 'open') {
      if (c.shell && b.hasAttribute('data-app')) return; // same-origin screen: shell window
      e.stopPropagation();
      return window.open(a.open, '_blank', 'noopener,noreferrer'); // stock Runtipi Open; Access login needs top-level navigation
    }
    e.stopPropagation();
    run(b.dataset.wamDo);
  });
  paint();
}

/** Runtipi install (uses first, like `wam install`), then count it in the index. */
async function install(a, c) {
  const have = new Set((await loadInstalled(c, true)).map((x) => x.id));
  const form = { openPort: false, exposed: false, exposedLocal: false };
  for (const dep of a.uses || []) {
    if (have.has(dep)) continue;
    await post(`${c.api}/app-lifecycle/${encodeURIComponent(`${dep}:${STORE}`)}/install`, form).catch(noop);
  }
  await post(`${c.api}/app-lifecycle/${encodeURIComponent(a.urn)}/install`, form);
  a.installed = true;
  a.status = 'installing';
  post(`${c.index}/installs`, { id: a.id, repo: a.repo }).catch(noop);
}

// ---- App Store ---------------------------------------------------------------------------------------------------
const SORTS = [
  ['popular', 'Popular'],
  ['new', 'New'],
  ['name', 'A–Z'],
];

export function renderStore(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  bindImages(el);
  let chips = ctx.chipsEl;
  if (!chips) {
    chips = document.createElement('div');
    el.before(chips);
  }
  chips.classList.add('wam', 'wam-chips');
  const s = { q: '', tag: '', sort: 'popular', items: [], next: null, tags: [], seq: 0 };

  const paintChips = () => {
    const chip = (id, label) => `<button class="wam-chip${s.tag === id ? ' on' : ''}" data-wam-tag="${esc(id)}">${esc(label)}</button>`;
    const sorts = SORTS.map(([k, l]) => `<button class="${s.sort === k ? 'on' : ''}" data-wam-sort="${k}">${l}</button>`).join('');
    chips.innerHTML = `<div class="wam-tags">${chip('', 'All')}${s.tags.map((t) => chip(t, t.charAt(0).toUpperCase() + t.slice(1))).join('')}</div><span class="wam-sort">${sorts}</span>`;
  };
  const paint = () => {
    const have = new Set((cache.apps || []).map((a) => a.id));
    for (const a of s.items) a.have = have.has(a.id);
    const more = s.next != null ? '<button class="wam-more" data-wam-more>More</button>' : '';
    const back = ctx.onBack ? '<button class="wam-back" data-wam-back>‹ My apps</button>' : '';
    const list = s.items.length
      ? s.q
        ? `<div class="lgroup">${s.items.map(row).join('')}</div>`
        : grid(s.items)
      : note(s.q ? 'Nothing found' : 'Loading…');
    el.innerHTML = `${back}${list}${more}`;
  };
  const load = async (reset) => {
    const seq = ++s.seq;
    const p = new URLSearchParams({ q: s.q, tag: s.tag, sort: s.sort, limit: '48', offset: reset ? '0' : String(s.next || 0) });
    try {
      const d = await req(`${c.index}/search?${p}`);
      if (seq !== s.seq) return;
      const got = (d.apps || []).map((a) => fromIndex(a, c));
      s.items = reset ? got : [...s.items, ...got];
      s.next = d.next_offset ?? null;
      paint();
      if (!s.items.length) el.innerHTML = note(s.q ? 'Nothing found' : 'No apps here yet');
    } catch {
      if (seq === s.seq) el.innerHTML = note('Can’t reach the App Store');
    }
  };

  chips.onclick = (e) => {
    const t = e.target.closest('[data-wam-tag]');
    const so = e.target.closest('[data-wam-sort]');
    if (t) s.tag = t.dataset.wamTag;
    else if (so) s.sort = so.dataset.wamSort;
    else return;
    e.stopPropagation();
    paintChips();
    load(true);
  };
  bindClicks(el, (e) => {
    if (e.target.closest('[data-wam-back]')) {
      e.stopPropagation();
      return ctx.onBack();
    }
    if (e.target.closest('[data-wam-more]')) {
      e.stopPropagation();
      return load(false);
    }
    const t = e.target.closest('[data-wam-id]');
    if (!t) return;
    e.stopPropagation();
    const a = s.items.find((x) => x.id === t.dataset.wamId);
    const mine = (cache.apps || []).find((x) => x.id === a?.id);
    if (a) ctx.onApp ? ctx.onApp(mine || a, 'store') : renderCard(el, mine || a, { ...ctx, from: 'store', onBack: () => paint() });
  });

  paintChips();
  paint();
  req(`${c.index}/tags`)
    .then((d) => {
      s.tags = (d.tags || [])
        .slice()
        .sort((x, y) => y.count - x.count)
        .slice(0, 8)
        .map((t) => t.id);
      paintChips();
    })
    .catch(noop);
  loadInstalled(ctx).then(paint).catch(noop);
  load(true);

  let timer = 0;
  return {
    chips,
    setQuery(q) {
      clearTimeout(timer);
      timer = setTimeout(() => {
        s.q = String(q || '').trim();
        load(true);
      }, 180);
    },
    repaint: paint,
  };
}

// ---- Resources ---------------------------------------------------------------------------------------------------
export async function renderResources(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  let armed = '';
  const meter = (pct) => {
    const p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
    return `<span class="wam-meter${p >= 85 ? ' hot' : ''}"><em>${p}%</em><span class="bar"><i style="width:${p}%"></i></span></span>`;
  };
  const r = (ic, title, sub, pct) =>
    `<div class="st-row static"><span class="st-ic">${ic}</span><span class="st-tx"><b>${title}</b><small>${esc(sub)}</small></span>${meter(pct)}</div>`;
  const acts = () =>
    `<div class="wam-acts wam-all">${[
      ['start-all', 'Start all'],
      ['stop-all', 'Stop all'],
      ['restart-all', 'Restart all'],
    ]
      .map(([k, l]) => `<button class="wam-btn" data-wam-all="${k}">${armed === k ? 'Tap again' : l}</button>`)
      .join('')}</div>`;
  const paint = (d) => {
    const memUsed = (Number(d.memoryTotal) * Number(d.percentUsedMemory)) / 100;
    el.innerHTML = `<div class="st-group"><h4>Resources</h4>
      ${r('◫', 'Storage', `${d.diskUsed} of ${d.diskSize} GB used`, d.percentUsed)}
      ${r('◔', 'Processor', 'Uninstall apps to reduce load', d.cpuLoad)}
      ${r('▦', 'Memory', `${memUsed.toFixed(memUsed < 10 ? 1 : 0)} of ${d.memoryTotal} GB used`, d.percentUsedMemory)}
      ${acts()}<p class="st-note wam-note" data-wam-msg></p></div>`;
  };
  bindClicks(el, async (e) => {
    const b = e.target.closest('[data-wam-all]');
    if (!b) return;
    e.stopPropagation();
    const k = b.dataset.wamAll;
    if (k === 'stop-all' && armed !== k) {
      armed = k;
      b.textContent = 'Tap again';
      setTimeout(() => {
        armed = '';
        if (b.isConnected) b.textContent = 'Stop all';
      }, 3000);
      return;
    }
    armed = '';
    const m = el.querySelector('[data-wam-msg]');
    b.disabled = true;
    b.textContent = '…';
    try {
      await post(`${c.api}/app-lifecycle/${k}`);
      if (m) m.textContent = k === 'stop-all' ? 'Stopping every app' : k === 'start-all' ? 'Starting every app' : 'Restarting every app';
    } catch (err) {
      if (m) m.textContent = `Couldn’t do that: ${err.message}`;
    }
    b.disabled = false;
    b.textContent = { 'start-all': 'Start all', 'stop-all': 'Stop all', 'restart-all': 'Restart all' }[k];
  });
  if (el._wamLoad) paint(el._wamLoad);
  try {
    el._wamLoad = await req(`${c.api}/system/load`);
    paint(el._wamLoad);
  } catch {
    if (!el._wamLoad) el.innerHTML = `<div class="st-group"><h4>Resources</h4>${note('Can’t reach the App Manager')}</div>`;
  }
}

// ---- Connections -------------------------------------------------------------------------------------------------
// Generated by the App Manager from the same declarations that build the networks (apps.json `connections`, written
// by wam/network.py): an app reaches exactly what it declares, nothing else. The live check compares the containers'
// actual Docker networks with those declarations.
const REACH = {
  link: '',
  gateway: ' · through the gateway network',
  external: ' · outside the App Manager: not reachable from apps',
  host: ' · from the server’s network, on its 127.0.0.1 port',
  'not-installed': ' · not installed',
  unmanaged: ' · not isolated',
  shared: ' · shared network',
};
const STATE = {
  isolated: 'Isolated',
  'host-network': 'Server network',
  exposed: 'Shared network (published through Runtipi)',
  custom: 'Own network settings',
  'no-network': 'No network',
  shared: 'Shared network',
};

export async function renderConnections(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  let open = '';
  const ago = (t) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - Number(t || 0)));
    return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
  };
  const paint = (d) => {
    if (!d || !Array.isArray(d.apps)) {
      el.innerHTML = `<div class="st-group wam-conn"><h4>Connections</h4>${note(d?.error ? `Can’t read the app networks: ${d.error}` : 'No connection data yet: run `wam connections` on the server')}</div>`;
      return;
    }
    const head = d.enabled
      ? `Each app reaches only the apps it declares. ${d.gateway?.detail ? `The agent gateway: ${d.gateway.detail}.` : ''}`
      : 'App isolation is off: every app can reach every other app.';
    const check = !d.live
      ? `<span class="wam-tag warn">Live check unavailable</span>`
      : d.ok
        ? `<span class="wam-tag good">Matches the declarations</span>`
        : `<span class="wam-tag bad">Drift found</span>`;
    const rows = d.apps
      .map((a) => {
        const reach = a.can_reach || [];
        const by = (a.reached_by || []).filter((e) => e.state === 'link' || e.state === 'gateway').map((e) => e.from);
        const drift = (a.drift || []).length;
        const warn = drift || (a.host_access || []).length || a.state === 'exposed';
        const own = reach.filter((e) => e.state !== 'peer');
        const peers = reach.filter((e) => e.state === 'peer').map((e) => e.to);
        const sub = own.length ? `Uses ${own.map((e) => e.to).join(', ')}` : 'Uses no other app';
        const detail =
          open === a.id
            ? `<div class="wam-conn-detail">
          <p><b>Uses</b> ${own.length ? own.map((e) => `${esc(e.to)}${esc(REACH[e.state] ?? '')}`).join('; ') : 'no other app'}</p>
          ${peers.length ? `<p><b>Two-way links</b> ${esc(peers.join(', '))} <span class="wam-conn-why">(they use this app; a link is one network shared by both)</span></p>` : ''}
          <p><b>Reached by</b> ${by.length ? esc(by.join(', ')) : 'no other app'}</p>
          <p><b>Agent gateway</b> ${esc(a.gateway)}</p>
          <p><b>Internet</b> ${a.internet === true ? 'yes' : a.internet === false ? 'no' : 'unknown'}</p>
          ${a.why ? `<p class="wam-conn-why">${esc(a.why)}</p>` : ''}
          ${(a.host_access || []).map((h) => `<p class="wam-conn-bad">Host access: ${esc(h)}</p>`).join('')}
          ${(a.drift || []).map((x) => `<p class="wam-conn-bad">${esc(x)}</p>`).join('')}
        </div>`
            : '';
        return `<button class="st-row wam-conn-row${warn ? ' warn' : ''}" data-wam-conn="${esc(a.id)}" aria-expanded="${open === a.id}">
          <span class="st-tx"><b>${esc(a.name || a.id)}</b><small>${esc(sub)}</small></span>
          <span class="wam-tag${a.state === 'isolated' ? '' : ' warn'}${drift ? ' bad' : ''}">${drift ? 'Drift' : esc(STATE[a.state] || a.state)}</span>
        </button>${detail}`;
      })
      .join('');
    el.innerHTML = `<div class="st-group wam-conn"><h4>Connections</h4>
      <p class="st-note wam-note">${esc(head)} ${check} <span class="wam-conn-at">Checked ${esc(ago(d.checked_at))}</span></p>
      ${rows || note('No apps installed')}</div>`;
  };
  bindClicks(el, (e) => {
    const b = e.target.closest('[data-wam-conn]');
    if (!b) return;
    e.stopPropagation();
    open = open === b.dataset.wamConn ? '' : b.dataset.wamConn;
    paint(el._wamConn);
  });
  if (el._wamConn) paint(el._wamConn);
  // Live first (wam's loopback API: /api/connections checks Docker now), else the registry snapshot in apps.json.
  try {
    const live = await req(ctx.connections || '/api/connections');
    if (!Array.isArray(live.apps)) throw new Error('no live data');
    el._wamConn = live;
  } catch {
    try {
      const reg = await req(`${c.wizard}/wizard-apps`);
      el._wamConn = reg.connections || { error: 'this App Manager does not report connections yet' };
    } catch (err) {
      el._wamConn = { error: err.message };
    }
  }
  paint(el._wamConn);
}
