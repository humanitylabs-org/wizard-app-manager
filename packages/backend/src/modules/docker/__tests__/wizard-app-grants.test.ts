import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { beforeEach, afterEach, describe, it, expect } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage } from '../wizard-storage';
import { mountAppGrants, providerEndpoint } from '../wizard-app-grants';

let root: string;
let installed: { appName: string; appStoreSlug: string }[];
let store: WizardStorage;
const comp = {
  services: {
    main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 8799 }, networks: { main: { aliases: ['wizard-connectors-calendar'] } } },
  },
};
const data = (id: string) => path.join(root, 'app-data/wizardapps', id);
const file = async (id: string) => JSON.parse(await fs.readFile(path.join(data(id), 'wizard/grants.json'), 'utf8'));
async function install(id: string, fields: any = {}, compose: any = comp) {
  const dir = path.join(root, 'apps/wizardapps', id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify({ id, ...fields })}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(compose));
  installed.push({ appName: id, appStoreSlug: 'wizardapps' });
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'app-grants-'));
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
describe('app grants layer 2', () => {
  it('seeds uses with unique 32-byte keys, endpoints, hashes; boot/restart retains keys and mirrors only metadata', async () => {
    await install('wizard-connectors');
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    await fs.mkdir(path.join(root, 'state/wizard-os'), { recursive: true });
    await fs.writeFile(path.join(root, 'state/wizard-os/apps.json'), '{"apps":[],"other":1}');
    await store.reconcile();
    const c = await file('wizard-calendar');
    const key = c.outgoing['wizard-connectors'];
    expect(Buffer.from(key, 'hex')).toHaveLength(32);
    expect(c.provider_endpoints['wizard-connectors']).toBe('http://wizard-connectors-calendar:8799');
    expect((await file('wizard-connectors')).incoming).toEqual([{ from: 'wizard-calendar', sha256: createHash('sha256').update(key).digest('hex') }]);
    await store.reconcile();
    expect((await file('wizard-calendar')).outgoing).toEqual(c.outgoing);
    expect(JSON.stringify(await store.readGrants())).not.toContain(key);
    const mirror = JSON.parse(await fs.readFile(path.join(root, 'state/wizard-os/apps.json'), 'utf8'));
    expect(mirror.other).toBe(1);
    expect(mirror.grants).toEqual((await store.readGrants()).grants);
    expect(JSON.stringify(mirror)).not.toContain(key);
    const shared = path.join(root, 'app-data/wizard-os');
    await fs.mkdir(shared, { recursive: true });
    await fs.writeFile(path.join(shared, 'apps.json'), '{"apps":[],"other":2}');
    await store.reconcile();
    expect(JSON.parse(await fs.readFile(path.join(shared, 'apps.json'), 'utf8')).grants).toEqual((await store.readGrants()).grants);
  });
  it('revoke and regrant change both files immediately without Docker/restart; denial survives boot and reinstall', async () => {
    await install('wizard-connectors');
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    await store.reconcile();
    const old = (await file('wizard-calendar')).outgoing['wizard-connectors'];
    const g = { kind: 'app', from: 'wizard-calendar', to: 'wizard-connectors' };
    await store.change(g, false);
    await store.reconcile();
    expect((await file('wizard-connectors')).incoming).toEqual([]);
    expect((await file('wizard-calendar')).outgoing).toEqual({});
    await store.remove('wizard-calendar');
    await store.reconcile();
    expect((await store.readGrants()).denied).toEqual([expect.objectContaining(g)]);
    await store.change(g, true);
    expect((await file('wizard-calendar')).outgoing['wizard-connectors']).not.toBe(old);
    expect((await file('wizard-connectors')).incoming).toHaveLength(1);
  });
  it('consumer first, provider later; denied proposals never return', async () => {
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    await store.reconcile();
    expect((await file('wizard-calendar')).outgoing).toEqual({});
    await install('wizard-connectors');
    await store.reconcile();
    expect((await store.readGrants()).grants).toEqual([
      expect.objectContaining({ from: 'wizard-calendar', to: 'wizard-connectors', approved: 'install' }),
    ]);
    await store.change({ kind: 'app', from: 'wizard-calendar', to: 'wizard-connectors' }, false);
    await store.reconcile();
    expect(await store.proposed()).toEqual([]);
  });
  it('requires and slot swap preserve slot key, no agent defaults or self grants', async () => {
    await install('router-old', { provides: ['connection-router'] });
    await install('consumer-one', { requires: { models: ['chat'] } });
    await install('hermes-agent', { category: 'agent', mcp: { path: '/mcp' } });
    await store.reconcile();
    const key = (await file('consumer-one')).outgoing['slot:connection-router'];
    await install('router-new', { provides: ['connection-router'] });
    installed = installed.filter((a) => a.appName !== 'router-old');
    await store.reconcile();
    expect((await file('consumer-one')).outgoing['slot:connection-router']).toBe(key);
    expect((await file('hermes-agent')).outgoing).toEqual({});
    expect((await file('router-new')).incoming).toHaveLength(1);
  });
  it('uninstall removes both directions, does not re-add while DB deletion is pending', async () => {
    await install('wizard-connectors');
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    await store.reconcile();
    await store.remove('wizard-calendar');
    await store.reconcile();
    expect((await store.readGrants()).grants).toEqual([]);
    expect((await file('wizard-connectors')).incoming).toEqual([]);
  });
  it('adopts hand-written calendar bridge and router shape, keys, enforce and unmanaged gateway rows; revoke updates old mounts', async () => {
    await install('wizard-connectors');
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    const key = 'fixture-not-a-live-key';
    const digest = createHash('sha256').update(key).digest('hex');
    for (const [id, role] of [
      ['wizard-calendar', 'consumer'],
      ['wizard-connectors', 'provider'],
    ]) {
      const bridge = path.join(root, 'repos/wizardapps/apps/.calendar-grants', role!);
      await fs.mkdir(bridge, { recursive: true });
      const doc = {
        outgoing: role === 'consumer' ? { 'wizard-connectors': key } : {},
        incoming: role === 'provider' ? [{ from: 'calendar', sha256: digest }] : [],
        provider_endpoints: role === 'consumer' ? { 'wizard-connectors': 'http://wizard-connectors-calendar:8799' } : {},
        enforce: true,
      };
      await fs.writeFile(path.join(bridge, 'grants.json'), JSON.stringify(doc));
      const conf = path.join(root, 'user-config/wizardapps', id!);
      await fs.mkdir(conf, { recursive: true });
      await fs.writeFile(path.join(conf, 'docker-compose.yml'), yaml.stringify({ services: { main: { volumes: [`${bridge}:/wizard:ro`] } } }));
    }
    await install('wizard-connection-router');
    await install('wizard-claude');
    await install('wizard-tinfoil');
    await fs.mkdir(path.join(data('wizard-connection-router'), 'wizard'), { recursive: true });
    const router = {
      outgoing: { 'wizard-claude': 'fixture-router-claude-key', 'wizard-tinfoil': 'fixture-router-tinfoil-key' },
      incoming: [
        { from: 'wisdom', sha256: 'f'.repeat(64) },
        { from: 'wizard-recorder', sha256: 'e'.repeat(64) },
      ],
      provider_endpoints: {
        'wizard-claude': 'http://127.0.0.1:18799',
        'wizard-chatgpt': 'http://127.0.0.1:18801',
        'wizard-tinfoil': 'http://127.0.0.1:18804',
      },
      enforce: true,
    };
    await fs.writeFile(path.join(data('wizard-connection-router'), 'wizard/grants.json'), JSON.stringify(router));
    await store.reconcile();
    expect((await file('wizard-calendar')).outgoing['wizard-connectors']).toBe(key);
    expect((await file('wizard-connectors')).incoming).toEqual([
      { from: 'calendar', sha256: digest },
      { from: 'wizard-calendar', sha256: digest },
    ]);
    expect((await file('wizard-connection-router')).incoming).toEqual(router.incoming);
    expect((await file('wizard-connection-router')).outgoing).toEqual(router.outgoing);
    expect((await file('wizard-connection-router')).provider_endpoints).toEqual(router.provider_endpoints);
    expect((await file('wizard-connection-router')).enforce).toBe(true);
    expect(Object.keys(await file('wizard-connection-router')).sort()).toEqual(Object.keys(router).sort());
    await store.change({ kind: 'app', from: 'wizard-calendar', to: 'wizard-connectors' }, false);
    const legacy = JSON.parse(await fs.readFile(path.join(root, 'repos/wizardapps/apps/.calendar-grants/provider/grants.json'), 'utf8'));
    expect(legacy.incoming).toEqual([]);
    expect(legacy.enforce).toBe(true);
  });
  it('mounts dir read-only and sets env on EVERY service, never changes networks; is idempotent', () => {
    const source = {
      services: { ui: { environment: ['EXISTING=yes'], networks: ['main'] }, broker: { environment: { EXISTING: 'yes' }, network_mode: 'none' } },
      networks: { main: {} },
    };
    const out = mountAppGrants(source);
    for (const svc of Object.values(out.services) as any[]) expect(svc.volumes).toContain('${APP_DATA_DIR}/wizard:/wizard:ro');
    expect(out.services.ui.environment).toContain('WIZARD_GRANTS_FILE=/wizard/grants.json');
    expect(out.services.broker.environment.WIZARD_GRANTS_FILE).toBe('/wizard/grants.json');
    expect(out.services.ui.networks).toEqual(source.services.ui.networks);
    expect(out.networks).toEqual(source.networks);
    expect(mountAppGrants(out)).toEqual(out);
    expect(source.services.ui).not.toHaveProperty('volumes');
  });
  it('updates derived endpoints after provider update, but preserves owner overrides', async () => {
    await install('wizard-connectors');
    await install('wizard-calendar', { uses: ['wizard-connectors'] });
    await store.reconcile();
    const pathToManifest = path.join(root, 'apps/wizardapps/wizard-connectors/wizard.md');
    await fs.writeFile(pathToManifest, `---\n${yaml.stringify({ id: 'wizard-connectors', api: { endpoint: 'http://changed:8798' } })}---\n`);
    await store.reconcile();
    expect((await file('wizard-calendar')).provider_endpoints['wizard-connectors']).toBe('http://changed:8798');
    const doc = await file('wizard-calendar');
    doc.provider_endpoints['wizard-connectors'] = 'http://owner:8797';
    await fs.writeFile(path.join(data('wizard-calendar'), 'wizard/grants.json'), JSON.stringify(doc));
    await store.reconcile();
    await store.reconcile();
    expect((await file('wizard-calendar')).provider_endpoints['wizard-connectors']).toBe('http://owner:8797');
  });
  it('rejects malformed files rather than erasing valid keys and respects explicit endpoint', async () => {
    await install('wizard-connectors');
    await store.reconcile();
    await fs.writeFile(path.join(data('wizard-connectors'), 'wizard/grants.json'), '{');
    await expect(store.reconcile()).rejects.toThrow();
    const app = (await store.apps())[0]!;
    app.manifest.api = { endpoint: 'http://ingress:8799' };
    expect(providerEndpoint(app)).toBe('http://ingress:8799');
    delete app.manifest.api;
    app.compose.networks = { main: { external: true, name: '${WIZARD_MAIN_NETWORK:-runtipi_tipi_main_network}' } };
    expect(providerEndpoint(app)).toBe('http://wizard-connectors_wizardapps-main-1:8799');
    app.manifest.api = { endpoint: 'http://secret@ingress:8799' };
    expect(() => providerEndpoint(app)).toThrow();
  });
});
