import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import 'reflect-metadata';
import { beforeEach, afterEach, describe, it, expect } from 'vitest';
import * as yaml from 'yaml';
import { GUARDS_METADATA } from '@nestjs/common/constants';
import { WizardStorage } from '../wizard-storage';
import { mountAppGrants } from '../wizard-app-grants';
import { WizardSharesController } from '../wizard-shares.controller';
import { AuthGuard } from '../../auth/auth.guard';

let root: string;
let installed: { appName: string; appStoreSlug: string }[];
let store: WizardStorage;
const comp = { services: { main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 8799 } } } };
const data = (id: string) => path.join(root, 'app-data/wizardapps', id);
const keys = async (id: string) => JSON.parse(await fs.readFile(path.join(data(id), 'wizard/grants.json'), 'utf8'));
async function install(id: string, fields: any = {}) {
  const dir = path.join(root, 'apps/wizardapps', id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify({ id, ...fields })}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(comp));
  if (!installed.some((a) => a.appName === id)) installed.push({ appName: id, appStoreSlug: 'wizardapps' });
}
const update = install; // an update replaces the installed recipe; the App Manager reconciles before the start
async function request(id: string, body: unknown) {
  await fs.mkdir(path.join(data(id), 'wizard-requests'), { recursive: true });
  await fs.writeFile(path.join(data(id), 'wizard-requests/connections.json'), JSON.stringify(body));
}
const state = () => store.connections();
const pair = (from: string, to: string) => expect.objectContaining({ from, to });

beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'conn-policy-'));
  installed = [];
  store = new WizardStorage(
    { dataDir: root, appDataDir: path.join(root, 'app-data') },
    async () => installed,
    async () => ({ stdout: '' }),
  );
  await install('resend-connector', { name: 'Resend' });
  await install('twenty-crm', { name: 'Twenty CRM' });
  await install('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm'] });
  await store.reconcile();
});
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

describe('connection policy: seamless by default, owner in control', () => {
  it('approves declared connections at install quietly', async () => {
    const s = await state();
    expect(s.grants).toEqual([expect.objectContaining({ from: 'carlos-campaigns', to: 'twenty-crm', approved: 'install' })]);
    expect(s.notices).toEqual([]);
    expect((await keys('carlos-campaigns')).outgoing['twenty-crm']).toMatch(/^[a-f0-9]{64}$/);
  });

  it('adds a connection an update declares automatically, with a quiet notice', async () => {
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    const s = await state();
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'carlos-campaigns', to: 'resend-connector', approved: 'install' }));
    expect(s.notices).toEqual([expect.objectContaining({ kind: 'added', text: 'Carlos Campaigns now connects to Resend.', delivered: false })]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeTruthy();
    await store.reconcile();
    expect((await state()).notices).toHaveLength(1); // once, not on every reconcile
  });

  it("keeps the owner's Disconnect across updates, reinstalls and runtime requests; Reconnect brings it back", async () => {
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'twenty-crm' }, false);
    expect((await keys('carlos-campaigns')).outgoing).toEqual({});
    await update('carlos-campaigns', { name: 'Carlos Campaigns', version: '2', uses: ['twenty-crm'] });
    await request('carlos-campaigns', { connections: ['twenty-crm'] });
    await store.collectRequests();
    await store.reconcile();
    let s = await state();
    expect(s.grants.filter((g) => g.from === 'carlos-campaigns')).toEqual([]);
    expect(s.denied).toEqual([pair('carlos-campaigns', 'twenty-crm')]);
    expect(s.notices).toEqual([]);
    expect((await keys('carlos-campaigns')).outgoing).toEqual({});
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'twenty-crm' }, true);
    s = await state();
    expect(s.denied).toEqual([]);
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'carlos-campaigns', to: 'twenty-crm', approved: 'owner' }));
    expect((await keys('carlos-campaigns')).outgoing['twenty-crm']).toBeTruthy();
  });

  it('a Disconnect by app id is not undone by a request for a slot that app provides', async () => {
    await install('resend-connector', { name: 'Resend', provides: ['email'] });
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'resend-connector' }, false);
    await request('carlos-campaigns', { connections: ['email', 'slot:email'] });
    await store.collectRequests();
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector', 'slot:email'] });
    await store.reconcile();
    const s = await state();
    expect(s.grants.filter((g) => g.from === 'carlos-campaigns').map((g) => g.to)).toEqual(['twenty-crm']);
    expect((await keys('carlos-campaigns')).outgoing).not.toHaveProperty('slot:email');
  });

  it('Disconnect cuts every name for the same provider (id and slot)', async () => {
    await install('resend-connector', { name: 'Resend', provides: ['email'] });
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await request('carlos-campaigns', { connections: ['slot:email'] });
    await store.collectRequests();
    expect(
      (await state()).grants
        .filter((g) => g.from === 'carlos-campaigns')
        .map((g) => g.to)
        .sort(),
    ).toEqual(['resend-connector', 'slot:email', 'twenty-crm']);
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'resend-connector' }, false);
    const s = await state();
    expect(s.grants.filter((g) => g.from === 'carlos-campaigns').map((g) => g.to)).toEqual(['twenty-crm']);
    expect(s.denied.map((g) => g.to).sort()).toEqual(['resend-connector', 'slot:email']);
    expect(Object.keys((await keys('carlos-campaigns')).outgoing)).toEqual(['twenty-crm']);
  });

  it('approves a runtime request automatically (the app asks in its own requests folder)', async () => {
    await request('carlos-campaigns', { connections: [{ to: 'resend-connector', why: 'send the campaign' }, 'carlos-campaigns', 'BAD id'] });
    expect(await store.collectRequests()).toBe(true);
    const s = await state();
    expect(s.grants).toContainEqual(
      expect.objectContaining({ from: 'carlos-campaigns', to: 'resend-connector', approved: 'request', reason: 'request' }),
    );
    expect(s.grants.filter((g) => g.to === 'carlos-campaigns')).toEqual([]); // never itself; malformed ids ignored
    expect(s.notices).toEqual([expect.objectContaining({ kind: 'requested', text: 'Carlos Campaigns now connects to Resend.' })]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeTruthy();
    expect(await store.collectRequests()).toBe(false); // unchanged file: no work
  });

  it('retries a runtime request whose reconcile failed', async () => {
    await request('carlos-campaigns', { connections: ['resend-connector'] });
    const real = store.reconcile.bind(store);
    store.reconcile = async () => {
      throw new Error('transient');
    };
    await expect(store.collectRequests()).rejects.toThrow('transient');
    store.reconcile = real;
    expect(await store.collectRequests()).toBe(true);
    expect((await state()).grants).toContainEqual(expect.objectContaining({ from: 'carlos-campaigns', to: 'resend-connector', approved: 'request' }));
  });

  it('ignores a requests file that is a symlink (it could point anywhere on the host)', async () => {
    const target = path.join(root, 'elsewhere.json');
    await fs.writeFile(target, JSON.stringify({ connections: ['resend-connector'] }));
    await fs.mkdir(path.join(data('carlos-campaigns'), 'wizard-requests'), { recursive: true });
    await fs.symlink(target, path.join(data('carlos-campaigns'), 'wizard-requests/connections.json'));
    await store.collectRequests();
    expect((await state()).grants.filter((g) => g.to === 'resend-connector')).toEqual([]);
  });

  it('keeps new connections TO a sensitive app pending with a notice; Approve links it', async () => {
    await store.setMode('declared'); // declared mode: every connection the app doesn't have yet is new
    await store.setSensitive('resend-connector', true);
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    let s = await state();
    expect(s.sensitive).toEqual(['resend-connector']);
    expect(s.pending).toEqual([pair('carlos-campaigns', 'resend-connector')]);
    expect(s.grants.filter((g) => g.to === 'resend-connector')).toEqual([]);
    expect(s.notices).toEqual([expect.objectContaining({ kind: 'pending', text: expect.stringContaining('wants to connect to Resend') })]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeUndefined();
    // existing connections to other apps are untouched
    expect((await keys('carlos-campaigns')).outgoing['twenty-crm']).toBeTruthy();
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'resend-connector' }, true);
    s = await state();
    expect(s.pending).toEqual([]);
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'carlos-campaigns', to: 'resend-connector', approved: 'owner' }));
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeTruthy();
  });

  it('a runtime request to a sensitive app waits too; Deny is sticky', async () => {
    await store.setMode('declared'); // declared mode: every connection the app doesn't have yet is new
    await store.setSensitive('resend-connector', true);
    await request('carlos-campaigns', { connections: ['resend-connector'] });
    await store.collectRequests();
    expect((await state()).pending).toEqual([pair('carlos-campaigns', 'resend-connector')]);
    await store.change({ kind: 'app', from: 'carlos-campaigns', to: 'resend-connector' }, false);
    expect((await state()).denied).toEqual([expect.objectContaining({ reason: 'request', approved: 'owner' })]); // still shown as asked-for
    await request('carlos-campaigns', { connections: ['resend-connector'], again: true });
    await store.collectRequests();
    await store.setSensitive('resend-connector', false);
    const s = await state();
    expect(s.pending).toEqual([]);
    expect(s.denied).toEqual([pair('carlos-campaigns', 'resend-connector')]);
    expect(s.grants.filter((g) => g.to === 'resend-connector')).toEqual([]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeUndefined();
  });

  it('a bare-slot runtime request to a sensitive provider stays one pending request with one notice', async () => {
    await store.setMode('declared');
    await install('notifier', { name: 'Notifier', provides: ['alerts'] });
    await store.setSensitive('notifier', true);
    await request('carlos-campaigns', { connections: ['alerts'] });
    await store.collectRequests();
    await store.reconcile();
    await store.reconcile();
    const s = await state();
    expect(s.pending).toEqual([pair('carlos-campaigns', 'slot:alerts')]);
    expect(s.notices.filter((n) => n.kind === 'pending')).toHaveLength(1);
  });

  it('turning Sensitive off approves what was waiting', async () => {
    await store.setSensitive('resend-connector', true);
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    await store.setSensitive('resend-connector', false);
    const s = await state();
    expect(s.pending).toEqual([]);
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'carlos-campaigns', to: 'resend-connector', approved: 'install' }));
  });

  it('a dropped declaration takes its automatic grant away; declaring it again is new (and noticed)', async () => {
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: [] });
    await store.reconcile();
    expect((await state()).grants).toEqual([]);
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm'] });
    await store.reconcile();
    expect((await state()).notices).toEqual([expect.objectContaining({ kind: 'added', text: 'Carlos Campaigns now connects to Twenty CRM.' })]);
  });

  it('records delivered notices; refuses malformed ids', async () => {
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    const [n] = (await state()).notices;
    await store.markNotices([String(n?.id)]);
    expect((await state()).notices[0]?.delivered).toBe(true);
    await expect(store.markNotices(['../x'])).rejects.toThrow();
    await expect(store.setSensitive('not-installed', true)).rejects.toThrow();
  });

  it('owner-only: every grant, sensitive and notice route sits behind the App Manager owner sign-in', () => {
    expect(Reflect.getMetadata(GUARDS_METADATA, WizardSharesController)).toEqual([AuthGuard]);
    const routes = Object.getOwnPropertyNames(WizardSharesController.prototype).filter((n) => n !== 'constructor');
    expect(routes).toEqual(expect.arrayContaining(['appChange', 'sensitive', 'notices', 'connections']));
    for (const r of routes) expect(Reflect.getMetadata(GUARDS_METADATA, (WizardSharesController.prototype as any)[r]) ?? []).toEqual([]);
  });

  it('mounts the app its own requests folder and refuses a recipe that claims the path', () => {
    const out = mountAppGrants({ services: { main: { volumes: ['data:/data'], environment: ['A=1'] } } });
    expect(out.services.main.volumes).toEqual([
      'data:/data',
      '${APP_DATA_DIR}/wizard:/wizard:ro',
      '${APP_DATA_DIR}/wizard-requests:/wizard-requests',
    ]);
    expect(out.services.main.environment).toContain('WIZARD_CONNECTION_REQUESTS=/wizard-requests/connections.json');
    expect(mountAppGrants(out)).toEqual(out);
    expect(() => mountAppGrants({ services: { main: { volumes: ['/etc:/wizard-requests'] } } })).toThrow(/Reserved/);
  });
});

describe('open mode (the default): everything connected unless the owner disconnects it', () => {
  const opens = async (to: string) =>
    (await state()).grants
      .filter((g) => g.to === to && g.approved === 'open')
      .map((g) => g.from)
      .sort();

  it('is the default and the owner can switch it; anything else is refused', async () => {
    expect((await state()).mode).toBe('open');
    expect((await store.setMode('declared')).mode).toBe('declared');
    expect((await state()).mode).toBe('declared');
    expect((await store.setMode('open')).mode).toBe('open');
    await expect(store.setMode('everything' as any)).rejects.toThrow(/Invalid connection mode/);
    const c = new WizardSharesController({ storage: store } as any);
    await expect(c.mode({ mode: 'all' })).rejects.toThrow(/open or declared/);
    expect((await c.mode({ mode: 'declared' })).mode).toBe('declared');
  });

  it('marking an app sensitive keeps the connections it has: no request waits, no key is minted', async () => {
    await store.setSensitive('resend-connector', true);
    const s = await state();
    expect(await opens('resend-connector')).toEqual(['carlos-campaigns', 'twenty-crm']);
    expect(s.pending).toEqual([]);
    expect(s.notices).toEqual([]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeUndefined();
    // declaring a connection it already has is not new: an ordinary grant (with its key), never a wait
    await update('carlos-campaigns', { name: 'Carlos Campaigns', uses: ['twenty-crm', 'resend-connector'] });
    await store.reconcile();
    expect((await state()).pending).toEqual([]);
    expect((await keys('carlos-campaigns')).outgoing['resend-connector']).toBeTruthy();
  });

  it('a newly installed app waits for the owner before it reaches a sensitive app; Approve / Deny decide', async () => {
    await store.setSensitive('resend-connector', true);
    await install('stripe-connector', { name: 'Stripe' });
    await install('zoom-connector', { name: 'Zoom' });
    await store.reconcile();
    await store.reconcile();
    let s = await state();
    expect(s.pending.map((g) => `${g.from}>${g.to}:${g.reason}`).sort()).toEqual([
      'stripe-connector>resend-connector:open',
      'zoom-connector>resend-connector:open',
    ]);
    expect(s.notices.filter((n) => n.kind === 'pending')).toHaveLength(2); // once each
    await store.change({ kind: 'app', from: 'stripe-connector', to: 'resend-connector' }, true);
    await store.change({ kind: 'app', from: 'zoom-connector', to: 'resend-connector' }, false);
    s = await state();
    expect(s.pending).toEqual([]);
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'stripe-connector', to: 'resend-connector', approved: 'owner' }));
    expect(s.denied).toEqual([pair('zoom-connector', 'resend-connector')]);
    await store.reconcile();
    expect((await state()).pending).toEqual([]); // decided: nothing waits again
  });

  it('turning Sensitive off forgets the kept connections and what was waiting (open mode connects them anyway)', async () => {
    await store.setSensitive('resend-connector', true);
    await install('stripe-connector', { name: 'Stripe' });
    await store.reconcile();
    expect((await state()).pending).toHaveLength(1);
    await store.setSensitive('resend-connector', false);
    const s = await state();
    expect(s.pending).toEqual([]);
    expect(await opens('resend-connector')).toEqual([]);
    expect(s.grants.filter((g) => g.to === 'resend-connector')).toEqual([]);
  });

  it('switching to declared drops open-mode waits; the kept connections stay recorded but never link there', async () => {
    await store.setSensitive('resend-connector', true);
    await install('stripe-connector', { name: 'Stripe' });
    await store.reconcile();
    await store.setMode('declared');
    const s = await state();
    expect(s.pending).toEqual([]);
    expect(await opens('resend-connector')).toEqual(['carlos-campaigns', 'twenty-crm']);
  });

  it("a sensitive app's own new connection to an app that may not reach it waits too (the network is two-way)", async () => {
    await install('stripe-connector', { name: 'Stripe' });
    await store.reconcile(); // stripe is an existing app now
    await store.setSensitive('resend-connector', true); // stripe keeps its connection to it
    await install('zoom-connector', { name: 'Zoom' });
    await store.reconcile();
    await store.change({ kind: 'app', from: 'zoom-connector', to: 'resend-connector' }, false); // zoom may not reach it
    await update('resend-connector', { name: 'Resend', uses: ['stripe-connector', 'zoom-connector'] });
    await store.reconcile();
    let s = await state();
    // stripe already reaches it: an ordinary grant; zoom doesn't: the Disconnect stands, nothing is added
    expect(s.grants).toContainEqual(expect.objectContaining({ from: 'resend-connector', to: 'stripe-connector', approved: 'install' }));
    expect(s.grants.filter((g) => g.from === 'resend-connector' && g.to === 'zoom-connector')).toEqual([]);
    await store.change({ kind: 'app', from: 'zoom-connector', to: 'resend-connector' }, true); // reconnect, then
    await install('calendly-connector', { name: 'Calendly' }); // a new app: its pair with resend waits
    await store.reconcile();
    await update('resend-connector', { name: 'Resend', uses: ['stripe-connector', 'calendly-connector'] });
    await store.reconcile();
    s = await state();
    expect(s.pending.map((g) => `${g.from}>${g.to}`).sort()).toEqual(['calendly-connector>resend-connector', 'resend-connector>calendly-connector']);
    expect(s.notices.map((n) => n.text)).toContain(
      'Resend, which you marked sensitive, wants to connect to Calendly. Approve or deny it in App Manager → Connections.',
    );
    expect(s.grants.filter((g) => g.from === 'resend-connector' && g.to === 'calendly-connector')).toEqual([]);
    // Approving one direction opens the two-way pair: the other direction's wait is retired, not left to be denied
    await store.change({ kind: 'app', from: 'resend-connector', to: 'calendly-connector' }, true);
    await store.reconcile();
    s = await state();
    expect(s.pending).toEqual([]);
    expect(s.denied).toEqual([]);
  });

  it('refuses Sensitive for the shell, Agent Service and agent (the gateway reaches every app)', async () => {
    await install('wizard-agent-service', { name: 'Agent Service', provides: ['agent-service'] });
    await expect(store.setSensitive('wizard-agent-service', true)).rejects.toThrow(/can’t be marked sensitive/);
    expect((await state()).sensitive).toEqual([]);
    expect((await store.setSensitive('wizard-agent-service', false)).sensitive).toEqual([]); // turning it off always works
  });

  it('the mode route sits behind the owner sign-in like the others', () => {
    expect(Reflect.getMetadata(GUARDS_METADATA, WizardSharesController)).toEqual([AuthGuard]);
    expect(Object.getOwnPropertyNames(WizardSharesController.prototype)).toContain('mode');
  });
});
