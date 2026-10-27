import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import type { ModuleRef } from '@nestjs/core';
import type { AppUrn } from '@runtipi/common/types';
import type Dockerode from 'dockerode';
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { mock } from 'vitest-mock-extended';
import * as yaml from 'yaml';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { RestartAppCommand } from '@/modules/app-lifecycle/commands/restart-app-command';
import { StartAppCommand } from '@/modules/app-lifecycle/commands/start-app-command';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { AppHelpers } from '@/modules/apps/app.helpers';
import { AppsRepository } from '@/modules/apps/apps.repository';
import { AppsService } from '@/modules/apps/apps.service';
import { DockerService } from '../docker.service';
import { LEGACY_CLAUDE, type Compose } from '../wizard-storage';

const claude = 'wizard-claude:core' as AppUrn;
const agent = 'agent-philosopher:wizardapps' as AppUrn;
const connectors = 'wizard-connectors:wizardapps' as AppUrn;
const legacy = {
  services: { main: { image: 'example', 'x-runtipi': { is_main: true }, volumes: ['login:/claude:rw'] } },
  volumes: { login: { name: LEGACY_CLAUDE } },
};
const connectorCompose = {
  services: { main: { image: 'example', 'x-runtipi': { is_main: true }, volumes: ['ui-data:/ui', 'google-data:/google'] } },
  volumes: { 'ui-data': {}, 'google-data': {} },
};
let root: string;
let service: DockerService;
let files: ReturnType<typeof mock<AppFilesManager>>;
let repository: ReturnType<typeof mock<AppsRepository>>;
let logger: ReturnType<typeof mock<LoggerService>>;
let moduleRef: ModuleRef;
let running: Set<string>;
let calls: string[][];
let inspectDevice: string;
let dockerRun: MockInstance<typeof service.storage.run>;
const generated = (urn: string) => path.join(root, `${urn}.generated.yml`);

async function recipe(urn: string, compose: Compose = legacy) {
  const [id, slug] = urn.split(':');
  if (!id || !slug) throw new Error('Invalid fixture urn');
  const dir = path.join(root, 'apps', slug, id);
  await fs.mkdir(dir, { recursive: true });
  await fs.writeFile(path.join(dir, 'wizard.md'), `---\nid: ${id}\n---\n`);
  await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(compose));
  await fs.writeFile(generated(urn), yaml.stringify(compose));
}
async function lifecycle(kind: 'restart' | 'start', urn: AppUrn) {
  const command = kind === 'restart' ? new RestartAppCommand(moduleRef, mock<Dockerode>()) : new StartAppCommand(moduleRef, mock<Dockerode>());
  // Generation is not the seam under test: use the real storage preparation,
  // repository lookup and lifecycle command with a stateful fake Docker runner.
  vi.spyOn(command as typeof command & { ensureAppDir: (...args: unknown[]) => Promise<void> }, 'ensureAppDir').mockResolvedValue();
  return command.execute(urn, { skipEnv: true, skipPull: true });
}
beforeEach(async () => {
  root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'wizard-restart-incident-'));
  files = mock<AppFilesManager>();
  repository = mock<AppsRepository>();
  logger = mock<LoggerService>();
  const config = mock<ConfigurationService>();
  config.get.calledWith('directories').mockReturnValue({ dataDir: root, appDataDir: path.join(root, 'app-data') } as never);
  const rows = [claude, agent, connectors].map((urn) => {
    const [appName, appStoreSlug] = urn.split(':');
    return { appName, appStoreSlug, status: 'running' };
  });
  repository.getApps.mockResolvedValue(rows as Awaited<ReturnType<AppsRepository['getApps']>>);
  files.getInstalledAppInfo.mockResolvedValue({ force_pull: false } as never);
  files.getGeneratedDockerComposeYaml.mockImplementation(async (urn) => ({
    path: generated(urn),
    content: await fs.readFile(generated(urn), 'utf8'),
  }));
  const appsService = mock<AppsService>();
  repository.getAppByUrn.mockResolvedValue({ exposed: false, exposedLocal: false } as never);
  service = new DockerService(logger, config, files, mock<FilesystemService>(), appsService, repository);
  running = new Set([claude, agent, connectors]);
  calls = [];
  // No Docker daemon calls: emulate down/up and track real command ordering.
  vi.spyOn(
    service as unknown as { runComposeApp: (urn: AppUrn, command: string) => Promise<{ success: boolean; stdout: string; stderr: string }> },
    'runComposeApp',
  ).mockImplementation(async (urn, command) => {
    calls.push([urn, command]);
    if (/^(down|stop)/.test(command)) running.delete(urn);
    if (command.startsWith('up')) running.add(urn);
    return { success: true, stdout: '', stderr: '' };
  });
  inspectDevice = '/daemon/app-data/core/wizard-claude/data/claude';
  dockerRun = vi.spyOn(service.storage, 'run').mockImplementation(async (_file, args) => {
    if (args[0] === 'volume' && args[1] === 'ls')
      return { stdout: `${LEGACY_CLAUDE}\nwizard-connectors_wizardapps_ui-data\nwizard-connectors_wizardapps_google-data\n` };
    if (args[0] === 'volume' && args[1] === 'inspect')
      return { stdout: JSON.stringify({ Driver: 'local', Options: { type: 'none', o: 'bind', device: inspectDevice } }) };
    if (args[0] === 'ps') return { stdout: args.includes(`volume=${LEGACY_CLAUDE}`) ? 'running-philosopher' : '' };
    // Named connector volumes are empty; the production copy helper succeeds.
    if (args[0] === 'run' && args.some((v) => v.startsWith('type=volume,src=wizard-connectors_wizardapps_'))) return { stdout: '' };
    throw new Error(`Unexpected Docker mutation: ${args.join(' ')}`);
  });
  moduleRef = {
    get: (token: unknown) =>
      new Map<unknown, unknown>([
        [LoggerService, logger],
        [AppFilesManager, files],
        [DockerService, service],
        [AppHelpers, mock<AppHelpers>()],
      ]).get(token),
  } as ModuleRef;
  await recipe(claude);
  await recipe(agent);
  await recipe(connectors, connectorCompose);
  // Exactly the incident: an uninstalled old recipe and only app.env under its
  // app-data. No row or container exists for wizard-claude:wizardapps.
  await recipe('wizard-claude:wizardapps');
  const staleData = path.join(root, 'app-data/wizardapps/wizard-claude');
  await fs.mkdir(staleData, { recursive: true });
  await fs.writeFile(path.join(staleData, 'app.env'), 'APP_DATA_DIR=/daemon/app-data/wizardapps/wizard-claude\n');
  const data = path.join(root, 'app-data/core/wizard-claude');
  await fs.mkdir(path.join(data, 'data/claude'), { recursive: true });
  await fs.writeFile(path.join(data, 'app.env'), 'APP_DATA_DIR=/daemon/app-data/core/wizard-claude\n');
  await fs.writeFile(path.join(data, 'data/.claude.wizard-migrated'), '');
});
afterEach(async () => {
  vi.restoreAllMocks();
  await fs.rm(root, { recursive: true, force: true });
});

describe('owner publication at the real pre-start seam', () => {
  it.each([{ published: false, hasMode: false }, { published: true, hasMode: false },
    { published: false, hasMode: true }, { published: true, hasMode: true }])(
    'does not trust secondary labels when publication=$published, network_mode present=$hasMode', async ({ published, hasMode }) => {
    const compose = {
      services: {
        main: { image: 'example', 'x-runtipi': { is_main: true, internal_port: 80 },
          ...(hasMode ? { network_mode: null } : {}),
          labels: { 'traefik.enable': 'true' }, networks: { tipi_main_network: {} } },
        engine: { image: 'example', labels: { 'traefik.enable': 'true' }, networks: { tipi_main_network: {} } },
      }, networks: { tipi_main_network: { external: true, name: 'runtipi_tipi_main_network' } },
      'x-runtipi': { schema_version: 2 },
    };
    await recipe(connectors, compose);
    repository.getAppByUrn.mockResolvedValue({ exposed: published, exposedLocal: false } as never);
    files.getSourceDockerComposeYaml.mockResolvedValue({ content: compose, path: 'source.yml' } as never);
    await service.prepareApp(connectors);
    const output = yaml.parse(await fs.readFile(generated(connectors), 'utf8'));
    expect(Boolean(output.services.main.networks?.tipi_main_network)).toBe(published && !hasMode);
    expect(output.services.engine.networks?.tipi_main_network).toBeUndefined();
    expect(repository.getAppByUrn).toHaveBeenCalledWith(connectors);
  });
  it('an unavailable owner row never becomes permission and does not block private startup', async () => {
    repository.getAppByUrn.mockRejectedValue(new Error('owner row temporarily unavailable'));
    await expect(service.prepareApp(connectors)).resolves.toBeUndefined();
    expect(files.getSourceDockerComposeYaml).not.toHaveBeenCalled();
  });
});

describe('22 October stale provider directory incident', () => {
  it.each([claude, agent, connectors])('restarts %s while ignoring the stale directory and retaining the running legacy consumer', async (urn) => {
    expect(await lifecycle('restart', urn)).toEqual({ success: true, message: `App ${urn} restarted successfully` });
    expect(running).toEqual(new Set([claude, agent, connectors]));
    expect(calls.map(([app]) => app)).toEqual([urn, urn]);
    expect(logger.warn).toHaveBeenCalledWith(expect.stringContaining('wizard-claude:wizardapps'));
    expect(repository.getApps).toHaveBeenCalled();
    expect(await fs.readdir(path.join(root, 'app-data/wizardapps/wizard-claude'))).toEqual(['app.env']);
    if (urn === connectors) expect(dockerRun.mock.calls.filter(([, args]) => args[0] === 'run')).toHaveLength(2);
  });
  it.each(['restart', 'start'] as const)('leaves the running app untouched when %s bridge validation fails', async (kind) => {
    // Remove the stale directory so this regression isolates stop ordering.
    await fs.rm(path.join(root, 'apps/wizardapps/wizard-claude'), { recursive: true });
    inspectDevice = '/daemon/wrong-provider-folder';
    expect(await lifecycle(kind, agent)).toEqual({
      success: false,
      message: 'Legacy Claude volume must be a bind alias to the provider folder',
      deploymentUnchanged: true,
    });
    expect(running.has(agent)).toBe(true);
    expect(calls).toEqual([]);
  });
  it.each(['storage', 'shares', 'grants'] as const)('does not stop a running app when %s validation fails', async (failure) => {
    if (failure === 'storage') {
      await recipe(agent, { ...legacy, volumes: { login: { name: 'unapproved-fixed-volume' } } });
    } else if (failure === 'shares') {
      await fs.writeFile(
        path.join(root, 'apps/wizardapps/agent-philosopher/wizard.md'),
        '---\nid: agent-philosopher\nmounts:\n  - {app: missing-provider, share: login, path: /shared}\n---\n',
      );
      const grants = await service.storage.readGrants();
      grants.grants.push({ kind: 'share', from: 'agent-philosopher', to: 'missing-provider', share: 'login' });
      await service.storage.writeGrants(grants);
    } else {
      await fs.mkdir(path.join(root, 'state'), { recursive: true });
      await fs.writeFile(service.storage.grantsPath, '{"schema":0}');
    }
    expect(await lifecycle('restart', agent)).toMatchObject({ success: false, deploymentUnchanged: true });
    expect(running.has(agent)).toBe(true);
    expect(calls).toEqual([]);
  });
  it('ignores even malformed stale manifests for installed shares and bridge lookup', async () => {
    await fs.writeFile(path.join(root, 'apps/wizardapps/wizard-claude/wizard.md'), 'not a manifest');
    expect((await service.storage.apps()).map((a) => a.urn).sort()).toEqual([claude, agent, connectors].sort());
    expect(await service.storage.proposed()).toEqual([]);
  });
  it.each([false, true])('still refuses two installed provider rows before stopping anything (missing recipe=%s)', async (missingRecipe) => {
    if (missingRecipe) await fs.rm(path.join(root, 'apps/wizardapps/wizard-claude'), { recursive: true });
    const rows = await repository.getApps();
    repository.getApps.mockResolvedValue([...rows, { appName: 'wizard-claude', appStoreSlug: 'wizardapps' } as (typeof rows)[number]]);
    expect(await lifecycle('restart', claude)).toEqual({ success: false, message: 'Ambiguous installed provider ids', deploymentUnchanged: true });
    expect(running.has(claude)).toBe(true);
    expect(calls).toEqual([]);
  });
  it('restarts the live layout: agent declares the legacy login external and an unrelated app is installed twice', async () => {
    await recipe(agent, { ...legacy, volumes: { login: { name: LEGACY_CLAUDE, external: true } } });
    const rows = await repository.getApps();
    repository.getApps.mockResolvedValue([
      ...rows,
      { appName: 'defleur-video', appStoreSlug: 'wizard', status: 'stopped' },
      { appName: 'defleur-video', appStoreSlug: 'wizardapps', status: 'running' },
    ] as typeof rows);
    for (const urn of [claude, agent, connectors]) {
      calls.length = 0;
      expect(await lifecycle('restart', urn)).toEqual({ success: true, message: `App ${urn} restarted successfully` });
      expect(calls.map(([app]) => app)).toEqual([urn, urn]);
    }
    expect(running).toEqual(new Set([claude, agent, connectors]));
  });
  it('still refuses an external volume that is not the legacy Claude login', async () => {
    await recipe(agent, { ...legacy, volumes: { login: { name: 'other-volume', external: true } } });
    expect(await lifecycle('restart', agent)).toMatchObject({ success: false, deploymentUnchanged: true });
    expect(running.has(agent)).toBe(true);
  });
  it('does not fall back to directory discovery if the installed repository fails', async () => {
    repository.getApps.mockRejectedValue(new Error('database unavailable'));
    expect(await lifecycle('restart', claude)).toEqual({ success: false, message: 'database unavailable', deploymentUnchanged: true });
    expect(calls).toEqual([]);
    expect(running.has(claude)).toBe(true);
  });
});
