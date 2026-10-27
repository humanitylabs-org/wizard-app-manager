// Box-local overlay for Wizard UX (wizard-os spec/SHELL.md layer 4). The front injects it into the shell's entry
// documents; it changes nothing in James's code, only what his launcher and Settings sheet show:
//   - swipe-up launcher: his "Wizard apps" group becomes My Apps (his other groups stay); "Get apps" turns the same
//     sheet into the App Store, with one chip row above his search bar, and the search bar searches the index
//   - Settings sheet: a Resources group and a Connections group (what each app can reach)
import { renderCard, renderConnections, renderMyApps, renderResources, renderStore } from './wizard-apps.js';

const $ = (s) => document.querySelector(s);
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
  // His tiles in that group that aren't App Manager apps (Chats, his own links) stay first.
  const keep = (group, ids) => [...group.querySelectorAll('.lapp[data-app]')].filter((b) => !ids.has(b.dataset.app)).map((b) => b.outerHTML);

  function injectMyApps() {
    if (body.querySelector(':scope > .wam-host')) return;
    const group = [...body.querySelectorAll(':scope > .lgroup')].find((g) => /^wizard apps/i.test(g.querySelector('h4')?.textContent.trim() || ''));
    if (!group) return;
    const host = document.createElement('div');
    host.className = 'wam-host';
    const theirs = group.cloneNode(true);
    group.replaceWith(host);
    const ctx = {
      shell: true,
      onGetApps: openStore,
      onApp: (a) => openCard(a, 'apps'),
    };
    // The shell's remote tiles are the same apps; My Apps shows them with status, so drop the duplicates.
    fetch('/wizard/v1/apps', { credentials: 'same-origin', cache: 'no-store' })
      .then((r) => (r.ok ? r.json() : { apps: [] }))
      .catch(() => ({ apps: [] }))
      .then((d) => {
        ctx.extraTiles = keep(theirs, new Set((d.apps || []).map((a) => a.id)));
        return renderMyApps(host, ctx);
      })
      .then(() => host.isConnected && toBottom());
  }

  function setStoreUi(on) {
    chips.hidden = !on;
    input.placeholder = on ? 'Search the App Store' : placeholder;
  }

  function openStore() {
    mode = 'store';
    input.value = '';
    const host = document.createElement('div');
    host.className = 'wam-host wam-store';
    const api = renderStore(host, { shell: true, chipsEl: chips, onBack: backToApps, onApp: (a) => openCard(a, 'store') });
    store = { host, api };
    setStoreUi(true);
    body.replaceChildren(host);
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

// Settings sheet: Resources after his "Wizard" group.
const settings = $('#settingsBody');
if (settings) {
  const add = () => {
    if (!settings.children.length || settings.querySelector(':scope > .wam-res')) return;
    const host = document.createElement('div');
    host.className = 'wam-res';
    const wiz = [...settings.querySelectorAll(':scope > .st-group')].find((g) => /^wizard$/i.test(g.querySelector('h4')?.textContent.trim() || ''));
    if (wiz) wiz.after(host);
    else settings.append(host);
    renderResources(host, {});
    const conn = document.createElement('div');
    conn.className = 'wam-conns';
    host.after(conn);
    renderConnections(conn, {});
  };
  new MutationObserver(add).observe(settings, { childList: true });
  add();
}
