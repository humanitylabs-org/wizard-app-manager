// Standalone page: the views full screen (no tabs); switch with the Admin menu or #apps / #store / #resources /
// #connections.
import { renderConnections, renderMyApps, renderResources, renderStore } from './wizard-apps.js';

// Shared facade owns Appearance reception; standalone keeps the CSS dark fallback.
window.WizardApp?.ready({ name: 'App Manager' });

const $ = (s) => document.querySelector(s);
const body = $('#body');
const chips = $('#chips');
const form = $('#searchForm');
const input = $('#search');
const btn = $('#adminBtn');
const menu = $('#adminMenu');
let store = null;
// Served by `wam serve` (127.0.0.1:7790/views/): only the Connections data is there, so only that view is offered.
const only = document.body.dataset.only || '';
// ?embed=settings: the shell's Settings tab hosts the page (no title, sheet or canvas of its own; standalone.css). The
// owner listener marks <html> before the first paint; this covers the same page served anywhere else.
if (new URLSearchParams(location.search).get('embed') === 'settings') document.documentElement.dataset.embed = 'settings';
// On the shell's origin (wam's owner listener, /wizard-os-home/store-install/_connections/) the page names its own
// owner API: relative paths, and the header every owner POST there must carry.
const ds = document.body.dataset;
const connCtx = {
  ...(ds.api ? { api: ds.api } : {}),
  ...(ds.connections ? { connections: ds.connections } : {}),
  ...(ds.ownerHeader ? { headers: { [ds.ownerHeader]: '1' } } : {}),
};
if (only) {
  for (const a of menu.querySelectorAll('a')) if (a.getAttribute('href') !== `#${only}`) a.remove();
  // One view: nothing to switch to, so no Admin menu (the shell's title bar already names the window).
  btn.parentElement.hidden = true;
}

function show() {
  const view = only || (location.hash || '#apps').slice(1);
  const inStore = view === 'store';
  form.hidden = !inStore;
  chips.hidden = !inStore;
  body.classList.toggle('top', view === 'resources' || view === 'connections');
  body.replaceChildren();
  store = null;
  input.value = '';
  for (const a of menu.querySelectorAll('a')) a.classList.toggle('on', a.getAttribute('href') === `#${view}`);
  const host = document.createElement('div');
  body.append(host);
  if (inStore) store = renderStore(host, { chipsEl: chips });
  else if (view === 'resources') renderResources(host, {});
  else if (view === 'connections') renderConnections(host, connCtx);
  else renderMyApps(host, { onGetApps: () => location.assign('#store') });
}

input.addEventListener('input', () => store?.setQuery(input.value));
form.addEventListener('submit', (e) => {
  e.preventDefault();
  store?.setQuery(input.value);
});
btn.addEventListener('click', (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  btn.setAttribute('aria-expanded', String(!menu.hidden));
});
document.addEventListener('click', () => {
  menu.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
});
window.addEventListener('hashchange', show);
show();
