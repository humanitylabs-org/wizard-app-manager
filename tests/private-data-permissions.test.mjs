// Real-filesystem lifecycle regression; only Docker, compose generation and DI are fixtures.
// Run after packages/common build: node --test tests/private-data-permissions.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const backend = path.join(repo, 'packages/backend');
const require = createRequire(path.join(backend, 'package.json'));
const ts = require('typescript');
const common = require('@runtipi/common/schemas');
const yaml = require('yaml');
const logger = { info() {}, debug() {}, error() {}, warn() {} };
const urn = 'demo:store';
const compose = common.convertLegacyToYaml({ schemaVersion: 2, services: [{ name: 'app', image: 'example:1', isMain: true, internalPort: 80 }] });

function loadProduction(root) {
  const cache = new Map();
  const tokens = new Map();
  const real = new Set([
    'modules/apps/app-files-manager', 'core/filesystem/filesystem.service',
    'core/archive/archive.service', 'modules/backups/backup.manager',
    'modules/app-lifecycle/commands/command', 'modules/app-lifecycle/commands/install-app-command',
    'modules/app-lifecycle/commands/update-app-command', 'modules/app-stores/packaging',
    'common/helpers/exec-helpers', 'common/helpers/app-helpers', 'common/helpers/file-helpers',
  ]);
  const load = (name) => {
    if (cache.has(name)) return cache.get(name).exports;
    const file = path.join(backend, 'src', `${name}.ts`);
    const source = process.env.PERMISSION_TEST_BASE_REV
      ? execFileSync('git', ['show', `${process.env.PERMISSION_TEST_BASE_REV}:${path.relative(repo, file)}`], { cwd: repo, encoding: 'utf8' })
      : fs.readFileSync(file, 'utf8');
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, experimentalDecorators: true, esModuleInterop: true }, fileName: file,
    }).outputText;
    const mod = { exports: {} };
    cache.set(name, mod);
    const localRequire = (specifier) => {
      if (specifier === '@nestjs/common') return { Injectable: () => (value) => value };
      if (specifier === '@sentry/nestjs') return { captureException() {} };
      if (specifier === '@/common/constants') return { DATA_DIR: root, APP_DATA_DIR: path.join(root, 'app-data'), APP_DIR: root, APP_REL_COMPOSE_FILENAME: 'docker-compose.yml', APP_GENERATED_COMPOSE_FILENAME: 'docker-compose.generated.yml' };
      if (specifier === '@/modules/docker/builders/compose.builder') return { DockerComposeBuilder: class { getDockerCompose() { return yaml.stringify(compose); } } };
      const resolved = specifier.startsWith('@/') ? specifier.slice(2)
        : specifier.startsWith('.') ? path.posix.normalize(path.posix.join(path.posix.dirname(name), specifier)) : null;
      if (resolved && real.has(resolved)) return load(resolved);
      if (resolved) {
        if (!tokens.has(resolved)) tokens.set(resolved, new Proxy({}, { get: (obj, key) => obj[key] ??= class {} }));
        return tokens.get(resolved);
      }
      return require(specifier);
    };
    new Function('require', 'module', 'exports', code)(localRequire, mod, mod.exports);
    return mod.exports;
  };
  return { load, tokens };
}

async function fixture(t, oldPolicy, nextPolicy = oldPolicy) {
  const root = await fs.promises.mkdtemp(path.join(process.env.TMPDIR || os.tmpdir(), 'private-permissions-'));
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  const { load, tokens } = loadProduction(root);
  const { AppFilesManager } = load('modules/apps/app-files-manager');
  const { FilesystemService } = load('core/filesystem/filesystem.service');
  const { ArchiveService } = load('core/archive/archive.service');
  const { BackupManager } = load('modules/backups/backup.manager');
  const { InstallAppCommand } = load('modules/app-lifecycle/commands/install-app-command');
  const { UpdateAppCommand } = load('modules/app-lifecycle/commands/update-app-command');
  const dirs = { dataDir: root, appDataDir: path.join(root, 'app-data') };
  const configuration = { getConfig: () => ({ directories: dirs }), get: (key) => key === 'directories' ? dirs : key === 'userSettings' ? { maxBackups: 5 } : 'amd64' };
  const filesystem = new FilesystemService(logger);
  filesystem.createTempDirectory = () => fs.promises.mkdtemp(path.join(root, 'staging-'));
  const manager = new AppFilesManager(configuration, filesystem, logger);
  const paths = manager.getAppPaths(urn);
  const secret = path.join(paths.appDataDir, 'private', 'synthetic-secret');
  const write = async (p, value) => { await fs.promises.mkdir(path.dirname(p), { recursive: true }); await fs.promises.writeFile(p, value); };
  const config = (privateData) => ({ id: 'demo', available: true, name: 'Synthetic demo', tipi_version: 1, short_desc: 'Synthetic', author: 'Test', source: 'https://example.com', ...(privateData === undefined ? {} : { private_data: privateData }) });
  const recipe = async (policy) => {
    await write(path.join(paths.appInstalledDir, 'config.json'), JSON.stringify(config(policy)));
    await write(path.join(paths.appInstalledDir, 'docker-compose.yml'), yaml.stringify(compose));
  };
  await recipe(oldPolicy);
  await write(path.join(paths.appDataDir, 'app.env'), 'APP_PORT=8000\n');
  await write(secret, 'synthetic fixture only');
  await fs.promises.chmod(path.dirname(secret), 0o700);
  await fs.promises.chmod(secret, 0o600);
  const modes = async () => [(await fs.promises.stat(path.dirname(secret))).mode & 0o777, (await fs.promises.stat(secret)).mode & 0o777];
  const backup = new BackupManager(new ArchiveService(logger), logger, configuration, filesystem, manager);
  const backups = [];
  const backupService = {
    backupApp: async (...args) => { backups.push(await modes()); return backup.backupApp(...args); },
    cleanupOldBackups: (...args) => backup.cleanupOldBackups(...args),
  };
  const operations = [];
  let failUp = false;
  const dockerService = {
    prepareApp: async () => { operations.push('prepare'); },
    composeApp: async (_urn, operation) => {
      operations.push(operation);
      if (operation.startsWith('up ') && failUp) { failUp = false; throw new Error('synthetic up failure'); }
      // Simulate an app init making its private files owner-only on install/start.
      if (operation.startsWith('up ')) { await fs.promises.chmod(path.dirname(secret), 0o700); await fs.promises.chmod(secret, 0o600); }
      return { success: true, stdout: '', stderr: '' };
    },
  };
  const appHelpers = { generateEnvFile: async () => {} };
  const marketplace = { getSourceDockerComposeYaml: async () => ({ content: compose }), copyAppFromRepoToInstalled: async () => recipe(nextPolicy), copyDataDir: async () => {} };
  const services = new Map([[AppFilesManager, manager], [FilesystemService, filesystem], [BackupManager, backupService]]);
  const named = {
    'core/config/configuration.service': ['ConfigurationService', configuration],
    'core/logger/logger.service': ['LoggerService', logger],
    'modules/docker/docker.service': ['DockerService', dockerService],
    'modules/apps/app.helpers': ['AppHelpers', appHelpers],
    'modules/apps/apps.repository': ['AppsRepository', { getAppByUrn: async () => ({ id: 1, subnet: '10.0.0.0/24', maxBackups: 5 }), updateAppById: async () => {} }],
    'modules/marketplace/marketplace.service': ['MarketplaceService', marketplace],
    'modules/env/env.utils': ['EnvUtils', { envStringToMap: () => new Map() }],
    'modules/network/subnet-manager.service': ['SubnetManagerService', { allocateSubnet: async () => '10.0.0.0/24' }],
  };
  for (const [name, [key, value]] of Object.entries(named)) services.set(tokens.get(name)[key], value);
  const moduleRef = { get: (key) => { assert.ok(services.has(key), 'unexpected DI dependency'); return services.get(key); } };
  const docker = { pruneContainers: async () => ({ ContainersDeleted: [], SpaceReclaimed: 0 }) };
  const install = new InstallAppCommand(moduleRef, docker);
  const update = new UpdateAppCommand(moduleRef, docker, true, true);
  return { root, paths, modes, manager, backups, install, update, failNextUp: () => { failUp = true; } };
}

test('private install preserves init-owned 0700/0600 after compose up', async (t) => {
  const f = await fixture(t, true, true);
  const result = await f.install.execute(urn, {});
  assert.equal(result.success, true, result.message);
  assert.deepEqual(await f.modes(), [0o700, 0o600]);
});

for (const [name, oldPolicy, nextPolicy] of [['existing opt-in', true, true], ['first opt-in', undefined, true], ['old opt-in cannot be removed in-flight', true, false]]) {
  test(`private update and its real archive preserve 0700/0600: ${name}`, async (t) => {
    const f = await fixture(t, oldPolicy, nextPolicy);
    const result = await f.update.execute(urn, {});
    assert.equal(result.success, true, result.message);
    assert.deepEqual(f.backups, [[0o700, 0o600]]);
    assert.deepEqual(await f.modes(), [0o700, 0o600]);
    const backupDir = path.join(f.root, 'backups/store/demo');
    const archive = (await fs.promises.readdir(backupDir)).find((p) => p.endsWith('.tar.gz'));
    assert.ok(archive);
    const extracted = path.join(f.root, 'extracted');
    await fs.promises.mkdir(extracted);
    execFileSync('tar', ['-xzf', path.join(backupDir, archive), '-C', extracted]);
    const findSecret = async (dir) => {
      for (const entry of await fs.promises.readdir(dir, { withFileTypes: true })) {
        const p = path.join(dir, entry.name);
        if (entry.name === 'synthetic-secret') return p;
        if (entry.isDirectory()) { const found = await findSecret(p); if (found) return found; }
      }
    };
    const secret = await findSecret(extracted);
    assert.ok(secret);
    assert.equal((await fs.promises.stat(secret)).mode & 0o777, 0o600);
    assert.equal((await fs.promises.stat(path.dirname(secret))).mode & 0o777, 0o700);
  });
}

test('first opt-in remains private during recovery to the old unflagged recipe', async (t) => {
  const f = await fixture(t, undefined, true);
  f.failNextUp();
  assert.equal((await f.update.execute(urn, {})).success, false);
  assert.deepEqual(await f.modes(), [0o700, 0o600]);
  assert.deepEqual(f.backups, [[0o700, 0o600]]);
});

test('legacy apps retain a+rwx; malformed or unreadable declarations fail closed', async (t) => {
  const f = await fixture(t, undefined);
  await f.manager.setAppDataDirPermissions(urn);
  assert.deepEqual(await f.modes(), [0o777, 0o777]);
  await fs.promises.writeFile(path.join(f.paths.appInstalledDir, 'config.json'), '{"private_data":"false"}');
  await assert.rejects(f.manager.setAppDataDirPermissions(urn), /Invalid private_data/);
  await fs.promises.unlink(path.join(f.paths.appInstalledDir, 'config.json'));
  await assert.rejects(f.manager.setAppDataDirPermissions(urn), /Cannot determine/);
});
