// Connections as one picture: the plain-words model behind the map in renderConnections (wizard-apps.js).
// No DOM here, so tests can check every sentence the owner reads (tests/connections-map.test.mjs).
//
// The report (wam/network.py, merged with the owner's decisions by mergeOwnerState) says, per app, what it can reach.
// Declared mode (the default): apps can't talk to each other unless connected, so the map draws only the connections
// that exist, each a line the owner can tap to disconnect; everything else sits apart. Open mode (the owner's choice):
// one cluster of apps that all talk to each other, with only the exceptions drawn (a pair the owner cut off, a request
// waiting for his OK, an app set to "Ask me first"). Agents use every app by default; they reach apps through Wizard,
// never through the app networks. Words like mesh, grant, drift or declared stay in Details.

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
  { key: 'agent', title: 'Agents', blurb: 'Who does the work for you.' },
  { key: 'app', title: 'Apps', blurb: 'Things you use.' },
  { key: 'agent-app', title: 'Agent apps', blurb: 'Tools mainly for your agent. You can watch what it does with them.' },
  { key: 'connector', title: 'Connectors', blurb: 'Links to outside services and your accounts.' },
];
const KIND = new Set(BANDS.map((b) => b.key));
// An older report says `system` for Wizard's own core: it sits with the apps then, still with its badge.
const kindOf = (a, fallback = 'app') => (KIND.has(a?.kind) ? a.kind : fallback);

/** The small badge on Wizard's own pieces (the shell, the Agent Service), in whatever group they sit. */
export const BUILTIN = 'Comes with Wizard';

/** Which group an item is in, for its sheet: { where: "In Connectors", blurb: "Links to outside services …" }. */
export function bandOf(kind) {
  const b = BANDS.find((x) => x.key === kind) || BANDS[1];
  return { key: b.key, where: `In ${b.title}`, blurb: b.blurb };
}

/** What an agent can use, in words that are true today: everything, unless its app lists leave some out. */
export const canUse = (agent) => {
  const l = agent?.limit;
  if (!l) return 'everything';
  return l.can ? `${l.can} of your ${l.of} apps` : 'none of your apps';
};

/** The Agents group's line: "Your agent can use everything." (agents use every app unless their lists say less). */
export function agentsLine(agents) {
  const a = (agents || []).filter(Boolean);
  if (a.length <= 1) return `Your agent can use ${canUse(a[0])}.`;
  return a.every((x) => !x.limit) ? 'Your agents can use everything.' : 'Each agent can use what it says below.';
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
  const linkRows = new Map(); // Strict mode: the decision rows behind each connected pair (for the line's sheet)
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
          linkRows.set(pairKey(r.from, p), [...(linkRows.get(pairKey(r.from, p)) || []), r]);
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
    let alone = false;
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
      alone = !words.length;
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
      kind: kindOf(a),
      // Strict mode: connected to no other app (it sits apart on the map)
      alone: mode === 'declared' && alone,
      state: a.state,
      why: a.why || '',
    };
  });
  // Strict mode: the groups of apps joined by lines (connected components), so the map can keep each group together
  const comp = {};
  if (mode === 'declared') {
    const up = {};
    const find = (x) => (up[x] && up[x] !== x ? (up[x] = find(up[x])) : x);
    for (const [x, y] of links.values()) up[find(x)] = find(y);
    const roots = [...new Set([...links.values()].flat().map(find))];
    const nameOfRoot = (r) =>
      [...links.values()]
        .flat()
        .filter((x) => find(x) === r)
        .map(nm)
        .sort()[0];
    roots.sort((x, y) => nameOfRoot(x).localeCompare(nameOfRoot(y)));
    for (const x of new Set([...links.values()].flat())) comp[x] = roots.indexOf(find(x));
  }
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
        : 'Apps can’t talk to each other unless connected.';
  // Strict mode's second line: what the lines are and what a tap does (and what can't be kept apart)
  const nLinks = links.size;
  const hint =
    mode !== 'declared'
      ? ''
      : [
          nLinks
            ? `${nLinks === 1 ? 'One connection' : `${nLinks} connections`}: tap a line to disconnect it, or an app to connect it.`
            : 'Nothing is connected. Tap an app to connect it to another.',
          shared.size ? 'Apps published through Runtipi can still reach each other (see Details).' : '',
        ]
          .filter(Boolean)
          .join(' ');
  const core = all.filter((a) => coreIds.has(a.id));
  // The agents: the ones the box knows that are not apps (the host's agent, the Agent Service's agents registry), and
  // an agent that is part of the core (a fresh box's agent app). None known: one "Your agent", as before.
  // Wizard's own core sits in the group its wizard.md gives it, with the "comes with Wizard" badge; it can't be cut off.
  const coreItems = core.map((a) => ({ id: a.id, name: a.name || a.id, kind: kindOf(a), builtin: true }));
  // What an agent may use (the Agent Service's lists, enforced by the gateway), against the apps here.
  const usable = out.filter((a) => a.kind !== 'agent');
  const limitOf = (list) => {
    if (!Array.isArray(list)) return null;
    const ok = new Set(list);
    const can = usable.filter((a) => ok.has(a.id)).length;
    return can >= usable.length ? null : { can, of: usable.length };
  };
  // the ids it may use when its lists leave some out (its sheet marks the rest), else null: everything
  const allowOf = (list) => (limitOf(list) ? [...list] : null);
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
      // the main agent's lists live in .main-agent.json (main_agent_apps), never in a registry entry
      limit: limitOf(a.main ? mainApps : a.apps),
      allow: allowOf(a.main ? mainApps : a.apps),
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
        allow: allowOf(mainApps),
      })),
  ];
  if (!agents.length)
    agents.push({ id: '', name: 'Your agent', real: '', main: true, private: false, limit: limitOf(mainApps), allow: allowOf(mainApps) });
  return {
    mode,
    apps: out,
    agents,
    backing: [...backing],
    builtin: coreItems.filter((a) => a.kind !== 'agent'),
    agent: { ids: core.map((a) => a.id), detail: d?.gateway?.detail || '' },
    cuts: [...cuts.values()],
    waits: [...waits.values()],
    links: [...links.values()],
    linkRows: Object.fromEntries([...linkRows].map(([k, v]) => [k, v])),
    comp,
    hint,
    single: [...single],
    groups,
    pending,
    lead,
  };
}

/**
 * The map's groups top to bottom, each with its items: [{ key, title, blurb, agents, apps, builtin }]. An empty group
 * is left out. `agents` are agent nodes (the group's line says what they can use), `apps` the app nodes (connected,
 * cut, waiting, ask first), `builtin` Wizard's own pieces in that group (badge, no connection controls).
 */
export function bands(model) {
  return BANDS.map((b) => ({
    ...b,
    blurb: b.key === 'agent' ? agentsLine(model.agents) : b.blurb,
    agents: b.key === 'agent' ? model.agents : [],
    apps: model.apps.filter((a) => a.kind === b.key),
    builtin: (model.builtin || []).filter((s) => s.kind === b.key),
  })).filter((b) => b.agents.length + b.apps.length + b.builtin.length);
}

/** The sentence at the top of an app's sheet: "Money can talk to: SimpleFIN", "Movies can’t talk to any other app". */
export const talksSentence = (app) => (app.alone ? `${app.name} can’t talk to any other app.` : `${app.name} can talk to: ${app.talksTo}`);

/** Strict mode: the items an app can be connected to (Connect to…): every other item on the app networks that it
 * isn't connected to or cut off from (a cut comes back with Reconnect), by name. */
export function connectable(model, id) {
  const me = model.apps.find((a) => a.id === id);
  if (model.mode !== 'declared' || !me?.enforce) return [];
  const linked = new Set(model.links.filter((p) => p.includes(id)).flat());
  const no = new Set([id, ...linked, ...me.cut]);
  return model.apps.filter((a) => a.enforce && !no.has(a.id)).sort((x, y) => x.name.localeCompare(y.name));
}

/** Strict mode: one line of the map in words, for its sheet: who connected the pair and what Disconnect does. */
export function linkInfo(model, a, b) {
  const rows = (model.linkRows || {})[pairKey(a, b)] || [];
  const nm = (id) => model.apps.find((x) => x.id === id)?.name || slotName(id);
  const why = rows.map((r) => {
    const to = (r.providers || []).length > 1 ? list(r.providers.map(nm)) : nm((r.providers || [])[0] || slotName(r.to));
    return r.origin === 'owner'
      ? 'You connected them.'
      : r.origin === 'requested'
        ? `${nm(r.from)} asked to talk to ${to}, and it was allowed.`
        : `${nm(r.from)} says it needs ${to} to work.`;
  });
  const groupOnly = rows.length > 0 && rows.every((r) => (r.providers || []).length > 1);
  const needs = rows.filter((r) => r.origin !== 'owner').map((r) => nm(r.from));
  return {
    a,
    b,
    title: `${nm(a)} and ${nm(b)} are connected`,
    why: [...new Set(why)],
    // a connection to a slot several apps provide is cut as one, never one provider alone
    group: groupOnly ? rows[0] : null,
    warn: needs.length ? `${list([...new Set(needs)])} may stop working properly without it.` : '',
  };
}

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
    connect: `${aName} and ${bName} are connected now.`,
    disconnect: `${aName} and ${bName} aren’t connected now.`,
    strict: 'Apps can’t talk to each other now unless connected.',
    open: 'All your apps can talk to each other now.',
  }[kind];
}
