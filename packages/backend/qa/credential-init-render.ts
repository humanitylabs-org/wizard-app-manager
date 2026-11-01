// Forum-only fixture driver: real file manager + builder + prepareApp. Never serving state.
import 'reflect-metadata';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import * as yaml from 'yaml';
import { AppFilesManager } from '../src/modules/apps/app-files-manager';
import { DockerComposeBuilder } from '../src/modules/docker/builders/compose.builder';
import { DockerService } from '../src/modules/docker/docker.service';
import { WizardStorage } from '../src/modules/docker/wizard-storage';
import { enforceAppNetworks } from '../src/modules/docker/wizard-networks';

const [stageArg, scenario, phase] = process.argv.slice(2);
assert(stageArg && /^\/srv\/work\/credential-init-exposure-[a-z0-9-]+$/.test(stageArg));
assert(process.env.WIZARD_CREDENTIAL_SYNTHETIC_ROOT === stageArg && process.getuid?.() === 0);
assert(['fresh', 'existing', 'wrong-owner', 'populated-root', 'symlink', 'hardlink', 'fifo'].includes(scenario!));
assert(['install', 'restart', 'update'].includes(phase!));
const stage = path.resolve(stageArg), dataDir = path.join(stage, 'runtime', scenario!);
const dirs = { dataDir, appDataDir: path.join(dataDir, 'app-data') };
const inside = (p: string) => { const r = path.resolve(p); assert(r.startsWith(dataDir + '/')); return r; };
const logger: any = { error: () => {}, warn: () => {}, debug: () => {}, info: () => {} };
// File transport adapter confines every path to synthetic state. Production
// FilesystemService has fixed /data /app-data prefixes; no host aliases are created.
const disk: any = {
  isFile: async (p: string) => fs.stat(inside(p)).then(s => s.isFile(), e => { if (e.code !== 'ENOENT') throw e; return false; }),
  readTextFile: (p: string) => fs.readFile(inside(p), 'utf8'),
  readYamlFile: async (p: string) => yaml.parse(await fs.readFile(inside(p), 'utf8')),
  readJsonFile: async (p: string) => JSON.parse(await fs.readFile(inside(p), 'utf8')),
  writeTextFile: async (p: string, value: string) => { await fs.writeFile(inside(p), value); return true; },
};
const cfg: any = { getConfig: () => ({ directories: dirs }), get: (key: string) => key === 'directories' ? dirs : key === 'architecture' ? 'amd64' : undefined };
const manager = new AppFilesManager(cfg, disk, logger);
const rows = ['wizard-chatgpt', 'wizard-claude'].map(appName => ({ appName, appStoreSlug: 'wizardapps' }));
const volumeCalls: any[] = [];
// The outer daemon preflight proves this exact task-owned legacy name absent.
// Never enumerate or migrate a real provider's volume in this fixture.
const store = new WizardStorage(dirs, async () => rows, async (exe, args) => {
  assert.equal(exe, 'docker'); assert.deepEqual(args, ['volume', 'ls', '--format', '{{.Name}}']);
  volumeCalls.push([exe, ...args]); return { stdout: '' };
}, `${path.basename(stage)}-absent-legacy`, () => {});
const repo: any = { getAppByUrn: async () => ({ exposed: false, exposedLocal: false }) };
const docker = new DockerService(logger, cfg, manager, disk, { getApp: async () => ({ app: { userConfigEnabled: false } }) } as any, repo);
Object.defineProperty(docker, 'storage', { get: () => store });
const baselinePath = path.join(stage, 'base-wam/packages/backend/src/modules/docker/builders/compose.builder.ts');
const { DockerComposeBuilder: BaseBuilder } = await import(baselinePath);
await fs.mkdir(path.join(dataDir, 'user-config'), { recursive: true });
await fs.writeFile(path.join(dataDir, 'user-config/wizard-app-networks.json'), '{"enabled":true,"allow_host_access":[]}\n');
const metadata: any = { phase, scenario, apps: {}, adapters: ['synthetic confined file transport', 'task-owned legacy volume absence (daemon prechecked)'] };
for (const [i, app] of ['chatgpt', 'claude'].entries()) {
  const name = `wizard-${app}`, urn = `${name}:wizardapps` as any;
  const sourceDir = path.join(stage, 'sources', app);
  const { appInstalledDir, appDataDir } = manager.getAppPaths(urn);
  if (phase === 'install') {
    await fs.mkdir(appInstalledDir, { recursive: true });
    await fs.copyFile(path.join(sourceDir, 'wizard.md'), path.join(appInstalledDir, 'wizard.md'));
    await fs.copyFile(path.join(sourceDir, 'runtipi/docker-compose.yml'), path.join(appInstalledDir, 'docker-compose.yml'));
  }
  // Real create-only lifecycle entry point, not a hand-created equivalent root.
  await manager.setAppDataDirPermissions(urn);
  const env = `APP_DATA_DIR=${appDataDir}\nAPP_SOURCE_DIR=${sourceDir}\nAPP_PORT=${18811 + i}\n`;
  assert(await manager.writeAppEnv(urn, env));
  const source = (await manager.getSourceDockerComposeYaml(urn)).content; assert(source);
  const builder = new DockerComposeBuilder(), subnet = `10.237.${240 + i}.0/24`;
  const generated = builder.getDockerCompose(source, {}, urn, subnet, 'amd64');
  // The old builder ignores the new declaration; all ordinary services must be identical.
  const legacy = new BaseBuilder().getDockerCompose(source, {}, urn, subnet, 'amd64');
  await manager.writeDockerComposeYml(urn, generated);
  let applied = false;
  await docker.prepareApp(urn, async () => { applied = true; });
  assert(applied);
  const final = await manager.getGeneratedDockerComposeYaml(urn); assert(final.content);
  const actual = yaml.parse(final.content);
  const old = yaml.parse(await enforceAppNetworks(urn, legacy, dataDir));
  assert.deepEqual(actual.networks, old.networks);
  for (const [n, s] of Object.entries<any>(actual.services)) {
    if (n.endsWith('-data-init') || n.endsWith('-data-validate')) {
      assert.equal(s.volumes.length, 1); assert.equal(s.network_mode, 'none');
      assert.deepEqual(s.networks ?? {}, {}); assert.equal(s.read_only, true);
      assert(!('WIZARD_GRANTS_FILE' in (s.environment ?? {}))); assert(!('WIZARD_CONNECTION_REQUESTS' in (s.environment ?? {})));
      assert(!s['x-runtipi']);
    } else assert.deepEqual(s, old.services[n]);
  }
  const output = path.join(stage, 'evidence', `${scenario}-${phase}-${app}.prepared.yml`);
  await fs.writeFile(output, final.content);
  const args = await docker.getBaseComposeArgsApp(urn);
  assert.equal(args.isCustomConfig, false);
  metadata.apps[app] = { appDataDir, prepared: output, envFile: path.join(appDataDir, 'app.env'), composeArgs: args.args,
    target: path.join(appDataDir, app === 'chatgpt' ? 'data' : 'data/claude'), normalServicesExactlyMatchBase: true,
    completeGeneratedServices: actual.services, completeGeneratedNetworks: actual.networks, shareCarriers: actual['x-wizard-share-carriers'] };
}
metadata.volumeAbsenceCalls = volumeCalls;
await fs.writeFile(path.join(stage, 'evidence', `${scenario}-${phase}-manager.json`), JSON.stringify(metadata, null, 2) + '\n');
