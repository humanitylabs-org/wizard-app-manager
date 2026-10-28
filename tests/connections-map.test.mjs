// The Connections map's plain words (wizard-views/connections-map.js): what the owner reads must match the report.
import assert from 'node:assert/strict';
import test from 'node:test';

import { cuttable, doneWords, list, mapModel, pairRow, talksSentence } from '../wizard-views/connections-map.js';

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
