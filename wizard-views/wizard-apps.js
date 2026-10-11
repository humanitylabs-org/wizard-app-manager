// Wizard App Manager views for Wizard UX: My Apps, App Store, Resources. Vanilla ES module, no build step.
// Markup reuses Wizard UX classes (.lgroup .lgrid .lapp .lrow .st-group .st-row .now-card); pieces it adds are .wam-*.
//
//   renderMyApps(el, ctx)         installed apps as a graph: Views / Agents / Apps / System rows around one anchor
//   renderStore(el, ctx)          the App Index as the same graph; chip row (tags + sort) goes into ctx.chipsEl
//   renderResources(el, ctx)      a Settings group: Storage, Processor, Memory, Start/Stop/Restart all
//   renderResourceCard(el, ctx)   the home-screen card: three small live meters; ctx.onOpen() when tapped
//   renderCard(el, app, ctx)      one app: summary, status, what it works with, only the buttons that apply
//
// ctx (all optional): api ('/api', Runtipi), wizard ('/wizard/v1'), index ('/wizard/v1/index', or
// 'https://apps.aiwizards.com/v1'), chipsEl, shellTiles ({ Views: [html], Apps: [html] }: the shell's own tiles,
// left as they are so the shell still opens them), extraTiles (same as shellTiles.Apps), viewsAt ('top' | 'bottom';
// default bottom on a narrow screen, where the thumb is, top on a wide one), shell (true: Open buttons carry data-app
// and bubble to the shell's own launcher), onApp(app, from), onGetApps(), onBack(), onOpen().
//
// The graph: tap a tile to make it the anchor (its links move into view, the rest dims); tap the anchor to open it.

import { runtipiDomainUrl } from './runtipi-open.js';
import { ROWS, layersOf, layout, lineage, linksOf, neighbours } from './wizard-graph.js';

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
const STATUS = { running: 'Running', stopped: 'Stopped', missing: 'Not running' };
const GET = '__get';

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
const cache = { apps: null, at: 0, index: null, indexAt: 0, slots: new Map(), parking: [] };

async function indexMeta(c) {
  if (cache.index && Date.now() - cache.indexAt < 300e3) return cache.index;
  const d = await req(`${c.index}/search?limit=100&sort=name`);
  cache.index = new Map((d.apps || []).map((a) => [a.id, fromIndex(a, c)]));
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
    needs: m.needs || [],
    provides: m.provides || i.provides || [],
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
  // Parking spots: which app fills which system role (agent, files, transcription...). Swappable, so shown.
  cache.parking = meta.parking || [];
  cache.slots = new Map(cache.parking.filter((p) => p.active).map((p) => [p.active, p]));
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
    provides: a.provides || [],
    repo: a.repo,
    installs: Number(a.installs || 0),
    screen: a.type !== 'service',
    logo: `${c.api}/marketplace/apps/${encodeURIComponent(`${a.id}:${STORE}`)}/image`,
    installed: false,
  };
}

// ---- pieces ------------------------------------------------------------------------------------------------------
/** Agents are agents; apps filling a parking spot (or built as system) are System; anything with a screen is a View. */
const rowOf = (a) =>
  a.type === 'agent' || a.category === 'agent'
    ? 'Agents'
    : cache.slots.has(a.id) || a.category === 'system'
      ? 'System'
      : a.type === 'view' && a.screen
        ? 'Views'
        : 'Apps';

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

/** The tile label: "Hermes Desktop in Browser" reads as "Hermes Desktop"; the card and tooltip keep the full name. */
const shortName = (a) => a.short_name || String(a.name || a.id).replace(/\s+(?:in|for|on|via)\s+\S.*$/i, '');

/**
 * One tile. Marks, one meaning each: a small dot = running (red when it isn't), a quiet ↑ = update available, a faint
 * solid ring = no screen. In the store, ✓ = on this box and an amber + = will be added. The anchor gets the selection
 * ring and an action pill: Open (it has a screen and is running) or Details (it opens the card).
 */
function tile(a, cls = '', style = '', mark = '', act = '', spot = '') {
  const dot = a.installed ? `<i class="wam-dot ${esc(a.status)}"></i>` : '';
  const up = a.update ? '<i class="wam-up" title="Update available">↑</i>' : '';
  const ok = mark || (!a.installed && a.have ? '<i class="wam-ok">✓</i>' : '');
  const pill = act ? `<span class="wam-act" ${act}</span>` : '';
  const tip = `${a.name}${a.summary ? ` · ${a.summary}` : ''}${a.screen ? '' : ' · No screen: ask your agent to use it'}`;
  return `<button class="lapp${a.screen ? '' : ' headless'} ${cls}" style="${style}" data-wam-id="${esc(a.id)}" data-wam-key="${esc(a.id)}" title="${esc(tip)}"><span class="ic" style="${gstyle(a)}">${icon(a)}${dot}${up}${ok}${pill}</span><span class="nm">${esc(shortName(a))}</span>${spot ? `<span class="wam-spot-k">${esc(spot)} spot</span>` : ''}</button>`;
}

/** The anchor's pill: Open goes straight to the app's screen (as the card's Open does); Details opens the card. */
function pillFor(a, c) {
  if (a.installed && a.screen && a.open && a.status === 'running') {
    const same = c.shell && new URL(a.open, location.href).origin === location.origin;
    return `data-wam-act="open"${same ? ` data-app="${esc(a.id)}"` : ''}>Open`;
  }
  return 'data-wam-act="details">Details';
}

/**
 * One of the shell's own tiles (Inbox, Chats...), as a graph node like any other: its look is kept, its data-app
 * moves aside so the first tap selects it (the shell would open it at once); the second tap hands it back.
 */
function shellTile(html, cls, style, key, anchor) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  const b = t.content.firstElementChild;
  if (!b) return '';
  b.classList.add(...cls.split(' ').filter(Boolean));
  b.style.gridColumn = style.col;
  b.style.gridRow = style.row;
  b.dataset.wamKey = key;
  b.dataset.wamId = key;
  if (b.dataset.app) b.dataset.wamApp = b.dataset.app;
  b.removeAttribute('data-app');
  if (anchor) b.querySelector('.ic')?.insertAdjacentHTML('beforeend', '<span class="wam-act" data-wam-act="shell">Open</span>');
  return b.outerHTML;
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

// ---- graph -------------------------------------------------------------------------------------------------------
const still = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const colsFor = (el) => Math.max(4, Math.min(8, Math.floor(((el.clientWidth || 352) + 8) / 92)));
const viewsAt = (el, ctx) => ctx.viewsAt || ((el.clientWidth || 0) >= 560 ? 'top' : 'bottom');
let maskN = 0;

/**
 * Draw one graph into el. g: { rows, links, state: { anchor, anchorCol, open }, html(id, cls, place), pinned,
 * note(row, layout), dashed: Set of ids whose line is a missing requirement, viewsAt }.
 */
function drawGraph(el, g) {
  const cols = colsFor(el);
  const st = g.state;
  st.shut = st.shut || new Set();
  const before = new Map();
  for (const n of el.querySelectorAll('[data-wam-key]')) before.set(n.dataset.wamKey, { r: n.getBoundingClientRect(), dim: n.classList.contains('wam-dim') });
  // Views at the bottom: the bottom edge stays put (it is under the thumb), so a row opening grows upward.
  let sc = g.viewsAt === 'bottom' ? el.parentElement : null;
  while (sc && !/(auto|scroll)/.test(getComputedStyle(sc).overflowY)) sc = sc.parentElement;
  const fromBottom = sc ? sc.scrollHeight - sc.scrollTop : 0;
  // Connections run only between layers. My Apps: the anchor's whole line up to the views and down to the
  // system. App Store ('down'): only what the anchor connects to below it.
  const layer = layersOf(g.rows);
  const E = !st.anchor
    ? []
    : g.mode === 'down'
      ? neighbours(g.links, st.anchor, layer).filter(([u]) => u === st.anchor)
      : lineage(g.links, st.anchor, layer).edges;
  const rel = new Set(E.flat().filter((id) => id !== st.anchor));
  if (g.mode === 'down' && st.anchor && st._openedFor !== st.anchor) {
    // picking something opens the rows its connections are in
    st._openedFor = st.anchor;
    for (const k of ROWS) if ((g.rows[k] || []).some((id) => rel.has(id) || id === st.anchor)) st.shut.delete(k);
  }
  const L = layout(g.rows, g.links, st.anchor, {
    cols,
    anchorCol: st.anchorCol,
    open: st.open,
    pinned: g.pinned,
    related: rel,
    wrap: g.wrap,
    shut: st.shut,
  });
  const order = g.viewsAt === 'top' ? ROWS : [...ROWS].reverse();
  const place = (c) => ({ col: String(c.col + 1), row: String(c.line + 1), css: `grid-column:${c.col + 1};grid-row:${c.line + 1}` });
  const row = (k) => {
    const r = L.rows[k];
    const fold = g.collapsible?.has(k);
    const head = `<h4${fold ? ` data-wam-row="${k}" class="wam-fold-h${r.shut ? ' shut' : ''}"` : ''}><b>${g.label?.(k) || k}</b><span>${g.note(k, L)}${fold ? '<i class="wam-chev"></i>' : ''}</span></h4>`;
    if (r.shut) return `<div class="lgroup wam-row shut" data-row="${k}">${head}</div>`;
    // Nothing in this layer yet: just its title line, so lines that pass it run by a title, never an empty box.
    if (!r.cells.length && !r.more)
      return `<div class="lgroup wam-row empty" data-row="${k}"><h4><b>${g.label?.(k) || k}</b><span><em class="wam-none-k">None yet</em></span></h4></div>`;
    const cells = r.cells.map((c) => g.html(c.id, `wam-${c.kind}`, place(c))).join('');
    const m = r.more;
    const more = m
      ? `<button class="lapp wam-fold${m.open ? ' open' : ''}" data-wam-more="${k}" data-wam-key="more:${k}" style="${place(m).css}"><span class="ic">${m.open ? '−' : `+${m.ids.length}`}</span><span class="nm">${m.open ? 'Less' : 'More'}</span></button>`
      : '';
    const empty = r.cells.length || m ? '' : '<span class="wam-none">None yet</span>';
    return `<div class="lgroup wam-row" data-row="${k}">${head}<div class="lgrid wam-line" style="--cols:${cols}">${cells}${more}${empty}</div></div>`;
  };
  // The Agent Service sits between the views and the agents: every view reaches its agent through it.
  const svc = g.service && !L.rows.Views.shut && !L.rows.Agents.shut
    ? `<div class="wam-svc" title="Agent Service: connects your views to your agents. A swappable spot."><span>Agent Service</span><i></i></div>`
    : '';
  const parts = order.map((k) => row(k));
  if (svc) parts.splice(order.indexOf('Views') + (g.viewsAt === 'top' ? 1 : 0), 0, svc);
  el.innerHTML = `<div class="wam-graph${before.size ? ' settled' : ''}" data-views="${g.viewsAt}"><svg class="wam-wires" aria-hidden="true"></svg>${parts.join('')}</div>`;
  if (sc) sc.scrollTop = before.size ? sc.scrollHeight - fromBottom : sc.scrollHeight;
  wires(el, L, E);
  clearTimeout(el._wamT);
  el._wamT = setTimeout(() => el.isConnected && el._wamL === L && wires(el, L, E), 560);
  if (before.size && !still()) flip(el, before);
  el._wamL = L;
  return L;
}

/** An orthogonal path through points, with small round corners. */
function elbow(pts, r = 7) {
  const p = pts.filter((q, n) => !n || Math.abs(q[0] - pts[n - 1][0]) + Math.abs(q[1] - pts[n - 1][1]) > 0.5);
  let d = `M${p[0][0]} ${p[0][1]}`;
  for (let n = 1; n < p.length; n++) {
    const [x, y] = p[n];
    const next = p[n + 1];
    if (!next) {
      d += `L${x} ${y}`;
      break;
    }
    const [px, py] = p[n - 1];
    const k = Math.min(r, Math.hypot(x - px, y - py) / 2, Math.hypot(next[0] - x, next[1] - y) / 2);
    const ux = Math.sign(x - px);
    const uy = Math.sign(y - py);
    const vx = Math.sign(next[0] - x);
    const vy = Math.sign(next[1] - y);
    d += `L${x - ux * k} ${y - uy * k}Q${x} ${y} ${x + vx * k} ${y + vy * k}`;
  }
  return d;
}

/**
 * Lines between layers only, never along a row: a row's items tie in through the rows above and below it.
 * E is a list of [a, b] edges. Each line leaves the upper tile under its name, runs along the empty band under
 * that row, and drops into the top of the lower tile; a line that skips a row goes down a column gap, so it never
 * crosses a tile. Anything touching the Views row passes through the Agent Service node between Views and Agents.
 * Row titles mask the line where it passes behind them, so the text always stays clean.
 */
const SVC = '__svc';
function wires(el, L, E) {
  const box = el.querySelector('.wam-graph');
  const svg = box?.querySelector('.wam-wires');
  if (!svg) return;
  el._wamE = E;
  const W = box.scrollWidth;
  const H = box.scrollHeight;
  svg.setAttribute('width', W);
  svg.setAttribute('height', H);
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  if (!E.length) {
    svg.innerHTML = '';
    return;
  }
  const o = box.getBoundingClientRect();
  const R = (n) => {
    const r = n.getBoundingClientRect();
    return { l: r.left - o.left, r: r.right - o.left, t: r.top - o.top, b: r.bottom - o.top, x: Math.round((r.left + r.right) / 2 - o.left) };
  };
  const parts = [...box.children].filter((n) => n !== svg);
  const svc = box.querySelector(':scope > .wam-svc');
  const pill = svc && R(svc.querySelector('i'));
  const node = (id) => {
    if (id === SVC) return { key: SVC, vi: parts.indexOf(svc), x: pill.x, top: pill.t, bottom: pill.b, bt: pill.t, first: true, last: true, row: SVC };
    const t = box.querySelector(`[data-wam-key="${CSS.escape(id)}"]`);
    const row = t?.closest('.wam-row');
    if (!row) return null;
    const ic = R(t.querySelector('.ic'));
    const tb = R(t);
    // leave from just under the tile's last line of text, so the drop into the band is always visible
    const txt = [...t.querySelectorAll('.nm, .wam-spot-k')].map(R).filter((r) => r.b > r.t);
    const textBottom = txt.length ? Math.max(...txt.map((r) => r.b)) : tb.b;
    // a row that wraps: tiles not on its first/last line leave and enter through the gaps around them
    const lr = L.rows[row.dataset.row];
    const cell = lr?.cells.find((c) => c.id === id);
    const maxLine = Math.max(0, ...(lr?.cells || []).map((c) => c.line), lr?.more ? lr.more.line : 0);
    return { key: id, vi: parts.indexOf(row), x: ic.x, top: ic.t, bottom: textBottom, bt: tb.t, first: !cell || cell.line === 0, last: !cell || cell.line === maxLine, row: row.dataset.row };
  };
  const segs = new Map();
  const add = (p, q) => {
    if (!p || !q || p.vi === q.vi) return;
    const [u, d] = p.vi < q.vi ? [p, q] : [q, p];
    segs.set(`${u.key}|${d.key}`, [u, d]);
  };
  for (const [x, y] of E) {
    const a = node(x);
    const b = node(y);
    if (!a || !b) continue;
    if (svc && (a.row === 'Views') !== (b.row === 'Views')) {
      const s = node(SVC);
      add(a, s);
      add(s, b);
    } else add(a, b);
  }
  // free band under part i (between its bottom and the top of part i+1)
  const lo = (n) => (n === svc ? pill.b : R(n.querySelector('.lgrid') || n.querySelector('h4')).b);
  const hi = (n) => (n === svc ? pill.t : R(n.querySelector('h4')).t);
  const band = (i) => ({ y: (lo(parts[i]) + hi(parts[i + 1])) / 2, half: Math.max(2, (hi(parts[i + 1]) - lo(parts[i])) / 2 - 3) });
  const lanes = new Map(); // band -> source keys, so lines from different tiles run side by side, not on top
  const laneOf = (i, key) => {
    if (!lanes.has(i)) lanes.set(i, []);
    const l = lanes.get(i);
    if (!l.includes(key)) l.push(key);
  };
  const deg = new Map();
  for (const [u, d] of segs.values()) for (const k of [u.key, d.key]) deg.set(k, (deg.get(k) || 0) + 1);
  const hub = (u, d) => ((deg.get(u.key) || 0) >= (deg.get(d.key) || 0) ? u.key : d.key);
  for (const [u, d] of segs.values()) {
    laneOf(u.vi, hub(u, d));
    if (d.vi - u.vi > 1) laneOf(d.vi - 1, hub(u, d));
  }
  const yOf = (i, key) => {
    const b = band(i);
    const l = lanes.get(i);
    const step = Math.min(5, (2 * b.half) / Math.max(1, l.length));
    return Math.round(b.y + (l.indexOf(key) - (l.length - 1) / 2) * step);
  };
  // column gaps (all rows share the same columns) for lines that skip a row
  const g = box.querySelector('.lgrid');
  let gaps = [];
  if (g) {
    const gr = R(g);
    const cs = getComputedStyle(g);
    const n = Math.max(2, cs.gridTemplateColumns.split(' ').length);
    const gap = parseFloat(cs.columnGap) || 0;
    const w = (gr.r - gr.l - gap * (n - 1)) / n;
    gaps = Array.from({ length: n - 1 }, (_, k) => Math.round(gr.l + (k + 1) * (w + gap) - gap / 2));
  }
  const texts = [...box.querySelectorAll('.wam-row h4 > *, .wam-svc > span')].map(R).filter((r) => r.r - r.l > 1);
  const hits = (x, y0, y1) => texts.some((r) => x > r.l - 5 && x < r.r + 5 && Math.max(y0, y1) > r.t - 3 && Math.min(y0, y1) < r.b + 3);
  const near = (x, y0, y1) => {
    const ok = y0 == null ? gaps : gaps.filter((g) => !hits(g, y0, y1));
    const pool = ok.length ? ok : gaps;
    return pool.length ? pool.reduce((p, q) => (Math.abs(q - x) < Math.abs(p - x) ? q : p)) : x;
  };
  const d = [];
  for (const [u, dn] of segs.values()) {
    const ex = dn.x;
    const pts = [[u.x, Math.round(u.bottom + 2)]];
    let x = u.x;
    if (!u.last && gaps.length) {
      const y = Math.round(u.bottom + 6);
      x = near(u.x);
      pts.push([u.x, y], [x, y]);
    }
    let y = yOf(u.vi, hub(u, dn));
    pts.push([x, y]);
    if (dn.vi - u.vi > 1 && gaps.length) {
      const used = Math.max(0, ...parts.slice(u.vi + 1, dn.vi).map((n) => n.querySelectorAll?.('.lgrid > .lapp').length || 0));
      const clear = gaps.filter((_, i) => i >= Math.min(used, gaps.length - 1));
      x = clear.length ? clear.reduce((p, q) => (Math.abs(q - ex) < Math.abs(p - ex) ? q : p)) : near(ex, y, yOf(dn.vi - 1, hub(u, dn)));
      pts.push([x, y]);
      y = yOf(dn.vi - 1, hub(u, dn));
      pts.push([x, y]);
    }
    const yy = Math.round(dn.bt - 5);
    if ((!dn.first || hits(ex, y, dn.top)) && gaps.length) {
      // under a row title (or inside a wrapped row): come down a column gap, then in just above the tile
      const gx = near(ex, y, yy);
      pts.push([gx, y], [gx, yy], [ex, yy]);
    } else pts.push([ex, y]);
    pts.push([ex, Math.round(dn.top - 3)]);
    d.push(elbow(pts));
  }
  const id = `wam-m-${++maskN}`;
  const holes = texts
    .map((r) => `<rect x="${r.l - 5}" y="${r.t - 3}" width="${r.r - r.l + 10}" height="${r.b - r.t + 6}" rx="4" fill="#000"/>`)
    .join('');
  // One stroke: where lines share a run they draw over each other exactly, with no brighter seams.
  svg.innerHTML = `<defs><mask id="${id}" maskUnits="userSpaceOnUse" x="0" y="0" width="${W}" height="${H}"><rect width="${W}" height="${H}" fill="#fff"/>${holes}</mask></defs><path class="wam-wire" mask="url(#${id})" d="${d.join('')}"/>`;
}

/** Tiles glide from where they were to where they are now; dimming fades. */
function flip(el, before) {
  const moved = [];
  for (const n of el.querySelectorAll('[data-wam-key]')) {
    const b = before.get(n.dataset.wamKey);
    if (!b) {
      n.classList.add('wam-new');
      continue;
    }
    const r = n.getBoundingClientRect();
    const dx = b.r.left - r.left;
    const dy = b.r.top - r.top;
    const fade = b.dim !== n.classList.contains('wam-dim');
    if (Math.abs(dx) + Math.abs(dy) < 1 && !fade) continue;
    n.style.transition = 'none';
    if (Math.abs(dx) + Math.abs(dy) >= 1) n.style.transform = `translate(${dx}px, ${dy}px)`;
    if (fade) n.classList.add(b.dim ? 'wam-was-dim' : 'wam-was-lit');
    moved.push(n);
  }
  if (!moved.length) return;
  el.getBoundingClientRect();
  requestAnimationFrame(() => {
    for (const n of moved) {
      n.style.transition = 'transform 380ms var(--ease)';
      n.style.transform = '';
      n.classList.remove('wam-was-dim', 'wam-was-lit');
    }
  });
}

/** Repaint when the width changes the number of columns (the sheet opening, a window resize). */
function watchWidth(el, repaint) {
  el._wamRepaint = repaint;
  if (el._wamRO || typeof ResizeObserver === 'undefined') return;
  let cols = colsFor(el);
  let w = el.clientWidth;
  el._wamRO = new ResizeObserver(() => {
    if (!el.querySelector(':scope > .wam-graph')) return; // showing something else now (a card, the store)
    const n = colsFor(el);
    if (n !== cols) {
      cols = n;
      el._wamRepaint?.();
    } else if (el.clientWidth !== w && el._wamL) {
      wires(el, el._wamL, el._wamE || []);
    }
    w = el.clientWidth;
  });
  el._wamRO.observe(el);
}

/** Tap on a graph: +N folds/unfolds, a tile becomes the anchor, the anchor opens. Returns true if handled. */
function graphTap(e, el, st, open, repaint) {
  const more = e.target.closest('[data-wam-more]');
  if (more) {
    e.stopPropagation();
    const k = more.dataset.wamMore;
    st.open.has(k) ? st.open.delete(k) : st.open.add(k);
    repaint();
    return true;
  }
  const t = e.target.closest('[data-wam-id]');
  if (!t) return false;
  e.stopPropagation();
  const id = t.dataset.wamId;
  if (id === st.anchor) {
    open(id);
    return true;
  }
  const cell = el._wamL ? Object.values(el._wamL.rows).flatMap((r) => r.cells).find((c) => c.id === id) : null;
  st.anchor = id;
  st.anchorCol = cell && cell.line === 0 ? cell.col : undefined;
  st.open.clear();
  repaint();
  return true;
}

const slotName = (id) => {
  const s = cache.slots.get(id);
  return s ? String(s.name || s.slot).replace(/\s*\(.*\)$/, '') : '';
};

// ---- My Apps -----------------------------------------------------------------------------------------------------
const GETS = { Views: 'Get views', Agents: 'Get agents', Apps: 'Get apps', System: 'Get system apps' };
// Spots shown elsewhere: the shell is the screen itself, the Agent Service is the dot between Views and Agents,
// and the agent spot is the Agents row.
const OWN_SPOT = new Set(['shell', 'agent-service', 'agent']);
const isNode = (id) => !/^(get|spot):/.test(id);

const moreTile = (k, p) =>
  `<button class="lapp wam-addnext" style="${p.css}" data-wam-get="${k}" data-wam-key="get:${k}" aria-label="${GETS[k]}" title="${GETS[k]}"><span class="ic">+</span><span class="nm">&nbsp;</span></button>`;
const spotTile = (s, cls, p) =>
  `<button class="lapp wam-espot ${cls}" style="${p.css}" data-wam-get="System" data-wam-key="spot:${esc(s.slot)}" title="${esc(`${s.name} spot is empty. Get an app to fill it.`)}"><span class="ic">+</span><span class="nm">${esc(String(s.name).replace(/\s*\(.*\)$/, ''))}</span><span class="wam-spot-k">Empty spot</span></button>`;

/**
 * Until permissions exist, every agent can reach every app and system app, and every view talks to the agent.
 * Permissions will take connections away; the graph will show only what is allowed.
 */
function connectAgents(links, rows, agent, views = []) {
  const join = (x, y) => {
    for (const k of [x, y]) if (!links.has(k)) links.set(k, new Set());
    links.get(x).add(y);
    links.get(y).add(x);
  };
  for (const ag of rows.Agents.filter(isNode)) for (const id of [...rows.Apps, ...rows.System].filter(isNode)) join(ag, id);
  if (agent) for (const v of views) join(v, agent);
  return links;
}

const readShut = () => {
  try {
    return new Set(JSON.parse(localStorage.getItem('wam.shut') || '[]'));
  } catch {
    return new Set();
  }
};
const saveShut = (s) => {
  try {
    localStorage.setItem('wam.shut', JSON.stringify([...s]));
  } catch {
    /* private mode */
  }
};

export async function renderMyApps(el, ctx = {}) {
  el.classList.add('wam');
  bindImages(el);
  const st = el._wamG || (el._wamG = { anchor: null, anchorCol: undefined, open: new Set(), shut: readShut() });
  // The shell's own tiles (Inbox, Chats, the reviews, the owner's links) are views like any other.
  const theirs = [...(ctx.shellTiles?.Views || []), ...(ctx.shellTiles?.Apps || []), ...(ctx.extraTiles || [])];

  const paint = (apps) => {
    if (!el.isConnected && el._wamPainted) return;
    el._wamPainted = true;
    const byId = new Map(apps.map((a) => [a.id, a]));
    const rows = Object.fromEntries(ROWS.map((k) => [k, []]));
    const shellIds = new Map();
    theirs.forEach((html, n) => {
      shellIds.set(`shell:${n}`, html);
      rows.Views.push(`shell:${n}`);
    });
    // System: one tile per parking spot, filled or empty, in the spots' order.
    const spots = new Map();
    for (const s of cache.parking) {
      if (OWN_SPOT.has(s.slot)) continue;
      const id = s.active && byId.has(s.active) ? s.active : `spot:${s.slot}`;
      spots.set(id, s);
      rows.System.push(id);
    }
    for (const a of apps) if (!spots.has(a.id)) rows[rowOf(a)].push(a.id);
    for (const k of ROWS) rows[k].push(`get:${k}`); // the "+" sits where the next app will go
    const agent = cache.parking.find((s) => s.slot === 'agent')?.active;
    const parked = new Map([...spots].map(([id, s]) => [s.slot, id]));
    if (agent) parked.set('agent', agent);
    const empty = [...spots].filter(([id]) => id.startsWith('spot:')).map(([id, s]) => ({ id, provides: [s.slot] }));
    const links = connectAgents(linksOf([...apps, ...empty], parked), rows, byId.has(agent) ? agent : rows.Agents[0], [...shellIds.keys()]);
    const all = new Set(ROWS.flatMap((k) => rows[k]));
    if (!st.anchor || !all.has(st.anchor) || !isNode(st.anchor)) {
      // Start from the agent: its line runs up to every view and down to everything it can use.
      st.anchor = (byId.has(agent) && agent) || rows.Agents.find(isNode) || rows.Views.find(isNode) || null;
      st.anchorCol = undefined;
    }
    el._wamShell = shellIds;
    drawGraph(el, {
      rows,
      links,
      state: st,
      pinned: new Set(ROWS.map((k) => `get:${k}`)),
      viewsAt: viewsAt(el, ctx),
      service: true,
      wrap: new Set(ROWS),
      collapsible: new Set(['System']),
      html: (id, cls, p) => {
        if (id.startsWith('get:')) return moreTile(id.slice(4), p);
        if (shellIds.has(id)) return shellTile(shellIds.get(id), cls, p, id, id === st.anchor);
        if (id.startsWith('spot:')) return spotTile(spots.get(id), cls, p);
        const a = byId.get(id);
        const spot = spots.get(id);
        return tile(a, cls, p.css, '', id === st.anchor ? pillFor(a, conf(ctx)) : '', spot ? String(spot.name).replace(/\s*\(.*\)$/, '') : '');
      },
      note: (k) => {
        if (k === 'System') {
          const n = [...spots.keys()].filter(isNode).length;
          return spots.size ? `${n} of ${spots.size} spots filled` : '';
        }
        const n = rows[k].filter(isNode).length;
        return n ? String(n) : '';
      },
    });
  };
  const repaint = () => cache.apps && paint(cache.apps);
  watchWidth(el, repaint);
  bindClicks(el, (e) => {
    const get = e.target.closest('[data-wam-get]');
    if (get) {
      e.stopPropagation();
      const focus = get.dataset.wamGet;
      return ctx.onGetApps ? ctx.onGetApps(focus) : renderStore(el, { ...ctx, focus });
    }
    const h = e.target.closest('[data-wam-row]');
    if (h) {
      e.stopPropagation();
      const k = h.dataset.wamRow;
      st.shut.has(k) ? st.shut.delete(k) : st.shut.add(k);
      saveShut(st.shut);
      return repaint();
    }
    const go = e.target.closest('[data-wam-act="open"]');
    if (go) {
      if (go.hasAttribute('data-app')) return; // the shell opens it (its own tiles, same-origin screens)
      e.stopPropagation();
      const a = (cache.apps || []).find((x) => x.id === st.anchor);
      return a?.open && window.open(a.open, '_blank', 'noopener,noreferrer'); // stock Runtipi Open, top-level
    }
    graphTap(e, el, st, (id) => {
      if (id.startsWith('shell:')) return launchShell(el, id, ctx);
      const a = (cache.apps || []).find((x) => x.id === id);
      if (a) ctx.onApp ? ctx.onApp(a, 'apps') : renderCard(el, a, { ...ctx, onBack: () => renderMyApps(el, ctx) });
    }, repaint);
  });
  if (cache.apps) paint(cache.apps);
  try {
    paint(await loadInstalled(ctx));
  } catch {
    if (!cache.apps) el.innerHTML = `<div class="lgroup"><h4>Apps</h4><div class="lgrid">${theirs.join('')}</div></div>${note('Can’t reach the App Manager')}`;
  }
}

/** Second tap on one of the shell's own tiles: hand it to the shell's launcher, exactly as its own tile would. */
function launchShell(el, id, ctx = {}) {
  const app = el.querySelector(`[data-wam-key="${CSS.escape(id)}"]`)?.dataset.wamApp;
  if (!app) return;
  if (ctx.onShellOpen) return ctx.onShellOpen(app);
  const b = document.createElement('button');
  b.hidden = true;
  b.dataset.app = app;
  el.append(b);
  b.click();
  b.remove();
}

// ---- App card ----------------------------------------------------------------------------------------------------
/** What an app works with (compatibility, from uses/needs/provides) and, apart, what it is allowed to use (later). */
function connections(a) {
  const mine = cache.apps || [];
  const pool = new Map([...(cache.index || new Map())]);
  for (const x of mine) pool.set(x.id, x);
  pool.set(a.id, a);
  const links = linksOf([...pool.values()], new Map([...cache.slots].map(([id, s]) => [s.slot, id])));
  const have = new Set(mine.map((x) => x.id));
  const list = [...(links.get(a.id) || [])].map((id) => pool.get(id)).filter(Boolean);
  const need = new Set(a.uses || []);
  list.sort((x, y) => Number(need.has(y.id)) - Number(need.has(x.id)) || x.name.localeCompare(y.name));
  const chip = (x) => {
    const on = have.has(x.id);
    const mark = on ? '<i class="ok">✓</i>' : need.has(x.id) ? '<i class="add">+</i>' : '';
    return `<span class="wam-mini${on ? '' : ' off'}"><span class="ic" style="${gstyle(x)}">${icon(x)}</span>${esc(x.name)}${mark}</span>`;
  };
  const works = list.length ? `<div class="wam-links"><h5>Works with</h5><div>${list.map(chip).join('')}</div></div>` : '';
  return `${works}<div class="wam-links wam-perm"><h5>Allowed to use</h5><div><span class="wam-slot">Not set yet</span></div></div>`;
}

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
    const slot = a.installed && slotName(a.id);
    el.innerHTML = `<div class="wam-card">
      <button class="wam-back" data-wam-back>‹ ${ctx.from === 'store' ? 'App Store' : 'My apps'}</button>
      <div class="wam-panel">
        <div class="st-row static"><span class="wam-ic" style="${gstyle(a)}">${icon(a)}</span><span class="st-tx"><b>${esc(a.name)}</b><small>${esc(a.summary)}</small></span></div>
        ${st ? `<div class="wam-meta"><i class="wam-dot ${esc(a.installed ? a.status : 'running')}"></i>${esc(st)}${slot ? ` · ${esc(slot)} spot` : ''}${!a.installed && a.installs > 0 ? ` · ${count(a.installs)} installs` : ''}</div>` : ''}
        ${acts.length ? `<div class="wam-acts">${acts.join('')}</div>` : ''}
        ${a.screen ? '' : note('No screen: ask your agent to use it')}
        ${msg ? note(msg) : ''}
      </div>
      ${connections(a)}
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
  // Get views / Get agents / ...: that section open, the others folded to their titles.
  const focus = ROWS.includes(ctx.focus) ? ctx.focus : '';
  const st = { anchor: null, anchorCol: undefined, open: new Set(), shut: new Set(focus ? ROWS.filter((k) => k !== focus) : []) };
  el._wamG = st;

  const paintChips = () => {
    const chip = (id, label) => `<button class="wam-chip${s.tag === id ? ' on' : ''}" data-wam-tag="${esc(id)}">${esc(label)}</button>`;
    // One small sort button (Popular → New → A–Z) instead of three, so the tags have the width.
    const n = SORTS.findIndex(([k]) => k === s.sort);
    const next = SORTS[(n + 1) % SORTS.length][0];
    chips.innerHTML = `<div class="wam-tags">${chip('', 'All')}${s.tags.map((t) => chip(t, t.charAt(0).toUpperCase() + t.slice(1))).join('')}</div><button class="wam-sort" data-wam-sort="${next}" title="Sort">${SORTS[n][1]}<i>⇅</i></button>`;
  };
  const paint = () => {
    const have = new Set((cache.apps || []).map((a) => a.id));
    const backLink = ctx.onBack ? '<button class="wam-back" data-wam-back>‹ My apps</button>' : '';
    const more = s.next != null ? '<button class="wam-more" data-wam-more-results>More</button>' : '';
    if (!s.items.length) {
      el.innerHTML = `${backLink}${note(s.loaded ? (s.q ? 'Nothing found' : 'No apps here yet') : 'Loading…')}`;
      return;
    }
    // The anchor's links come along even when the search or tag left them out.
    const shown = new Map(s.items.map((a) => [a.id, a]));
    const pool = new Map([...(cache.index || new Map()), ...shown]);
    const rows = Object.fromEntries(ROWS.map((k) => [k, []]));
    for (const a of pool.values()) rows[rowOf({ ...a, id: '' })].push(a.id);
    const links = connectAgents(linksOf([...pool.values()]), rows);
    if (st.anchor && !pool.has(st.anchor)) st.anchor = null;
    if (st.anchor) for (const id of links.get(st.anchor) || []) if (!shown.has(id)) shown.set(id, pool.get(id));
    for (const a of shown.values()) a.have = have.has(a.id);
    for (const k of ROWS) rows[k] = rows[k].filter((id) => shown.has(id));
    for (const a of shown.values()) if (!pool.has(a.id)) rows[rowOf({ ...a, id: '' })].push(a.id);
    const anchor = st.anchor ? shown.get(st.anchor) : null;
    const needs = new Set(anchor?.uses || []);
    const missing = new Set([...needs].filter((id) => !have.has(id)));
    let host = el.querySelector(':scope > .wam-graph-host');
    if (!host) {
      el.innerHTML = `${backLink}<div class="wam-graph-host"></div>`;
      host = el.querySelector(':scope > .wam-graph-host');
      watchWidth(host, paint);
    }
    el.querySelector(':scope > [data-wam-more-results]')?.remove();
    drawGraph(host, {
      rows,
      links,
      state: st,
      pinned: new Set(),
      viewsAt: viewsAt(el, ctx),
      mode: 'down', // the store shows what something connects to below it: what it will use
      service: true,
      collapsible: new Set(ROWS),
      wrap: new Set(focus ? [focus] : []),
      html: (id, cls, p) => {
        const a = shown.get(id);
        const mark = missing.has(id) ? '<i class="wam-add">+</i>' : a.have ? '<i class="wam-ok">✓</i>' : '';
        return tile(a, cls, p.css, mark, id === st.anchor ? 'data-wam-act="details">Details' : '');
      },
      note: (k) => {
        if (!anchor || rowOf({ ...anchor, id: '' }) !== k) return String(rows[k].length || '');
        if (anchor.have || !needs.size) return '';
        return missing.size ? `<em class="add">Adds ${missing.size}</em>` : '<em class="ok">Ready</em>';
      },
    });
    host._wamG = st;
    if (more) host.insertAdjacentHTML('afterend', more);
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
      s.loaded = true;
      if (reset) {
        st.open.clear();
        if (st.anchor && !got.some((a) => a.id === st.anchor)) st.anchor = null;
      }
      paint();
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
  const back = () => {
    bindClicks(el, onClick);
    paint();
  };
  const onClick = (e) => {
    if (e.target.closest('[data-wam-back]')) {
      e.stopPropagation();
      return ctx.onBack();
    }
    if (e.target.closest('[data-wam-more-results]')) {
      e.stopPropagation();
      return load(false);
    }
    const host = el.querySelector('.wam-graph-host');
    if (!host) return;
    const h = e.target.closest('[data-wam-row]');
    if (h) {
      e.stopPropagation();
      const k = h.dataset.wamRow;
      st.shut.has(k) ? st.shut.delete(k) : st.shut.add(k);
      return paint();
    }
    graphTap(e, host, st, (id) => {
      const a = s.items.find((x) => x.id === id) || cache.index?.get(id);
      const mine = (cache.apps || []).find((x) => x.id === id);
      if (a) ctx.onApp ? ctx.onApp(mine || a, 'store') : renderCard(el, mine || a, { ...ctx, from: 'store', onBack: back });
    }, paint);
  };
  bindClicks(el, onClick);

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
  Promise.all([loadInstalled(ctx), indexMeta(c)]).then(paint).catch(noop);
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
// One reading of /api/system/load feeds both the Settings group and the home card; the processor keeps a short history.
// The processor history is [time, %] pairs kept for 10 minutes, so a reload still has a line to draw.
const HISTORY = 10 * 60e3;
const load = { last: null, at: 0, cpu: [], waiting: null };
try {
  load.cpu = JSON.parse(localStorage.getItem('wam.cpu') || '[]').filter((s) => Date.now() - s[0] < HISTORY);
} catch {
  load.cpu = [];
}

async function readLoad(c, maxAge = 8e3) {
  if (load.last && Date.now() - load.at < maxAge) return load.last;
  if (!load.waiting)
    load.waiting = req(`${c.api}/system/load`)
      .then((d) => {
        load.last = d;
        load.at = Date.now();
        load.cpu = [...load.cpu, [load.at, Math.max(0, Math.min(100, Number(d.cpuLoad) || 0))]].filter((s) => load.at - s[0] < HISTORY).slice(-40);
        try {
          localStorage.setItem('wam.cpu', JSON.stringify(load.cpu));
        } catch {
          /* private mode */
        }
        return d;
      })
      .finally(() => {
        load.waiting = null;
      });
  return load.waiting;
}

const pct = (v) => Math.max(0, Math.min(100, Math.round(Number(v) || 0)));
const HOT = 80; // from here a meter turns amber
// One quiet colour for all three: the accent colours above (Unread, Working, Yesterday) mean something else.
const QUIET = 'var(--ink-2)';
const METRICS = [
  { k: 'Storage', c: QUIET, ic: '◫', v: (d) => d.percentUsed, sub: (d) => `${d.diskUsed} of ${d.diskSize} GB used` },
  { k: 'Processor', c: QUIET, ic: '◔', v: (d) => d.cpuLoad, sub: () => 'Uninstall apps to reduce load', spark: true },
  {
    k: 'Memory',
    c: QUIET,
    ic: '▦',
    v: (d) => d.percentUsedMemory,
    sub: (d) => {
      const used = (Number(d.memoryTotal) * Number(d.percentUsedMemory)) / 100;
      return `${used.toFixed(used < 10 ? 1 : 0)} of ${d.memoryTotal} GB used`;
    },
  },
];

/** The processor's last minutes as a small line beside its number, scaled to its own range so changes show. */
function spark(m) {
  if (!m.spark || load.cpu.length < 3) return '';
  const t0 = load.cpu[0][0];
  const span = Math.max(1, load.cpu[load.cpu.length - 1][0] - t0);
  const vals = load.cpu.map((s) => s[1]);
  const mid = (Math.max(...vals) + Math.min(...vals)) / 2;
  const half = Math.max(8, (Math.max(...vals) - Math.min(...vals)) / 2 + 2);
  const y = (v) => (10 - ((v - mid) / half) * 8).toFixed(1);
  const line = load.cpu.map(([t, v], n) => `${n ? 'L' : 'M'}${(((t - t0) / span) * 100).toFixed(1)} ${y(v)}`).join('');
  const id = `wam-sg-${++maskN}`;
  return `<svg class="wam-spark" viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--c)" stop-opacity=".3"/><stop offset="1" style="stop-color:var(--c)" stop-opacity="0"/></linearGradient></defs><path class="fill" fill="url(#${id})" d="${line}L100 20L0 20Z"/><path d="${line}"/></svg>`;
}

/** Number, a thin bar of it, and for the processor its recent line. Amber from 80%. */
function meter(m, d, calm = false) {
  const p = pct(m.v(d));
  const hot = !calm && p >= HOT; // the home card stays one colour; Settings warns
  return `<span class="wam-meter${hot ? ' hot' : ''}" style="--c:${hot ? 'var(--amber)' : m.c}"><span class="wam-mv"><em>${p}<small>%</small></em>${spark(m)}</span><span class="wam-bar"><i style="width:${p}%"></i></span></span>`;
}

/** Repaint every ~10 s while el is on screen and the page is visible. */
function poll(el, c, paint) {
  clearInterval(el._wamPoll);
  const tick = () => {
    if (!el.isConnected) return clearInterval(el._wamPoll);
    if (document.hidden || !el.offsetParent) return;
    readLoad(c).then(paint).catch(noop);
  };
  el._wamPoll = setInterval(tick, 10e3);
  if (!el._wamVis) {
    el._wamVis = () => !document.hidden && el.isConnected && tick();
    document.addEventListener('visibilitychange', el._wamVis);
  }
}

export async function renderResources(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  let armed = '';
  const row = (m, d) =>
    `<div class="st-row static wam-res-row"><span class="st-ic" style="--c:${m.c}">${m.ic}</span><span class="st-tx"><b>${m.k}</b><small>${esc(m.sub(d))}</small></span>${meter(m, d)}</div>`;
  const acts = () =>
    `<div class="wam-acts wam-all">${[
      ['start-all', 'Start all'],
      ['stop-all', 'Stop all'],
      ['restart-all', 'Restart all'],
    ]
      .map(([k, l]) => `<button class="wam-btn" data-wam-all="${k}">${armed === k ? 'Tap again' : l}</button>`)
      .join('')}</div>`;
  const paint = (d) => {
    if (el.querySelector('[data-wam-all][disabled]')) return; // a button is mid-action: leave it be
    const msg = el.querySelector('[data-wam-msg]')?.textContent || '';
    el.innerHTML = `<div class="st-group"><h4>Resources</h4>${METRICS.map((m) => row(m, d)).join('')}${acts()}<p class="st-note wam-note" data-wam-msg>${esc(msg)}</p></div>`;
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
  if (load.last) paint(load.last);
  poll(el, c, paint);
  try {
    paint(await readLoad(c, 0));
  } catch {
    if (!load.last) el.innerHTML = `<div class="st-group"><h4>Resources</h4>${note('Can’t reach the App Manager')}</div>`;
  }
}

/** Home card: Storage, Processor, Memory side by side, in the shell's home-card style. */
export async function renderResourceCard(el, ctx = {}) {
  const c = conf(ctx);
  el.classList.add('wam');
  const cell = (m, d) => `<span class="wam-g" style="--c:${m.c}"><span class="k">${m.k}</span>${d ? meter(m, d, true) : '<span class="wam-meter"><em>–</em><span class="wam-bar"></span></span>'}</span>`;
  const paint = (d) => {
    el.innerHTML = `<button class="now-card wam-res-card" data-wam-res aria-label="Resources">${METRICS.map((m) => cell(m, d)).join('')}</button>`;
  };
  bindClicks(el, (e) => {
    if (!e.target.closest('[data-wam-res]')) return;
    e.stopPropagation();
    ctx.onOpen?.();
  });
  paint(load.last);
  poll(el, c, paint);
  try {
    paint(await readLoad(c));
  } catch {
    /* the card stays with dashes */
  }
}
