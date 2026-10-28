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

/**
 * The map's groups, top to bottom, in plain words. Which group an item is in comes from its wizard.md (wam/kinds.py,
 * the report's `kind`); the words here are all the owner reads about it.
 */
export const BANDS = [
  { key: 'agent', title: 'Agents', blurb: 'Your agents. They do things for you with your apps and connectors.' },
  { key: 'app', title: 'Apps', blurb: 'Things you open and use. Each has its own screen.' },
  { key: 'connector', title: 'Connectors', blurb: 'Background services your apps and agent use. Nothing to open day to day.' },
  { key: 'system', title: 'System', blurb: 'Wizard itself. It runs everything and links your agent to your apps.' },
];
const KIND = new Set(BANDS.map((b) => b.key));
const kindOf = (a, fallback = 'app') => (KIND.has(a?.kind) ? a.kind : fallback);

/** Which group an item is in, for its sheet: { where: "In Connectors", blurb: "Background services …" }. */
export function bandOf(kind) {
  const b = BANDS.find((x) => x.key === kind) || BANDS[1];
  return { key: b.key, where: `In ${b.title}`, blurb: b.blurb };
}

/** What an agent can use, in words that are true today: every app and connector, unless its app lists leave some out. */
export const canUse = (agent) => {
  const l = agent?.limit;
  if (!l) return 'every app and connector';
  return l.can ? `${l.can} of your ${l.of} apps and connectors` : 'none of your apps or connectors';
};

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
  // An agent that runs as an installed app (`agent-<id>`, its registry entry's `app`) is shown once, as the agent:
  // its app copy is left off the map and out of every cut and Ask me first. A request it makes for the owner's OK
  // stays (hiding it would leave the agent waiting forever) and is said with the agent's name.
  const known = (Array.isArray(d?.agents) ? d.agents : []).filter((a) => a && a.id);
  const allIds = new Set(all.map((a) => a.id));
  const claimed = new Map(known.filter((a) => a.app && allIds.has(a.app)).map((a) => [a.app, a]));
  const backing = new Set([...claimed.keys()].filter((x) => !coreIds.has(x)));
  const names = Object.fromEntries(all.map((a) => [a.id, claimed.get(a.id)?.name || a.name || a.id]));
  const nm = (id) => names[id] || slotName(id);
  const controls = Array.isArray(d?.controls) ? d.controls : [];
  const sensitive = new Set(d?.sensitive || all.filter((a) => a.sensitive).map((a) => a.id));
  const apps = all.filter((a) => !coreIds.has(a.id) && !backing.has(a.id));
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
  // Strict mode: the connections that exist are drawn; one the owner cut stays on the map as a cut pair, so it can
  // be reconnected from either app.
  const declaredCut = new Map();
  const single = new Set(); // pairs joined by a connection of their own (not one slot shared by several providers)
  const groups = []; // connections to a slot several apps provide: cut as one
  if (mode === 'declared')
    for (const r of controls)
      if (['connected', 'disconnected'].includes(r.status) && (r.providers || []).filter((x) => ids.has(x)).length > 1 && ids.has(r.from))
        groups.push({
          from: r.from,
          to: r.to,
          cut: r.status === 'disconnected',
          providers: r.providers.filter((x) => ids.has(x)),
          names: list(r.providers.filter((x) => ids.has(x)).map(nm)),
        });
  if (mode === 'declared') {
    for (const r of controls)
      for (const p of (r.providers || []).length ? r.providers : [slotName(r.to)]) {
        if (!ids.has(r.from) || !ids.has(p) || p === r.from) continue;
        if (r.status === 'connected') {
          links.set(pairKey(r.from, p), [r.from, p].sort());
          if ((r.providers || []).length <= 1) single.add(pairKey(r.from, p));
        }
        if (r.status === 'disconnected') {
          cuts.set(pairKey(r.from, p), [r.from, p].sort());
          if ((r.providers || []).filter((x) => ids.has(x)).length > 1) continue; // a slot cut as one: cutGroups
          for (const [x, y] of [
            [r.from, p],
            [p, r.from],
          ])
            declaredCut.set(x, [...(declaredCut.get(x) || []), y]);
        }
      }
  }
  for (const r of pending) for (const p of r.toIds) if (ids.has(r.from) && ids.has(p)) waits.set(pairKey(r.from, p), [r.from, p].sort());
  const shared = new Set(); // Strict mode: pairs that still meet on Runtipi's shared network
  const held = new Map(); // Open mode: pairs a sensitive app holds back (waiting for the owner, or not connected yet)
  const out = apps.map((a) => {
    const un = mode === 'open' && a.open ? a.open.unreachable || [] : [];
    const cut = mode === 'declared' ? [...new Set(declaredCut.get(a.id) || [])] : un.filter((r) => r.why === 'disconnected').map((r) => r.app);
    const ask = un.filter((r) => r.why !== 'disconnected').map((r) => r.app);
    if (mode === 'open') for (const b of cut) if (ids.has(b)) cuts.set(pairKey(a.id, b), [a.id, b].sort());
    let talksTo;
    if (mode === 'off') talksTo = 'every app';
    else if (mode === 'open') {
      const no = un.map((r) => r.app).filter((x) => x !== a.id);
      talksTo = a.open ? (no.length ? `everything except ${list(no.map(nm))}` : 'every app') : 'every app, through the server';
    } else {
      const peers = [...links.values()].filter((p) => p.includes(a.id)).map((p) => (p[0] === a.id ? p[1] : p[0]));
      // Apps that keep Runtipi's shared network (published through its reverse proxy) still reach each other there,
      // undeclared: wam reports them as `shared-network`, and the map says so instead of overstating isolation.
      const viaShared = (a.can_reach || []).filter((e) => e.state === 'shared-network' && e.to !== a.id && !peers.includes(e.to)).map((e) => e.to);
      for (const x of viaShared) shared.add(pairKey(a.id, x));
      const words = [...peers, ...viaShared].map(nm);
      talksTo = words.length ? list(words) : 'no other app';
    }
    const waiting = pending.some((r) => r.from === a.id || r.toIds.includes(a.id));
    const warn = (a.drift || []).length > 0 || (a.host_access || []).length > 0 || !['isolated', undefined].includes(a.state);
    for (const r of un) if (r.why !== 'disconnected' && ids.has(r.app)) held.set(pairKey(a.id, r.app), r.why);
    // In Open mode only apps on the app networks (isolated or published) can be cut off: the others aren't enforced.
    const inOpen = mode === 'open' && !!a.open;
    // Only apps on the app networks (isolated, or published through Runtipi) can be held back for the owner's OK.
    const enforce = ['isolated', 'exposed'].includes(a.state);
    return {
      id: a.id,
      name: a.name || a.id,
      sensitive: sensitive.has(a.id),
      waiting,
      cut,
      ask,
      talksTo,
      warn,
      inOpen,
      enforce,
      // System is Wizard's own core only (it can't be cut off); an app on the networks is never shown there
      kind: kindOf(a) === 'system' ? 'app' : kindOf(a),
      state: a.state,
      why: a.why || '',
    };
  });
  const nCuts = cuts.size;
  const nWait = [...held.values()].filter((w) => w === 'pending').length;
  const nAsk = held.size - nWait;
  const except = [
    nCuts ? `${nCuts === 1 ? 'one pair' : `${nCuts} pairs`} you cut off` : '',
    nWait ? `${nWait === 1 ? 'one' : nWait} waiting for your OK` : '',
    nAsk ? `${nAsk === 1 ? 'one new pair' : `${nAsk} new pairs`} you haven’t approved yet` : '',
  ].filter(Boolean);
  const lead =
    mode === 'off'
      ? 'All your apps can talk to each other. Keeping apps apart is turned off on this Wizard.'
      : mode === 'open'
        ? except.length
          ? `All your apps can talk to each other, except ${list(except)}. Tap an app to change it.`
          : 'All your apps can talk to each other. Tap one to cut it off from another.'
        : shared.size
          ? 'Strict mode: each app talks only to the apps it asks for, except apps published through Runtipi, which can still reach each other (see Details). Tap one to see or change them.'
          : 'Strict mode: each app talks only to the apps it asks for. Tap one to see or change them.';
  const core = all.filter((a) => coreIds.has(a.id));
  // The agents: the ones the box knows that are not apps (the host's agent, the Agent Service's agents registry), and
  // an agent that is part of the core (a fresh box's agent app). None known: one "Your agent", as before.
  const coreItems = core.map((a) => ({ id: a.id, name: a.name || a.id, kind: kindOf(a, 'system') }));
  // What an agent may use (the Agent Service's lists, enforced by the gateway), against the apps and connectors here.
  const usable = out.filter((a) => a.kind === 'app' || a.kind === 'connector');
  const limitOf = (list) => {
    if (!Array.isArray(list)) return null;
    const ok = new Set(list);
    const can = usable.filter((a) => ok.has(a.id)).length;
    return can >= usable.length ? null : { can, of: usable.length };
  };
  // The main agent without its own lists uses every installed app except private ones (Agent Service #58).
  const privateIds = new Set(all.filter((a) => a.privacy === 'private').map((a) => a.id));
  const mainApps = Array.isArray(d?.main_agent_apps)
    ? d.main_agent_apps
    : privateIds.size
      ? usable.map((a) => a.id).filter((x) => !privateIds.has(x))
      : null;
  const agents = [
    ...known.map((a) => ({
      id: a.id,
      name: a.main ? 'Your agent' : a.name || a.id,
      real: a.name || a.id,
      main: !!a.main,
      private: !!a.private,
      limit: limitOf(a.main && !Array.isArray(a.apps) ? mainApps : a.apps),
    })),
    // a core agent app (a fresh box's agent) unless a registry entry already shows it; it is the main agent
    // unless the box names another one
    ...coreItems
      .filter((a) => a.kind === 'agent' && !claimed.has(a.id))
      .map((a) => ({
        id: a.id,
        name: known.some((k) => k.main) ? a.name : 'Your agent',
        real: a.name,
        main: !known.some((k) => k.main),
        private: false,
        limit: limitOf(mainApps),
      })),
  ];
  if (!agents.length) agents.push({ id: '', name: 'Your agent', real: '', main: true, private: false, limit: limitOf(mainApps) });
  return {
    mode,
    apps: out,
    agents,
    backing: [...backing],
    system: coreItems.filter((a) => a.kind !== 'agent'),
    agent: { ids: core.map((a) => a.id), detail: d?.gateway?.detail || '' },
    cuts: [...cuts.values()],
    waits: [...waits.values()],
    links: [...links.values()],
    single: [...single],
    groups,
    pending,
    lead,
  };
}

/**
 * The map's groups top to bottom, each with its items: [{ key, title, blurb, agents, apps, system }]. An empty group
 * is left out. `agents` are agent nodes, `apps` the app nodes (cut, waiting, ask first), `system` plain nodes.
 */
export function bands(model) {
  return BANDS.map((b) => ({
    ...b,
    agents: b.key === 'agent' ? model.agents : [],
    apps: model.apps.filter((a) => a.kind === b.key),
    system: (model.system || []).filter((s) => s.kind === b.key),
  })).filter((b) => b.agents.length + b.apps.length + b.system.length);
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

/**
 * The decisions a cut writes. Open mode: one Disconnect for the pair (it cuts both ways). Strict mode: a denial of
 * every connected row between the two, whichever app declared it, since each row links on its own.
 */
export function cutRows(mode, controls, a, b) {
  if (mode !== 'declared') return [{ from: a, to: b }];
  const rows = (controls || []).filter((r) => r.status === 'connected' && ((r.from === a && provides(r, b)) || (r.from === b && provides(r, a))));
  return rows.length ? rows.map((r) => ({ from: r.from, to: r.to })) : [{ from: a, to: b }];
}

/** The decisions Reconnect undoes: every Disconnect recorded between the pair, either way (falls back to pairRow). */
export function reconnectRows(controls, a, b) {
  const rows = (controls || []).filter((r) => r.status === 'disconnected' && ((r.from === a && provides(r, b)) || (r.from === b && provides(r, a))));
  return rows.length
    ? rows.map((r) => ({ from: r.from, to: r.to }))
    : [pairRow(controls, a, b, 'disconnected')].map((r) => ({ from: r.from, to: r.to }));
}

/** The apps an app can be cut off from (it still talks to them), by name. */
export function cuttable(model, id) {
  const me = model.apps.find((a) => a.id === id);
  if (!me) return [];
  if (model.mode === 'open') {
    if (!me.inOpen) return [];
    const no = new Set([id, ...me.cut, ...me.ask]);
    return model.apps.filter((a) => a.inOpen && !no.has(a.id)).sort((x, y) => x.name.localeCompare(y.name));
  }
  if (model.mode === 'declared') {
    // a peer reached only through a slot several apps provide is cut with that whole connection (cutGroups)
    const own = new Set(model.single || []);
    const peers = new Set(model.links.filter((p) => p.includes(id) && own.has(pairKey(p[0], p[1]))).flat());
    peers.delete(id);
    return model.apps.filter((a) => peers.has(a.id)).sort((x, y) => x.name.localeCompare(y.name));
  }
  return [];
}

/** Strict mode: connections of this app to a slot several apps provide, each cut as one ("Cloudflare and S3"). */
export function cutGroups(model, id, cut = false) {
  return model.mode === 'declared' ? (model.groups || []).filter((g) => !!g.cut === cut && (g.from === id || g.providers.includes(id))) : [];
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
