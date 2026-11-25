/* Wizard UI kit · "Try new features" (docs/APP_SWITCHES.md, wizard-os#627).
 * The one standard place where an app shows its features under test: the very bottom of its Settings, same look and
 * wording in every app. Use: <section id="try-features"></section> as the LAST thing in Settings, then
 *   WzTryFeatures.mount(document.getElementById('try-features'), { path: 'api/switches' });
 * It reads the app's own wizard.app-switches/1 listing, shows one row per switch (name, one line, On/Off and, when the
 * app keeps votes, thumbs up / down), and hides itself when the app has no switches. Flips and votes POST to the same
 * path (owner only, X-Wizard-Switch: 1). Re-reads when the page becomes visible, because the owner may flip a switch
 * from the Tracker. Needs tokens.css + ui.css (wz-sec, wz-row, wz-main). No dependencies. */
(() => {
  'use strict';
  const TITLE = 'Try new features';
  const NOTE = 'Features still being tested. Approved ones become part of the app; the rest go away. Thumbs up the ones you like.';
  const CSS = `
.wz-try-note { margin: 0; padding: 0 8px 8px; font-size: var(--wz-text-sm); color: var(--ink-3); }
.wz-try-row { cursor: default; }
.wz-try-row:active { background: transparent; }
.wz-try-side { flex: none; display: flex; align-items: center; gap: 6px; }
.wz-try-vote { flex: none; width: 34px; height: 34px; border-radius: 50%; border: 1px solid transparent; background: transparent; font: inherit; line-height: 1; cursor: pointer; opacity: .55; filter: grayscale(1); transition: opacity 140ms, filter 140ms, background 140ms; }
.wz-try-vote[aria-pressed="true"] { opacity: 1; filter: none; background: var(--fill); border-color: var(--line-2); }
.wz-try-toggle { flex: none; position: relative; width: 46px; height: 28px; border-radius: var(--r-pill); border: 1px solid var(--line-2); background: var(--fill); cursor: pointer; transition: background 160ms var(--ease), border-color 160ms; }
.wz-try-toggle::after { content: ""; position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: var(--ink-3); transition: transform 180ms var(--ease), background 160ms; }
.wz-try-toggle[aria-checked="true"] { background: var(--accent); border-color: var(--accent); }
.wz-try-toggle[aria-checked="true"]::after { transform: translateX(18px); background: var(--on-accent); }
.wz-try-toggle:disabled, .wz-try-vote:disabled { cursor: progress; opacity: .5; }
.wz-try-error { margin: 0; padding: 4px 8px; font-size: var(--wz-text-sm); color: var(--danger); }
html[data-skin="signal"] .wz-try-toggle, html[data-skin="signal"] .wz-try-toggle::after { border-radius: 2px; }
`;
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const validVote = v => (v === 'up' || v === 'down' ? v : null);

  /** The switches worth showing, from a wizard.app-switches/1 listing. */
  function rows(listing) {
    const list = listing && Array.isArray(listing.switches) ? listing.switches : [];
    return list.filter(s => s && typeof s.id === 'string' && s.id).map(s => ({
      id: s.id, name: String(s.name || s.id), description: String(s.description || ''), on: s.on === true,
      votes: Object.prototype.hasOwnProperty.call(s, 'vote'), vote: validVote(s.vote), tried: s.tried === true,
    }));
  }

  /** The section's HTML; '' when there is nothing to show (the section hides). */
  function html(listing, opts = {}) {
    const items = rows(listing);
    if (!items.length) return '';
    const row = s => `<li class="wz-row wz-try-row" data-try-id="${esc(s.id)}"><div class="wz-main"><b>${esc(s.name)}</b>${s.description ? `<small>${esc(s.description)}</small>` : ''}</div><div class="wz-try-side">${s.votes ? `<button type="button" class="wz-try-vote" data-try-vote="up" aria-pressed="${s.vote === 'up'}" aria-label="Thumbs up for ${esc(s.name)}">👍</button><button type="button" class="wz-try-vote" data-try-vote="down" aria-pressed="${s.vote === 'down'}" aria-label="Thumbs down for ${esc(s.name)}">👎</button>` : ''}<button type="button" class="wz-try-toggle" role="switch" data-try-toggle aria-checked="${s.on}" aria-label="${esc(s.name)}: ${s.on ? 'on' : 'off'}"></button></div></li>`;
    return `<h2 class="wz-sec-h">${esc(opts.title || TITLE)}</h2><p class="wz-try-note">${esc(opts.note || NOTE)}</p>${opts.error ? `<p class="wz-try-error" role="alert">${esc(opts.error)}</p>` : ''}<ul class="wz-list">${items.map(row).join('')}</ul>`;
  }

  /** The POST body for a tap: a flip, or a vote (tapping the active thumb clears it). */
  function body(listing, id, action) {
    const s = rows(listing).find(r => r.id === id);
    if (!s) return null;
    if (action === 'toggle') return { id, on: !s.on };
    if (action === 'up' || action === 'down') return s.votes ? { id, vote: s.vote === action ? null : action } : null;
    return null;
  }

  function injectCss(doc) {
    if (!doc || !doc.head || doc.getElementById('wz-try-css')) return;
    const st = doc.createElement('style'); st.id = 'wz-try-css'; st.textContent = CSS; doc.head.appendChild(st);
  }

  function mount(el, opts = {}) {
    const path = opts.path || 'api/switches';
    const doFetch = opts.fetch || ((...a) => window.fetch(...a));
    const doc = opts.document || (typeof document !== 'undefined' ? document : null);
    injectCss(doc);
    if (el.classList) el.classList.add('wz-sec', 'wz-try');
    let listing = null, busy = false, error = '';
    const paint = () => { const h = html(listing, { ...opts, error }); el.innerHTML = h; el.hidden = !h; };
    async function read(res) {
      const data = await res.json().catch(() => null);
      if (!res.ok || !data || !Array.isArray(data.switches)) throw new Error((data && data.error) || 'Could not save that change.');
      return data;
    }
    async function load() {
      try { listing = await read(await doFetch(path, { credentials: 'same-origin', cache: 'no-store' })); error = ''; }
      catch (_) { /* keep the last good listing; none hides the section */ }
      paint();
      return listing;
    }
    async function send(id, action) {
      const b = body(listing, id, action);
      if (!b || busy) return null;
      busy = true;
      el.querySelectorAll && el.querySelectorAll('[data-try-id] button').forEach(x => { x.disabled = true; });
      try {
        listing = await read(await doFetch(path, { method: 'POST', credentials: 'same-origin', cache: 'no-store',
          headers: { 'Content-Type': 'application/json', 'X-Wizard-Switch': '1' }, body: JSON.stringify(b) }));
        error = '';
        if (typeof opts.onChange === 'function') { try { opts.onChange(listing, b); } catch (_) {} }
      } catch (e) { error = (e && e.message) || 'Could not save that change.'; }
      finally { busy = false; paint(); }
      return listing;
    }
    el.addEventListener('click', e => {
      const btn = e.target && e.target.closest && e.target.closest('button');
      const row = btn && btn.closest('[data-try-id]');
      if (!row) return;
      const id = row.getAttribute('data-try-id');
      if (btn.hasAttribute('data-try-toggle')) send(id, 'toggle');
      else if (btn.hasAttribute('data-try-vote')) send(id, btn.getAttribute('data-try-vote'));
    });
    if (doc && doc.addEventListener) doc.addEventListener('visibilitychange', () => { if (doc.visibilityState === 'visible') load(); });
    el.hidden = true;
    const ready = load();
    return { reload: load, send, ready, get listing() { return listing; } };
  }

  const api = { mount, html, rows, body, TITLE, NOTE };
  if (typeof window !== 'undefined') window.WzTryFeatures = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
