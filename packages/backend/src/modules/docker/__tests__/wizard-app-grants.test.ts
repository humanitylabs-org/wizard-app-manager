import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { beforeEach, afterEach, describe, it, expect } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage } from '../wizard-storage';
import { mountAppGrants, providerEndpoint } from '../wizard-app-grants';
import crossStore from './fixtures/grants-cross-store.json';

let root: string;
let installed: { appName: string; appStoreSlug: string }[];
let store: WizardStorage;
const comp = {
  services: {
    main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 8799 }, networks: { main: { aliases: ['wizard-connectors-calendar'] } } },
  },
};
const data = (id: string, slug = 'wizardapps') => path.join(root, 'app-data', slug, id);
const file = async (id: string, slug = 'wizardapps') => JSON.parse(await fs.readFile(path.join(data(id, slug), 'wizard/grants.json'), 'utf8'));
async function install(id: string, fields: any = {}, compose: any = comp, slug = 'wizardapps') {
  const dir = path.join(root, 'apps', slug, id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify({ id, ...fields })}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(compose));
  installed.push({ appName: id, appStoreSlug: slug });
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
  it('adopts the cross-store owner topology without slot replacement or key/endpoint/hash loss', async () => {
    // Shapes match the private migration checkpoint; all keys/hashes are dummy.
    // Status is included deliberately: stopped installed providers still exist.
    for (const { id, store: slug, status, ...fields } of crossStore.apps) {
      await install(id, { ...fields, status }, comp, slug);
    }
    for (const [id, doc] of [
      ['wizard-connection-router', crossStore.router],
      ['wizard-tinfoil', crossStore.tinfoil],
    ] as const) {
      await fs.mkdir(path.join(data(id), 'wizard'), { recursive: true });
      await fs.writeFile(path.join(data(id), 'wizard/grants.json'), JSON.stringify(doc));
    }
    const bridgeRoot = path.join(root, 'repos/wizardapps/apps/.calendar-grants');
    // The core provider keeps its own legacy /run/router-grants mount.
    const claudeLegacy = path.join(data('wizard-claude', 'core'), 'router-grants');
    await fs.mkdir(claudeLegacy, { recursive: true });
    await fs.writeFile(path.join(claudeLegacy, 'grants.json'), JSON.stringify(crossStore.claude));
    const claudeConfig = path.join(root, 'user-config/core/wizard-claude');
    await fs.mkdir(claudeConfig, { recursive: true });
    await fs.writeFile(
      path.join(claudeConfig, 'docker-compose.yml'),
      yaml.stringify({
        services: {
          'wizard-claude-engine': {
            environment: { WIZARD_GRANTS_FILE: '/run/router-grants/grants.json' },
            volumes: [`${claudeLegacy}:/run/router-grants:ro`],
          },
        },
      }),
    );
    for (const [id, role, doc] of [
      ['wizard-calendar', 'consumer', crossStore.calendar],
      ['wizard-connectors', 'provider', crossStore.connectors],
    ] as const) {
      const bridge = path.join(bridgeRoot, role);
      await fs.mkdir(bridge, { recursive: true });
      await fs.writeFile(path.join(bridge, 'grants.json'), JSON.stringify(doc));
      const config = path.join(root, 'user-config/wizardapps', id);
      await fs.mkdir(config, { recursive: true });
      await fs.writeFile(path.join(config, 'docker-compose.yml'), yaml.stringify({ services: { [id]: { volumes: [`${bridge}:/wizard:ro`] } } }));
    }
    for (let boot = 0; boot < 2; boot++) {
      await store.reconcile();
      expect(await file('wizard-connection-router')).toEqual(crossStore.router);
      expect(await file('wizard-tinfoil')).toMatchObject({ ...crossStore.tinfoil, outgoing: {} });
      expect((await file('wizard-tinfoil')).incoming).toEqual(crossStore.tinfoil.incoming);
      expect(await file('wizard-calendar')).toEqual(crossStore.calendar);
      const connectors = await file('wizard-connectors');
      expect(connectors.incoming).toEqual([
        ...crossStore.connectors.incoming,
        { from: 'wizard-calendar', sha256: crossStore.connectors.incoming[0]?.sha256 },
      ]);
      expect(await file('wizard-claude', 'core')).toMatchObject({
        incoming: [
          { from: 'wizard-connection-router', sha256: createHash('sha256').update(crossStore.router.outgoing['wizard-claude']).digest('hex') },
        ],
      });
      expect((await file('wizard-chatgpt', 'core')).incoming).toEqual([]);
      expect(JSON.parse(await fs.readFile(path.join(claudeLegacy, 'grants.json'), 'utf8'))).toEqual({
        ...crossStore.claude,
        outgoing: {},
        provider_endpoints: {},
      });
      const grants = (await store.readGrants()).grants;
      expect(grants).toHaveLength(2);
      expect(grants).toContainEqual(expect.objectContaining({ from: 'wizard-connection-router', to: 'wizard-claude', approved: 'owner' }));
      expect(grants).toContainEqual(expect.objectContaining({ from: 'wizard-calendar', to: 'wizard-connectors', approved: 'owner' }));
      expect(grants.some((g) => g.to === 'slot:ai-connection')).toBe(false);
      expect(await store.proposed()).toEqual([]);
      expect(JSON.parse(await fs.readFile(path.join(bridgeRoot, 'consumer/grants.json'), 'utf8'))).toEqual(crossStore.calendar);
      expect(JSON.parse(await fs.readFile(path.join(bridgeRoot, 'provider/grants.json'), 'utf8'))).toEqual(connectors);
    }
    // Explicit revoke must still remove both the adopted direct key and hash.
    await store.change({ kind: 'app', from: 'wizard-connection-router', to: 'wizard-claude' }, false);
    expect((await file('wizard-connection-router')).outgoing).not.toHaveProperty('wizard-claude');
    expect((await file('wizard-claude', 'core')).incoming).toEqual([]);
    expect(JSON.parse(await fs.readFile(path.join(claudeLegacy, 'grants.json'), 'utf8')).incoming).toEqual([]);
    expect((await file('wizard-tinfoil')).incoming).toEqual(crossStore.tinfoil.incoming);
  });
  it('defaults only missing outgoing and preserves provider-only extension fields and verification rows', async () => {
    await install('wizard-tinfoil');
    const doc = {
      ...crossStore.tinfoil,
      owner: { note: '  literal\\nvalue  ', enabled: false },
      incoming: [{ ...crossStore.tinfoil.incoming[0], owner_extension: { scope: 'original' } }],
    };
    await fs.mkdir(path.join(data('wizard-tinfoil'), 'wizard'), { recursive: true });
    await fs.writeFile(path.join(data('wizard-tinfoil'), 'wizard/grants.json'), JSON.stringify(doc));
    await store.reconcile();
    expect(await file('wizard-tinfoil')).toEqual({ ...doc, outgoing: {}, provider_endpoints: {} });
  });
  it.each([null, [], '', 7, false, { provider: 7 }, { provider: '' }])(
    'rejects malformed outgoing %j without changing the file',
    async (outgoing) => {
      await install('wizard-tinfoil');
      const target = path.join(data('wizard-tinfoil'), 'wizard/grants.json');
      await fs.mkdir(path.dirname(target), { recursive: true });
      const bytes = JSON.stringify({ ...crossStore.tinfoil, outgoing });
      await fs.writeFile(target, bytes);
      await expect(store.reconcile()).rejects.toThrow('Invalid app grants file');
      expect(await fs.readFile(target, 'utf8')).toBe(bytes);
    },
  );
  it.each([null, [], '', 7, { provider: false }])('rejects malformed endpoint maps %j', async (provider_endpoints) => {
    await install('wizard-tinfoil');
    const target = path.join(data('wizard-tinfoil'), 'wizard/grants.json');
    await fs.mkdir(path.dirname(target), { recursive: true });
    const bytes = JSON.stringify({ ...crossStore.tinfoil, provider_endpoints });
    await fs.writeFile(target, bytes);
    await expect(store.reconcile()).rejects.toThrow('Invalid app grants file');
    expect(await fs.readFile(target, 'utf8')).toBe(bytes);
  });
  it('keeps unresolved and opaque owner outgoing keys and endpoints until explicit revoke', async () => {
    await install('consumer-one', {}, comp, 'core');
    const target = path.join(data('consumer-one', 'core'), 'wizard/grants.json');
    const doc = {
      outgoing: { 'missing-provider': 'dummy-missing-key', 'owner.alias': 'dummy-opaque-key' },
      incoming: [],
      provider_endpoints: { 'missing-provider': 'http://owner:42', 'owner.alias': 'http://owner:43' },
      enforce: true,
    };
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.writeFile(target, JSON.stringify(doc));
    await store.reconcile();
    await store.reconcile();
    expect(await file('consumer-one', 'core')).toEqual(doc);
    await store.change({ kind: 'app', from: 'consumer-one', to: 'missing-provider' }, false);
    expect(await file('consumer-one', 'core')).toMatchObject({ outgoing: { 'owner.alias': 'dummy-opaque-key' } });
    expect((await file('consumer-one', 'core')).outgoing).not.toHaveProperty('missing-provider');
  });
  it('does not overwrite duplicate consumer installs in different stores', async () => {
    await install('wizard-connectors');
    for (const slug of ['wizardapps', 'wizard']) {
      await install('consumer-one', { uses: ['wizard-connectors'] }, comp, slug);
      await fs.mkdir(path.join(data('consumer-one', slug), 'wizard'), { recursive: true });
      await fs.writeFile(
        path.join(data('consumer-one', slug), 'wizard/grants.json'),
        JSON.stringify({
          outgoing: { 'wizard-connectors': `dummy-key-${slug}` },
          incoming: [],
          enforce: true,
        }),
      );
    }
    await store.reconcile();
    await store.reconcile();
    for (const slug of ['wizardapps', 'wizard']) {
      expect((await file('consumer-one', slug)).outgoing['wizard-connectors']).toBe(`dummy-key-${slug}`);
      expect((await file('wizard-connectors')).incoming).toContainEqual({
        from: 'consumer-one',
        sha256: createHash('sha256').update(`dummy-key-${slug}`).digest('hex'),
      });
    }
  });
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
