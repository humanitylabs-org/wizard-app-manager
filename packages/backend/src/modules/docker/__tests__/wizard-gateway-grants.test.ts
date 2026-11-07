import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { beforeEach, afterEach, describe, it, expect } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage } from '../wizard-storage';

// The Agent Service gateway as a layer-2 consumer (wizard-os spec/PERMISSIONS.md): apps that declare
// `mcp.checks_gateway` get a `host:agent-service` row; the gateway's key goes to its own 0600 file.
let root: string;
let installed: { appName: string; appStoreSlug: string }[];
let store: WizardStorage;
const sha = (k: string) => createHash('sha256').update(k).digest('hex');
const comp = { services: { main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 8080 } } } };
const data = (id: string) => path.join(root, 'app-data/wizardapps', id);
const appFile = async (id: string) => JSON.parse(await fs.readFile(path.join(data(id), 'wizard/grants.json'), 'utf8'));
const gwDir = () => path.join(data('wizard-agent-service'), 'data/wizard-os/grants');
const gwFile = () => path.join(gwDir(), 'agent-service.json');
const gw = async () => JSON.parse(await fs.readFile(gwFile(), 'utf8'));
const gatewayRows = async (id: string) => (await appFile(id)).incoming.filter((r: any) => r.from === 'host:agent-service');
async function install(id: string, fields: any = {}) {
  const dir = path.join(root, 'apps/wizardapps', id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify({ id, ...fields })}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(comp));
  installed.push({ appName: id, appStoreSlug: 'wizardapps' });
}
const checks = { mcp: { path: '/mcp', port: 8080, checks_gateway: true } };
async function service() {
  await install('wizard-agent-service', { mcp: { path: '/wizard/mcp', port: 8787 } });
  // The Service makes its own home on first start.
  await fs.mkdir(path.join(data('wizard-agent-service'), 'data/wizard-os'), { recursive: true });
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'gateway-grants-'));
  installed = [];
  store = new WizardStorage(
    { dataDir: root, appDataDir: path.join(root, 'app-data') },
    async () => installed,
    async () => ({ stdout: '' }),
  );
});
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

describe('gateway grants', () => {
  it('gives every app that checks the gateway a host:agent-service row and the gateway its key (0600 in a 0700 folder)', async () => {
    await service();
    for (const id of ['wizard-google', 'wizard-microsoft', 'wizard-mail', 'wizard-calendar']) await install(id, checks);
    await install('movies', { mcp: { path: '/mcp', port: 8080 } });
    await store.reconcile();

    const keys = (await gw()).outgoing;
    expect(Object.keys(keys).sort()).toEqual(['wizard-calendar', 'wizard-google', 'wizard-mail', 'wizard-microsoft']);
    for (const id of Object.keys(keys)) {
      expect(keys[id]).toMatch(/^[a-f0-9]{64}$/);
      expect(await gatewayRows(id)).toEqual([{ from: 'host:agent-service', sha256: sha(keys[id]) }]);
    }
    expect(new Set(Object.values(keys)).size).toBe(4);
    expect(await gatewayRows('movies')).toEqual([]);
    expect((await fs.stat(gwFile())).mode & 0o777).toBe(0o600);
    expect((await fs.stat(gwDir())).mode & 0o777).toBe(0o700);
    // The key never leaves the gateway file: not in the App Manager's state, not in any app's file.
    const state = JSON.stringify(await store.readGrants());
    for (const k of Object.values(keys)) {
      expect(state).not.toContain(k);
      expect(JSON.stringify(await appFile('wizard-mail'))).not.toContain(k);
    }
  });

  it('keeps keys across reconciles and restarts, and keeps an operator-written key and row (Messenger)', async () => {
    await service();
    await install('messenger');
    await install('wizard-mail', checks);
    const hand = 'a'.repeat(64);
    await fs.mkdir(gwDir(), { recursive: true, mode: 0o700 });
    await fs.writeFile(gwFile(), JSON.stringify({ outgoing: { messenger: hand }, note: 1 }), { mode: 0o600 });
    await fs.mkdir(path.join(data('messenger'), 'wizard'), { recursive: true });
    await fs.writeFile(
      path.join(data('messenger'), 'wizard/grants.json'),
      JSON.stringify({ outgoing: {}, incoming: [{ from: 'host:agent-service', sha256: sha(hand) }], enforce: false }),
    );
    await store.reconcile();
    const first = await gw();
    await store.reconcile();
    await store.reconcile();

    expect(await gw()).toEqual(first);
    expect(first.note).toBe(1);
    expect(first.outgoing.messenger).toBe(hand);
    expect(await gatewayRows('messenger')).toEqual([{ from: 'host:agent-service', sha256: sha(hand) }]);
    expect(await gatewayRows('wizard-mail')).toEqual([{ from: 'host:agent-service', sha256: sha(first.outgoing['wizard-mail']) }]);
  });

  it('adopts an existing valid key for an app that now declares the check, so its hand-written row keeps working', async () => {
    await service();
    await install('messenger', checks);
    const hand = 'b'.repeat(64);
    await fs.mkdir(gwDir(), { recursive: true, mode: 0o700 });
    await fs.writeFile(gwFile(), JSON.stringify({ outgoing: { messenger: hand } }), { mode: 0o600 });
    await store.reconcile();

    expect((await gw()).outgoing.messenger).toBe(hand);
    expect(await gatewayRows('messenger')).toEqual([{ from: 'host:agent-service', sha256: sha(hand) }]);
  });

  it('removes its own key and row when an app stops declaring the check or is removed', async () => {
    await service();
    await install('wizard-mail', checks);
    await install('wizard-google', checks);
    await store.reconcile();
    const google = (await gw()).outgoing['wizard-google'];

    await fs.writeFile(path.join(root, 'apps/wizardapps/wizard-mail/wizard.md'), `---\n${yaml.stringify({ id: 'wizard-mail' })}---\n`);
    installed = installed.filter((a) => a.appName !== 'wizard-google');
    await store.reconcile();

    expect((await gw()).outgoing).toEqual({});
    expect(await gatewayRows('wizard-mail')).toEqual([]);
    expect(google).toMatch(/^[a-f0-9]{64}$/);
  });

  it('brings an opened gateway file back to 0600 / 0700 on the next reconcile', async () => {
    await service();
    await install('wizard-mail', checks);
    await store.reconcile();
    await fs.chmod(gwDir(), 0o777);
    await fs.chmod(gwFile(), 0o777);
    await store.reconcile();

    expect((await fs.stat(gwFile())).mode & 0o777).toBe(0o600);
    expect((await fs.stat(gwDir())).mode & 0o777).toBe(0o700);
  });

  it('changes nothing without a started Agent Service, and never replaces a malformed or linked gateway file', async () => {
    await install('wizard-mail', checks);
    await store.reconcile();
    expect(await gatewayRows('wizard-mail')).toEqual([]);

    await service();
    await fs.mkdir(gwDir(), { recursive: true, mode: 0o700 });
    await fs.writeFile(gwFile(), 'not json', { mode: 0o600 });
    await store.reconcile();
    expect(await fs.readFile(gwFile(), 'utf8')).toBe('not json');
    expect(await gatewayRows('wizard-mail')).toEqual([]);

    const elsewhere = path.join(root, 'elsewhere.json');
    await fs.writeFile(elsewhere, '{"outgoing":{}}');
    await fs.rm(gwFile());
    await fs.symlink(elsewhere, gwFile());
    await store.reconcile();
    expect(await fs.readFile(elsewhere, 'utf8')).toBe('{"outgoing":{}}');
    expect(await gatewayRows('wizard-mail')).toEqual([]);
  });
});
