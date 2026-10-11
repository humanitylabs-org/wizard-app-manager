// Box-local overlay for Wizard UX (wizard-os spec/SHELL.md layer 4). The front injects it into the shell's entry
// documents; it changes nothing in James's code, only what his launcher, home and Settings show:
//   phone   - swipe-up launcher: his "Views" and "Wizard apps" groups become the My Apps graph (his own tiles stay in
//             it, views at the bottom near the thumb); "Get apps" turns the same sheet into the App Store, with one
//             chip row above his search bar, and the search bar searches the index
//           - home: a Resources card under his cards; tapping it opens Settings at Resources
//           - Settings sheet: a Resources group
//   desktop - the ⌘K palette shows the My Apps graph (views at the top, near the input) while the query is empty;
//             "Get apps" turns it into the App Store and the input searches the index
//           - Settings card: a Resources group
// His code rewrites these containers' innerHTML freely, so each piece is put back by a MutationObserver.
import { renderCard, renderMyApps, renderResourceCard, renderResources, renderStore } from './wizard-apps.js';

const $ = (s) => document.querySelector(s);
const groupTitle = (g) => g.querySelector('h4')?.textContent.trim() || '';
// His tiles that aren't App Manager apps (Chats, Inbox, his own links) stay; the remote duplicates go.
const keep = (group, ids) => (group ? [...group.querySelectorAll('.lapp[data-app]')].filter((b) => !ids.has(b.dataset.app)).map((b) => b.outerHTML) : []);
const remoteIds = () =>
  fetch('/wizard/v1/apps', { credentials: 'same-origin', cache: 'no-store' })
    .then((r) => (r.ok ? r.json() : { apps: [] }))
    .catch(() => ({ apps: [] }))
    .then((d) => new Set((d.apps || []).map((a) => a.id)));

// ---- phone: launcher -----------------------------------------------------------------------------------------------
const body = $('#launcherBody');
const sheet = $('#launcher');
const form = $('#searchForm');
const input = $('#search');

if (body && sheet && form && input) {
  const placeholder = input.placeholder;
  const chips = document.createElement('div');
  chips.hidden = true;
  form.before(chips);
  let mode = 'apps'; // apps | store | card
  let store = null; // { host, api }
  let card = null; // host element

  const toBottom = () => {
    body.scrollTop = body.scrollHeight;
  };

  function injectMyApps() {
    if (body.querySelector(':scope > .wam-host')) return;
    const groups = [...body.querySelectorAll(':scope > .lgroup')];
    const views = groups.find((g) => /^views/i.test(groupTitle(g)));
    const wiz = groups.find((g) => /^wizard apps/i.test(groupTitle(g)));
    if (!wiz) return;
    // Recent goes: the graph already shows everything, and it doubled Chats and Inbox.
    groups.find((g) => /^recent/i.test(groupTitle(g)))?.remove();
    const host = document.createElement('div');
    host.className = 'wam-host';
    const theirViews = views?.cloneNode(true);
    const theirs = wiz.cloneNode(true);
    (views || wiz).replaceWith(host);
    if (views) wiz.remove();
    const ctx = {
      shell: true,
      viewsAt: 'bottom',
      onGetApps: openStore,
      onApp: (a) => openCard(a, 'apps'),
    };
    remoteIds()
      .then((ids) => {
        ctx.shellTiles = { Views: keep(theirViews, ids), Apps: keep(theirs, ids) };
        return renderMyApps(host, ctx);
      })
      .then(() => host.isConnected && toBottom());
  }

  function setStoreUi(on) {
    chips.hidden = !on;
    input.placeholder = on ? 'Search the App Store' : placeholder;
  }

  function openStore(focus) {
    mode = 'store';
    input.value = '';
    const host = document.createElement('div');
    host.className = 'wam-host wam-store';
    setStoreUi(true);
    body.replaceChildren(host);
    const api = renderStore(host, { shell: true, viewsAt: 'bottom', focus, chipsEl: chips, onBack: backToApps, onApp: (a) => openCard(a, 'store') });
    store = { host, api };
    body.scrollTop = 0;
  }

  function openCard(a, from) {
    mode = 'card';
    card = document.createElement('div');
    card.className = 'wam-host';
    renderCard(card, a, { shell: true, from, onBack: from === 'store' ? backToStore : backToApps });
    setStoreUi(false);
    body.replaceChildren(card);
    body.scrollTop = 0;
  }

  function backToStore() {
    if (!store) return backToApps();
    mode = 'store';
    setStoreUi(true);
    body.replaceChildren(store.host);
    store.api.repaint();
  }

  function backToApps() {
    mode = 'apps';
    store = null;
    card = null;
    setStoreUi(false);
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true })); // his launcher repaints; the observer adds My Apps
  }

  // His launcher repaints on open, on typing and on live updates: keep our part in place.
  new MutationObserver(() => {
    if (mode === 'store' && store && !store.host.isConnected) body.replaceChildren(store.host);
    else if (mode === 'card' && card && !card.isConnected) body.replaceChildren(card);
    else if (mode === 'apps' && !input.value.trim()) injectMyApps();
  }).observe(body, { childList: true });

  // Closing the sheet leaves the store/card: next swipe-up is My Apps again.
  new MutationObserver(() => {
    if (!sheet.classList.contains('open') && mode !== 'apps') {
      mode = 'apps';
      store = null;
      card = null;
      setStoreUi(false);
      input.value = '';
    }
  }).observe(sheet, { attributes: true, attributeFilter: ['class'] });

  // In the store, the search bar searches the App Index instead of running a command.
  document.addEventListener(
    'input',
    (e) => {
      if (e.target !== input || mode === 'apps') return;
      e.stopImmediatePropagation();
      if (mode === 'card') backToStore();
      store?.api.setQuery(input.value);
    },
    true,
  );
  document.addEventListener(
    'submit',
    (e) => {
      if (e.target !== form || mode === 'apps') return;
      e.preventDefault();
      e.stopImmediatePropagation();
      store?.api.setQuery(input.value);
      input.blur();
    },
    true,
  );

  injectMyApps();
}

// ---- phone: home card ----------------------------------------------------------------------------------------------
// One card element for the page's life: his home re-renders often, so it is moved back in, not rebuilt.
const track = $('#track');
if (track) {
  const card = document.createElement('div');
  card.className = 'wam-res-host';
  let started = false;
  const openResources = () => {
    window.__wos?.openSettings?.();
    requestAnimationFrame(() => $('#settingsBody .wam-res')?.scrollIntoView({ block: 'start', behavior: 'smooth' }));
  };
  const place = () => {
    const cards = track.querySelector('.home-desk .home > .constellation');
    if (!cards || cards.nextElementSibling === card) return;
    cards.after(card);
    if (!started) {
      started = true;
      renderResourceCard(card, { onOpen: openResources });
    }
  };
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => {
      queued = false;
      place();
    });
  }).observe(track, { childList: true, subtree: true });
  place();
}

// ---- Settings: Resources (phone sheet and desktop card) ------------------------------------------------------------
function resourcesIn(root, bodyOf, wide) {
  const add = () => {
    const into = bodyOf();
    if (!into || !into.children.length || into.querySelector(':scope > .wam-res')) return;
    const host = document.createElement('div');
    host.className = wide ? 'wam-res wux' : 'wam-res';
    const wiz = [...into.querySelectorAll(':scope > .st-group')].find((g) => /^wizard$/i.test(groupTitle(g)));
    if (wiz) wiz.after(host);
    else into.append(host);
    renderResources(host, {});
  };
  new MutationObserver(add).observe(root, { childList: true, subtree: root !== bodyOf() });
  add();
}
const settings = $('#settingsBody');
if (settings) resourcesIn(settings, () => settings, false);
const dsettings = $('#dsettings');
if (dsettings) resourcesIn(dsettings, () => dsettings.querySelector('.set-body'), true);

// ---- desktop: ⌘K palette -------------------------------------------------------------------------------------------
const pal = $('#palette');
const palIn = $('#palInput');
const palList = $('#palList');
if (pal && palIn && palList && !body) {
  // His desktop stylesheet has no launcher tiles; the views' copy of them is scoped to .wux.
  if (!document.querySelector('link[href$="/wizard-views/wux.css"]'))
    document.head.insertAdjacentHTML('beforeend', '<link rel="stylesheet" href="/wizard-views/wux.css">');
  const palCard = pal.querySelector('.pal-card') || pal;
  const placeholder = palIn.placeholder;
  const wrap = document.createElement('div');
  wrap.className = 'wux wam wam-pal';
  const chips = document.createElement('div');
  chips.hidden = true;
  const host = document.createElement('div');
  host.className = 'wam-host';
  wrap.append(chips, host);
  palList.before(wrap);
  let mode = 'apps'; // apps | store | card
  let store = null;

  // His own views (Chats, Inbox...) as tiles, the way his phone launcher draws them; the box's apps come from us.
  const desk = window.WizCore?.apps?.('desktop');
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (ch) => `&#${ch.charCodeAt(0)};`);
  const deskViews = () =>
    (desk?.APPS || [])
      .filter((a) => a.kind !== 'wizard' && a.kind !== 'native')
      .map((a) => {
        const ic = window.WizSigil ? window.WizSigil.box('span', 'ic', a) : `<span class="ic" style="--g1:${a.g?.[0]};--g2:${a.g?.[1]}">${esc(a.icon || '◇')}</span>`;
        return `<button class="lapp" data-app="${esc(a.id)}">${ic}<span class="nm">${esc(a.name)}</span></button>`;
      });
  // Opening one runs his own palette entry for it, so it opens exactly as it always has.
  const deskOpen = (id) => {
    const a = desk?.APP?.[id];
    if (!a) return;
    palIn.value = '';
    palIn.dispatchEvent(new Event('input', { bubbles: true }));
    const it = [...palList.querySelectorAll('[data-i]')].find((b) => b.querySelector('.nm')?.firstChild?.textContent.trim() === a.name);
    it?.click();
  };

  const sync = () => {
    const graph = mode !== 'apps' || !palIn.value.trim();
    wrap.hidden = !graph;
    palList.hidden = mode !== 'apps';
    chips.hidden = mode !== 'store';
    palCard.classList.toggle('wam-pal-on', graph);
    palIn.placeholder = mode === 'store' ? 'Search the App Store' : placeholder;
  };
  const myApps = () => {
    mode = 'apps';
    store = null;
    palIn.value = '';
    host.className = 'wam-host';
    host.replaceChildren();
    delete host._wamG;
    sync();
    renderMyApps(host, {
      viewsAt: 'top',
      shellTiles: { Views: deskViews() },
      onShellOpen: deskOpen,
      onGetApps: openStore,
      onApp: (a) => openCard(a, 'apps'),
    });
  };
  function openStore(focus) {
    mode = 'store';
    palIn.value = '';
    host.className = 'wam-host wam-store';
    host.replaceChildren();
    sync();
    store = renderStore(host, { viewsAt: 'top', focus, chipsEl: chips, onBack: myApps, onApp: (a) => openCard(a, 'store') });
    palIn.focus();
  }
  function openCard(a, from) {
    const back = from === 'store' && store ? backToStore : myApps;
    mode = 'card';
    const cardEl = document.createElement('div');
    cardEl.className = 'wam-host';
    host.replaceWith(cardEl);
    sync();
    renderCard(cardEl, a, {
      from,
      onBack: () => {
        cardEl.replaceWith(host);
        back();
      },
    });
  }
  function backToStore() {
    mode = 'store';
    sync();
    store.repaint();
  }

  // Opening the palette starts at My Apps.
  new MutationObserver(() => {
    if (!pal.hidden) myApps();
  }).observe(pal, { attributes: true, attributeFilter: ['hidden'] });
  palIn.addEventListener('input', () => mode === 'apps' && sync());
  document.addEventListener(
    'input',
    (e) => {
      if (e.target !== palIn || mode === 'apps') return;
      e.stopImmediatePropagation();
      store?.setQuery(palIn.value);
    },
    true,
  );
  document.addEventListener(
    'keydown',
    (e) => {
      if (pal.hidden || mode === 'apps') return;
      if (e.key === 'Escape') {
        e.preventDefault();
        e.stopImmediatePropagation();
        const back = wrap.querySelector('[data-wam-back]') || pal.querySelector('.wam-card [data-wam-back]');
        return back ? back.click() : myApps();
      }
      if (e.target === palIn && ['Enter', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
        e.preventDefault();
        e.stopImmediatePropagation();
        if (e.key === 'Enter') store?.setQuery(palIn.value);
      }
    },
    true,
  );
  if (!pal.hidden) myApps();
}
