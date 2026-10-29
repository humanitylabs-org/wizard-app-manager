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

import {
  BUILTIN,
  agentList,
  agentName,
  agentNote,
  bandOf,
  bands,
  canUse,
  connectable,
  cutGroups,
  cutRows,
  cuttable,
  doneWords,
  linkInfo,
  linkTag,
  linkedApps,
  list,
  listLayout,
  mapModel,
  pickFirst,
  reconnectRows,
  restoreWords,
  talksSentence,
} from './connections-map.js';
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
const post = (url, data, method = 'POST', headers = {}) =>
  req(url, { method, headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(data || {}) });

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
// The owner's app connections as one list on every screen (listLayout: one column on a phone, Linked apps in a second
// column on a wide pane), and behind See map as one picture, in the visual language of Wizard's Home map (round discs, wires drawn
// between them, every word on a small chip, simple at rest and more on tap). Data: the network report from
// wam/network.py (live from wam's /api/connections, else the apps.json snapshot) plus the App Manager's connection
// state (GET {api}/apps/wizard/connections) for what the owner decided; connections-map.js turns both into plain words.
//
// Open mode draws one "All connected" cluster and only the exceptions: a red broken line for a pair the owner cut
// off, an amber pulse for a request waiting for his OK, a lock for an app that asks him first (sensitive). Strict
// mode (declared) draws the connections that exist. Tapping an app opens the one pop-up (WzSheet) with what it can
// talk to and Cut off from… / Reconnect / Ask me first. Every change goes to the App Manager's owner-authenticated API
// (the same sign-in as install/start/stop); without it the view is read-only. wam serve applies changes within
// seconds. The technical words (mesh, drift, declared, the gateway) live behind Details.

// How an app sits on the networks, for Details (wam/network.py `state`).
const STATE_WORDS = {
  'host-network': 'runs on the server’s own network.',
  exposed: 'is published through Runtipi’s reverse proxy, so it also keeps Runtipi’s shared network.',
  custom: 'has its own network settings.',
  'no-network': 'has no network.',
  shared: 'is on Runtipi’s shared network.',
};

// Owner decisions from the App Manager win over the (possibly older) report snapshot.
function mergeOwnerState(d, st) {
  if (!d || !Array.isArray(d.apps) || !st || !Array.isArray(st.grants)) return d;
  const key = (g) => `${g.from}\0${g.to}`;
  const pend = new Set((st.pending || []).map(key));
  const den = new Set((st.denied || []).map(key));
  const ok = new Set((st.grants || []).map(key));
  const controls = (d.controls || []).map((r) => {
    const k = key(r);
    return { ...r, status: pend.has(k) ? 'pending' : den.has(k) ? 'disconnected' : ok.has(k) ? 'connected' : r.status };
  });
  const names = Object.fromEntries(d.apps.map((a) => [a.id, a.name || a.id]));
  // Connections the (possibly older) report doesn't know yet: a runtime request approved since, a decision just made.
  const rows = [
    ...(st.pending || []).map((g) => [g, 'pending']),
    ...(st.denied || []).map((g) => [g, 'disconnected']),
    ...(st.grants || []).filter((g) => g.approved !== 'install' && g.approved !== 'open').map((g) => [g, 'connected']),
  ];
  for (const [g, status] of rows)
    if (names[g.from] && !controls.some((r) => key(r) === key(g))) {
      const to = String(g.to).replace(/^slot:/, '');
      const origin = g.reason === 'request' ? 'requested' : ['uses', 'requires'].includes(g.reason) ? 'declared' : 'owner';
      controls.push({ from: g.from, to: g.to, providers: names[to] ? [to] : [], status, origin });
    }
  const sensitive = Array.isArray(st.sensitive) ? st.sensitive : [];
  return {
    ...d,
    mode: st.mode === 'declared' || st.mode === 'open' ? st.mode : d.mode,
    controls,
    sensitive,
    owner_state: true,
    notices: [...(st.notices || [])].reverse().slice(0, 20),
    apps: d.apps.map((a) => ({ ...a, sensitive: sensitive.includes(a.id) })),
  };
}

// Shared Wizard pieces when the page runs on the shell's origin (connections_view.py links them): app glyphs
// (WizSigil, core/sigils.js) and the one pop-up (WzSheet, kit/wizard-sheet.js). wam's loopback page has neither, so a
// letter disc and a plain <dialog> stand in.
const cmLetter = (a) =>
  `<span class="wam-cm-ic wam-cm-letter" aria-hidden="true">${esc(
    String(a.name || a.id || '?')
      .trim()
      .charAt(0)
      .toUpperCase(),
  )}</span>`;
const cmGlyph = (a) => {
  try {
    if (window.WizSigil?.box) return window.WizSigil.box('span', 'wam-cm-ic', { id: a.id, name: a.name });
  } catch {
    /* a letter instead */
  }
  return cmLetter(a);
};
const LOCK =
  '<span class="wam-cm-badge" aria-hidden="true"><svg viewBox="0 0 24 24"><rect x="5.5" y="10.5" width="13" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/></svg></span>';
// Comes with Wizard: a small sparkle badge on the disc of Wizard's own pieces (the shell, the Agent Service).
const WIZ =
  '<span class="wam-cm-badge is-wiz" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M12 3.5l2.1 6.4 6.4 2.1-6.4 2.1L12 20.5l-2.1-6.4L3.5 12l6.4-2.1z"/></svg></span>';
const sheetOpen = (dlg) => {
  if (window.WzSheet?.open) return window.WzSheet.open(dlg);
  dlg.classList.add('wam-cm-own');
  if (!dlg.querySelector(':scope > .wz-sheet-x'))
    dlg.insertAdjacentHTML('afterbegin', '<button type="button" class="wz-sheet-x" aria-label="Close">×</button>');
  if (!dlg.open) dlg.showModal();
  return dlg;
};
const sheetClose = (dlg) => {
  if (!dlg?.open) return;
  if (window.WzSheet?.close) window.WzSheet.close(dlg);
  else dlg.close();
};

export async function renderConnections(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  let canAct = false;
  let readOnlyWhy = '';
  let busy = '';
  let toast = null; // { text, undo: {from, to} } after a change
  let toastTimer = 0;
  let details = false;
  let sheetId = ''; // the app whose sheet is open ('@agent:<i>' an agent, '@sys:<id>' part of Wizard, '@link:<a>|<b>' a line)
  let picking = false; // the sheet's chooser is open: Cut off from… (Open mode) or Connect to… (Strict mode)
  let showMap = false; // the list shows on every screen; the map is one tap away (See map)
  let access = null; // the agents and what each may use (the owner listener's api/agents; null: not known here)
  let restorePlan = null; // Restore defaults: what it would change (api/connections/restore), for its dialog
  let drawn = '';
  let dlg = null;
  const ago = (t) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - Number(t || 0)));
    return s < 90
      ? 'just now'
      : s < 5400
        ? `${Math.round(s / 60)} min ago`
        : s < 172800
          ? `${Math.round(s / 3600)} h ago`
          : `${Math.round(s / 86400)} days ago`;
  };
  const dis = (key) => (!canAct || busy ? ' disabled' : '') + (busy === key ? ' aria-busy="true"' : '');
  const sw = (on, attrs, key, label) =>
    `<button type="button" class="wam-cm-switch" role="switch" aria-checked="${on}" aria-label="${esc(label)}" ${attrs}${dis(key)}><i></i></button>`;
  const roOnly = () =>
    canAct
      ? ''
      : `<p class="wam-cm-ro">${esc(
          readOnlyWhy
            ? 'You can look here. Changes work from your phone or computer, not from this Wizard itself.'
            : 'Read-only here: changes need the App Manager owner sign-in (or `wam grants` on the server).',
        )}</p>`;

  // Open mode: the exceptions first and side by side (so a cut pair's broken line stays short), then the rest by name.
  // Strict mode: the apps joined by lines sit next to the group they connect to (at the end of the top group, at the
  // start of the others), each set of connected apps together and in the same order in every group, so lines stay
  // short; the ones on their own sit apart.
  const order = (m, apps = m.apps, pos = 'mid') => {
    const byName = [...apps].sort((x, y) => x.name.localeCompare(y.name));
    if (m.mode === 'declared') {
      const comp = m.comp || {};
      const linked = byName.filter((a) => a.id in comp).sort((x, y) => comp[x.id] - comp[y.id] || x.name.localeCompare(y.name));
      const apart = byName.filter((a) => !(a.id in comp));
      return pos === 'top' ? [...apart, ...linked] : [...linked, ...apart];
    }
    const out = [];
    const put = (id) => {
      const a = apps.find((x) => x.id === id);
      if (a && !out.includes(a)) out.push(a);
    };
    for (const [a, b] of [...m.cuts, ...m.waits]) {
      put(a);
      put(b);
    }
    for (const a of byName) if (a.sensitive) put(a.id);
    for (const a of byName) put(a.id);
    return out;
  };

  const node = (a, _nm, mode) => {
    // Strict mode draws only connections: a cut there is a connection that isn't, said in the app's sheet
    const cutShown = mode !== 'declared' && a.cut.length;
    const role = a.waiting
      ? ['Waiting', 'warn']
      : cutShown
        ? [a.cut.length === 1 ? 'Cut off' : `Cut off ×${a.cut.length}`, 'bad']
        : a.sensitive
          ? ['Asks you first', '']
          : null;
    const cls = `${a.waiting ? ' is-wait' : ''}${cutShown ? ' is-cut' : ''}${a.sensitive ? ' is-ask' : ''}${a.alone ? ' is-alone' : ''}`;
    return `<button type="button" class="wam-cm-node${cls}" data-cm-app="${esc(a.id)}" aria-haspopup="dialog" aria-label="${esc(
      `${a.name} can talk to ${a.talksTo}${a.sensitive ? '. Asks you first' : ''}${a.waiting ? '. Waiting for you' : ''}`,
    )}">
      <span class="wam-cm-disc">${cmGlyph(a)}${a.sensitive ? LOCK : ''}</span>
      <span class="wam-cm-label"><span class="wam-cm-name">${esc(a.name)}</span>${role ? `<span class="wam-cm-role ${role[1]}">${esc(role[0])}</span>` : ''}</span>
    </button>`;
  };

  const paint = (d) => {
    if (!d || !Array.isArray(d.apps)) {
      el.innerHTML = `<div class="wam-cm">${note(d?.error ? `Can’t read the app networks: ${d.error}` : 'No connection data yet: run `wam connections` on the server')}</div>`;
      drawn = '';
      return;
    }
    const m = mapModel(d);
    // The agents as the Agent Service has them (name, harness, what each may use), so the map's agent discs and the
    // list open the same agent page by the same index.
    if (access?.service && access.agents?.length)
      m.agents = agentList(m, access).map((r) => ({
        id: r.id,
        name: r.title,
        real: r.title,
        main: r.main,
        private: r.private,
        limit: null,
        allow: null,
        line: r.line,
      }));
    el._wamModel = m;
    const names = Object.fromEntries(d.apps.map((a) => [a.id, a.name || a.id]));
    const nm = (id) => names[id] || String(id).replace(/^slot:/, '');
    const sens = new Set(m.apps.filter((a) => a.sensitive).map((a) => a.id));
    const asks = m.pending.length
      ? `<div class="wam-cm-asks">${m.pending
          .map((p) => {
            const key = `ask:${p.from}:${p.to}`;
            const who = p.toIds.filter((x) => sens.has(x)).map(nm);
            return `<div class="wam-cm-ask" role="group" aria-label="${esc(`${p.fromName} wants to talk to ${p.toName}`)}">
              <span class="wam-cm-ask-ics">${cmGlyph({ id: p.from, name: p.fromName })}${cmGlyph({ id: p.toIds[0], name: nm(p.toIds[0]) })}</span>
              <p><b>${esc(p.fromName)}</b> wants to talk to <b>${esc(p.toName)}</b>${who.length ? `<small>${esc(list(who))} asks you first.</small>` : ''}</p>
              <span class="wam-cm-ask-acts"><button type="button" class="wam-btn" data-cm-deny data-from="${esc(p.from)}" data-to="${esc(p.to)}"${dis(key)}>Don’t allow</button><button type="button" class="wam-btn primary" data-cm-allow data-from="${esc(p.from)}" data-to="${esc(p.to)}"${dis(key)}>Allow</button></span>
            </div>`;
          })
          .join('')}</div>`
      : '';
    const tag = m.mode === 'declared' ? 'Kept apart unless connected' : 'All connected';
    // Groups top to bottom (connections-map.js bands): the agents (they can use everything), wired into one frame
    // holding the apps, agent apps and connectors. Strict mode draws a line for each connection between them; Open
    // mode means they all talk to each other. Each group says in one line what it is. Wizard's own pieces sit in their
    // group with a "Comes with Wizard" badge.
    const agentNode = (g, i) =>
      `<button type="button" class="wam-cm-node is-agent" data-cm-agent="${i}" aria-haspopup="dialog" aria-label="${esc(g.line ? `${g.name}. ${g.line}` : `${g.name} can use ${canUse(g)}`)}">
        <span class="wam-cm-disc">${cmGlyph({ id: g.main ? 'wizard' : g.id, name: g.name })}</span>
        <span class="wam-cm-label"><span class="wam-cm-name">${esc(g.name)}</span><span class="wam-cm-role">${esc(g.line || `Can use: ${canUse(g)}`)}</span></span></button>`;
    const sysNode = (s) =>
      `<button type="button" class="wam-cm-node is-sys" data-cm-sys="${esc(s.id)}" aria-haspopup="dialog" aria-label="${esc(`${s.name}. ${BUILTIN}`)}">
        <span class="wam-cm-disc">${cmGlyph(s)}${WIZ}</span><span class="wam-cm-label"><span class="wam-cm-name">${esc(s.name)}</span><span class="wam-cm-role is-builtin">${esc(BUILTIN)}</span></span></button>`;
    const head = (b) => `<header class="wam-cm-bh"><h3>${esc(b.title)}</h3><p>${esc(b.blurb)}</p></header>`;
    const groups = bands(m);
    const band = (b, i, all) => {
      const pos = i === 0 && all.length > 1 ? 'top' : 'mid';
      const ordered = order(m, b.apps, pos).map((a) => node(a, nm, m.mode));
      // Wizard's own pieces are never connected: they sit with the apps on their own
      const nodes = [
        ...b.agents.map((g) => agentNode(g, m.agents.indexOf(g))),
        ...(pos === 'top' ? [...b.builtin.map(sysNode), ...ordered] : [...ordered, ...b.builtin.map(sysNode)]),
      ];
      const wide = b.key === 'agent' ? ' wam-cm-top' : ' wam-cm-grid';
      return `<section class="wam-cm-band" data-cm-band="${b.key}" aria-label="${esc(b.title)}">${head(b)}<div class="wam-cm-nodes${wide}">${nodes.join('')}</div></section>`;
    };
    const inCluster = groups.filter((b) => b.key !== 'agent');
    // Shown whenever there is something to group: on a box with only Wizard itself, the agent and Wizard's own pieces.
    const map =
      m.apps.length || m.builtin.length
        ? `<div class="wam-cm-map" data-mode="${m.mode}">
          <svg class="wam-cm-lines"></svg>
          ${groups
            .filter((b) => b.key === 'agent')
            .map((b) => band(b, 0, []))
            .join('')}
          ${inCluster.length ? `<div class="wam-cm-cluster"><span class="wam-cm-tag" data-cm-tag>${esc(tag)}</span>${inCluster.map(band).join('')}</div>` : ''}
        </div>`
        : note('No apps installed');
    // Details: the words an expert wants (mode, the live Docker check, the gateway, what can't be enforced).
    const unenforced = [];
    for (const a of d.apps)
      for (const r of a.open?.unenforced || [])
        if (a.id < r.app)
          unenforced.push(
            `${nm(a.id)} and ${nm(r.app)} are cut off, but both are published through Runtipi’s reverse proxy and still share its network, so they can still reach each other. Unpublish one to enforce it.`,
          );
    const coreIds = new Set(d.gateway?.core || []);
    const odd = d.apps.filter((a) => !coreIds.has(a.id) && (a.state !== 'isolated' || (a.host_access || []).length));
    const meshCount = d.apps.filter((a) => a.open?.mesh).length;
    const linked = d.apps.filter((a) => a.open && !a.open.mesh);
    const notices = (d.notices || []).slice(0, 8);
    const check = d.live
      ? d.ok
        ? '<span class="wam-tag good">Docker matches</span>'
        : '<span class="wam-tag bad">Drift found</span>'
      : '<span class="wam-tag warn">Live check unavailable</span>';
    const detailsHtml = `<div class="wam-cm-details"${details ? '' : ' hidden'}>
        ${
          d.enabled && d.owner_state
            ? `<div class="wam-cm-row"><span class="wam-cm-tx"><b>Keep apps apart</b><small>${
                m.mode === 'declared'
                  ? 'On (the default): apps can’t talk to each other unless connected, either because an app says it needs another or because you connected them. Turn off to let all your apps talk to each other.'
                  : 'Off: every app can talk to every other app unless you cut it off.'
              }</small></span>${sw(m.mode === 'declared', `data-cm-strict="${m.mode === 'declared' ? 'open' : 'declared'}"`, 'mode', 'Keep apps apart')}</div>`
            : ''
        }
        <p>${check} <span class="wam-cm-at">Checked ${esc(ago(d.checked_at))}</span></p>
        <p><b>Groups</b> come from each app’s wizard.md: your own choice in <code>app-kinds.json</code> first, then a <code>kind:</code> line if it has one; else an agent is <code>type: agent</code>, a connector is <code>type: external</code> (or only has a setup screen, <code>category: system</code>), an agent app has no screen of its own, and the rest are apps. The shell and Agent Service come with Wizard: they can’t be cut off.</p>
        <p><b>Your agent</b> uses apps through Wizard itself, not the app networks: keeping apps apart never cuts it off.</p>
        ${d.gateway?.detail ? `<p><b>Your agent</b> reaches apps through the Wizard gateway: ${esc(d.gateway.detail)}.</p>` : ''}
        ${m.mode === 'open' ? `<p><b>Mode</b> Open: ${meshCount} apps share one app network (the mesh)${linked.length ? `; ${linked.map((a) => nm(a.id)).join(', ')} use${linked.length === 1 ? 's' : ''} direct links instead` : ''}.</p>` : m.mode === 'declared' ? '<p><b>Mode</b> Declared: one network per connection an app declares or you approve.</p>' : '<p><b>Mode</b> Isolation is off: apps share Runtipi’s network.</p>'}
        ${
          (m.idleCuts || []).length
            ? `<p><b>Cuts kept for later</b> ${esc(list(m.idleCuts.map((x) => x.text)))}. No app is designed to link ${m.idleCuts.length === 1 ? 'these two' : 'them'}, so while apps are kept apart ${m.idleCuts.length === 1 ? 'this cut changes' : 'these cuts change'} nothing; ${m.idleCuts.length === 1 ? 'it applies' : 'they apply'} if you turn Keep apps apart off. Connect two apps links them again.</p>`
            : ''
        }
        ${odd.map((a) => `<p class="wam-cm-warn"><b>${esc(a.name || a.id)}</b> ${esc(a.why || STATE_WORDS[a.state] || a.state || '')}${(a.host_access || []).length ? ` Host access: ${esc(a.host_access.join(', '))}.` : ''}</p>`).join('')}
        ${unenforced.map((t) => `<p class="wam-cm-warn">${esc(t)}</p>`).join('')}
        ${d.apps.flatMap((a) => (a.drift || []).map((x) => `<p class="wam-cm-warn"><b>${esc(a.name || a.id)}</b> ${esc(x)}</p>`)).join('')}
        ${notices.length ? `<h5>Recent changes</h5>${notices.map((n) => `<p>${esc(n.text)} <span class="wam-cm-at">${esc(ago(n.at))}</span></p>`).join('')}` : ''}
      </div>`;
    const toastHtml = toast
      ? `<div class="wam-cm-toast" role="status" aria-live="polite"><span>${esc(toast.text)}</span>${
          toast.undo ? `<button type="button" class="wam-btn" data-cm-undo${dis('undo')}>Undo</button>` : ''
        }</div>`
      : '<div class="wam-cm-toast" role="status" aria-live="polite" hidden></div>';
    // The list: one model and the same words on every screen (connections-map.js listLayout); a phone shows one
    // column, a wide pane puts Linked apps in a second column (the container query in wizard-apps.css). The map is
    // one tap away (See map).
    const chev = '<i class="wam-cm-go" aria-hidden="true"></i>';
    const lay = listLayout(m, access);
    const restoreBtn =
      lay.restore && el._wamConn?.owner_state
        ? `<button type="button" class="wam-btn" data-cm-restore aria-haspopup="dialog"${canAct ? '' : ' disabled'}>Restore defaults</button>`
        : '';
    const sec = (s, body) =>
      `<section class="wam-cl-sec is-${s.key}" aria-label="${esc(s.title)}"><h3>${esc(s.title)}${s.count == null ? '' : ` <span>${s.count}</span>`}</h3><div class="wam-cl-card">${body}</div></section>`;
    const sectionHtml = (s) => {
      if (s.key === 'wait')
        return `<section class="wam-cl-wait is-wait" aria-label="${esc(s.title)}"><h3>${esc(s.title)}</h3>${s.rows
          .map((p) => {
            const key = `ask:${p.from}:${p.to}`;
            return `<div class="wam-cl-ask"><span class="wam-cm-ask-ics">${cmGlyph({ id: p.from, name: p.fromName })}${cmGlyph({ id: p.toIds[0], name: nm(p.toIds[0]) })}</span><p>${esc(p.text)}</p>
              <span class="wam-cm-ask-acts"><button type="button" class="wam-btn" data-cm-deny data-from="${esc(p.from)}" data-to="${esc(p.to)}"${dis(key)}>Don’t allow</button><button type="button" class="wam-btn primary" data-cm-allow data-from="${esc(p.from)}" data-to="${esc(p.to)}"${dis(key)}>Allow</button></span></div>`;
          })
          .join('')}</section>`;
      if (s.key === 'agent')
        return sec(
          s,
          s.rows
            .map(
              (g) =>
                `<button type="button" class="wam-cl-row is-agent" data-cm-agent="${g.index}" aria-haspopup="dialog">${cmGlyph({ id: g.main ? 'wizard' : g.id, name: g.title })}<span class="wam-cl-tx"><b>${esc(agentName(g))}</b><small>${esc(g.line)}</small></span>${chev}</button>`,
            )
            .join(''),
        );
      if (s.key === 'linked') {
        const links = (r) =>
          r.links
            .map(
              (l) =>
                `<span class="wam-cl-link${l.on ? '' : ' is-off'}">${esc(l.name)}${!l.on ? `<small>${esc(linkTag(l).trim())}</small>` : ''}</span>`,
            )
            .join('<span class="wam-cl-sep">, </span>');
        const turnOn = (r) => {
          const l = r.turnOn;
          const key = `turnon:${l.pair || `${l.group.from}|${l.group.to}`}`;
          const at = l.pair ? `data-cm-recut="${esc(l.pair)}"` : `data-cm-regrant="${esc(`${l.group.from}|${l.group.to}`)}"`;
          return `<button type="button" class="wam-btn wam-cl-on" ${at} aria-label="${esc(`Turn on ${r.name} → ${l.name}`)}"${dis(key)}>Turn on</button>`;
        };
        const body = s.rows.length
          ? s.rows
              .map(
                (r) =>
                  `<div class="wam-cl-row has-act${r.links.every((l) => !l.on) ? ' is-off' : ''}"><button type="button" class="wam-cl-hit" data-cm-uses="${esc(r.id)}" aria-haspopup="dialog">${cmGlyph(r)}<span class="wam-cl-tx"><b>${esc(r.name)}</b> <span class="wam-cl-to">→ ${links(r)}</span></span>${r.turnOn ? '' : chev}</button>${r.turnOn ? turnOn(r) : ''}</div>`,
              )
              .join('')
          : `<p class="wam-cl-empty">${esc(s.empty)}</p>`;
        // One column: Restore defaults sits under the links it restores; a pane wide enough for two columns shows
        // the copy in the end row instead (CSS), so the page's last row isn't pushed below a short window.
        return `${sec(s, body).replace(/<\/section>$/, '')}${restoreBtn ? `<div class="wam-cl-sec-acts">${restoreBtn}</div>` : ''}</section>`;
      }
      return sec(
        s,
        s.rows
          .map((x) => {
            const at = x.pair ? `data-cm-recut="${esc(x.pair)}"` : `data-cm-regrant="${esc(`${x.group.from}|${x.group.to}`)}"`;
            const key = `recut:${x.pair || `${x.group.from}|${x.group.to}`}`;
            return `<div class="wam-cl-row is-static"><span class="wam-cm-pair">${cmGlyph({ id: x.a, name: nm(x.a) })}${cmGlyph({ id: x.b, name: nm(x.b) })}</span><span class="wam-cl-tx"><b>${esc(x.text)}</b></span><button type="button" class="wam-btn" ${at}${dis(key)}>Reconnect</button></div>`;
          })
          .join(''),
      );
    };
    const listHtml = `<div class="wam-cl${lay.columns === 2 ? ' is-two' : ''}">
        <h2 class="wam-cl-title">Connections</h2>
        <p class="wam-cl-lead">${esc(lay.lead)}</p>
        ${roOnly()}
        ${lay.sections.map(sectionHtml).join('')}
        <div class="wam-cl-end">
          <p class="wam-cl-foot">${esc(lay.footer)}</p>
          <div class="wam-cl-acts">${restoreBtn ? restoreBtn.replace('class="wam-btn"', 'class="wam-btn wam-cl-restore"') : ''}${
            lay.pick
              ? `<button type="button" class="wam-btn" data-cm-choose aria-haspopup="dialog"${canAct ? '' : ' disabled'}>${esc(lay.pick.label)}</button>`
              : ''
          }<button type="button" class="wam-btn wam-cl-mapbtn" data-cm-seemap>See map</button></div>
        </div>
      </div>`;
    const html = `<div class="wam-cm${showMap ? ' is-map' : ''}">
        ${listHtml}
        <div class="wam-cm-full">
        <button type="button" class="wam-btn wam-cl-back" data-cm-seelist>‹ Back to list</button>
        <p class="wam-cm-lead">${esc(m.lead)}${m.hint ? `<small>${esc(m.hint)}</small>` : ''}</p>
        ${roOnly()}${asks}${map}
        </div>
        <button type="button" class="wam-cm-more" data-cm-details aria-expanded="${details}">Details<i class="wam-cm-chev${details ? ' is-up' : ''}" aria-hidden="true"></i></button>
        ${detailsHtml}${toastHtml}
      </div>`;
    if (html !== el._wamHtml) {
      el._wamHtml = html;
      el.innerHTML = html;
      drawn = '';
    }
    requestAnimationFrame(drawLines);
    if (sheetId && dlg?.open) paintSheet();
  };

  // ---- wires, drawn from the real positions (like the Home map): only the agent's wire and the exceptions ----
  const SVG = 'http://www.w3.org/2000/svg';
  const sv = (tag, attrs, parent) => {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    parent?.appendChild(n);
    return n;
  };
  function drawLines() {
    const map = el.querySelector('.wam-cm-map');
    const svg = map?.querySelector('.wam-cm-lines');
    const m = el._wamModel;
    if (!svg || !m || typeof map.getBoundingClientRect !== 'function') return;
    const M = map.getBoundingClientRect();
    if (!M.width) return;
    const f = (v) => v.toFixed(1);
    const at = (e) => {
      const r = e.getBoundingClientRect();
      return { x: (r.left + r.right) / 2 - M.left, y: (r.top + r.bottom) / 2 - M.top, r: r.width / 2, t: r.top - M.top, b: r.bottom - M.top };
    };
    const disc = (id) => map.querySelector(`[data-cm-app="${CSS.escape(id)}"] .wam-cm-disc`);
    const specs = [];
    const tag = map.querySelector('[data-cm-tag]');
    // one wire from each agent into the cluster, as on the Home map
    for (const agent of tag ? map.querySelectorAll('[data-cm-agent] .wam-cm-disc') : []) {
      const a = at(agent);
      const t = at(tag);
      specs.push(['flow', `M${f(a.x)} ${f(a.b + 4)} L${f(t.x)} ${f(t.t - 2)}`]);
    }
    const pair = (kind, [p, q], key = kind === 'link' ? `${p}|${q}` : '') => {
      const A = disc(p);
      const B = disc(q);
      if (!A || !B) return;
      const a = at(A);
      const b = at(B);
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const L = Math.hypot(dx, dy) || 1;
      const [ux, uy] = [dx / L, dy / L];
      const s = { x: a.x + ux * (a.r + 4), y: a.y + uy * (a.r + 4) };
      const e = { x: b.x - ux * (b.r + 4), y: b.y - uy * (b.r + 4) };
      // side by side: a straight wire between the discs; further apart: a gentle arc that bows away from the labels
      // (Strict mode's connection lines bow less, so many of them stay readable)
      const bow = Math.abs(dy) < 4 && L < 3.2 * a.r + 80 ? 0 : kind === 'link' ? Math.min(28, L * 0.1) : Math.min(60, L * 0.22);
      const cx = (s.x + e.x) / 2 + uy * bow;
      const cy = (s.y + e.y) / 2 - Math.abs(ux) * bow;
      specs.push([kind, `M${f(s.x)} ${f(s.y)} Q${f(cx)} ${f(cy)} ${f(e.x)} ${f(e.y)}`, key]);
    };
    // Strict mode draws only what is connected (a line per connection, tappable); Open mode only the exceptions
    if (m.mode !== 'declared') for (const p of m.cuts) pair('cut', p);
    for (const p of m.waits) pair('wait', p);
    for (const p of m.links) pair('link', p);
    const key = JSON.stringify(specs);
    if (key === drawn && svg.firstChild) return;
    drawn = key;
    svg.replaceChildren();
    for (const [kind, d, key] of specs) {
      const base = sv('path', { d, class: `wam-cm-w is-${kind}` }, svg);
      if (key) {
        // a wide invisible path over the line takes the tap: it opens the line's sheet (Disconnect)
        const [p, q] = key.split('|');
        const hit = sv('path', { d, class: 'wam-cm-hit', 'data-cm-link': key }, svg);
        const t = document.createElementNS(SVG, 'title');
        t.textContent = `${nameOf(p)} and ${nameOf(q)} are connected`;
        hit.appendChild(t);
      }
      const L = base.getTotalLength?.() || 0;
      if (!L) continue;
      if (kind === 'flow') {
        // a light running down the agent's wire, as on the Home map
        const seg = Math.min(36, L * 0.45);
        const comet = sv('path', { d, class: 'wam-cm-comet', 'stroke-dasharray': `${f(seg)} ${f(L + seg)}` }, svg);
        comet.style.setProperty('--from', f(seg));
        comet.style.setProperty('--to', f(-L));
      }
      if (kind === 'cut') {
        const mid = base.getPointAtLength(L / 2);
        const g = sv('g', { class: 'wam-cm-x', transform: `translate(${f(mid.x)} ${f(mid.y)})` }, svg);
        sv('circle', { r: 9 }, g);
        sv('path', { d: 'M-3.2 -3.2 L3.2 3.2 M3.2 -3.2 L-3.2 3.2' }, g);
      }
    }
  }
  if (!el._wamResize && typeof ResizeObserver === 'function') {
    el._wamResize = new ResizeObserver(() => requestAnimationFrame(drawLines));
    el._wamResize.observe(el);
  }

  // ---- the sheet: what one app can talk to, in plain words, and the three things to do about it ----
  const ensureSheet = () => {
    if (dlg?.isConnected) return dlg;
    dlg = document.createElement('dialog');
    dlg.className = 'wz-sheet wam wam-cm-sheet';
    dlg.setAttribute('aria-labelledby', 'wamCmTitle');
    (el.closest('.wux') || document.body).appendChild(dlg);
    dlg.addEventListener('close', () => {
      sheetId = '';
      picking = false;
    });
    dlg.addEventListener('click', (e) => {
      if (!dlg.classList.contains('wam-cm-own')) return;
      if (e.target.closest('.wz-sheet-x')) return dlg.close();
      if (e.target === dlg) {
        const r = dlg.getBoundingClientRect();
        if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) dlg.close();
      }
    });
    bindClicks(dlg, onClick);
    return dlg;
  };
  const paintSheet = () => {
    const d = el._wamConn;
    const m = el._wamModel;
    if (!dlg || !d || !m) return;
    const body = dlg.querySelector('.wam-cm-sh') || dlg.appendChild(Object.assign(document.createElement('div'), { className: 'wam-cm-sh' }));
    const names = Object.fromEntries(d.apps.map((a) => [a.id, a.name || a.id]));
    const nm = (id) => names[id] || String(id).replace(/^slot:/, '');
    const set = (h) => {
      if (body._h === h) return;
      body._h = h;
      body.innerHTML = h;
    };
    const where = (kind) => {
      const b = bandOf(kind);
      return `<p class="wam-cm-where"><b>${esc(b.where)}</b> ${esc(b.blurb)}</p>`;
    };
    // The agent's list and the first-app chooser are long: on a wide screen their dialog is wider, in two columns.
    dlg.classList.toggle('is-wide', sheetId.startsWith('@agent:') || sheetId === '@pick' || picking);
    if (sheetId === '@pick') {
      // Connect two apps (Strict mode) / Cut two apps apart (Open mode), from the list: the first app, then its sheet
      // opens with the chooser for the second.
      const pk = pickFirst(m);
      if (!pk || !pk.apps.length) return sheetClose(dlg);
      set(`<div class="wam-cm-sh-head"><h2 id="wamCmTitle">${esc(pk.label)}</h2></div>
        <p class="wam-cm-says">${esc(pk.ask)}</p>
        <div class="wam-cm-chips is-all">${pk.apps
          .map((x) => `<button type="button" class="wam-cm-chip" data-cm-start="${esc(x.id)}">${cmGlyph(x)}<span>${esc(x.name)}</span></button>`)
          .join('')}</div>
        ${roOnly()}`);
      return;
    }
    if (sheetId === '@restore') {
      // Restore defaults: what changes, in plain words, then one confirm (the owner listener's plan and action)
      const w = restoreWords(restorePlan);
      const items = restorePlan
        ? w.items.length
          ? `<ul class="wam-cm-list">${w.items.map((t) => `<li>${esc(t)}</li>`).join('')}</ul>`
          : ''
        : '<p class="wam-cm-sh-note">Checking what would change…</p>';
      set(`<div class="wam-cm-sh-head"><h2 id="wamCmTitle">${esc(w.title)}</h2></div>
        ${restorePlan ? `<p class="wam-cm-says">${esc(w.says)}</p>` : ''}${items}
        <p class="wam-cm-sh-note">${esc(w.keep)}</p>
        <div class="wam-cm-sh-acts"><button type="button" class="wam-btn" data-cm-cancel>Cancel</button>${
          restorePlan && w.go ? `<button type="button" class="wam-btn primary" data-cm-restore-go${dis('restore')}>${esc(w.go)}</button>` : ''
        }</div>
        ${roOnly()}`);
      return;
    }
    if (sheetId.startsWith('@agent:')) {
      // An agent's page: what it may use, group by group, each item with its switch (on by default) and Turn all
      // on per group. A switch goes to the Agent Service, whose gateway enforces it on the agent's next call.
      const row = agentList(m, access)[Number(sheetId.slice(7))];
      if (!row) return sheetClose(dlg);
      const can = canAct && row.live;
      const swAt = (x) =>
        `<button type="button" class="wam-cm-switch" role="switch" aria-checked="${x.on}" aria-label="${esc(`${x.name} for ${row.title}`)}" data-cm-access="${esc(x.id)}"${
          !can || busy ? ' disabled' : ''
        }${busy === `access:${x.id}` ? ' aria-busy="true"' : ''}><i></i></button>`;
      const groups = row.groups
        .map(
          (b) =>
            `<div class="wam-cm-uses" role="group" aria-label="${esc(b.title)}"><div class="wam-cm-uses-h"><h5>${esc(b.title)}</h5>${
              row.live
                ? `<button type="button" class="wam-btn wam-cm-allon" data-cm-allon="${esc(b.key)}"${!can || busy || b.allOn ? ' disabled' : ''}${busy === `allon:${b.key}` ? ' aria-busy="true"' : ''}>Turn all on</button>`
                : ''
            }</div>${b.items
              .map(
                (x) =>
                  `<div class="wam-cm-srow wam-cm-use${x.on ? '' : ' is-off'}" data-cm-agent-use="${esc(x.id)}">${cmGlyph(x)}<span class="wam-cm-tx"><b>${esc(x.name)}</b>${
                    x.builtin ? `<small>${esc(BUILTIN)}</small>` : x.on ? '' : '<small>Off</small>'
                  }</span>${row.live ? swAt(x) : ''}</div>`,
              )
              .join('')}</div>`,
        )
        .join('');
      const runs = [row.harness && `Runs on ${row.harness}`, row.model && `${row.model}${row.provider ? ` (${row.provider})` : ''}`]
        .filter(Boolean)
        .join(' · ');
      set(`<div class="wam-cm-sh-head"><span class="wam-cm-disc">${cmGlyph({ id: row.main ? 'wizard' : row.id, name: row.title })}</span><h2 id="wamCmTitle">${esc(`${row.title} · what it can use`)}</h2></div>
        ${runs ? `<p class="wam-cm-runs">${esc(runs)}</p>` : where('agent')}
        <p class="wam-cm-says">${esc(row.line)}</p>
        <p class="wam-cm-sh-note">${esc(row.live ? agentNote(row) : 'What it can use comes from the Agent Service, which can’t be reached from here right now.')}</p>
        <div class="wam-cm-groups">${groups}</div>
        ${roOnly()}`);
      return;
    }
    if (sheetId.startsWith('@uses:')) {
      // The phone list: what one app is linked to, why, and Disconnect / Connect to…
      const id = sheetId.slice(6);
      const a = m.apps.find((x) => x.id === id);
      if (!a) return sheetClose(dlg);
      const r = linkedApps(m).find((x) => x.id === id) || { links: [] };
      const can = connectable(m, id);
      const rows = r.links
        .map((l) => {
          const id = l.pair || `${l.group.from}|${l.group.to}`;
          // on: Disconnect; off (cut by the owner): Turn on, in the same place
          const key = l.on ? (l.pair ? `disconnect:${l.pair}` : `ungroup:${id}`) : `turnon:${id}`;
          const at = l.on
            ? l.pair
              ? `data-cm-disconnect="${esc(l.pair)}"`
              : `data-cm-ungroup="${esc(id)}"`
            : l.pair
              ? `data-cm-recut="${esc(l.pair)}"`
              : `data-cm-regrant="${esc(id)}"`;
          const why = [...new Set([l.on ? '' : 'Off', l.added ? 'connected with your OK' : '', l.why].filter(Boolean))].join(' · ');
          return `<div class="wam-cm-srow${l.on ? '' : ' is-off'}">${cmGlyph({ id: l.id, name: l.name })}<span class="wam-cm-tx"><b>${esc(l.name)}</b><small>${esc(why)}</small></span><button type="button" class="wam-btn${l.on ? '' : ' primary'}" ${at}${dis(key)}>${l.on ? 'Disconnect' : 'Turn on'}</button></div>`;
        })
        .join('');
      const pick = picking
        ? `<div class="wam-cm-pick"><p>${esc(`Tap what ${a.name} should be able to talk to. It works both ways.`)}</p><div class="wam-cm-chips">${can
            .map(
              (x) =>
                `<button type="button" class="wam-cm-chip" data-cm-join="${esc(x.id)}"${dis(`join:${x.id}`)}>${cmGlyph(x)}<span>${esc(x.name)}</span></button>`,
            )
            .join('')}</div></div>`
        : '';
      set(`<div class="wam-cm-sh-head"><span class="wam-cm-disc">${cmGlyph(a)}</span><h2 id="wamCmTitle">${esc(a.name)}</h2></div>
        <p class="wam-cm-says">${esc(r.links.length ? `${a.name}’s links:` : `${a.name} isn’t linked to any app.`)}</p>
        ${rows}
        ${can.length && a.enforce ? `<div class="wam-cm-sh-acts"><button type="button" class="wam-btn${picking ? ' primary' : ''}" data-cm-pick aria-expanded="${picking}"${canAct ? '' : ' disabled'}>Connect to…</button></div>${pick}` : ''}
        ${roOnly()}`);
      return;
    }
    if (sheetId.startsWith('@sys:')) {
      const s = (m.builtin || []).find((x) => x.id === sheetId.slice(5));
      if (!s) return sheetClose(dlg);
      set(`<div class="wam-cm-sh-head"><span class="wam-cm-disc">${cmGlyph(s)}${WIZ}</span><h2 id="wamCmTitle">${esc(s.name)}</h2></div>
        ${where(s.kind)}
        <p class="wam-cm-says">${esc(`${BUILTIN}.`)}</p>
        <p class="wam-cm-sh-note">It is part of Wizard itself: your agent uses your apps through it, so it can’t be cut off, and keeping apps apart never touches it.</p>`);
      return;
    }
    if (sheetId.startsWith('@link:')) {
      const [p, q] = sheetId.slice(6).split('|');
      if (!m.links.some(([x, y]) => x === p && y === q)) return sheetClose(dlg);
      const info = linkInfo(m, p, q);
      const key = `disconnect:${p}|${q}`;
      set(`<div class="wam-cm-sh-head"><span class="wam-cm-pair">${cmGlyph({ id: p, name: nm(p) })}${cmGlyph({ id: q, name: nm(q) })}</span><h2 id="wamCmTitle">${esc(info.title)}</h2></div>
        ${info.why.map((w) => `<p class="wam-cm-says">${esc(w)}</p>`).join('')}
        ${info.groups
          .map(
            (g) =>
              `<p class="wam-cm-sh-note">${esc(`${nm(g.from)}’s connection goes to ${list((g.providers || []).map(nm))} together, so it is disconnected as one.`)}</p>`,
          )
          .join('')}
        ${info.warn ? `<p class="wam-cm-sh-note">${esc(info.warn)}</p>` : ''}
        <div class="wam-cm-sh-acts"><button type="button" class="wam-btn" data-cm-disconnect="${esc(`${p}|${q}`)}"${dis(key)}>Disconnect</button></div>
        ${roOnly()}`);
      return;
    }
    const a = m.apps.find((x) => x.id === sheetId);
    if (!a) return sheetClose(dlg);
    const cutList = a.cut.map(
      (
        b,
      ) => `<div class="wam-cm-srow">${cmGlyph({ id: b, name: nm(b) })}<span class="wam-cm-tx"><b>${esc(nm(b))}</b><small>Cut off by you</small></span>
        <button type="button" class="wam-btn primary" data-cm-reconnect="${esc(b)}"${dis(`reconnect:${b}`)}>Reconnect</button></div>`,
    );
    // Strict mode: a cut connection to a slot several apps provide comes back as one
    for (const [gi, g] of cutGroups(m, a.id, true).entries())
      cutList.push(`<div class="wam-cm-srow">${cmGlyph({ id: g.providers[0], name: nm(g.providers[0]) })}<span class="wam-cm-tx"><b>${esc(`${nm(g.from)} → ${g.names}`)}</b><small>Cut off by you, as one connection</small></span>
        <button type="button" class="wam-btn primary" data-cm-regroup="${gi}"${dis(`regroup:${gi}`)}>Reconnect</button></div>`);

    const askRows = a.ask.map((b) => {
      const waiting = (d.controls || []).find(
        (r) =>
          r.status === 'pending' &&
          ((r.from === a.id && ((r.providers || []).includes(b) || r.to === b)) ||
            (r.from === b && ((r.providers || []).includes(a.id) || r.to === a.id))),
      );
      return waiting
        ? `<div class="wam-cm-srow">${cmGlyph({ id: b, name: nm(b) })}<span class="wam-cm-tx"><b>${esc(nm(b))}</b><small>${esc(`${nm(waiting.from)} is waiting for your OK`)}</small></span>
          <span class="wam-cm-ask-acts"><button type="button" class="wam-btn" data-cm-deny data-from="${esc(waiting.from)}" data-to="${esc(waiting.to)}"${dis(`ask:${waiting.from}:${waiting.to}`)}>Don’t allow</button><button type="button" class="wam-btn primary" data-cm-allow data-from="${esc(waiting.from)}" data-to="${esc(waiting.to)}"${dis(`ask:${waiting.from}:${waiting.to}`)}>Allow</button></span></div>`
        : `<div class="wam-cm-srow">${cmGlyph({ id: b, name: nm(b) })}<span class="wam-cm-tx"><b>${esc(nm(b))}</b><small>Not connected yet: ${esc(a.sensitive ? a.name : nm(b))} asks you first</small></span>
          <button type="button" class="wam-btn primary" data-cm-connect="${esc(b)}"${dis(`connect:${b}`)}>Connect</button></div>`;
    });
    const strict = m.mode === 'declared';
    // Strict mode: each connection it has, with Disconnect; a connection to several apps doing one job is one row
    const groups = cutGroups(m, a.id);
    const linkRowsHtml = strict
      ? [
          ...cuttable(m, a.id).map((x) => {
            const pairId = [a.id, x.id].sort().join('|');
            const info = linkInfo(m, ...pairId.split('|'));
            return `<div class="wam-cm-srow">${cmGlyph(x)}<span class="wam-cm-tx"><b>${esc(x.name)}</b><small>${esc(`Connected. ${info.why.join(' ')}`)}</small></span>
              <button type="button" class="wam-btn" data-cm-disconnect="${esc(pairId)}"${dis(`disconnect:${pairId}`)}>Disconnect</button></div>`;
          }),
          ...groups.map(
            (
              g,
              i,
            ) => `<div class="wam-cm-srow">${cmGlyph({ id: g.providers[0], name: nm(g.providers[0]) })}<span class="wam-cm-tx"><b>${esc(`${nm(g.from)} → ${g.names}`)}</b><small>Connected, as one connection</small></span>
              <button type="button" class="wam-btn" data-cm-cutgroup="${i}"${dis(`cutgroup:${i}`)}>Disconnect</button></div>`,
          ),
        ]
      : [];
    const can = strict ? connectable(m, a.id) : cuttable(m, a.id);
    const pick =
      picking && strict
        ? `<div class="wam-cm-pick"><p>${esc(`Tap what ${a.name} should be able to talk to. It works both ways.`)}</p><div class="wam-cm-chips">${can
            .map(
              (x) =>
                `<button type="button" class="wam-cm-chip" data-cm-join="${esc(x.id)}"${dis(`join:${x.id}`)}>${cmGlyph(x)}<span>${esc(x.name)}</span></button>`,
            )
            .join('')}</div></div>`
        : picking
          ? `<div class="wam-cm-pick"><p>${esc(`Tap the app ${a.name} should stop talking to. It works both ways.`)}</p><div class="wam-cm-chips">${can
              .map(
                (x) =>
                  `<button type="button" class="wam-cm-chip" data-cm-cut="${esc(x.id)}"${dis(`cut:${x.id}`)}>${cmGlyph(x)}<span>${esc(x.name)}</span></button>`,
              )
              .join('')}${groups
              .map(
                (g, i) =>
                  `<button type="button" class="wam-cm-chip" data-cm-cutgroup="${i}"${dis(`cutgroup:${i}`)}>${cmGlyph({ id: g.providers[0], name: nm(g.providers[0]) })}<span>${esc(`${nm(g.from)} → ${g.names}`)}</span></button>`,
              )
              .join('')}</div>${groups.length ? '<p>A connection to several apps that do the same job is cut as one.</p>' : ''}</div>`
          : '';

    set(`<div class="wam-cm-sh-head"><span class="wam-cm-disc">${cmGlyph(a)}</span><h2 id="wamCmTitle">${esc(a.name)}</h2></div>
      ${where(a.kind)}
      <p class="wam-cm-says">${esc(talksSentence(a))}</p>
      ${linkRowsHtml.join('')}${cutList.join('')}${askRows.join('')}
      ${
        strict
          ? can.length && a.enforce
            ? `<div class="wam-cm-sh-acts"><button type="button" class="wam-btn${picking ? ' primary' : ''}" data-cm-pick aria-expanded="${picking}"${canAct ? '' : ' disabled'}>Connect to…</button></div>${pick}`
            : ''
          : can.length || groups.length
            ? `<div class="wam-cm-sh-acts"><button type="button" class="wam-btn${picking ? ' primary' : ''}" data-cm-pick aria-expanded="${picking}"${canAct ? '' : ' disabled'}>Cut off from…</button></div>${pick}`
            : ''
      }
      ${
        m.mode === 'off'
          ? ''
          : !a.enforce && !a.sensitive
            ? `<p class="wam-cm-sh-note">${esc(`Ask me first isn’t available for ${a.name}: it runs outside the app networks, so its connections can’t be held back.`)}</p>`
            : `<div class="wam-cm-row"><span class="wam-cm-tx"><b>Ask me first</b><small>${esc(
                a.sensitive
                  ? `On: a new app that wants to talk to ${a.name} waits for your OK. Apps it already talks to stay connected.`
                  : strict
                    ? `Off: an app that says it needs ${a.name} is connected to it right away. Turn on to approve each one first.`
                    : `Off: new apps can talk to ${a.name} right away. Turn on to approve each one first.`,
              )}</small></span>${sw(a.sensitive, `data-cm-askfirst="${esc(a.id)}"`, `ask-first:${a.id}`, 'Ask me first')}</div>`
      }
      ${roOnly()}`);
  };
  const openSheet = (id, pick = false) => {
    sheetId = id;
    picking = pick;
    ensureSheet();
    paintSheet();
    sheetOpen(dlg);
    // A sheet opens at its top, its title and intro in view, whatever the last one scrolled to (or focus scrolled to).
    const top = () => {
      dlg.scrollTop = 0;
      const sh = dlg.querySelector('.wam-cm-sh');
      if (sh) sh.scrollTop = 0;
    };
    top();
    requestAnimationFrame(top);
  };

  // ---- data and changes ----
  const owner = async () => {
    try {
      const st = await req(`${c.api}/apps/wizard/connections`);
      if (!Array.isArray(st.grants)) throw new Error('no state');
      // The owner listener answers reads from anywhere, but refuses changes from the box itself (its apps reach it
      // with the owner's identity too); it says so up front instead of after a tap.
      canAct = st.owner_writes !== false;
      readOnlyWhy = st.owner_writes === false ? st.owner_writes_why || '' : '';
      return st;
    } catch {
      canAct = false;
      readOnlyWhy = '';
      return null;
    }
  };
  const load = async () => {
    let d;
    // Live first (wam's loopback API: /api/connections checks Docker now), else the registry snapshot in apps.json.
    try {
      d = await req(ctx.connections || '/api/connections');
      if (!Array.isArray(d.apps)) throw new Error('no live data');
    } catch {
      try {
        const reg = await req(`${c.wizard}/wizard-apps`);
        d = reg.connections || { error: 'this App Manager does not report connections yet' };
      } catch (err) {
        d = { error: err.message };
      }
    }
    const st = await owner();
    // The agents and what each may use (the owner listener asks the Agent Service); elsewhere the report's lists.
    try {
      const got = await req(ctx.agents || `${c.api}/agents`);
      access = got && Array.isArray(got.agents) ? got : null;
    } catch {
      access = null;
    }
    el._wamConn = st ? mergeOwnerState(d, st) : d;
    paint(el._wamConn);
  };
  const say = (t) => {
    toast = t;
    clearTimeout(toastTimer);
    if (t)
      toastTimer = setTimeout(
        () => {
          toast = null;
          paint(el._wamConn);
        },
        t.undo ? 12000 : 6000,
      );
  };
  // A finishing action (cut, reconnect, allow, don't allow) closes the sheet and says what happened, with Undo for a
  // cut; a switch (Ask me first, Strict mode) stays where it is.
  const run = async (key, fn, done, { close = false, undo = null } = {}) => {
    busy = key;
    paint(el._wamConn);
    if (sheetId) paintSheet();
    let ok = false;
    try {
      await fn();
      ok = true;
      say({ text: done, undo });
    } catch (err) {
      say({
        text: /401|403|unauthor|forbidden|bearer/i.test(err.message)
          ? 'Not changed: this needs the App Manager owner sign-in.'
          : `Not changed: ${err.message}`,
      });
    }
    busy = '';
    if (ok && close) sheetClose(dlg);
    await load();
    // wam serve applies the change to Docker within seconds; read the live result once more.
    setTimeout(() => void load(), 5000);
  };
  const grant = (from, to, approve) => post(`${c.api}/apps/${encodeURIComponent(from)}:${STORE}/app-grants`, { to, approve }, 'POST', c.headers);
  const nameOf = (id) => el._wamModel?.apps.find((a) => a.id === id)?.name || String(id).replace(/^slot:/, '');

  function onClick(e) {
    const t = (sel) => e.target.closest(sel);
    const d = el._wamConn;
    const appNode = t('[data-cm-app]');
    if (appNode) return openSheet(appNode.dataset.cmApp);
    const agentNode = t('[data-cm-agent]');
    if (agentNode) return openSheet(`@agent:${agentNode.dataset.cmAgent}`);
    const sysNode = t('[data-cm-sys]');
    if (sysNode) return openSheet(`@sys:${sysNode.dataset.cmSys}`);
    const line = t('[data-cm-link]');
    if (line) return openSheet(`@link:${line.getAttribute('data-cm-link')}`);
    const usesRow = t('[data-cm-uses]');
    if (usesRow) return openSheet(`@uses:${usesRow.dataset.cmUses}`);
    if (t('[data-cm-choose]:not([disabled])')) return openSheet('@pick');
    const first = t('[data-cm-start]');
    if (first) {
      // the first app picked: its own sheet, with the chooser for the second already open
      const id = first.dataset.cmStart;
      return openSheet(el._wamModel?.mode === 'declared' ? `@uses:${id}` : id, true);
    }
    if (t('[data-cm-seemap]') || t('[data-cm-seelist]')) {
      showMap = !!t('[data-cm-seemap]');
      paint(d);
      el.querySelector(showMap ? '.wam-cl-back' : '.wam-cl')?.scrollIntoView?.({ block: 'start' });
      return;
    }
    if (t('[data-cm-details]')) {
      details = !details;
      el._wamHtml = '';
      return paint(d);
    }
    const b = t('button');
    if (!b || b.disabled) return;
    if (b.matches('[data-cm-pick]')) {
      picking = !picking;
      return paintSheet();
    }
    if (b.matches('[data-cm-cut]')) {
      const to = b.dataset.cmCut;
      const rows = cutRows(el._wamModel?.mode, d.controls, sheetId, to);
      return void run(
        `cut:${to}`,
        async () => {
          for (const r of rows) await grant(r.from, r.to, false);
        },
        doneWords('cut', nameOf(sheetId), nameOf(to)),
        { close: true, undo: { rows, a: sheetId, b: to } },
      );
    }
    if (b.matches('[data-cm-disconnect]')) {
      // Strict mode: a line's Disconnect denies every connection between the pair, whichever app declared it; a
      // connection to a slot several apps provide is denied as one
      const [p, q] = b.dataset.cmDisconnect.split('|');
      const info = linkInfo(el._wamModel, p, q);
      const rows = info.rows.length ? info.rows : cutRows('declared', d.controls, p, q);
      return void run(
        `disconnect:${p}|${q}`,
        async () => {
          for (const r of rows) await grant(r.from, r.to, false);
        },
        doneWords('disconnect', nameOf(p), nameOf(q)),
        { close: true, undo: { rows, a: p, b: q } },
      );
    }
    if (b.matches('[data-cm-join]')) {
      // Strict mode: Connect to… records the owner's own connection (it links both ways)
      const to = b.dataset.cmJoin;
      const from = sheetId.replace(/^@uses:/, '');
      return void run(`join:${to}`, () => grant(from, to, true), doneWords('connect', nameOf(from), nameOf(to)), { close: true });
    }
    if (b.matches('[data-cm-cutgroup]')) {
      const g = cutGroups(el._wamModel, sheetId)[Number(b.dataset.cmCutgroup)];
      if (!g) return;
      const rows = [{ from: g.from, to: g.to }];
      return void run(
        `cutgroup:${b.dataset.cmCutgroup}`,
        async () => {
          for (const r of rows) await grant(r.from, r.to, false);
        },
        doneWords('cut', nameOf(g.from), g.names),
        { close: true, undo: { rows, a: g.from, b: g.providers[0] } },
      );
    }
    if (b.matches('[data-cm-regroup]')) {
      const g = cutGroups(el._wamModel, sheetId, true)[Number(b.dataset.cmRegroup)];
      if (!g) return;
      return void run(`regroup:${b.dataset.cmRegroup}`, () => grant(g.from, g.to, true), doneWords('reconnect', nameOf(g.from), g.names), {
        close: true,
      });
    }
    if (b.matches('[data-cm-recut]')) {
      // Linked apps: Turn on a link the owner cut (inline or in the app's sheet) undoes every Disconnect between the pair
      const [p, q] = b.dataset.cmRecut.split('|');
      const rows = reconnectRows(d.controls, p, q);
      return void run(
        `turnon:${p}|${q}`,
        async () => {
          for (const r of rows) await grant(r.from, r.to, true);
        },
        doneWords('reconnect', nameOf(p), nameOf(q)),
        { close: !!dlg?.open },
      );
    }
    if (b.matches('[data-cm-access]') || b.matches('[data-cm-allon]')) {
      // An agent's page: one item's switch, or Turn all on for a group. The Agent Service's gateway enforces it.
      const row = agentList(el._wamModel, access)[Number(sheetId.slice(7))];
      if (!row?.live) return;
      const one = b.matches('[data-cm-access]');
      const group = one ? null : row.groups.find((g) => g.key === b.dataset.cmAllon);
      const ids = one ? [b.dataset.cmAccess] : (group?.items || []).filter((x) => !x.on).map((x) => x.id);
      if (!ids.length) return;
      const on = one ? b.getAttribute('aria-checked') !== 'true' : true;
      const item = one ? row.groups.flatMap((g) => g.items).find((x) => x.id === ids[0]) : null;
      return void run(
        one ? `access:${ids[0]}` : `allon:${group.key}`,
        () => post(`${c.api}/agents/${encodeURIComponent(row.id)}/access`, { apps: ids, on }, 'POST', c.headers),
        one
          ? on
            ? `${row.title} can use ${item?.name || ids[0]} again.`
            : `${row.title} can’t use ${item?.name || ids[0]} now.`
          : `${row.title} can use every item in ${group.title} again.`,
      );
    }
    if (b.matches('[data-cm-restore]')) {
      restorePlan = null;
      openSheet('@restore');
      req(`${c.api}/connections/restore`)
        .then((p) => {
          restorePlan = p && Array.isArray(p.changes) ? p : { changes: [] };
        })
        .catch(() => {
          restorePlan = { changes: [], error: true };
        })
        .finally(() => sheetId === '@restore' && paintSheet());
      return;
    }
    if (b.matches('[data-cm-cancel]')) return sheetClose(dlg);
    if (b.matches('[data-cm-restore-go]')) {
      return void run(
        'restore',
        () => post(`${c.api}/connections/restore`, { confirm: true, plan: restorePlan?.plan || '' }, 'POST', c.headers),
        restoreWords(restorePlan).done,
        {
          close: true,
        },
      );
    }
    if (b.matches('[data-cm-regrant], [data-cm-ungroup]')) {
      // a connection to a slot several apps provide, reconnected (Cut by you) or disconnected (an app's sheet) as one
      const back = b.matches('[data-cm-regrant]');
      const [from, to] = (back ? b.dataset.cmRegrant : b.dataset.cmUngroup).split('|');
      const g = (el._wamModel?.groups || []).find((x) => x.from === from && x.to === to);
      const names = g?.names || nameOf(to);
      return void run(
        `${back ? 'turnon' : 'ungroup'}:${from}|${to}`,
        () => grant(from, to, back),
        doneWords(back ? 'reconnect' : 'disconnect', nameOf(from), names),
        back ? { close: !!dlg?.open } : { close: true, undo: { rows: [{ from, to }], a: from, b: g?.providers?.[0] || to } },
      );
    }
    if (b.matches('[data-cm-reconnect]')) {
      const other = b.dataset.cmReconnect;
      const rows = reconnectRows(d.controls, sheetId, other);
      return void run(
        `reconnect:${other}`,
        async () => {
          for (const r of rows) await grant(r.from, r.to, true);
        },
        doneWords('reconnect', nameOf(sheetId), nameOf(other)),
        { close: true },
      );
    }

    if (b.matches('[data-cm-connect]')) {
      const other = b.dataset.cmConnect;
      // the request goes to the app that asks first, like an Approve
      const [from, to] = (d.sensitive || []).includes(other) ? [sheetId, other] : [other, sheetId];
      return void run(`connect:${other}`, () => grant(from, to, true), doneWords('reconnect', nameOf(from), nameOf(to)), { close: true });
    }
    if (b.matches('[data-cm-allow], [data-cm-deny]')) {
      const { from, to } = b.dataset;
      const allow = b.matches('[data-cm-allow]');
      return void run(`ask:${from}:${to}`, () => grant(from, to, allow), doneWords(allow ? 'allow' : 'deny', nameOf(from), nameOf(to)), {
        close: true,
      });
    }
    if (b.matches('[data-cm-undo]') && toast?.undo) {
      // Every Disconnect now recorded between the pair, read fresh: the App Manager may have recorded more rows than
      // the one the cut asked for (a slot the same app also fills).
      const { a, b: other } = toast.undo;
      const rows = reconnectRows(el._wamConn?.controls, a, other);
      return void run(
        'undo',
        async () => {
          for (const r of rows) await grant(r.from, r.to, true);
        },
        doneWords('reconnect', nameOf(a), nameOf(other)),
      );
    }
    if (b.matches('[data-cm-askfirst]')) {
      const id = b.dataset.cmAskfirst;
      const on = b.getAttribute('aria-checked') !== 'true';
      return void run(
        `ask-first:${id}`,
        () => post(`${c.api}/apps/${encodeURIComponent(id)}:${STORE}/sensitive`, { sensitive: on }, 'POST', c.headers),
        doneWords(on ? 'ask-on' : 'ask-off', nameOf(id)),
      );
    }
    if (b.matches('[data-cm-strict]')) {
      const mode = b.dataset.cmStrict;
      return void run(
        'mode',
        () => post(`${c.api}/apps/wizard/connection-mode`, { mode }, 'POST', c.headers),
        doneWords(mode === 'declared' ? 'strict' : 'open'),
      );
    }
  }
  bindClicks(el, onClick);
  // Esc closes the open dialog first. Inside Wizard the shell takes every key from the window (capture on the
  // document) and, in full screen, would leave full screen and keep the dialog open; the window's capture runs first.
  if (!el._wamEsc) {
    el._wamEsc = (e) => {
      if (e.key !== 'Escape' || e.isComposing || !dlg?.open) return;
      e.preventDefault();
      e.stopPropagation();
      sheetClose(dlg);
    };
    window.addEventListener('keydown', el._wamEsc, true);
  }
  if (el._wamConn) paint(el._wamConn);
  await load();
  // Live: a request waiting for the owner, or a change made elsewhere, shows up without a reload.
  if (!el._wamPoll)
    el._wamPoll = setInterval(() => {
      if (document.visibilityState === 'visible' && el.isConnected && !busy) void load();
    }, 20000);
}
