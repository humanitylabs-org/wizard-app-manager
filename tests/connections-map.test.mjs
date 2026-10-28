// The Connections map's plain words (wizard-views/connections-map.js): what the owner reads must match the report.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  bandOf,
  bands,
  canUse,
  cutGroups,
  cutRows,
  cuttable,
  doneWords,
  reconnectRows,
  list,
  mapModel,
  pairRow,
  talksSentence,
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
  assert.equal(talksSentence(m.apps[2]), 'Files can talk to: no other app');
  assert.match(m.lead, /^Strict mode: each app talks only to the apps it asks for/);
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
  assert.equal(talksSentence(m.apps[0]), 'Backup can talk to: no other app');
  assert.deepEqual(pairRow(d.controls, 'wizard-cloudflare', 'backup', 'disconnected'), d.controls[0]);
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
  assert.match(m.lead, /except apps published through Runtipi, which can still reach each other/);
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

test('Groups: the agents on top, then apps, connectors and Wizard itself, each from the report kind', () => {
  const d = report(
    [
      app('cinequeue', 'Movies', [], { kind: 'app' }),
      app('wizard-monarch', 'Monarch', [], { kind: 'connector' }),
      app('transcriber', 'Transcriber', [], { kind: 'connector' }),
      app('old', 'Old App'), // an older App Manager without kinds: an app
    ],
    { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }] },
  );
  d.apps.find((a) => a.id === 'wizard-ux').kind = 'system';
  const m = mapModel(d);
  const g = bands(m);
  assert.deepEqual(
    g.map((b) => [b.key, b.title]),
    [
      ['agent', 'Agents'],
      ['app', 'Apps'],
      ['connector', 'Connectors'],
      ['system', 'System'],
    ],
  );
  assert.deepEqual(
    g[0].agents.map((a) => [a.name, a.real, a.main]),
    [['Your agent', 'host Hermes', true]],
  );
  assert.deepEqual(
    g[1].apps.map((a) => a.id),
    ['cinequeue', 'old'],
  );
  assert.deepEqual(
    g[2].apps.map((a) => a.id),
    ['wizard-monarch', 'transcriber'],
  );
  // the core without a kind is System too
  assert.deepEqual(
    g[3].system.map((a) => a.id),
    ['wizard-ux', 'wizard-agent-service'],
  );
  assert.equal(g[2].blurb, 'Background services your apps and agent use. Nothing to open day to day.');
  // every group says in one line what it is, and each sheet says which group
  for (const b of g) assert.ok(b.blurb.length > 10 && b.blurb.length < 90, b.key);
  assert.deepEqual(bandOf('connector').where, 'In Connectors');
  assert.deepEqual(bandOf('nonsense').where, 'In Apps');
  // a connector is still on the map like any app: it can be cut off from an app
  assert.ok(cuttable(m, 'wizard-monarch').some((a) => a.id === 'cinequeue'));
});

test('Agents: one "Your agent" when the box names none; what an agent can use is said truthfully', () => {
  const m = mapModel(report([app('cinequeue', 'Movies')]));
  assert.deepEqual(
    m.agents.map((a) => a.name),
    ['Your agent'],
  );
  assert.equal(bands(m)[0].key, 'agent');
  assert.equal(canUse(m.agents[0]), 'every app and connector');
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
      ['Your agent', 'every app and connector'],
      ['Private', '1 of your 3 apps and connectors'],
      ['Sealed', 'none of your apps or connectors'],
    ],
  );
  // the main agent's own lists (.main-agent.json) when its entry carries none; without lists, private apps are left out
  const listed = mapModel(report(apps, { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }], main_agent_apps: ['files'] }));
  assert.equal(canUse(listed.agents[0]), '1 of your 3 apps and connectors');
  const priv = report(apps, { agents: [{ id: 'host-hermes', name: 'host Hermes', main: true }] });
  priv.apps[0].privacy = 'private';
  assert.equal(canUse(mapModel(priv).agents[0]), '2 of your 3 apps and connectors');
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
  assert.equal(canUse(g[0].agents[0]), 'every app and connector');
});

test('A box with only Wizard itself still has its Agents and System groups', () => {
  const m = mapModel(report([]));
  assert.deepEqual(m.apps, []);
  assert.deepEqual(
    bands(m).map((b) => b.key),
    ['agent', 'system'],
  );
});

test('Groups: an agent app in the core is an agent, not System', () => {
  const d = report([app('cinequeue', 'Movies')], { gateway: { core: ['wizard-ux', 'wizard-agent-service', 'hermes-agent'] } });
  d.apps.push({ id: 'hermes-agent', name: 'Hermes', state: 'isolated', open: null, kind: 'agent' });
  const m = mapModel(d);
  assert.deepEqual(
    m.agents.map((a) => [a.id, a.name]),
    [['hermes-agent', 'Your agent']],
  );
  assert.deepEqual(
    m.system.map((a) => a.id),
    ['wizard-ux', 'wizard-agent-service'],
  );
});
