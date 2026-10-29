import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { beforeEach, afterEach, describe, it, expect, vi } from 'vitest';
import * as yaml from 'yaml';
import { WizardStorage, rewriteVolumes, validateStorage, confined, COPY_IMAGE, LEGACY_CLAUDE } from '../wizard-storage';
import { DockerComposeBuilder } from '../builders/compose.builder';
import type { AppUrn } from '@runtipi/common/types';

// Storage keeps the production cross-process lock, not an in-memory stand-in.
vi.mock('node:fs', async (importOriginal) => importOriginal<typeof import('node:fs')>());

let root: string;
let store: WizardStorage;
let installed: { appName: string; appStoreSlug: string }[];
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
  installed.push({ appName: m.id, appStoreSlug: 'wizardapps' });
  return `${m.id}:wizardapps`;
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'wizard-storage-'));
  run = vi.fn().mockResolvedValue({ stdout: '' });
  installed = [];
  store = new WizardStorage({ dataDir: root, appDataDir: path.join(root, 'app-data') }, async () => installed, run);
});
afterEach(async () => {
  await fs.rm(root, { recursive: true, force: true });
});
describe('rule 15 and volume compile', () => {
  it('runs the rewrite at compile time for Wizard recipes but leaves stock Runtipi recipes alone', () => {
    const source = { ...compose, volumes: { login: {} }, services: { main: { ...compose.services.main, volumes: ['login:/login'] } } };
    const builder = new DockerComposeBuilder();
    const wizard = yaml.parse(builder.getDockerCompose(source as any, {}, 'example:wizardapps' as AppUrn, '10.1.0.0/24'));
    expect(wizard.services.main.volumes).toEqual(['${APP_DATA_DIR}/volumes/login:/login', '${APP_DATA_DIR}/wizard:/wizard:ro']);
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
describe('fresh volume ownership', () => {
  it('initialises a new bind for its numeric service user and never chowns existing data', async () => {
    const uid = process.getuid?.() ?? 0;
    const gid = process.getgid?.() ?? 0;
    const source = { services: { main: { ...compose.services.main, user: `${uid}:${gid}`, volumes: ['fresh:/data'] } }, volumes: { fresh: {} } };
    const urn = await install({ id: 'fresh-app' }, source);
    const built = new DockerComposeBuilder().getDockerCompose(source as any, {}, urn as AppUrn, '10.1.0.0/24');
    await store.prepare(urn, built);
    const folder = path.join(root, 'app-data/wizardapps/fresh-app/volumes/fresh');
    expect((await fs.stat(folder)).uid).toBe(uid);
    await fs.writeFile(path.join(folder, 'owner-state'), 'preserved');
    await store.prepare(urn, built);
    expect(await fs.readFile(path.join(folder, 'owner-state'), 'utf8')).toBe('preserved');
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
  it('preserves every registry projection and grant metadata with a distinct WIZARD_OS_HOME', async () => {
    const oldHome = process.env.WIZARD_OS_HOME;
    const home = path.join(root, 'owner-wizard');
    process.env.WIZARD_OS_HOME = home;
    const mirrors = [path.join(root, 'state/wizard-os/apps.json'), path.join(root, 'app-data/wizard-os/apps.json'), path.join(home, 'apps.json')];
    const original = { schema: 2, apps: [{ id: 'kept-app', custom: true }], slots: { primary: 'kept-app' }, custom: { keep: true } };
    try {
      await install(provider);
      await install(consumer);
      for (const mirror of mirrors) {
        await fs.mkdir(path.dirname(mirror), { recursive: true });
        await fs.writeFile(mirror, JSON.stringify(original));
      }
      const base = await store.readGrants();
      base.grants.push({ kind: 'mcp', from: 'hermes-agent', to: 'other' });
      base.seeded = ['hermes-agent'];
      await store.writeGrants(base);
      const grant = (await store.proposed())[0]!;
      await store.change(grant, true);
      for (const mirror of mirrors) {
        const saved = JSON.parse(await fs.readFile(mirror, 'utf8'));
        expect(saved).toMatchObject(original);
        expect(saved.grants).toEqual((await store.readGrants()).grants);
        expect(saved.grants).toHaveLength(2);
      }
      await store.change(grant, false);
      expect((await store.readGrants()).seeded).toEqual(['hermes-agent']);
      expect((await store.readGrants()).denied).toEqual([{
        kind: 'share', from: grant.from, to: grant.to, share: grant.share,
        reason: 'mounts', approved: 'owner', created_at: expect.any(Number),
      }]);
      const denied = (await store.readGrants()).denied[0]!;
      expect(Number.isInteger(denied.created_at)).toBe(true);
      expect(denied.created_at).toBeGreaterThan(0);
      for (const mirror of mirrors) {
        const saved = JSON.parse(await fs.readFile(mirror, 'utf8'));
        expect(saved).toMatchObject(original);
        expect(saved.grants).toEqual([{ kind: 'mcp', from: 'hermes-agent', to: 'other' }]);
      }
    } finally {
      if (oldHome === undefined) delete process.env.WIZARD_OS_HOME;
      else process.env.WIZARD_OS_HOME = oldHome;
    }
  });
  it('serializes concurrent approvals without losing rows', async () => {
    await install({ ...provider, shares: [...provider.shares, { id: 'second', path: 'second' }] });
    await install({ ...consumer, mounts: [...consumer.mounts, { app: 'wizard-claude', share: 'second', path: '/second' }] });
    await Promise.all((await store.proposed()).map((g) => store.change(g, true)));
    expect((await store.readGrants()).grants).toHaveLength(2);
  });
});
describe('live legacy Claude compatibility', () => {
  const legacy = {
    ...compose,
    volumes: { login: { name: LEGACY_CLAUDE } },
    services: { main: { ...compose.services.main, volumes: ['login:/claude'] } },
  };
  async function coreProvider() {
    await install({ id: 'wizard-claude' }, legacy);
    await fs.mkdir(path.join(root, 'apps/core'), { recursive: true });
    await fs.rename(path.join(root, 'apps/wizardapps/wizard-claude'), path.join(root, 'apps/core/wizard-claude'));
    installed.find((a) => a.appName === 'wizard-claude')!.appStoreSlug = 'core';
    const data = path.join(root, 'app-data/core/wizard-claude');
    await fs.mkdir(path.join(data, 'data/claude'), { recursive: true });
    await fs.writeFile(path.join(data, 'app.env'), 'APP_DATA_DIR=/daemon/app-data/core/wizard-claude\n');
    await fs.writeFile(path.join(data, 'data/claude/login'), 'retained-test-login');
    const app = (await store.apps()).find((a) => a.urn === 'wizard-claude:core');
    if (!app) throw new Error('Missing test provider');
    return app;
  }
  function alias(device = '/daemon/app-data/core/wizard-claude/data/claude') {
    run.mockImplementation(async (_f, args) => {
      if (args[0] === 'volume' && args[1] === 'ls') return { stdout: `${LEGACY_CLAUDE}\n` };
      if (args[0] === 'volume' && args[1] === 'inspect')
        return { stdout: JSON.stringify({ Driver: 'local', Options: { type: 'none', o: 'bind', device } }) };
      if (args[0] === 'ps') return { stdout: 'running-agent' };
      throw new Error(`Unexpected Docker mutation: ${args.join(' ')}`);
    });
  }
  it('checks the host marker before Docker or active legacy consumers', async () => {
    const app = await coreProvider();
    await fs.writeFile(path.join(app.data, 'data/.claude.wizard-migrated'), '');
    alias();
    await store.migrate(LEGACY_CLAUDE, 'data/claude', app);
    expect(run).not.toHaveBeenCalled();
  });
  it('rejects a symlink migration marker rather than trusting it', async () => {
    const app = await coreProvider();
    await fs.symlink(path.join(app.data, 'data/claude/login'), path.join(app.data, 'data/.claude.wizard-migrated'));
    alias();
    await expect(store.migrate(LEGACY_CLAUDE, 'data/claude', app)).rejects.toThrow('marker');
  });
  it('adopts an exact bind alias without copying or stopping running agents and writes a missing marker', async () => {
    const app = await coreProvider();
    alias();
    await store.migrate(LEGACY_CLAUDE, 'data/claude', app);
    expect((await fs.stat(path.join(app.data, 'data/.claude.wizard-migrated'))).isFile()).toBe(true);
    expect(await fs.readFile(path.join(app.data, 'data/claude/login'), 'utf8')).toBe('retained-test-login');
    expect(run.mock.calls.every(([, args]) => args[0] === 'volume' && ['ls', 'inspect'].includes(args[1] ?? ''))).toBe(true);
  });
  it('still refuses a hot copy when the alias points somewhere else', async () => {
    const app = await coreProvider();
    alias('/daemon/other-login');
    await expect(store.migrate(LEGACY_CLAUDE, 'data/claude', app)).rejects.toThrow('stop containers');
  });
  it('rewrites Claude itself without deleting or recreating the live alias', async () => {
    const app = await coreProvider();
    alias();
    const result = yaml.parse(await store.prepare(app.urn, yaml.stringify(legacy)));
    expect(result.services.main.volumes).toEqual(['${APP_DATA_DIR}/data/claude:/claude']);
    expect(result.volumes).toBeUndefined();
    expect(run.mock.calls.every(([, args]) => args[0] === 'volume' && ['ls', 'inspect'].includes(args[1] ?? ''))).toBe(true);
  });
  it.each(['agent-philosopher', 'agent-private', 'custom-consumer'])(
    'preserves installed %s legacy mount paths, flags and services through compile and prepare',
    async (id) => {
      await coreProvider();
      const source = {
        volumes: { login: { name: LEGACY_CLAUDE }, cache: {} },
        services: {
          main: { ...compose.services.main, labels: { wizard_type: 'agent' }, volumes: ['login:/claude:rw', 'cache:/cache'] },
          sidecar: { image: 'example', volumes: [{ type: 'volume', source: 'login', target: '/official-cli', read_only: true }] },
        },
      };
      const urn = await install({ id }, source);
      alias();
      const built = new DockerComposeBuilder().getDockerCompose(
        source as unknown as Parameters<DockerComposeBuilder['getDockerCompose']>[0],
        {},
        urn as AppUrn,
        '10.1.0.0/24',
      );
      const result = yaml.parse(await store.prepare(urn, built));
      expect(result.volumes).toEqual({ login: { name: LEGACY_CLAUDE } });
      expect(result.services.main.volumes).toEqual(['login:/claude:rw', '${APP_DATA_DIR}/volumes/cache:/cache', '${APP_DATA_DIR}/wizard:/wizard:ro']);
      expect(result.services.sidecar.volumes).toEqual([...source.services.sidecar.volumes, '${APP_DATA_DIR}/wizard:/wizard:ro']);
      expect(result['x-wizard-volumes']).toEqual([{ name: 'cache', old: 'cache', relative: 'volumes/cache' }]);
      expect(run.mock.calls.some(([, args]) => args[0] === 'volume' && args[1] === 'inspect')).toBe(true);
      expect(run.mock.calls.some(([, args]) => args[0] === 'run')).toBe(false);
      expect((await store.readGrants()).grants).toEqual([]); // retained alias authority, not a new share grant
    },
  );
  it('does not trust a provider marker as evidence that a consumer alias is safe', async () => {
    const app = await coreProvider();
    await fs.writeFile(path.join(app.data, 'data/.claude.wizard-migrated'), '');
    const urn = await install({ id: 'agent-private' }, legacy);
    alias('/daemon/other-login');
    await expect(store.prepare(urn, yaml.stringify(legacy))).rejects.toThrow('bind alias');
  });
  it.each([
    { Driver: 'nfs', Options: { type: 'none', o: 'bind' } },
    { Driver: 'local', Options: { type: 'nfs', o: 'bind' } },
    { Driver: 'local', Options: { type: 'none', o: 'ro' } },
    { Driver: 'local', Options: null },
  ])('refuses a non-bind alias before starting a consumer: %j', async (info) => {
    await coreProvider();
    const urn = await install({ id: 'agent-private' }, legacy);
    run.mockResolvedValue({
      stdout: JSON.stringify({ ...info, Options: info.Options && { ...info.Options, device: '/daemon/app-data/core/wizard-claude/data/claude' } }),
    });
    const apply = vi.fn();
    await expect(store.prepare(urn, yaml.stringify(legacy), apply)).rejects.toThrow('bind alias');
    expect(apply).not.toHaveBeenCalled();
  });
  it('aborts before consumer start when volume inspection fails', async () => {
    await coreProvider();
    const urn = await install({ id: 'agent-private' }, legacy);
    run.mockRejectedValue(new Error('inspect failed'));
    const apply = vi.fn();
    await expect(store.prepare(urn, yaml.stringify(legacy), apply)).rejects.toThrow('inspect failed');
    expect(apply).not.toHaveBeenCalled();
  });
  it('preserves a private-store consumer on the raw pre-start rewrite path', async () => {
    await coreProvider();
    await install({ id: 'agent-private' }, legacy);
    await fs.mkdir(path.join(root, 'apps/private'), { recursive: true });
    await fs.rename(path.join(root, 'apps/wizardapps/agent-private'), path.join(root, 'apps/private/agent-private'));
    installed.find((a) => a.appName === 'agent-private')!.appStoreSlug = 'private';
    alias();
    const result = yaml.parse(await store.prepare('agent-private:private', yaml.stringify(legacy)));
    expect(result.volumes).toEqual(legacy.volumes);
    expect(result.services.main.volumes).toEqual(['login:/claude']);
  });
  it('does not allow a new candidate to inherit the installed-volume exception', async () => {
    await coreProvider();
    const dir = path.join(root, 'repos/wizardapps/apps/agent-new');
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, 'wizard.md'), '---\nid: agent-new\n---\n');
    await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(legacy));
    await expect(store.proposed('agent-new')).rejects.toThrow('fixed name');
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
      store = new WizardStorage(store.dirs, async () => installed, run, selected);
      const legacy = {
        ...compose,
        volumes: { login: { name: LEGACY_CLAUDE } },
        services: { main: { ...compose.services.main, volumes: ['login:/claude'] } },
      };
      await install({ id: 'wizard-claude' }, legacy);
      const urn = await install({ id: 'hermes-agent' }, legacy);
      run.mockImplementation(async (_f, args) => ({
        stdout: args[0] === 'volume' ? (args[1] === 'inspect' ? JSON.stringify({ Driver: 'local', Options: null }) : `${selected}\n`) : '',
      }));
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
