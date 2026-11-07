'use strict';
// A Wizard list. Words, groups and extra fields come from list.json; change them there, not here.
// Taps paint first (optimistic), then the server write reconciles. Back closes the editor or sheet; it never exits.
const el = id => document.getElementById(id);
const base = new Map(), groups = new Map(), pending = new Map(), failed = new Map();
let C = {title: 'List', item: {one: 'item', many: 'items', placeholder: 'Add an item'}, group: {one: 'group', many: 'groups'}, fields: [], done: {}, empty: {}};
let revision = -1, loaded = false, filter = 'active', query = '', editing = null, undoItem = null, hold = null, refreshing = false;
let queue = Promise.resolve(), composing = null, deferredRender = false, folded;
try { folded = new Set(JSON.parse(localStorage.getItem('list.folded') || '[]')); } catch (_) { folded = new Set(); }
const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
const announce = text => { el('announcement').textContent = text; };
const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; };
function button(label, handler, icon) {
  const b = node('button', icon ? 'wz-icon-button' : 'wz-button secondary'); b.type = 'button'; b.setAttribute('aria-label', label); b.title = label; b.onclick = handler;
  if (icon) { const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); const p = document.createElementNS(svg.namespaceURI, 'path'); p.setAttribute('d', icon); svg.append(p); b.append(svg); } else b.textContent = label;
  return b;
}
function syncHold() {
  // Ask the shell not to reload the app under unsaved work.
  const dirty = !!editing || !!document.querySelector('dialog[open] input') || pending.size > 0 || failed.size > 0;
  if (dirty && !hold && window.WizardApp?.version === 1) hold = WizardApp.update.hold('Unsaved ' + C.item.many);
  if (!dirty && hold) { hold.release(); hold = null; }
}
async function request(path, data) {
  const init = data === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Wizard-Request': '1'}, body: JSON.stringify(data)};
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
  try { const response = await fetch(path, {...init, signal: controller.signal}); const body = await response.json(); if (!response.ok) throw new Error(body.error || 'Could not save. Try again.'); return body; }
  catch (error) { if (error.name === 'AbortError' || error instanceof TypeError) throw new Error('The connection was interrupted. Try again; nothing will be duplicated.'); throw error; }
  finally { clearTimeout(timer); }
}
function applySnapshot(data) {
  if (!Array.isArray(data.items) || !Array.isArray(data.groups) || !Number.isInteger(data.revision)) throw new Error('The list returned an incomplete response. Try again.');
  if (data.revision >= revision) { revision = data.revision; base.clear(); groups.clear(); for (const t of data.items) base.set(t.id, t); for (const g of data.groups) groups.set(g.id, g); }
  loaded = true;
}
function visibleState() {
  const items = [...base.values()].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id)).map(t => ({...t}));
  const gs = [...groups.values()].sort((a, b) => a.position - b.position || a.id.localeCompare(b.id)).map(g => ({...g}));
  for (const op of pending.values()) {
    const list = op.entity === 'group' ? gs : items;
    let t = list.find(t => t.id === op.id);
    if (!t) { t = {id: op.id, completed: false, deleted: false, fields: {}}; list.push(t); }
    Object.assign(t, Object.fromEntries(['title', 'name', 'completed', 'deleted', 'group_id'].filter(k => k in op).map(k => [k, op[k]])), {pending: true});
    if (op.fields) t.fields = {...t.fields, ...op.fields};
    if (op.create || 'after_id' in op || 'group_id' in op) {
      list.splice(list.indexOf(t), 1);
      const peers = op.entity === 'group' ? list : list.filter(x => x.group_id === t.group_id);
      const anchor = op.after_id ? peers.find(x => x.id === op.after_id) : peers.at(-1);
      list.splice(anchor ? list.indexOf(anchor) + 1 : list.length, 0, t);
    }
  }
  return {items: items.filter(t => !t.deleted), gs};
}
function persistFold() { try { localStorage.setItem('list.folded', JSON.stringify([...folded])); } catch (_) {} }
function cancelEdit() { if (!editing || composing) return false; editing = null; render(); syncHold(); return true; }
function focusEditor() { editing?.field.focus({preventScroll: true}); editing?.field.scrollIntoView({block: 'nearest'}); }
function startEdit(item) {
  if (editing?.id === item.id) { focusEditor(); return; }
  if (!finishEdit(false)) return;
  makeEditor({id: item.id, group_id: item.group_id, title: item.title, before: item});
}
function newDraft(group, after = null) {
  if (!finishEdit(false)) return;
  filter = 'active'; query = ''; el('search').value = ''; folded.delete(group); persistFold();
  makeEditor({id: crypto.randomUUID(), group_id: group, after_id: after, title: '', create: true});
}
function makeEditor(spec) {
  const field = node('input', 'inline-title wz-input'); field.placeholder = C.item.placeholder || 'Add'; field.maxLength = C.title_max || 240; field.autocomplete = 'off'; field.value = spec.title;
  field.setAttribute('aria-label', spec.create ? 'New ' + C.item.one : 'Edit ' + C.item.one); field.enterKeyHint = 'next';
  editing = {...spec, field};
  field.oninput = syncHold;
  trackComposition(field);
  field.onkeydown = e => { if (e.isComposing || composing || e.keyCode === 229) return; if (e.key === 'Enter') { e.preventDefault(); if (!e.repeat) finishEdit(true); } if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); } };
  render(); syncHold(); focusEditor();
}
function finishEdit(next) {
  // Enter saves this line and opens a new one directly below it.
  if (!editing) return true;
  if (composing) return false;
  const e = editing, title = e.field.value.trim();
  if (!e.create && [...pending.values()].some(op => op.id === e.id)) return false;
  if (!title) { if (!next && e.create) { editing = null; render(); syncHold(); return true; } return false; }
  editing = null;
  if (e.create || title !== e.before.title) enqueue({id: e.id, title, ...(e.create ? {create: true, group_id: e.group_id, after_id: e.after_id} : {})}, e.create ? 'add' : 'edit', e.before);
  if (next) makeEditor({id: crypto.randomUUID(), group_id: e.group_id, after_id: e.id, title: '', create: true});
  else { render(); syncHold(); }
  return true;
}
function fieldSummary(item) { return C.fields.map(f => item.fields?.[f.name]).filter(v => v !== undefined && v !== null && v !== '').join(' · '); }
function itemRow(item) {
  const li = node('li', 'item' + (item.completed ? ' done' : '')); li.dataset.itemId = item.id;
  const row = node('div', 'item-row');
  const check = node('input'); check.type = 'checkbox'; check.checked = item.completed; check.disabled = !!item.pending;
  check.setAttribute('aria-label', (item.completed ? 'Reopen ' : 'Mark done ') + item.title);
  check.onchange = () => enqueue({id: item.id, completed: !item.completed}, 'complete', item);
  const toggle = node('label', 'item-toggle'); toggle.append(check); row.append(toggle);
  if (editing?.id === item.id) row.append(editing.field, button('Save', () => finishEdit(false), 'M5 12l4 4L19 6'), button('Cancel edit', cancelEdit, 'M6 6l12 12M6 18L18 6'));
  else {
    const title = button('Edit ' + item.title, () => startEdit(item)); title.className = 'item-title'; title.replaceChildren(node('b', '', item.title));
    const extra = fieldSummary(item); if (extra) title.append(node('small', '', extra));
    title.disabled = !!item.pending; row.append(title);
  }
  if (item.pending) row.append(node('small', 'saving', 'Saving…'));
  const more = button('Actions for ' + item.title, () => itemActions(item), 'M5 12h.01M12 12h.01M19 12h.01'); more.disabled = !!item.pending; row.append(more); li.append(row); return li;
}
function draftRow() { const li = node('li', 'item draft-row'); li.dataset.itemId = editing.id; const row = node('div', 'item-row'); row.append(node('span', 'draft-plus', '+'), editing.field, button('Save', () => finishEdit(false), 'M5 12l4 4L19 6'), button('Cancel edit', cancelEdit, 'M6 6l12 12M6 18L18 6')); li.append(row); return li; }
function render() {
  if (composing) { deferredRender = true; return; }
  const active = document.activeElement, focused = active === editing?.field, selection = focused ? [active.selectionStart, active.selectionEnd] : null, scroll = el('list-scroll').scrollTop;
  const focusItem = active?.closest('[data-item-id]')?.dataset.itemId, focusLabel = active?.getAttribute('aria-label');
  const {items, gs} = visibleState(), shown = items.filter(t => (filter === 'all' || t.completed === (filter === 'completed')) && t.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  el('loading').hidden = loaded; el('groups').setAttribute('aria-busy', String(!loaded)); el('count').textContent = items.filter(t => !t.completed).length + ' active';
  el('list-heading').firstChild.textContent = ({active: C.title + ' ', completed: (C.done.heading || 'Done') + ' ', all: 'All ' + C.item.many + ' '})[filter];
  el('empty').hidden = !loaded || shown.length > 0;
  el('empty-title').textContent = query ? 'No matching ' + C.item.many : filter === 'completed' ? 'Nothing done yet' : items.length ? 'All clear' : (C.empty.title || 'Nothing here yet');
  el('empty-copy').textContent = query ? 'Try another search or switch the filter.' : filter === 'completed' ? 'Finished ' + C.item.many + ' wait here.' : (C.empty.copy || 'Tap Add below.');
  // Keep the exact input node, value and caret; a refresh must never replace an IME target.
  if (editing?.field.isConnected) editing.field.remove();
  const list = el('groups'); list.replaceChildren();
  for (const g of gs) {
    const section = node('section', 'group'); section.dataset.groupId = g.id;
    const head = node('div', 'group-head');
    const fold = button((folded.has(g.id) ? 'Expand ' : 'Collapse ') + g.name, () => { if (editing?.group_id === g.id && !finishEdit(false)) return; folded.has(g.id) ? folded.delete(g.id) : folded.add(g.id); persistFold(); render(); }, 'M9 5l7 7-7 7');
    fold.setAttribute('aria-expanded', String(!folded.has(g.id))); fold.classList.add('group-fold');
    const name = button('Rename ' + g.name, () => groupEditor(g)); name.className = 'group-name'; name.textContent = g.name; name.disabled = !!g.pending;
    head.append(fold, name, node('span', 'wz-count', String(shown.filter(t => t.group_id === g.id).length))); if (g.pending) head.append(node('small', 'saving', 'Saving…')); section.append(head);
    if (!folded.has(g.id)) {
      const ul = node('ul', 'group-items'); ul.setAttribute('aria-label', g.name);
      for (const item of shown.filter(t => t.group_id === g.id)) { ul.append(itemRow(item)); if (editing?.create && editing.group_id === g.id && editing.after_id === item.id) ul.append(draftRow()); }
      if (editing?.create && editing.group_id === g.id && !ul.querySelector('.draft-row')) ul.append(draftRow());
      const add = button('Add ' + C.item.one, () => newDraft(g.id), 'M12 5v14M5 12h14'); add.setAttribute('aria-label', 'Add ' + C.item.one + ' to ' + g.name); add.classList.add('add-item'); add.append(node('span', '', 'Add ' + C.item.one)); add.disabled = !!g.pending;
      section.append(ul, add);
    }
    list.append(section);
  }
  el('add-group').hidden = !loaded; el('add-group').textContent = '+ Add ' + C.group.one;
  for (const b of document.querySelectorAll('[data-filter]')) b.setAttribute('aria-pressed', String(b.dataset.filter === filter));
  el('undo-area').hidden = !undoItem; if (undoItem) el('undo-copy').textContent = 'Deleted “' + undoItem.title + '”';
  el('failures').replaceChildren();
  for (const op of failed.values()) { const box = node('div', 'failure'); box.append(node('span', '', 'Could not save “' + op.displayTitle + '”. ' + op.error), button('Retry', () => { failed.delete(op.operation_id); enqueue(op, op.kind, op.before); }), button('Discard', () => { failed.delete(op.operation_id); render(); syncHold(); })); el('failures').append(box); }
  el('list-scroll').scrollTop = scroll;
  if (focused && editing?.field.isConnected) { editing.field.focus({preventScroll: true}); editing.field.setSelectionRange(...selection); }
  else if (focusItem && focusLabel) { const row = [...document.querySelectorAll('[data-item-id]')].find(n => n.dataset.itemId === focusItem); [...(row?.querySelectorAll('[aria-label]') || [])].find(n => n.getAttribute('aria-label') === focusLabel)?.focus({preventScroll: true}); }
}
function endComposition(field) { if (composing !== field) return; composing = null; if (deferredRender) { deferredRender = false; render(); } }
function trackComposition(field) { field.addEventListener('compositionstart', () => { composing = field; }); field.addEventListener('compositionend', () => endComposition(field)); }
function sheet(title) {
  // The one pop-up: a shared WzSheet. Tap outside, ×, Esc, swipe and Back all close it.
  const d = node('dialog', 'wz-sheet'); d.append(node('h2', '', title)); document.body.append(d);
  d.addEventListener('close', () => { if (composing && d.contains(composing)) endComposition(composing); d.remove(); syncHold(); });
  return d;
}
function groupEditor(g) {
  if (!finishEdit(false)) return;
  const d = sheet(g ? 'Rename ' + C.group.one : 'Add ' + C.group.one), form = node('form', 'sheet-form'), input = node('input', 'wz-input');
  input.maxLength = 80; input.required = true; input.value = g?.name || ''; input.setAttribute('aria-label', cap(C.group.one) + ' name');
  const save = button('Save', null); save.type = 'submit'; form.append(input, save); d.append(form);
  let submitted = false;
  form.onsubmit = e => { e.preventDefault(); if (submitted || composing || !input.value.trim()) return; submitted = true; const name = input.value.trim(); WzSheet.close(d); enqueue({entity: 'group', id: g?.id || crypto.randomUUID(), name, ...(!g ? {create: true} : {expected_version: g.version})}, 'group', g); };
  trackComposition(input); input.autofocus = true; WzSheet.open(d); syncHold(); input.focus();
}
function itemActions(item) {
  const d = sheet(item.title);
  d.append(button('Edit ' + item.title, () => { WzSheet.close(d); startEdit(item); }, 'M4 16v4h4L20 8l-4-4L4 16m10-10 4 4'), button('Add ' + C.item.one + ' below', () => { WzSheet.close(d); newDraft(item.group_id, item.id); }));
  if (C.fields.length) {
    const form = node('form', 'sheet-form'), inputs = {};
    for (const f of C.fields) {
      const label = node('label', '', f.label || f.name), input = node('input', 'wz-input');
      input.type = f.type === 'number' ? 'number' : f.type === 'date' ? 'date' : 'text'; if (f.max) input.maxLength = f.max;
      input.value = item.fields?.[f.name] ?? ''; label.append(input); form.append(label); inputs[f.name] = input; trackComposition(input);
    }
    const save = button('Save details', null); save.type = 'submit'; form.append(save); d.append(form);
    form.onsubmit = e => { e.preventDefault(); if (composing) return; const fields = {};
      for (const f of C.fields) { const v = inputs[f.name].value.trim(); fields[f.name] = v === '' ? null : f.type === 'number' ? Number(v) : v; }
      WzSheet.close(d); enqueue({id: item.id, fields}, 'edit', item); };
  }
  if (groups.size > 1) {
    const label = node('label', '', 'Move to ' + C.group.one), select = node('select', 'wz-input');
    for (const g of visibleState().gs) { if (g.pending) continue; const o = node('option', '', g.name); o.value = g.id; o.selected = g.id === item.group_id; select.append(o); }
    label.append(select); d.append(label);
    select.onchange = () => { WzSheet.close(d); enqueue({id: item.id, group_id: select.value, after_id: null}, 'move', item); };
  }
  d.append(button('Delete ' + item.title, () => { undoItem = item; WzSheet.close(d); enqueue({id: item.id, deleted: true}, 'delete', item); }, 'M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13M10 10v7M14 10v7'));
  WzSheet.open(d);
}
function enqueue(change, kind, before) {
  if (kind !== 'undo' && [...pending.values()].some(op => op.id === change.id)) return;
  const op = {...change, operation_id: change.operation_id || crypto.randomUUID(), kind, before, displayTitle: change.displayTitle || change.title || change.name || before?.title || C.item.one};
  pending.set(op.operation_id, op); render(); syncHold();  // paint now; the server reconciles below
  // Serialize writes: a fast Enter chain may anchor to a row that is still saving.
  queue = queue.then(async () => {
    try {
      const keys = ['operation_id', 'id', 'title', 'name', 'fields', 'completed', 'deleted', 'create', 'expected_version', 'group_id', 'after_id'];
      const payload = Object.fromEntries(keys.filter(k => k in op).map(k => [k, op[k]]));
      const written = await request(op.entity === 'group' ? 'api/groups/change' : 'api/items/change', payload), readback = await request('api/items?include_deleted=true');
      if (readback.revision < written.revision) throw Error('Could not confirm the saved change. Retry safely.');
      applySnapshot(readback); pending.delete(op.operation_id); failed.delete(op.operation_id); announce(kind === 'delete' ? 'Deleted. Undo is available.' : 'Saved.');
    } catch (error) { pending.delete(op.operation_id); failed.set(op.operation_id, {...op, error: error.message}); if (kind === 'delete' && undoItem?.id === op.id) undoItem = null; }
    finally { render(); syncHold(); }
  });
}
async function refresh() {
  if (refreshing) return; refreshing = true; el('load-retry').disabled = true; el('loading').hidden = loaded;
  try { applySnapshot(await request('api/items?include_deleted=true')); el('load-error').hidden = true; render(); }
  catch (error) { el('load-error').hidden = false; el('load-error').firstElementChild.textContent = error.message; }
  finally { refreshing = false; el('load-retry').disabled = false; el('loading').hidden = true; }
}
function applyConfig(config) {
  C = {...C, ...config, item: {...C.item, ...config.item}, group: {...C.group, ...config.group}, fields: config.fields || [], done: config.done || {}, empty: config.empty || {}};
  document.title = C.title; document.querySelector('main').setAttribute('aria-label', C.title);
  el('search').placeholder = 'Search ' + C.item.many; el('search').setAttribute('aria-label', 'Search ' + C.item.many);
  const done = document.querySelector('[data-filter=completed]'); if (C.done.filter) done.textContent = C.done.filter;
}
el('add-group').onclick = () => groupEditor(null);
el('undo').onclick = () => { if (!undoItem) return; const item = undoItem; undoItem = null; enqueue({id: item.id, deleted: false}, 'undo', item); };
for (const b of document.querySelectorAll('[data-filter]')) b.onclick = () => { if (!finishEdit(false)) return; filter = b.dataset.filter; render(); };
el('search').oninput = () => { query = el('search').value; render(); }; el('load-retry').onclick = refresh;
if (window.WizardApp?.version === 1) { WizardApp.ready({name: C.title, version: '0.1.0'}); WizardApp.back.register({canGoBack: () => editing !== null, back: cancelEdit}); }
request('list.json').then(config => { applyConfig(config); render(); }).catch(() => {});
render(); refresh(); document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
