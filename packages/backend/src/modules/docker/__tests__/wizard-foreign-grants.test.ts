import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage } from '../wizard-storage';
import { reconcileAppGrants } from '../wizard-app-grants';

let root: string;
let installed: { appName: string; appStoreSlug: string }[];
let store: WizardStorage;
const consumer = 'sample-consumer';
const provider = 'sample-provider';
const target = 'slot:sample-data';
const key = 'fixture-external-key';
const endpoint = 'http://external-provider:8799';
const data = (id: string, slug = 'wizardapps') => path.join(root, 'app-data', slug, id);
const grantPath = (id = consumer, slug = 'wizardapps') => path.join(data(id, slug), 'wizard/grants.json');
const read = async () => JSON.parse(await fs.readFile(grantPath(), 'utf8'));
const original = (to = provider) => ({
  outgoing: { [to]: key },
  incoming: [{ from: 'unmanaged-gateway', sha256: 'f'.repeat(64) }],
  provider_endpoints: { [to]: endpoint },
  provider_endpoints_all: { [to]: { [provider]: endpoint, 'stale-provider': 'http://stale:8799' } as Record<string, string> },
  enforce: true,
  owner_extension: { retained: true },
});
async function install(id: string, slug = 'wizardapps', fields = {}) {
  const dir = path.join(root, 'apps', slug, id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify({ id, ...fields })}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify({
    services: { main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 8799 } } },
  }));
  installed.push({ appName: id, appStoreSlug: slug });
}
async function setup(to = provider) {
  await install(consumer);
  await install(provider, 'another-store', { provides: ['sample-data'] });
  await fs.mkdir(path.dirname(grantPath()), { recursive: true });
  await fs.writeFile(grantPath(), JSON.stringify(original(to)));
  const external = grantPath(provider, 'another-store');
  await fs.mkdir(path.dirname(external), { recursive: true });
  // A sentinel, deliberately not parseable as a managed grant file. Never read or rewrite it.
  await fs.writeFile(external, 'external store owns this document\n');
  return external;
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'foreign-grants-'));
  installed = [];
  store = new WizardStorage({ dataDir: root, appDataDir: path.join(root, 'app-data') }, async () => installed, async () => ({ stdout: '' }));
  await store.writeGrants({ schema: 1, updated_at: 0, seeded: [], grants: [], denied: [], mode: 'declared' });
});
afterEach(async () => { await fs.rm(root, { recursive: true, force: true }); });

describe('existing keys to providers managed by another store', () => {
  it.each([provider, target])('preserves the key and provider-qualified endpoint for %s on migration and restart', async (to) => {
    const external = await setup(to);
    await store.reconcile();
    await store.reconcile();
    const doc = await read();
    expect(doc.outgoing).toEqual({ [to]: key });
    expect(doc.provider_endpoints).toEqual({ [to]: endpoint });
    expect(doc.provider_endpoints_all).toEqual({ [to]: { [provider]: endpoint } });
    expect(doc.incoming).toEqual(original(to).incoming);
    expect(doc.enforce).toBe(true);
    expect(doc.owner_extension).toEqual({ retained: true });
    expect(await fs.readFile(external, 'utf8')).toBe('external store owns this document\n');
    expect(JSON.stringify(await store.readGrants())).not.toContain(key);
  });

  it.each([[provider, target], [target, provider]])('a deny of %s revokes the existing %s alias and survives restart', async (deny, to) => {
    await setup(to);
    await store.reconcile();
    await store.change({ kind: 'app', from: consumer, to: deny }, false);
    await store.reconcile();
    expect((await read()).outgoing).toEqual({});
    expect((await read()).provider_endpoints).toEqual({});
    expect((await read()).provider_endpoints_all).toEqual({});
  });

  it.each(['uninstalled', 'excluded', 'removing'])('drops keys/endpoints when the foreign provider is %s', async (how) => {
    const external = await setup();
    await store.reconcile();
    const state = await store.readGrants();
    if (how === 'uninstalled') installed = installed.filter((a) => a.appName !== provider);
    if (how === 'removing') state.layer2_removing = [provider];
    await reconcileAppGrants(await store.apps(), state, root, (f) => store.writeGrants(f), how === 'excluded' ? provider : undefined);
    expect((await read()).outgoing).toEqual({});
    expect((await read()).provider_endpoints).toEqual({});
    expect((await read()).provider_endpoints_all).toEqual({});
    expect(await fs.readFile(external, 'utf8')).toBe('external store owns this document\n');
  });

  it('never mints an uncheckable foreign key from a declaration or an owner approval', async () => {
    await setup();
    const doc = original();
    doc.outgoing = {};
    doc.provider_endpoints = {};
    doc.provider_endpoints_all = {};
    await fs.writeFile(grantPath(), JSON.stringify(doc));
    await store.change({ kind: 'app', from: consumer, to: provider }, true);
    await store.reconcile();
    expect((await read()).outgoing).toEqual({});
  });

  it('combines preserved foreign and freshly derived managed endpoints in a shared slot', async () => {
    await setup(target);
    await install('managed-provider', 'wizardapps', { provides: ['sample-data'] });
    await store.reconcile();
    const doc = await read();
    expect(doc.outgoing[target]).toBe(key);
    expect(doc.provider_endpoints_all[target]).toEqual({ [provider]: endpoint, 'managed-provider': 'http://main:8799' });
    const managed = JSON.parse(await fs.readFile(grantPath('managed-provider'), 'utf8'));
    expect(managed.incoming).toHaveLength(1);
    installed = installed.filter((a) => a.appName !== 'managed-provider');
    await store.reconcile();
    expect((await read()).provider_endpoints_all[target]).toEqual({ [provider]: endpoint });
  });

  it('adopts legacy mounts once and preserves post-upgrade writes across restart/rollback-shaped documents', async () => {
    await setup();
    await fs.rm(grantPath());
    const legacy = path.join(data(consumer), 'legacy/grants.json');
    await fs.mkdir(path.dirname(legacy), { recursive: true });
    await fs.writeFile(legacy, JSON.stringify(original()));
    const conf = path.join(root, 'user-config/wizardapps', consumer);
    await fs.mkdir(conf, { recursive: true });
    await fs.writeFile(path.join(conf, 'docker-compose.yml'), yaml.stringify({ services: { main: { volumes: [`${path.dirname(legacy)}:/wizard:ro`] } } }));
    await store.reconcile();
    const current = await read();
    current.outgoing[provider] = 'fixture-post-upgrade-write';
    current.provider_endpoints[provider] = 'http://new-owner-endpoint:8800';
    current.owner_extension = { retained: true, addedAfterUpgrade: true };
    // Previous releases use the same outgoing/endpoint shape; a rollback may omit
    // the additive qualified map. Re-upgrade must read current bytes, not a backup.
    delete current.provider_endpoints_all;
    await fs.writeFile(grantPath(), JSON.stringify(current));
    const restarted = new WizardStorage(store.dirs, async () => installed, async () => ({ stdout: '' }));
    await restarted.reconcile();
    await restarted.reconcile();
    expect((await read()).outgoing[provider]).toBe('fixture-post-upgrade-write');
    expect((await read()).provider_endpoints[provider]).toBe('http://new-owner-endpoint:8800');
    expect((await read()).owner_extension.addedAfterUpgrade).toBe(true);
    expect(JSON.parse(await fs.readFile(legacy, 'utf8'))).toEqual(await read());
  });

  it('migrates an already-adopted old installation without dropping its existing foreign grant', async () => {
    await setup();
    const state = await store.readGrants();
    state.layer2 = { adopted: [consumer] };
    await store.writeGrants(state);
    await store.reconcile();
    expect((await read()).outgoing[provider]).toBe(key);
    expect((await store.readGrants()).grants).toContainEqual(expect.objectContaining({ from: consumer, to: provider, approved: 'owner' }));
    await store.reconcile();
    expect((await read()).outgoing[provider]).toBe(key);
  });

  it.each([false, true])('Restore defaults revokes foreign access even after an interrupted reset (interrupted=%s)', async (interrupted) => {
    await setup();
    await store.reconcile();
    if (interrupted) {
      const write = store.writeGrants.bind(store);
      let saves = 0;
      const spy = vi.spyOn(store, 'writeGrants').mockImplementation(async (f) => {
        if (++saves === 2) throw new Error('fixture interrupted reset before physical revocation');
        return write(f);
      });
      try {
        await expect(store.resetConnections()).rejects.toThrow('fixture interrupted reset');
      } finally { spy.mockRestore(); }
      expect((await read()).outgoing[provider]).toBe(key);
    } else {
      await store.resetConnections();
    }
    await store.reconcile();
    await store.reconcile();
    expect((await read()).outgoing).toEqual({});
    expect((await read()).provider_endpoints).toEqual({});
    expect((await read()).provider_endpoints_all).toEqual({});
    expect((await store.readGrants()).grants.some((g) => g.from === consumer && g.to === provider)).toBe(false);
  });

  it.each([false, true])('replaces a removed qualified primary without resurrecting it in mixed slots (mixed=%s)', async (mixed) => {
    await setup(target);
    await install('second-provider', 'another-store', { provides: ['sample-data'] });
    if (mixed) await install('managed-provider', 'wizardapps', { provides: ['sample-data'] });
    const doc = original(target);
    doc.provider_endpoints_all[target] = { ...doc.provider_endpoints_all[target], 'second-provider': 'http://second-provider:8800' };
    await fs.writeFile(grantPath(), JSON.stringify(doc));
    await store.reconcile();
    installed = installed.filter((a) => a.appName !== provider);
    await store.reconcile();
    await store.reconcile();
    const now = await read();
    expect(now.outgoing[target]).toBe(key);
    expect(now.provider_endpoints[target]).not.toBe(endpoint);
    expect(Object.values(now.provider_endpoints_all[target])).toContain(now.provider_endpoints[target]);
    expect(now.provider_endpoints_all[target][provider]).toBeUndefined();
    expect(now.provider_endpoints_all[target]['second-provider']).toBe('http://second-provider:8800');
  });

  it('retries an interrupted metadata save without restoring stale keys over newer writes', async () => {
    await setup();
    const state = await store.readGrants();
    let calls = 0;
    await expect(reconcileAppGrants(await store.apps(), state, root, async (f) => {
      if (++calls === 2) throw new Error('fixture interrupted final metadata save');
      await store.writeGrants(f);
    })).rejects.toThrow('fixture interrupted');
    const current = await read();
    current.outgoing[provider] = 'fixture-write-after-interruption';
    await fs.writeFile(grantPath(), JSON.stringify(current));
    await store.reconcile();
    expect((await read()).outgoing[provider]).toBe('fixture-write-after-interruption');
  });
});
