import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DockerService } from '../docker.service';
import { assertNoWritableBindAncestors, executableModel, verifyBindSources } from '../compose-consumption';
import { helperRun, runBindHelper } from '../bind-helper';

// These tests exercise real files and inherited kernel lock descriptors. The
// normal backend setup's memfs mock cannot supply a descriptor to flock.
vi.mock('node:fs', async (importOriginal) => importOriginal<typeof import('node:fs')>());
vi.mock('../bind-helper', async (importOriginal) => ({
  ...await importOriginal<typeof import('../bind-helper')>(),
  helperRun: vi.fn(), runBindHelper: vi.fn(async () => ({ stdout: 'safe\n' })),
}));

const roots: string[] = [];
afterEach(async () => {
  for (const root of roots.splice(0)) await fs.rm(root, { recursive: true, force: true });
});
const context = {
  appId: 'demo',
  store: 'wizardapps',
  appDataDir: '/synthetic/app-data/store/demo',
  appDataRoot: '/synthetic/app-data',
  filesRoot: '/synthetic/media',
  sourceRoot: '/synthetic/source',
  envDirs: [],
};

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'compose-consumption-'));
  roots.push(root);
  await fs.mkdir(path.join(root, 'user-config'));
  await fs.writeFile(path.join(root, '.env'), 'synthetic-manager-config');
  const file = path.join(root, 'docker-compose.yml');
  await fs.writeFile(file, 'services:\n  web:\n    image: example\n');
  const service: any = Object.create(DockerService.prototype);
  service.config = { get: () => ({ dataDir: root, appDataDir: path.join(root, 'app-data') }) };
  service.getBaseComposeArgsApp = vi.fn(async () => ({ args: ['--project-name', 'demo_wizardapps', '--file', file], isCustomConfig: false }));
  return { service, root, file };
}

describe('final compose consumption', () => {
  it('refuses unsupported raw file readers before running Compose config', async () => {
    const { service, file } = await fixture();
    await fs.writeFile(file, 'services:\n  web:\n    image: example\n    env_file: /synthetic/not-read.env\n');
    service.assertHostPolicy = vi.fn();
    await expect(service.validatedCompose('demo:wizardapps')).rejects.toThrow(/refused before container start/);
    expect(service.assertHostPolicy).not.toHaveBeenCalled();
  });

  it('executes the checked model, not the override/env paths, and preserves dollar values', async () => {
    const { service, file } = await fixture();
    service.assertHostPolicy = vi.fn(async (_urn, args) => {
      const snap = args[args.indexOf('--file') + 1];
      expect(snap).not.toBe(file);
      expect(await fs.readFile(snap, 'utf8')).toContain('image: example');
      return { model: { services: { web: { image: 'example', environment: { APP_SECRET: 'synthetic$VALUE' } } } }, ctx: context };
    });
    const executable = await service.validatedCompose('demo:wizardapps');
    await fs.writeFile(file, 'services: {}');
    expect(executable.args).toContain('-');
    expect(executable.args).not.toContain(file);
    expect(JSON.parse(executable.input).services.web.environment.APP_SECRET).toBe('synthetic$$VALUE');
  });

  it('refuses a raw Compose restart rather than resolving new teardown input', async () => {
    const { service } = await fixture();
    service.getBaseComposeArgsApp.mockClear();
    await expect(service.composeApp('demo:wizardapps', 'restart')).rejects.toThrow('Unsupported app lifecycle command');
    expect(service.getBaseComposeArgsApp).not.toHaveBeenCalled();
  });

  it('a final-policy refusal happens before restart down', async () => {
    const { service } = await fixture();
    service.prepareApp = vi.fn(async (_urn, apply) => apply('synthetic'));
    service.validatedCompose = vi.fn(async () => {
      throw new Error('synthetic policy refusal');
    });
    const beforeStart = vi.fn();
    await expect(service.composeApp('demo:wizardapps', 'up --detach', beforeStart)).rejects.toThrow('synthetic policy refusal');
    expect(beforeStart).not.toHaveBeenCalled();
  });

  it('rejects an unverifiable daemon path, even with a policy-approved bind', async () => {
    const model = { services: { web: { volumes: [{ type: 'bind', source: `${context.appDataDir}/data`, target: '/data' }] } } };
    const run = vi.fn(async () => {
      throw new Error('unsafe ancestor');
    });
    await expect(verifyBindSources(model, context, 54321, run)).rejects.toThrow(/unsafe or unverifiable host ancestry/);
  });

  it('keeps stable sources and enforces private propagation/no auto-create after daemon ancestry validation', async () => {
    const mount: any = { type: 'bind', source: `${context.appDataDir}/data`, target: '/data' };
    const run = vi.fn(async (_file, args) => {
      expect(args).toContain('--read-only');
      expect(args).toContain('none');
      expect(args).toContain('ALL');
      expect(args).toContain(mount.source);
      return { stdout: 'safe\n' };
    });
    await verifyBindSources({ services: { web: { volumes: [mount] } } }, context, 54321, run);
    expect(mount.source).toBe(`${context.appDataDir}/data`);
    expect(mount.bind).toEqual({ create_host_path: false, propagation: 'rprivate' });
  });

  it('never creates a missing media/share/foreign bind', async () => {
    const create = vi.fn();
    await expect(
      verifyBindSources(
        { services: { web: { volumes: [{ type: 'bind', source: '/synthetic/media/missing', target: '/data' }] } } },
        context,
        54321,
        async () => ({ stdout: 'missing\n' }),
        create,
      ),
    ).rejects.toThrow(/unavailable or unsafe/);
    expect(create).not.toHaveBeenCalled();
  });

  it('refuses writable ancestor overlaps in candidate and already running layouts', () => {
    const model = { services: { web: { volumes: [{ type: 'bind', source: '/safe/root/data', target: '/data' }] } } };
    expect(() => assertNoWritableBindAncestors(model, ['/safe/root'])).toThrow('overlap');
    expect(() => assertNoWritableBindAncestors(model, ['/'])).toThrow('overlap');
    expect(() => assertNoWritableBindAncestors(model, ['/safe/root/data'])).not.toThrow();
    expect(() => assertNoWritableBindAncestors(model, [], ['/safe/root/data/nested'])).toThrow('overlap');
    expect(() => assertNoWritableBindAncestors(model, [], ['/safe/root/data'])).not.toThrow();
    model.services.web.volumes.push({ type: 'bind', source: '/safe/root', target: '/root' });
    expect(() => assertNoWritableBindAncestors(model)).toThrow('overlap');
  });

  it('refuses a stopped restart-always writable ancestor before helper or app create', async () => {
    const { service } = await fixture();
    service.assertHostPolicy = vi.fn(async () => ({ model: { services: { web: { volumes: [
      { type: 'bind', source: '/safe/root/data', target: '/data' },
    ] } } }, ctx: context }));
    vi.mocked(runBindHelper).mockClear();
    vi.mocked(helperRun).mockImplementation(async (args) => {
      if (args[0] === 'ps') return { stdout: args.includes('-a') ? 'stopped-id\n' : '' };
      expect(args).toEqual(['inspect', '--type', 'container', 'stopped-id']);
      return { stdout: JSON.stringify([{ Id: 'stopped-id', Config: { Hostname: 'ordinary-app' },
        State: { Running: false }, HostConfig: { RestartPolicy: { Name: 'always' } },
        Mounts: [{ Type: 'bind', Source: '/safe/root', RW: true }] }]) };
    });
    await expect(service.validatedCompose('demo:wizardapps')).rejects.toThrow('overlap');
    expect(helperRun).toHaveBeenCalledWith(['ps', '-a', '-q', '--no-trunc'], expect.any(AbortSignal));
    expect(runBindHelper).not.toHaveBeenCalled();
  });

  it('refuses a new writable ancestor of a stopped persisted readonly child before create', async () => {
    const { service } = await fixture();
    service.assertHostPolicy = vi.fn(async () => ({ model: { services: { web: { volumes: [
      { type: 'bind', source: '/safe/root', target: '/data' },
    ] } } }, ctx: context }));
    vi.mocked(runBindHelper).mockClear();
    vi.mocked(helperRun).mockImplementation(async (args) => {
      if (args[0] === 'ps') return { stdout: args.includes('-a') ? 'stopped-child\n' : '' };
      expect(args).toEqual(['inspect', '--type', 'container', 'stopped-child']);
      return { stdout: JSON.stringify([{ Id: 'stopped-child', Config: { Hostname: 'ordinary-app' },
        State: { Running: false }, HostConfig: { RestartPolicy: { Name: 'always' } },
        Mounts: [{ Type: 'bind', Source: '/safe/root/data', RW: false }] }]) };
    });
    await expect(service.validatedCompose('demo:wizardapps')).rejects.toThrow('overlap');
    expect(runBindHelper).not.toHaveBeenCalled();
  });

  it('escapes values, never mapping keys', () => {
    expect(executableModel({ $key: ['value$A', { x: '$$' }] })).toEqual({ $key: ['value$$A', { x: '$$$$' }] });
  });

  it('emits the exact validated stdin for the worker real-Compose dollar regression', async () => {
    const values = {
      SINGLE: 'synthetic$VALUE', BRACED: 'synthetic${VALUE}', DOUBLE: 'synthetic$$VALUE',
      TRAILING: 'synthetic$', BARE: '$', MIXED: '$VALUE:${VALUE}:$$:end$',
    };
    const { service } = await fixture();
    const model = { services: { probe: {
      image: 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc',
      network_mode: 'none', read_only: true, cap_drop: ['ALL'], security_opt: ['no-new-privileges:true'],
      labels: { 'wizard.privsep.security': process.env.WIZARD_COMPOSE_PROOF_RUN_ID ?? 'unit-only' },
      environment: values,
      entrypoint: ['/bin/sh', '-ec'],
      command: ['printf "%s\\n" "$SINGLE" "$BRACED" "$DOUBLE" "$TRAILING" "$BARE" "$MIXED"'],
    } } };
    service.assertHostPolicy = vi.fn(async () => ({ model, ctx: context }));
    const pinned = await service.validatedCompose('demo:wizardapps');
    expect(pinned.args.slice(-2)).toEqual(['--file', '-']);
    const decoded = JSON.parse(pinned.input);
    expect(decoded.services.probe.environment).toEqual(
      Object.fromEntries(Object.entries(values).map(([key, value]) => [key, value.split('$').join('$$')])),
    );
    if (process.env.WIZARD_COMPOSE_PROOF_DIR) {
      // No recreated model: the worker consumes precisely validatedCompose's stdin.
      const dir = process.env.WIZARD_COMPOSE_PROOF_DIR;
      await fs.writeFile(path.join(dir, 'model.json'), pinned.input);
      await fs.writeFile(path.join(dir, 'expected.txt'), Object.values(values).join('\n') + '\n');
    }
  });
});
