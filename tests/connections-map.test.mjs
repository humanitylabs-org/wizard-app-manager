// The Connections map's plain words (wizard-views/connections-map.js): what the owner reads must match the report.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  BUILTIN,
  agentList,
  agentName,
  agentNote,
  agentRows,
  agentsLine,
  bandOf,
  bands,
  canUse,
  canUseLine,
  connectable,
  cutGroups,
  cutList,
  cutRows,
  cuttable,
  doneWords,
  footerLine,
  reconnectRows,
  linkInfo,
  linkReason,
  linkTag,
  linkedApps,
  list,
  listLayout,
  listLead,
  mapModel,
  pairRow,
  pickFirst,
  restoreWords,
  talksSentence,
  waitingLines,
} from '../wizard-views/connections-map.js';

const app = (id, name, unreachable = [], extra = {}) => ({
  id,
  name,
  state: 'isolated',
  open: { mesh: !unreachable.length, links: [], unreachable, unenforced: [] },
  ...extra,
});
const core = (id, name) => ({ id, name, state: 'host-network', open: null });
const report = (apps, extra = {}) => ({
  enabled: true,
  mode: 'open',
  gateway: { core: ['wizard-ux', 'wizard-agent-service'], detail: 'on the server' },
  apps: [...apps, core('wizard-ux', 'Wizard Shell'), core('wizard-agent-service', 'Wizard Agent Service')],
  controls: [],
  ...extra,
});

test('Open mode with nothing cut: one sentence, every app talks to every app, no lines', () => {
  const m = mapModel(report([app('cinequeue', 'Movies'), app('wizard-shopping', 'Shopping'), app('files', 'Files')]));
  assert.equal(m.mode, 'open');
  assert.equal(m.lead, 'All your apps can talk to each other. Tap one to cut it off from another.');
  assert.deepEqual(m.cuts, []);
  assert.deepEqual(m.links, []);
  assert.equal(talksSentence(m.apps[0]), 'Movies can talk to: every app');
  // the Wizard core is the agent node, not an app on the map
  assert.deepEqual(
    m.apps.map((a) => a.id),
    ['cinequeue', 'wizard-shopping', 'files'],
  );
  assert.deepEqual(m.agent.ids, ['wizard-ux', 'wizard-agent-service']);
});

test('A cut pair is one exception line, said in plain words on both apps', () => {
  const m = mapModel(
    report([
      app('cinequeue', 'Movies', [{ app: 'wizard-shopping', why: 'disconnected' }]),
      app('wizard-shopping', 'Shopping', [{ app: 'cinequeue', why: 'disconnected' }]),
      app('files', 'Files'),
    ]),
  );
  assert.deepEqual(m.cuts, [['cinequeue', 'wizard-shopping']]);
  assert.equal(m.lead, 'All your apps can talk to each other, except one pair you cut off. Tap an app to change it.');
  assert.equal(talksSentence(m.apps[0]), 'Movies can talk to: everything except Shopping');
  assert.deepEqual(m.apps[0].cut, ['wizard-shopping']);
  // Cut off from… offers only apps it still talks to
  assert.deepEqual(
    cuttable(m, 'cinequeue').map((a) => a.id),
    ['files'],
  );
  assert.equal(doneWords('cut', 'Movies', 'Shopping'), 'Movies and Shopping can’t talk to each other now.');
});

test('A request to a sensitive app is a waiting pair and a card, not a cut', () => {
  const d = report(
    [app('zoom', 'Zoom', [{ app: 'resend', why: 'pending' }]), app('resend', 'Resend', [{ app: 'zoom', why: 'pending' }], { sensitive: true })],
    { sensitive: ['resend'], controls: [{ from: 'zoom', to: 'resend', providers: ['resend'], status: 'pending' }] },
  );
  const m = mapModel(d);
  assert.deepEqual(m.cuts, []);
  assert.deepEqual(m.waits, [['resend', 'zoom']]);
  assert.equal(m.pending.length, 1);
  assert.equal(`${m.pending[0].fromName} wants to talk to ${m.pending[0].toName}`, 'Zoom wants to talk to Resend');
  const resend = m.apps.find((a) => a.id === 'resend');
  assert.equal(resend.sensitive, true);
  assert.equal(resend.waiting, true);
  assert.deepEqual(resend.ask, ['zoom']);
  assert.equal(talksSentence(resend), 'Resend can talk to: everything except Zoom');
});

test('Strict mode draws the connections that exist and says who each app talks to', () => {
  const d = report([app('backup', 'Backup'), app('wizard-cloudflare', 'Cloudflare'), app('files', 'Files')], {
    mode: 'declared',
    controls: [
      { from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare'], status: 'connected' },
      { from: 'files', to: 'backup', providers: ['backup'], status: 'disconnected' },
    ],
  });
  for (const a of d.apps) a.open = null;
  const m = mapModel(d);
  assert.equal(m.mode, 'declared');
  assert.deepEqual(m.links, [['backup', 'wizard-cloudflare']]);
  assert.equal(talksSentence(m.apps[0]), 'Backup can talk to: Cloudflare');
  assert.equal(talksSentence(m.apps[2]), 'Files can’t talk to any other app.');
  assert.equal(m.lead, 'Apps can’t talk to each other unless connected.');
  assert.equal(m.hint, 'One connection: tap a line to disconnect it, or an app to connect it.');
  // connected apps are drawn with their line; the rest sit apart
  assert.deepEqual(
    m.apps.map((a) => [a.id, a.alone]),
    [
      ['backup', false],
      ['wizard-cloudflare', false],
      ['files', true],
    ],
  );
  assert.deepEqual(
    cuttable(m, 'backup').map((a) => a.id),
    ['wizard-cloudflare'],
  );
});

test('Reconnect goes to the row the owner decided, whichever side it was made from', () => {
  const controls = [{ from: 'wizard-shopping', to: 'cinequeue', providers: ['cinequeue'], status: 'disconnected' }];
  assert.deepEqual(pairRow(controls, 'cinequeue', 'wizard-shopping', 'disconnected'), controls[0]);
  assert.deepEqual(pairRow([], 'cinequeue', 'wizard-shopping', 'disconnected'), {
    from: 'cinequeue',
    to: 'wizard-shopping',
    providers: ['wizard-shopping'],
  });
});

test('Isolation off says so plainly; lists read naturally', () => {
  const m = mapModel({ enabled: false, apps: [app('a', 'A'), app('b', 'B')] });
  assert.equal(m.mode, 'off');
  assert.match(m.lead, /turned off/);
  assert.equal(list(['A']), 'A');
  assert.equal(list(['A', 'B', 'C']), 'A, B and C');
});

test('Jargon stays out of the words on the map', () => {
  const m = mapModel(
    report([app('cinequeue', 'Movies', [{ app: 'x', why: 'disconnected' }]), app('x', 'X', [{ app: 'cinequeue', why: 'disconnected' }])]),
  );
  const words = [m.lead, ...m.apps.map(talksSentence)].join(' ');
  assert.doesNotMatch(words, /mesh|grant|drift|declared|network/i);
});

test('Cut off from… offers only apps whose connections Open mode can enforce', () => {
  // an app on the server's own network (not the shell or agent) has no Open-mode topology: a cut would not hold
  const m = mapModel(
    report([app('cinequeue', 'Movies'), app('files', 'Files'), { id: 'hostapp', name: 'Host App', state: 'host-network', open: null }]),
  );
  assert.deepEqual(
    cuttable(m, 'cinequeue').map((a) => a.id),
    ['files'],
  );
  assert.deepEqual(cuttable(m, 'hostapp'), []);
});

test('Strict mode keeps a pair the owner cut on the map, so it can be reconnected', () => {
  const d = report([app('backup', 'Backup'), app('wizard-cloudflare', 'Cloudflare')], {
    mode: 'declared',
    controls: [{ from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare'], status: 'disconnected' }],
  });
  for (const a of d.apps) a.open = null;
  const m = mapModel(d);
  assert.deepEqual(m.cuts, [['backup', 'wizard-cloudflare']]);
  assert.deepEqual(m.apps[0].cut, ['wizard-cloudflare']);
  assert.deepEqual(m.apps[1].cut, ['backup']);
  assert.equal(talksSentence(m.apps[0]), 'Backup can’t talk to any other app.');
  assert.deepEqual(pairRow(d.controls, 'wizard-cloudflare', 'backup', 'disconnected'), d.controls[0]);
  // a cut isn't a line in Strict mode (only connections are), and Connect to… leaves it to Reconnect
  assert.deepEqual(m.links, []);
  assert.deepEqual(connectable(m, 'backup'), []);
  assert.equal(m.hint, 'Nothing is connected. Tap an app to connect it to another.');
});

test('The top line counts pairs a sensitive app holds back, not only cuts', () => {
  const waiting = mapModel(
    report([app('zoom', 'Zoom', [{ app: 'resend', why: 'pending' }]), app('resend', 'Resend', [{ app: 'zoom', why: 'pending' }])], {
      sensitive: ['resend'],
    }),
  );
  assert.equal(waiting.lead, 'All your apps can talk to each other, except one waiting for your OK. Tap an app to change it.');
  const both = mapModel(
    report([
      app('zoom', 'Zoom', [
        { app: 'resend', why: 'sensitive' },
        { app: 'files', why: 'disconnected' },
      ]),
      app('resend', 'Resend', [{ app: 'zoom', why: 'sensitive' }]),
      app('files', 'Files', [{ app: 'zoom', why: 'disconnected' }]),
    ]),
  );
  assert.equal(
    both.lead,
    'All your apps can talk to each other, except one pair you cut off and one new pair you haven’t approved yet. Tap an app to change it.',
  );
});

test('A Strict-mode cut denies the connection that exists, whichever app declared it', () => {
  const controls = [{ from: 'wizard-cloudflare', to: 'backup', providers: ['backup'], status: 'connected' }];
  assert.deepEqual(cutRows('declared', controls, 'backup', 'wizard-cloudflare'), [{ from: 'wizard-cloudflare', to: 'backup' }]);
  const both = [...controls, { from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare'], status: 'connected' }];
  assert.equal(cutRows('declared', both, 'backup', 'wizard-cloudflare').length, 2);
  assert.deepEqual(cutRows('open', controls, 'backup', 'wizard-cloudflare'), [{ from: 'backup', to: 'wizard-cloudflare' }]);
});

test('Strict mode says what apps on Runtipi’s shared network still reach', () => {
  const d = report([app('files', 'Files', [], { state: 'exposed' }), app('photos', 'Photos', [], { state: 'exposed' })], { mode: 'declared' });
  for (const a of d.apps) a.open = null;
  d.apps[0].can_reach = [{ to: 'photos', state: 'shared-network', declared: false }];
  d.apps[1].can_reach = [{ to: 'files', state: 'shared-network', declared: false }];
  const m = mapModel(d);
  assert.equal(talksSentence(m.apps[0]), 'Files can talk to: Photos');
  assert.equal(m.lead, 'Apps can’t talk to each other unless connected.');
  assert.match(m.hint, /Apps published through Runtipi can still reach each other \(see Details\)/);
});

test('Reconnect undoes every Disconnect between the pair, so no half-cut pair is left', () => {
  const controls = [
    { from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare'], status: 'disconnected' },
    { from: 'wizard-cloudflare', to: 'backup', providers: ['backup'], status: 'disconnected' },
    { from: 'backup', to: 'files', providers: ['files'], status: 'disconnected' },
  ];
  assert.deepEqual(reconnectRows(controls, 'wizard-cloudflare', 'backup'), [
    { from: 'backup', to: 'slot:backup-storage' },
    { from: 'wizard-cloudflare', to: 'backup' },
  ]);
  assert.deepEqual(reconnectRows([], 'cinequeue', 'wizard-shopping'), [{ from: 'cinequeue', to: 'wizard-shopping' }]);
});

test('Strict mode: a connection to a slot several apps provide is cut as one, never one provider alone', () => {
  const d = report([app('backup', 'Backup'), app('wizard-cloudflare', 'Cloudflare'), app('s3', 'S3'), app('files', 'Files')], {
    mode: 'declared',
    controls: [
      { from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare', 's3'], status: 'connected' },
      { from: 'backup', to: 'files', providers: ['files'], status: 'connected' },
    ],
  });
  for (const a of d.apps) a.open = null;
  const m = mapModel(d);
  assert.deepEqual(
    cuttable(m, 'backup').map((a) => a.id),
    ['files'],
  );
  assert.deepEqual(cuttable(m, 'wizard-cloudflare'), []);
  const [g] = cutGroups(m, 'backup');
  assert.equal(g.to, 'slot:backup-storage');
  assert.equal(g.names, 'Cloudflare and S3');
  assert.equal(cutGroups(m, 's3').length, 1);
});

test('Strict mode: a slot connection that was cut comes back as one, not per provider', () => {
  const d = report([app('backup', 'Backup'), app('wizard-cloudflare', 'Cloudflare'), app('s3', 'S3')], {
    mode: 'declared',
    controls: [{ from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare', 's3'], status: 'disconnected' }],
  });
  for (const a of d.apps) a.open = null;
  const m = mapModel(d);
  assert.deepEqual(m.apps[0].cut, []);
  const [g] = cutGroups(m, 'backup', true);
  assert.equal(g.names, 'Cloudflare and S3');
  assert.equal(cutGroups(m, 'backup').length, 0);
});

test('Ask me first is offered only where the app networks can hold a connection back', () => {
  const m = mapModel(
    report([app('files', 'Files', [], { state: 'exposed' }), { id: 'hostapp', name: 'Host App', state: 'host-network', open: null }, app('a', 'A')]),
  );
  assert.deepEqual(
    m.apps.map((a) => [a.id, a.enforce]),
    [
      ['files', true],
      ['hostapp', false],
      ['a', true],
    ],
  );
});

test('Four groups: Agents, Apps, Agent apps and Connectors, each from the report kind; Wizard’s own pieces carry a badge', () => {
  const d = report(
    [
      app('cinequeue', 'Movies', [], { kind: 'app' }),
      app('wizard-monarch', 'Monarch', [], { kind: 'connector' }),
      app('transcriber', 'Transcriber', [], { kind: 'agent-app' }),
      app('old', 'Old App'), // an older App Manager without kinds: an app
    ],
    { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }] },
  );
  d.apps.find((a) => a.id === 'wizard-ux').kind = 'app';
  d.apps.find((a) => a.id === 'wizard-agent-service').kind = 'connector';
  const m = mapModel(d);
  const g = bands(m);
  assert.deepEqual(
    g.map((b) => [b.key, b.title]),
    [
      ['agent', 'Agents'],
      ['app', 'Apps'],
      ['agent-app', 'Agent apps'],
      ['connector', 'Connectors'],
    ],
  );
  assert.deepEqual(
    g[0].agents.map((a) => [a.name, a.real, a.main]),
    [['Your agent', 'host Hermes', true]],
  );
  assert.equal(g[0].blurb, 'Your agent can use everything.');
  assert.deepEqual(
    g[1].apps.map((a) => a.id),
    ['cinequeue', 'old'],
  );
  assert.deepEqual(
    g[1].builtin.map((a) => [a.id, a.builtin]),
    [['wizard-ux', true]],
  );
  assert.deepEqual(
    g[2].apps.map((a) => a.id),
    ['transcriber'],
  );
  assert.deepEqual(
    g[3].apps.map((a) => a.id),
    ['wizard-monarch'],
  );
  assert.deepEqual(
    g[3].builtin.map((a) => a.id),
    ['wizard-agent-service'],
  );
  assert.equal(BUILTIN, 'Comes with Wizard');
  assert.equal(g[2].blurb, 'Tools mainly for your agent. You can watch what it does with them.');
  assert.equal(g[3].blurb, 'Links to outside services and your accounts.');
  // System is not a group any more
  assert.ok(!g.some((b) => b.key === 'system'));
  // every group says in one line what it is, and each sheet says which group
  for (const b of g) assert.ok(b.blurb.length > 10 && b.blurb.length < 90, b.key);
  assert.deepEqual(bandOf('agent-app').where, 'In Agent apps');
  assert.deepEqual(bandOf('connector').where, 'In Connectors');
  assert.deepEqual(bandOf('nonsense').where, 'In Apps');
  // a connector or an agent app is on the app networks like any app: it can be cut off; Wizard's own pieces can't
  assert.ok(cuttable(m, 'wizard-monarch').some((a) => a.id === 'cinequeue'));
  assert.ok(cuttable(m, 'transcriber').some((a) => a.id === 'cinequeue'));
  assert.ok(!cuttable(m, 'cinequeue').some((a) => a.id === 'wizard-ux'));
});

test('Strict mode is the default: only real connections are lines, each with its own words and Disconnect', () => {
  const d = report(
    [
      app('money', 'Money'),
      app('wizard-simplefin', 'SimpleFIN', [], { kind: 'connector' }),
      app('wizard-music', 'Music'),
      app('wizard-youtube-music', 'YouTube Music', [], { kind: 'connector' }),
      app('cinequeue', 'Movies'),
      { id: 'hostapp', name: 'Host App', state: 'host-network', open: null },
    ],
    {
      mode: undefined,
      controls: [
        { from: 'money', to: 'wizard-simplefin', providers: ['wizard-simplefin'], status: 'connected', origin: 'declared' },
        { from: 'wizard-music', to: 'wizard-youtube-music', providers: ['wizard-youtube-music'], status: 'connected', origin: 'owner' },
      ],
    },
  );
  for (const a of d.apps) a.open = null;
  const m = mapModel(d);
  assert.equal(m.mode, 'declared');
  assert.deepEqual(m.links, [
    ['money', 'wizard-simplefin'],
    ['wizard-music', 'wizard-youtube-music'],
  ]);
  assert.equal(m.hint, '2 connections: tap a line to disconnect it, or an app to connect it.');
  const money = linkInfo(m, 'money', 'wizard-simplefin');
  assert.equal(money.title, 'Money and SimpleFIN are connected');
  assert.deepEqual(money.why, ['Money says it needs SimpleFIN to work.']);
  assert.equal(money.warn, 'Money may stop working properly without it.');
  assert.deepEqual(money.groups, []);
  assert.deepEqual(money.rows, [{ from: 'money', to: 'wizard-simplefin' }]);
  const music = linkInfo(m, 'wizard-music', 'wizard-youtube-music');
  assert.deepEqual(music.why, ['They were connected with your OK.']);
  assert.equal(music.warn, '');
  // Disconnect denies the row that links them
  assert.deepEqual(cutRows('declared', d.controls, 'wizard-simplefin', 'money'), [{ from: 'money', to: 'wizard-simplefin' }]);
  // Connect to… offers every other item it isn't connected to, never one outside the app networks
  assert.deepEqual(
    connectable(m, 'money').map((a) => a.id),
    ['cinequeue', 'wizard-music', 'wizard-youtube-music'],
  );
  assert.deepEqual(connectable(m, 'hostapp'), []);
  assert.equal(m.apps.find((a) => a.id === 'cinequeue').alone, true);
  assert.equal(talksSentence(m.apps.find((a) => a.id === 'money')), 'Money can talk to: SimpleFIN');
  assert.equal(doneWords('disconnect', 'Money', 'SimpleFIN'), 'Money and SimpleFIN aren’t connected now.');
  assert.equal(doneWords('connect', 'Music', 'YouTube Music'), 'Music and YouTube Music are connected now.');
  // a line to a slot several apps provide is disconnected as one
  const g = report([app('backup', 'Backup'), app('wizard-cloudflare', 'Cloudflare'), app('s3', 'S3')], {
    mode: 'declared',
    controls: [{ from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare', 's3'], status: 'connected', origin: 'declared' }],
  });
  const gm = mapModel(g);
  const info = linkInfo(gm, 'backup', 's3');
  assert.equal(info.groups[0].to, 'slot:backup-storage');
  assert.deepEqual(info.why, ['Backup says it needs Cloudflare and S3 to work.']);
  // a pair joined by two slot connections: Disconnect denies both, once each (Codex review)
  g.controls.push({ from: 'backup', to: 'slot:offsite', providers: ['s3', 'wizard-cloudflare'], status: 'connected', origin: 'declared' });
  assert.deepEqual(linkInfo(mapModel(g), 'backup', 's3').rows, [
    { from: 'backup', to: 'slot:backup-storage' },
    { from: 'backup', to: 'slot:offsite' },
  ]);
});

test('Agents: one "Your agent" when the box names none; what an agent can use is said truthfully', () => {
  const m = mapModel(report([app('cinequeue', 'Movies')]));
  assert.deepEqual(
    m.agents.map((a) => a.name),
    ['Your agent'],
  );
  assert.equal(bands(m)[0].key, 'agent');
  assert.equal(canUse(m.agents[0]), 'everything');
  assert.equal(agentsLine(m.agents), 'Your agent can use everything.');
  assert.equal(m.agents[0].allow, null);
  const apps = [app('cinequeue', 'Movies'), app('files', 'Files'), app('wizard-monarch', 'Monarch', [], { kind: 'connector' })];
  const two = mapModel(
    report(apps, {
      agents: [
        { id: 'host-hermes', name: 'host Hermes', main: true, apps: ['cinequeue', 'files', 'wizard-monarch', 'gone-app'] },
        { id: 'private', name: 'Private', private: true, apps: ['files'] },
        { id: 'sealed', name: 'Sealed', private: true, apps: [] },
      ],
    }),
  );
  assert.deepEqual(
    two.agents.map((a) => [a.name, canUse(a)]),
    [
      ['Your agent', 'everything'],
      ['Private', '1 of your 3 apps'],
      ['Sealed', 'none of your apps'],
    ],
  );
  assert.equal(agentsLine(two.agents), 'Each agent can use what it says below.');
  assert.deepEqual(two.agents[1].allow, ['files']);
  // the main agent's own lists (.main-agent.json) when its entry carries none; without lists, private apps are left out
  const listed = mapModel(report(apps, { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }], main_agent_apps: ['files'] }));
  assert.equal(canUse(listed.agents[0]), '1 of your 3 apps');
  assert.equal(agentsLine(listed.agents), 'Your agent can use 1 of your 3 apps.');
  // and only there: a registry entry's own permissions never speak for the main agent
  const reg = mapModel(
    report(apps, {
      agents: [{ id: 'hermes', name: 'Hermes', main: true, apps: ['files'] }],
      main_agent_apps: ['cinequeue', 'files', 'wizard-monarch'],
    }),
  );
  assert.equal(canUse(reg.agents[0]), 'everything');
  const priv = report(apps, { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }] });
  priv.apps[0].privacy = 'private';
  assert.equal(canUse(mapModel(priv).agents[0]), '2 of your 3 apps');
});

test('An agent that runs as an installed app is shown once, as the agent', () => {
  const d = report([app('cinequeue', 'Movies'), app('agent-private', 'Private agent', [], { kind: 'agent' })], {
    agents: [{ id: 'private', name: 'Private', private: true, apps: ['cinequeue'], app: 'agent-private' }],
  });
  const g = bands(mapModel(d));
  assert.deepEqual(
    g[0].agents.map((a) => a.id),
    ['private'],
  );
  assert.deepEqual(g[0].apps, []);
  assert.equal(canUse(g[0].agents[0]), 'everything');
  // and out of every connection control
  const m = mapModel(d);
  assert.deepEqual(
    m.apps.map((a) => a.id),
    ['cinequeue'],
  );
  assert.deepEqual(cuttable(m, 'cinequeue'), []);
});

test('A box with only Wizard itself still shows its agent and Wizard’s own pieces', () => {
  const m = mapModel(report([]));
  assert.deepEqual(m.apps, []);
  assert.deepEqual(
    bands(m).map((b) => b.key),
    ['agent', 'app'],
  );
  assert.deepEqual(
    bands(m)[1].builtin.map((s) => s.id),
    ['wizard-ux', 'wizard-agent-service'],
  );
});

test('Groups: an agent in the core is an agent, not one of Wizard’s badged pieces', () => {
  const d = report([app('cinequeue', 'Movies')], { gateway: { core: ['wizard-ux', 'wizard-agent-service', 'hermes-agent'] } });
  d.apps.push({ id: 'hermes-agent', name: 'Hermes', state: 'isolated', open: null, kind: 'agent' });
  const m = mapModel(d);
  assert.deepEqual(
    m.agents.map((a) => [a.id, a.name]),
    [['hermes-agent', 'Your agent']],
  );
  assert.deepEqual(
    m.builtin.map((a) => a.id),
    ['wizard-ux', 'wizard-agent-service'],
  );
});

test('Only Wizard itself carries the badge: an app that calls itself system stays an app, with its controls', () => {
  const m = mapModel(report([app('cinequeue', 'Movies'), app('spare-shell', 'Spare Shell', [], { kind: 'system' })]));
  assert.equal(m.apps.find((a) => a.id === 'spare-shell').kind, 'app');
  assert.ok(!m.apps.some((a) => a.builtin));
  assert.deepEqual(
    bands(m)
      .find((b) => b.key === 'app')
      .builtin.map((s) => s.id),
    ['wizard-ux', 'wizard-agent-service'],
  );
  assert.ok(cuttable(m, 'cinequeue').some((a) => a.id === 'spare-shell'));
});

test('A request from an app-backed agent stays, said with the agent’s name; a registry-backed core agent shows once', () => {
  const d = report([app('cinequeue', 'Movies'), app('agent-private', 'agent-private', [], { kind: 'agent' })], {
    agents: [{ id: 'private', name: 'Private', private: true, app: 'agent-private' }],
    controls: [{ from: 'agent-private', to: 'cinequeue', status: 'pending' }],
  });
  const m = mapModel(d);
  assert.deepEqual(
    m.pending.map((p) => [p.fromName, p.toName]),
    [['Private', 'Movies']],
  );
  assert.deepEqual(m.waits, []); // no line to a node that isn't drawn
  const core = report([app('cinequeue', 'Movies')], {
    gateway: { core: ['wizard-ux', 'wizard-agent-service', 'hermes-agent'] },
    agents: [
      { id: 'hermes', name: 'Hermes', main: true, app: 'hermes-agent' },
      { id: 'private', name: 'Private', private: true },
    ],
  });
  core.apps.push({ id: 'hermes-agent', name: 'Hermes agent', state: 'isolated', open: null, kind: 'agent' });
  assert.deepEqual(
    mapModel(core).agents.map((a) => [a.id, a.name]),
    [
      ['hermes', 'Your agent'],
      ['private', 'Private'],
    ],
  );
  // without a registry entry the core agent app is the main agent, even beside extra agents
  core.agents = [{ id: 'private', name: 'Private', private: true }];
  assert.deepEqual(
    mapModel(core).agents.map((a) => [a.id, a.name, a.main]),
    [
      ['private', 'Private', false],
      ['hermes-agent', 'Your agent', true],
    ],
  );
});

// The phone list: the box as Miguel has it (Strict mode, a few links, one pair he cut, the host's agent).
const phoneBox = () => {
  const d = report(
    [
      app('money', 'Money', [], { provides: ['money'] }),
      app('card-benefits', 'Card Benefits'),
      app('messenger', 'Messenger'),
      app('wizard-claude', 'Claude', [], { kind: 'connector' }),
      app('wizard-simplefin', 'SimpleFIN', [], { kind: 'connector', provides: ['bank-connection', 'simplefin-bank-data'] }),
      app('wizard-monarch', 'Monarch', [], { kind: 'connector', provides: ['bank-connection', 'monarch-finance-data'] }),
      app('wizard-music', 'Music'),
      app('wizard-youtube-music', 'YouTube Music', [], { kind: 'connector', provides: ['odd-job'] }),
      app('transcriber', 'Transcriber', [], { kind: 'agent-app' }),
      app('backup', 'Backup'),
      app('wizard-cloudflare', 'Cloudflare', [], { kind: 'connector', provides: ['cloudflare-connection', 'backup-storage'] }),
      app('s3', 'S3', [], { kind: 'connector', provides: ['backup-storage'] }),
    ],
    {
      mode: 'declared',
      agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }],
      controls: [
        { from: 'money', to: 'wizard-simplefin', providers: ['wizard-simplefin'], status: 'connected', origin: 'declared' },
        { from: 'money', to: 'wizard-monarch', providers: ['wizard-monarch'], status: 'connected', origin: 'declared' },
        { from: 'card-benefits', to: 'transcriber', providers: ['transcriber'], status: 'connected', origin: 'declared' },
        { from: 'wizard-music', to: 'wizard-youtube-music', providers: ['wizard-youtube-music'], status: 'connected', origin: 'owner' },
        { from: 'backup', to: 'slot:backup-storage', providers: ['wizard-cloudflare', 's3'], status: 'connected', origin: 'declared' },
        { from: 'messenger', to: 'money', providers: ['money'], status: 'disconnected', origin: 'declared' },
        // cut while every app could talk: no app is designed to link them, so it changes nothing in Strict mode
        { from: 'backup', to: 'money', providers: ['money'], status: 'disconnected', origin: 'owner' },
        { from: 'wizard-claude', to: 'money', providers: ['money'], status: 'pending', origin: 'requested' },
      ],
    },
  );
  for (const a of d.apps) a.open = null;
  return d;
};

test('Linked apps: one row per app, by name, each link with a plain reason; a cut link stays in its row, off', () => {
  const m = mapModel(phoneBox());
  const rows = linkedApps(m);
  assert.deepEqual(
    rows.map((r) => r.line),
    ['Backup → Cloudflare and S3', 'Card Benefits → Transcriber', 'Messenger → Money (off)', 'Money → Monarch, SimpleFIN', 'Music → YouTube Music'],
  );
  // the cut link: in its app's row, off, with its own Turn on (the pair's Reconnect)
  const messenger = rows.find((r) => r.id === 'messenger');
  assert.deepEqual(
    messenger.links.map((l) => [l.name, l.on, l.added, l.pair]),
    [['Money', false, false, 'messenger|money']],
  );
  assert.equal(messenger.turnOn.pair, 'messenger|money');
  // a cut on a pair no app is designed to link (made in Open mode) is not a link here: not listed, not a Reconnect,
  // not drawn; Details names it, and the pair can still be connected
  assert.equal(rows.find((r) => r.id === 'backup').line, 'Backup → Cloudflare and S3');
  assert.deepEqual(
    m.idleCuts.map((x) => x.text),
    ['Backup ✕ Money'],
  );
  assert.ok(!m.cuts.some((p) => p.includes('backup')));
  assert.ok(connectable(m, 'backup').some((a) => a.id === 'money'));
  assert.deepEqual(mapModel({ ...phoneBox(), mode: 'open' }).idleCuts, []);
  assert.equal(rows.find((r) => r.id === 'money').turnOn, null);
  assert.equal(linkTag({ on: true, added: false }), '');
  // a link the app declares that the owner cut: off, not "added by you"
  const cut = phoneBox();
  cut.controls.find((r) => r.from === 'money' && r.to === 'wizard-monarch').status = 'disconnected';
  const moneyRow = linkedApps(mapModel(cut)).find((r) => r.id === 'money');
  assert.equal(moneyRow.line, 'Money → SimpleFIN, Monarch (off)');
  assert.equal(moneyRow.turnOn.pair, 'money|wizard-monarch');
  const money = rows.find((r) => r.id === 'money');
  assert.deepEqual(
    money.links.map((l) => [l.name, l.why, l.pair]),
    [
      ['Monarch', 'for bank balances', 'money|wizard-monarch'],
      ['SimpleFIN', 'for bank balances', 'money|wizard-simplefin'],
    ],
  );
  // no known job: who made the link instead
  assert.equal(rows.find((r) => r.id === 'card-benefits').links[0].why, 'declared by Card Benefits');
  assert.equal(rows.find((r) => r.id === 'wizard-music').links[0].why, 'connected with your OK');
  // a slot several apps provide is one link, disconnected as one, its reason from the slot
  const backup = rows.find((r) => r.id === 'backup').links;
  assert.equal(backup.length, 1);
  assert.deepEqual(backup[0].group, { from: 'backup', to: 'slot:backup-storage' });
  assert.equal(backup[0].why, 'to store backups');
  assert.equal(linkReason(m, { from: 'x', to: 'y', origin: 'requested' }, 'y'), 'allowed by you');
  // Open mode: everything is connected there, so only the pairs he cut are listed, off
  assert.deepEqual(
    linkedApps({ ...m, mode: 'open' }).map((r) => r.line),
    ['Messenger → Money (off)'],
  );
  assert.deepEqual(linkedApps({ ...m, mode: 'off' }), []);
});

test('Phone list: the agent row, the pairs cut by you, the waiting card and the last line', () => {
  const m = mapModel(phoneBox());
  const [hermes] = agentRows(m);
  assert.equal(hermes.title, 'Hermes');
  // every app, agent app and connector, plus Wizard's own pieces (it reaches everything through them)
  assert.equal(hermes.line, 'can use all 14 apps');
  assert.deepEqual(
    hermes.groups.map((g) => [g.title, g.items.length]),
    [
      ['Apps', 7],
      ['Agent apps', 1],
      ['Connectors', 6],
    ],
  );
  assert.ok(hermes.groups.flatMap((g) => g.items).every((x) => !x.off));
  // an agent whose lists leave some out says so, and its sheet marks those rows
  const d = phoneBox();
  d.agents = [
    { id: 'host-hermes', name: 'host Hermes', main: true },
    { id: 'helper', name: 'Helper', apps: ['money', 'backup'] },
  ];
  const two = agentRows(mapModel(d));
  assert.equal(two[1].title, 'Helper');
  assert.equal(two[1].line, 'can use 4 of 14 apps');
  assert.equal(two[1].groups.flatMap((g) => g.items).filter((x) => x.off).length, 10);
  assert.deepEqual(
    cutList(m).map((c) => [c.text, c.pair]),
    [['Messenger ✕ Money', 'messenger|money']],
  );
  assert.deepEqual(
    waitingLines(m).map((w) => w.text),
    ['Claude wants to use Money'],
  );
  assert.equal(footerLine('declared'), 'Everything else is kept apart.');
  assert.equal(footerLine('open'), 'Everything is connected unless you cut it.');
  // a cut slot connection is one row, reconnected as one
  const g = phoneBox();
  g.controls.find((r) => r.from === 'backup').status = 'disconnected';
  const gm = mapModel(g);
  const cuts = cutList(gm);
  assert.deepEqual(
    cuts.map((c) => c.text),
    ['Backup ✕ Cloudflare and S3', 'Messenger ✕ Money'],
  );
  assert.deepEqual(cuts[0].group, { from: 'backup', to: 'slot:backup-storage' });
  // and the words stay plain
  for (const w of [...linkedApps(m).flatMap((r) => [r.line, ...r.links.map((l) => l.why)]), hermes.line, footerLine('declared'), footerLine('open')])
    assert.doesNotMatch(w, /mesh|grant|drift|slot|network|gateway/i);
});

test('One list on every screen: the same sections in the same order; a wide pane only moves Linked apps aside', () => {
  const m = mapModel(phoneBox());
  const lay = listLayout(m);
  assert.equal(lay.lead, 'Apps are kept apart by default: they can’t talk to each other. These are the links they have.');
  // one Linked apps section: no separate list of cut pairs
  assert.deepEqual(
    lay.sections.map((s) => [s.key, s.title, s.count, s.side]),
    [
      ['wait', 'Waiting for your OK', null, 'main'],
      ['agent', 'Agents', null, 'main'],
      ['linked', 'Linked apps', 5, 'aside'],
    ],
  );
  assert.equal(lay.restore, true);
  // the rows are the phone list's rows, word for word
  assert.deepEqual(
    lay.sections[0].rows.map((r) => r.text),
    waitingLines(m).map((r) => r.text),
  );
  assert.deepEqual(
    lay.sections[2].rows.map((r) => r.line),
    linkedApps(m).map((r) => r.line),
  );
  assert.equal(lay.columns, 2);
  assert.equal(lay.footer, 'Everything else is kept apart.');
  // nothing waiting, nothing cut: those sections are left out, not shown empty
  const d = phoneBox();
  d.controls = d.controls.filter((r) => r.status === 'connected');
  assert.deepEqual(
    listLayout(mapModel(d)).sections.map((s) => s.key),
    ['agent', 'linked'],
  );
  // Open mode: the same sections; Linked apps holds the pairs he cut
  const open = listLayout({ ...m, mode: 'open' });
  assert.equal(open.lead, 'Your apps can all talk to each other. These are the pairs you cut.');
  assert.deepEqual(
    open.sections.find((s) => s.key === 'linked').rows.map((r) => r.line),
    ['Messenger → Money (off)'],
  );
  assert.equal(open.footer, 'Everything is connected unless you cut it.');
  // isolation off: nothing is linked or cut, no Linked apps and no Restore defaults
  const off = listLayout({ ...m, mode: 'off' });
  assert.ok(!off.sections.some((s) => s.key === 'linked'));
  assert.equal(off.columns, 1);
  assert.equal(off.restore, false);
  assert.equal(listLead('off'), 'All your apps can talk to each other. Keeping apps apart is turned off on this Wizard.');
  // no links at all: the section says so instead of disappearing
  const none = phoneBox();
  none.controls = [];
  const empty = listLayout(mapModel(none)).sections.find((s) => s.key === 'linked');
  assert.equal(empty.rows.length, 0);
  assert.equal(empty.empty, 'No links: every app is on its own.');
});

test('An app with no links can be connected from the list, without the map', () => {
  const m = mapModel(phoneBox());
  const pk = pickFirst(m);
  assert.equal(pk.label, 'Connect two apps');
  const ids = pk.apps.map((a) => a.id);
  // Messenger and S3 have no working links (Messenger's to Money was cut): both are offered
  assert.ok(ids.includes('messenger'));
  assert.ok(ids.includes('s3'));
  assert.ok(!linkedApps(m).some((r) => r.id === 's3'));
  assert.ok(
    linkedApps(m)
      .find((r) => r.id === 'messenger')
      .links.every((l) => !l.on),
  );
  // every app offered has someone to connect to, and the list is by name
  for (const a of pk.apps) assert.ok(connectable(m, a.id).length);
  assert.deepEqual(
    ids,
    [...pk.apps].sort((x, y) => x.name.localeCompare(y.name)).map((a) => a.id),
  );
  assert.deepEqual(listLayout(m).pick, pk);
  // Open mode cuts instead; isolation off has nothing to change
  assert.equal(pickFirst({ ...m, mode: 'open' }).label, 'Cut two apps apart');
  assert.equal(pickFirst({ ...m, mode: 'off' }), null);
  assert.equal(listLayout({ ...m, mode: 'off' }).pick, null);
  for (const w of [pk.label, pk.ask, pickFirst({ ...m, mode: 'open' }).ask]) assert.doesNotMatch(w, /mesh|grant|drift|slot|network|gateway/i);
});

test('The list is the main view at every width; the map only behind See map, dialogs close on Esc', () => {
  const css = readFileSync(new URL('../wizard-views/wizard-apps.css', import.meta.url), 'utf8');
  const js = readFileSync(new URL('../wizard-views/wizard-apps.js', import.meta.url), 'utf8');
  // no width hides the list or shows the map without See map
  assert.doesNotMatch(css, /\.wam-cl,\s*\n?\s*\.wam-cl-back\s*\{\s*display:\s*none/);
  assert.match(css, /\.wam-cm:not\(\.is-map\) > \.wam-cm-full,\s*\.wam-cm\.is-map > \.wam-cl \{\s*display: none;/);
  const topLevel = css.replace(/@container[^{]*\{(?:[^{}]*\{[^}]*\})*[^}]*\}/g, '');
  assert.match(topLevel, /\.wam-cl \{\s*display: flex;/);
  // the wide layout is the same sections placed in two columns
  assert.match(css, /@container \(min-width: 880px\)\s*\{\s*\.wam-cl\.is-two \{\s*display: grid;/);
  assert.match(js, /data-cm-choose/);
  assert.match(js, /window\.addEventListener\('keydown', el\._wamEsc, true\)/);
});

// The agents as the Agent Service has them (wam/agent_access.py → api/agents): one instance, Oscar on Hermes.
const access = (on, extra = {}) => ({
  service: true,
  agents: [
    {
      id: 'hermes',
      name: 'Oscar',
      harness: 'Hermes',
      model: 'claude-opus-5-5',
      provider: 'anthropic',
      main: true,
      private: false,
      on,
      known: [
        'money',
        'card-benefits',
        'messenger',
        'wizard-claude',
        'wizard-simplefin',
        'wizard-monarch',
        'wizard-music',
        'wizard-youtube-music',
        'transcriber',
        'backup',
        'wizard-cloudflare',
        's3',
        'wizard-agent-service',
      ],
      ...extra,
    },
  ],
});

test('Agents: each instance as "Oscar · Hermes", what it can use by group, everything on by default', () => {
  const d = phoneBox();
  d.apps.find((a) => a.id === 'wizard-agent-service').kind = 'connector';
  const m = mapModel(d);
  const all = access([
    'money',
    'card-benefits',
    'messenger',
    'wizard-claude',
    'wizard-simplefin',
    'wizard-monarch',
    'wizard-music',
    'wizard-youtube-music',
    'transcriber',
    'backup',
    'wizard-cloudflare',
    's3',
    'wizard-agent-service',
  ]);
  const [oscar] = agentList(m, all);
  assert.equal(agentName(oscar), 'Oscar · Hermes');
  assert.equal(oscar.line, 'Can use 5 apps, 1 agent app, 7 connectors');
  assert.deepEqual(
    oscar.groups.map((g) => [g.key, g.title, g.items.length, g.allOn]),
    [
      ['app', 'Apps', 5, true],
      ['agent-app', 'Agent apps', 1, true],
      ['connector', 'Connectors', 7, true],
    ],
  );
  // Wizard's own Agent Service is an item the agent uses (badge), the shell isn't something it can be given
  const conn = oscar.groups.find((g) => g.key === 'connector').items;
  assert.ok(conn.find((x) => x.id === 'wizard-agent-service').builtin);
  assert.ok(!oscar.groups.flatMap((g) => g.items).some((x) => x.id === 'wizard-ux'));
  // one item off: its switch is off, the group can Turn all on, the line says so
  const off = agentList(m, access(all.agents[0].on.filter((x) => x !== 'money')))[0];
  assert.equal(off.line, 'Can use 4 apps, 1 agent app, 7 connectors · 1 turned off');
  const apps = off.groups.find((g) => g.key === 'app');
  assert.equal(apps.allOn, false);
  assert.equal(apps.items.find((x) => x.id === 'money').on, false);
  assert.equal(canUseLine([]), 'Nothing to use yet');
  assert.match(agentNote(oscar), /^Everything is on unless you turn it off\./);
  // the Agents section uses the same rows
  assert.deepEqual(
    listLayout(m, all)
      .sections.find((s) => s.key === 'agent')
      .rows.map(agentName),
    ['Oscar · Hermes'],
  );
  for (const w of [oscar.line, off.line, agentNote(oscar)]) assert.doesNotMatch(w, /mesh|grant|drift|slot|network|gateway|harness/i);
});

test('Agents without the Agent Service: the report’s lists, read-only', () => {
  const m = mapModel(phoneBox());
  const [row] = agentList(m, null);
  assert.equal(row.live, false);
  assert.equal(row.line, 'Can use all 14 apps');
  assert.ok(row.groups.flatMap((g) => g.items).every((x) => x.on));
  assert.equal(agentList(m, { service: false, agents: [] })[0].live, false);
});

test('Restore defaults: the confirm dialog lists what changes, in plain words', () => {
  const w = restoreWords({
    changes: [
      { change: 'on', text: 'Messenger → Money will be turned back on.' },
      { change: 'remove', text: 'Movies → Shopping, which you added, will be removed.' },
    ],
  });
  assert.equal(w.title, 'Restore defaults?');
  assert.deepEqual(w.items, ['Messenger → Money will be turned back on.', 'Movies → Shopping, which you added, will be removed.']);
  assert.equal(w.go, 'Restore defaults');
  const none = restoreWords({ changes: [] });
  assert.equal(none.go, '');
  assert.match(none.says, /^Nothing to change/);
});
