// Connections as one picture: the plain-words model behind the map in renderConnections (wizard-apps.js).
// No DOM here, so tests can check every sentence the owner reads (tests/connections-map.test.mjs).
//
// The report (wam/network.py, merged with the owner's decisions by mergeOwnerState) says, per app, what it can reach.
// The map turns that into: one cluster of apps that all talk to each other (Open mode), and only the exceptions drawn:
// a pair the owner cut off, a request waiting for his OK, an app set to "Ask me first" (sensitive). Strict mode
// (declared) draws the connections that exist instead. Words like mesh, grant, drift or declared stay in Details.

const slotName = (to) => String(to ?? '').replace(/^slot:/, '');

/** "A", "A and B", "A, B and C". */
export function list(names) {
  const n = names.filter(Boolean);
  if (n.length < 2) return n.join('');
  return `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`;
}

const pairKey = (a, b) => (a < b ? `${a}\0${b}` : `${b}\0${a}`);
const provides = (r, id) => (r.providers || []).includes(id) || slotName(r.to) === id;

/**
 * The map's model of a connections report.
 * Returns { mode: 'open'|'declared'|'off', apps, agent, cuts, waits, links, pending, lead, canDraw }.
 *  - apps: [{ id, name, sensitive, waiting, cut: [id], ask: [id], talksTo, warn, why }] in report order, without the
 *    Wizard core (shell, Agent Service): those are the agent node.
 *  - cuts / waits: [[a, b]] pairs to draw (each once): cut off by the owner / a request waiting for his OK.
 *  - links: Strict mode only: [[a, b]] pairs that are connected.
 */
export function mapModel(d) {
  const enabled = d?.enabled !== false;
  const mode = enabled ? (d?.mode === 'open' ? 'open' : 'declared') : 'off';
  const coreIds = new Set(d?.gateway?.core || []);
  const all = Array.isArray(d?.apps) ? d.apps : [];
  const names = Object.fromEntries(all.map((a) => [a.id, a.name || a.id]));
  const nm = (id) => names[id] || slotName(id);
  const controls = Array.isArray(d?.controls) ? d.controls : [];
  const sensitive = new Set(d?.sensitive || all.filter((a) => a.sensitive).map((a) => a.id));
  const apps = all.filter((a) => !coreIds.has(a.id));
  const ids = new Set(apps.map((a) => a.id));
  const cuts = new Map();
  const waits = new Map();
  const links = new Map();
  const pending = controls
    .filter((r) => r.status === 'pending')
    .map((r) => {
      const to = (r.providers || []).length ? r.providers : [slotName(r.to)];
      return { from: r.from, to: r.to, toIds: to, fromName: nm(r.from), toName: list(to.map(nm)) };
    });
  if (mode === 'declared') {
    for (const r of controls)
      if (r.status === 'connected')
        for (const p of (r.providers || []).length ? r.providers : [slotName(r.to)])
          if (ids.has(r.from) && ids.has(p) && p !== r.from) links.set(pairKey(r.from, p), [r.from, p].sort());
  }
  for (const r of pending) for (const p of r.toIds) if (ids.has(r.from) && ids.has(p)) waits.set(pairKey(r.from, p), [r.from, p].sort());
  const out = apps.map((a) => {
    const un = mode === 'open' && a.open ? a.open.unreachable || [] : [];
    const cut = un.filter((r) => r.why === 'disconnected').map((r) => r.app);
    const ask = un.filter((r) => r.why !== 'disconnected').map((r) => r.app);
    if (mode === 'open') for (const b of cut) if (ids.has(b)) cuts.set(pairKey(a.id, b), [a.id, b].sort());
    let talksTo;
    if (mode === 'off') talksTo = 'every app';
    else if (mode === 'open') {
      const no = un.map((r) => r.app).filter((x) => x !== a.id);
      talksTo = a.open ? (no.length ? `everything except ${list(no.map(nm))}` : 'every app') : 'every app, through the server';
    } else {
      const peers = [...links.values()].filter((p) => p.includes(a.id)).map((p) => (p[0] === a.id ? p[1] : p[0]));
      talksTo = peers.length ? list(peers.map(nm)) : 'no other app';
    }
    const waiting = pending.some((r) => r.from === a.id || r.toIds.includes(a.id));
    const warn = (a.drift || []).length > 0 || (a.host_access || []).length > 0 || !['isolated', undefined].includes(a.state);
    return { id: a.id, name: a.name || a.id, sensitive: sensitive.has(a.id), waiting, cut, ask, talksTo, warn, state: a.state, why: a.why || '' };
  });
  const nCuts = cuts.size;
  const lead =
    mode === 'off'
      ? 'All your apps can talk to each other. Keeping apps apart is turned off on this Wizard.'
      : mode === 'open'
        ? nCuts
          ? `All your apps can talk to each other, except ${nCuts === 1 ? 'one pair' : `${nCuts} pairs`} you cut off. Tap an app to change it.`
          : 'All your apps can talk to each other. Tap one to cut it off from another.'
        : 'Strict mode: each app talks only to the apps it asks for. Tap one to see or change them.';
  const core = all.filter((a) => coreIds.has(a.id));
  return {
    mode,
    apps: out,
    agent: { ids: core.map((a) => a.id), detail: d?.gateway?.detail || '' },
    cuts: [...cuts.values()],
    waits: [...waits.values()],
    links: [...links.values()],
    pending,
    lead,
  };
}

/** The sentence at the top of an app's sheet: "Movies can talk to: everything except Shopping". */
export const talksSentence = (app) => `${app.name} can talk to: ${app.talksTo}`;

/**
 * The owner's decision row behind a pair (who asked whom), so Reconnect / Allow / Don't allow go to the same row the
 * App Manager keeps. Falls back to "this app -> that app".
 */
export function pairRow(controls, a, b, status) {
  const rows = (controls || []).filter((r) => !status || r.status === status);
  return rows.find((r) => r.from === a && provides(r, b)) || rows.find((r) => r.from === b && provides(r, a)) || { from: a, to: b, providers: [b] };
}

/** The apps an app can be cut off from (it still talks to them), by name. */
export function cuttable(model, id) {
  const me = model.apps.find((a) => a.id === id);
  if (!me) return [];
  if (model.mode === 'open') {
    const no = new Set([id, ...me.cut, ...me.ask]);
    return model.apps.filter((a) => !no.has(a.id)).sort((x, y) => x.name.localeCompare(y.name));
  }
  if (model.mode === 'declared') {
    const peers = new Set(model.links.filter((p) => p.includes(id)).flat());
    peers.delete(id);
    return model.apps.filter((a) => peers.has(a.id)).sort((x, y) => x.name.localeCompare(y.name));
  }
  return [];
}

/** What the toast says after a change, in plain words. */
export function doneWords(kind, aName, bName) {
  return {
    cut: `${aName} and ${bName} can’t talk to each other now.`,
    reconnect: `${aName} and ${bName} can talk again.`,
    allow: `${aName} can talk to ${bName} now.`,
    deny: `${aName} can’t talk to ${bName}.`,
    'ask-on': `${aName} now asks you first before a new app talks to it.`,
    'ask-off': `${aName} no longer asks you first.`,
    strict: 'Strict mode is on: apps only reach what they ask for.',
    open: 'Strict mode is off: all your apps can talk to each other.',
  }[kind];
}
