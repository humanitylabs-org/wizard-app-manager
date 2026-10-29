import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DockerService } from '../docker.service';
import { executableModel, verifyBindSources } from '../compose-consumption';

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
    service.assertHostPolicy = vi.fn(async (args) => {
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

  it('escapes values, never mapping keys', () => {
    expect(executableModel({ $key: ['value$A', { x: '$$' }] })).toEqual({ $key: ['value$$A', { x: '$$$$' }] });
  });
});
