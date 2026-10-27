import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import * as yaml from 'yaml';
import { enforceAppNetworks, networkEscapes, POLICY_FILE, pruneStaleOverride, readPolicy } from '../wizard-networks';

let root: string;
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'wizard-networks-'));
});
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});

const SHARED = { name: 'runtipi_tipi_main_network', external: true };
const generated = (extra: Record<string, unknown> = {}, svc: Record<string, unknown> = {}) =>
  yaml.stringify({
    services: {
      x: { image: 'a', networks: { tipi_main_network: { gw_priority: 1 } }, labels: { 'runtipi.managed': true }, ...svc },
    },
    networks: { tipi_main_network: SHARED, ...extra },
  });
const policy = (p: unknown) =>
  fs
    .mkdir(path.join(root, 'user-config'), { recursive: true })
    .then(() => fs.writeFile(path.join(root, 'user-config', POLICY_FILE), JSON.stringify(p)));
const override = async (app: string, text: string) => {
  const d = path.join(root, 'user-config', 'wizardapps', app);
  await fs.mkdir(d, { recursive: true });
  await fs.writeFile(path.join(d, 'docker-compose.yml'), text);
  return path.join(d, 'docker-compose.yml');
};

describe('app networks at every start (UI installs/updates included)', () => {
  it('never puts an unexposed Wizard app on the shared network, with no policy file (fail closed)', async () => {
    const out = yaml.parse(await enforceAppNetworks('x:wizardapps', generated(), root));
    expect(out.services.x.networks).toBeUndefined();
    expect(await readPolicy(root)).toEqual({ enabled: true, allow: [] });
  });

  it('keeps the shared network for the reverse proxy, for hand-set networks, other stores and isolation off', async () => {
    const traefik = generated({}, { labels: { 'traefik.enable': true } });
    expect(await enforceAppNetworks('x:wizardapps', traefik, root)).toBe(traefik);
    expect(await enforceAppNetworks('x:migrated', generated(), root)).toBe(generated());
    await override('x', 'services:\n  x:\n    networks: [tipi_main_network]\n');
    expect(await enforceAppNetworks('x:wizardapps', generated(), root)).toBe(generated());
    await fs.rm(path.join(root, 'user-config', 'wizardapps'), { recursive: true });
    await policy({ schema: 1, enabled: false, allow_host_access: [] });
    expect(await enforceAppNetworks('x:wizardapps', generated(), root)).toBe(generated());
  });

  it('keeps a multi-service app on its own network', async () => {
    const g = yaml.stringify({
      services: { x: { image: 'a', networks: { tipi_main_network: {}, x_wizardapps_network: {} } } },
      networks: { tipi_main_network: SHARED, x_wizardapps_network: { ipam: { config: [{ subnet: '10.128.0.0/24' }] } } },
    });
    const out = yaml.parse(await enforceAppNetworks('x:wizardapps', g, root));
    expect(Object.keys(out.services.x.networks)).toEqual(['x_wizardapps_network']);
  });

  it('refuses networks the app does not own unless the owner allowed it', async () => {
    const sneaky = [
      { sneak: { external: true, name: 'runtipi_tipi_main_network' } },
      { sneak: { external: true, name: 'twenty-crm_wizardapps_twenty-crm_wizardapps_network' } },
      { sneak: { name: 'wizard-link-carlos-crm--twenty-crm' } },
      { sneak: { name: 'shared-by-two-apps' } },
      { sneak: { driver: 'macvlan' } },
    ];
    for (const n of sneaky) {
      const g = generated(n, { networks: { tipi_main_network: {}, sneak: {} } });
      await expect(enforceAppNetworks('x:wizardapps', g, root)).rejects.toThrow(/x refused: it asks for host access or networks it does not own/);
    }
    for (const mode of ['host', 'bridge', 'container:runtipi'])
      expect(networkEscapes({ services: { x: { network_mode: mode } } }, 'x')).toHaveLength(1);
    expect(networkEscapes({ services: { x: { network_mode: 'service:y' }, y: {} }, networks: { own: { name: 'x_wizardapps_own' } } }, 'x')).toEqual(
      [],
    );
    await policy({ schema: 1, enabled: true, allow_host_access: ['x'] });
    const g = generated(sneaky[0], { networks: { tipi_main_network: {}, sneak: {} } });
    expect(await enforceAppNetworks('x:wizardapps', g, root)).toBe(g);
  });

  it('refuses privileged mode and a mounted runtime socket at every start unless allowed (wam network deny)', async () => {
    for (const svc of [
      { privileged: true },
      { volumes: ['/var/run/docker.sock:/var/run/docker.sock'] },
      { volumes: [{ type: 'bind', source: '/run/containerd/containerd.sock', target: '/s' }] },
    ])
      await expect(enforceAppNetworks('x:wizardapps', generated({}, svc), root)).rejects.toThrow(/x refused/);
    const ok = generated({}, { volumes: ['/root/runtipi/app-data/wizardapps/x/data:/data'] });
    await expect(enforceAppNetworks('x:wizardapps', ok, root)).resolves.toBeTypeOf('string');
    await policy({ schema: 1, enabled: true, allow_host_access: ['x'] });
    const priv = generated({}, { privileged: true });
    expect(await enforceAppNetworks('x:wizardapps', priv, root)).toBe(priv);
  });

  it("drops wam's managed entries for services an update renamed, keeping everything else", async () => {
    const file = await override(
      'x',
      '# App networks: the block marked x-wizard-networks is written by wam (`wam network reconcile`).\n' +
        'services:\n  old:\n    networks: !override\n      wizard-app-x:\n        gw_priority: 1\n  eng:\n    ports: !reset []\n' +
        'networks:\n  wizard-app-x:\n    name: wizard-app-x\n    external: true\n' +
        'x-wizard-networks:\n  services:\n  - old\n  networks:\n  - wizard-app-x\n',
    );
    expect(await pruneStaleOverride(file, ['x', 'eng'])).toEqual(['old']);
    const text = await fs.readFile(file, 'utf8');
    expect(text).toContain('ports: !reset []');
    expect(text.startsWith('# App networks')).toBe(true);
    const doc = yaml.parse(text, { customTags: [{ tag: '!reset', resolve: () => [] }] });
    expect(Object.keys(doc.services)).toEqual(['eng']);
    expect(doc['x-wizard-networks'].services).toEqual([]);
    expect(await pruneStaleOverride(file, ['x', 'eng'])).toEqual([]);
    const renamed = yaml.stringify({ services: { x: { image: 'a', networks: { tipi_main_network: {} } } }, networks: { tipi_main_network: SHARED } });
    await override(
      'y',
      'services:\n  gone:\n    networks: !override\n      wizard-app-y: null\nx-wizard-networks:\n  services: [gone]\n  networks: []\n',
    );
    await enforceAppNetworks('y:wizardapps', renamed.replace(/\bx\b/, 'y'), root);
    expect(await fs.readFile(path.join(root, 'user-config', 'wizardapps', 'y', 'docker-compose.yml'), 'utf8')).not.toContain('gone:');
  });
});
