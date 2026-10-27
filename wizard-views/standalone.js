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
if (only) {
  for (const a of menu.querySelectorAll('a')) if (a.getAttribute('href') !== `#${only}`) a.remove();
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
  else if (view === 'connections') renderConnections(host, {});
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
