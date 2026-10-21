import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage, rewriteVolumes, validateStorage, confined, COPY_IMAGE, LEGACY_CLAUDE } from '../wizard-storage';
import { DockerComposeBuilder } from '../builders/compose.builder';
import type { AppUrn } from '@runtipi/common/types';

let root: string;
let store: WizardStorage;
let run: ReturnType<typeof vi.fn<(file: string, args: string[]) => Promise<{ stdout: string }>>>;
const provider = { id: 'wizard-claude', shares: [{ id: 'claude-login', path: 'claude' }] };
const consumer = { id: 'hermes-agent', mounts: [{ app: 'wizard-claude', share: 'claude-login', path: '/claude' }] };
const compose = {
  services: { main: { image: 'example', user: `${process.getuid?.() ?? 0}:${process.getgid?.() ?? 0}`, 'x-runtipi': { is_main: true } } },
};
async function install(m: any, c: any = compose) {
  const dir = path.join(root, 'apps', 'wizardapps', m.id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify(m)}---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(c));
  return `${m.id}:wizardapps`;
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'wizard-storage-'));
  run = vi.fn().mockResolvedValue({ stdout: '' });
  store = new WizardStorage({ dataDir: root, appDataDir: path.join(root, 'app-data') }, run);
});
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});
describe('rule 15 and volume compile', () => {
  it('runs the rewrite at compile time for Wizard recipes but leaves stock Runtipi recipes alone', () => {
    const source = { ...compose, volumes: { login: {} }, services: { main: { ...compose.services.main, volumes: ['login:/login'] } } };
    const builder = new DockerComposeBuilder();
    const wizard = yaml.parse(builder.getDockerCompose(source as any, {}, 'example:wizardapps' as AppUrn, '10.1.0.0/24'));
    expect(wizard.services.main.volumes).toEqual(['${APP_DATA_DIR}/volumes/login:/login']);
    expect(wizard.volumes).toBeUndefined();
    const stock = yaml.parse(builder.getDockerCompose(source as any, {}, 'example:stock' as AppUrn, '10.1.0.0/24'));
    expect(stock.services.main.volumes).toEqual(['login:/login']);
    expect(stock.volumes).toEqual({ login: {} });
  });
  it('rewrites short+long mounts on every service without losing flags or mutating source', () => {
    const src = {
      services: {
        a: { volumes: ['state:/data:ro', './x:/x'] },
        b: { volumes: [{ type: 'volume', source: 'state', target: '/login', read_only: true, volume: { nocopy: true } }] },
      },
      volumes: { state: null },
    };
    const result = rewriteVolumes(src, 'example');
    expect(result.services.a.volumes).toEqual(['${APP_DATA_DIR}/volumes/state:/data:ro', './x:/x']);
    expect(result.services.b.volumes[0]).toEqual({ type: 'bind', source: '${APP_DATA_DIR}/volumes/state', target: '/login', read_only: true });
    expect(result.volumes).toBeUndefined();
    expect(src.volumes.state).toBeNull();
  });
  it.each([{ name: 'fixed' }, { external: true }, { driver: 'nfs' }, { driver_opts: { type: 'none' } }])('refuses nonportable volume %j', (opts) => {
    expect(() => validateStorage({ id: 'example' }, { ...compose, volumes: { state: opts } })).toThrow();
  });
  it.each(['../outside', '/absolute', 'nested/../outside', 'nested//folder', 'link\\escape', '$HOME', 'a:b'])('refuses unsafe share %s', (p) => {
    expect(() => validateStorage({ id: 'example', shares: [{ id: 'login', path: p }] }, compose)).toThrow();
  });
  it('rejects non-list, extra fields, duplicate share and relative mount', () => {
    for (const m of [
      { shares: {} },
      { shares: [{ id: 'login', path: 'ok', rw: true }] },
      {
        shares: [
          { id: 'login', path: 'ok' },
          { id: 'login', path: 'ok2' },
        ],
      },
      { mounts: [{ app: 'provider', share: 'login', path: 'relative' }] },
    ])
      expect(() => validateStorage({ id: 'example', ...m } as any, compose)).toThrow();
  });
  it('refuses a symlink escape at any ancestor', async () => {
    await fs.mkdir(path.join(root, 'outside'));
    await fs.symlink(path.join(root, 'outside'), path.join(root, 'link'));
    await expect(confined(root, 'link/sub', true)).rejects.toThrow('real directory');
    await expect(confined(root, '../outside', true)).rejects.toThrow('Unsafe');
  });
});
describe('owner share grants', () => {
  it('offers a share on the install sheet before the consumer is installed', async () => {
    await install(provider);
    const candidate = path.join(root, 'repos/wizardapps/apps/hermes-agent');
    await fs.mkdir(candidate, { recursive: true });
    await fs.writeFile(path.join(candidate, 'wizard.md'), `---\n${yaml.stringify(consumer)}---\n`);
    await fs.writeFile(path.join(candidate, 'docker-compose.yml'), yaml.stringify(compose));
    expect(await store.proposed('hermes-agent')).toEqual([expect.objectContaining({ kind: 'share', from: 'hermes-agent', share: 'claude-login' })]);
    expect((await store.apps()).some((a) => a.manifest.id === 'hermes-agent')).toBe(false);
  });
  it('proposes when consumer installed second and mounts only after approval; revoke detaches', async () => {
    await install(provider);
    const urn = await install(consumer);
    expect(await store.proposed('hermes-agent')).toHaveLength(1);
    expect(yaml.parse(await store.prepare(urn, yaml.stringify(compose))).services.main.volumes).toEqual([]);
    const grant = (await store.proposed())[0]!;
    await store.change(grant, true);
    const mounted = await store.prepare(urn, yaml.stringify(compose));
    expect(yaml.parse(mounted).services.main.volumes[0].source).toBe(`${root}/app-data/wizardapps/wizard-claude/data/claude`);
    await store.change(grant, false);
    expect(yaml.parse(await store.prepare(urn, mounted)).services.main.volumes).toEqual([]);
    expect(await store.proposed()).toEqual([]); // denied, not silently reproposed
  });
  it('proposes when provider installed later and keeps provider data on consumer uninstall', async () => {
    await install(consumer);
    expect(await store.proposed()).toEqual([]);
    await install(provider);
    const g = (await store.proposed())[0]!;
    await store.change(g, true);
    await store.prepare('hermes-agent:wizardapps', yaml.stringify(compose));
    const login = path.join(root, 'app-data/wizardapps/wizard-claude/data/claude/login');
    await fs.writeFile(login, 'test-only');
    await store.remove('hermes-agent');
    expect(await fs.readFile(login, 'utf8')).toBe('test-only');
    expect((await store.readGrants()).grants).toEqual([]);
  });
  it('cannot approve an undeclared share or absent provider', async () => {
    await install(consumer);
    await expect(store.change({ kind: 'share', from: 'hermes-agent', to: 'wizard-claude', share: 'claude-login' }, true)).rejects.toThrow(
      'not installed',
    );
    await expect(store.change({ kind: 'share', from: 'hermes-agent', to: 'other', share: 'login' }, true)).rejects.toThrow('declared');
  });
  it('preserves non-share grants, seeded flags, denies, and apps.json mirror on writes', async () => {
    await install(provider);
    await install(consumer);
    await fs.mkdir(path.join(root, 'state/wizard-os'), { recursive: true });
    await fs.writeFile(path.join(root, 'state/wizard-os/apps.json'), JSON.stringify({ schema: 2, apps: [], slots: {} }));
    const base = await store.readGrants();
    base.grants.push({ kind: 'mcp', from: 'hermes-agent', to: 'other' });
    base.seeded = ['hermes-agent'];
    await store.writeGrants(base);
    await store.change((await store.proposed())[0]!, true);
    expect((await store.readGrants()).grants).toHaveLength(2);
    expect((await store.readGrants()).seeded).toEqual(['hermes-agent']);
    expect(JSON.parse(await fs.readFile(path.join(root, 'state/wizard-os/apps.json'), 'utf8')).grants).toHaveLength(2);
  });
  it('holds the grant lock through actual compose creation so revoke cannot lose to a stale start', async () => {
    await install(provider);
    const urn = await install(consumer);
    const g = (await store.proposed())[0]!;
    await store.change(g, true);
    const events: string[] = [];
    let enter!: () => void;
    let release!: () => void;
    const entered = new Promise<void>((r) => {
      enter = r;
    });
    const finish = new Promise<void>((r) => {
      release = r;
    });
    const start = store.prepare(urn, yaml.stringify(compose), async () => {
      enter();
      await finish;
      events.push('started');
    });
    await entered;
    const revoke = store.change(g, false).then(() => {
      events.push('revoked');
    });
    await Promise.resolve();
    expect(events).toEqual([]);
    release();
    await Promise.all([start, revoke]);
    expect(events).toEqual(['started', 'revoked']);
    expect((await store.readGrants()).grants).toEqual([]);
  });
  it('serializes concurrent approvals without losing rows', async () => {
    await install({ ...provider, shares: [...provider.shares, { id: 'second', path: 'second' }] });
    await install({ ...consumer, mounts: [...consumer.mounts, { app: 'wizard-claude', share: 'second', path: '/second' }] });
    await Promise.all((await store.proposed()).map((g) => store.change(g, true)));
    expect((await store.readGrants()).grants).toHaveLength(2);
  });
});
describe('legacy copy and explicit Claude upgrade', () => {
  it('pins helper and uses project volume and read-only source; daemon failure aborts', async () => {
    const urn = await install(
      { id: 'example' },
      { ...compose, volumes: { login: null }, services: { main: { ...compose.services.main, volumes: ['login:/data'] } } },
    );
    run.mockImplementation(async (_f, args) => {
      if (args[0] === 'volume') return { stdout: 'example_wizardapps_login\n' };
      if (args[0] === 'run') throw new Error('copy failed');
      return { stdout: '' };
    });
    await expect(store.prepare(urn, yaml.stringify({ ...compose, volumes: { login: null } }))).rejects.toThrow('copy failed');
    const call = run.mock.calls.find((c) => c[1][0] === 'run')?.[1];
    expect(call).toContain(COPY_IMAGE);
    expect(call).toContain('type=volume,src=example_wizardapps_login,dst=/old,readonly');
    expect(run.mock.calls.some((c) => c[1].includes('rm') && c[1][0] === 'volume')).toBe(false);
  });
  it('refuses hot copies and daemon unavailability rather than starting empty', async () => {
    const urn = await install({ id: 'example' }, { ...compose, volumes: { login: null } });
    run.mockImplementation(async (_f, args) => ({ stdout: args[0] === 'volume' ? 'example_wizardapps_login\n' : 'running-container' }));
    await expect(store.prepare(urn, yaml.stringify({ ...compose, volumes: { login: null } }))).rejects.toThrow('stop containers');
    run.mockRejectedValue(new Error('daemon offline'));
    await expect(store.prepare(urn, yaml.stringify({ ...compose, volumes: { login: null } }))).rejects.toThrow('daemon offline');
  });
  it.each([LEGACY_CLAUDE, 'wizard-storage-unit-legacy'])(
    'upgrades existing fixed-volume pair once using selected legacy volume %s',
    async (selected) => {
      store = new WizardStorage(store.dirs, run, selected);
      const legacy = {
        ...compose,
        volumes: { login: { name: LEGACY_CLAUDE } },
        services: { main: { ...compose.services.main, volumes: ['login:/claude'] } },
      };
      await install({ id: 'wizard-claude' }, legacy);
      const urn = await install({ id: 'hermes-agent' }, legacy);
      run.mockImplementation(async (_f, args) => ({ stdout: args[0] === 'volume' ? `${selected}\n` : '' }));
      const result = yaml.parse(await store.prepare(urn, yaml.stringify(legacy)));
      expect(run.mock.calls.some(([, args]) => args.includes(`type=volume,src=${selected},dst=/old,readonly`))).toBe(true);
      const grants = (await store.readGrants()).grants;
      expect(grants).toEqual([
        expect.objectContaining({ kind: 'share', from: 'hermes-agent', to: 'wizard-claude', share: 'claude-login', approved: 'install' }),
      ]);
      expect(result.services.main.volumes).toEqual([
        expect.objectContaining({ source: `${root}/app-data/wizardapps/wizard-claude/data/claude`, target: '/claude' }),
      ]);
      await store.prepare(urn, yaml.stringify(result));
      expect((await store.readGrants()).grants).toHaveLength(1);
      await store.change(grants[0]!, false);
      await store.prepare(urn, yaml.stringify(result));
      expect((await store.readGrants()).grants).toEqual([]);
    },
  );
  it('does not autoapprove a new pair without old fixed-volume evidence', async () => {
    await install(provider);
    await install(consumer);
    await store.prepare('hermes-agent:wizardapps', yaml.stringify(compose));
    expect((await store.readGrants()).grants).toEqual([]);
  });
});
