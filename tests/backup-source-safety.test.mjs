// Focused real-filesystem BackupManager regression. Run directly with node --test.
// Only DI/unused parsing dependencies and fixture constants are substituted; manager,
// filesystem, archive, packaging, app/file/exec helpers execute their production code.
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
const logger = { info() {}, debug() {}, error() {}, warn() {} };
const urn = 'demo:store';
const commit = 'a'.repeat(40);
const unused = () => { throw new Error('Unused dependency invoked by focused test'); };

function loadProduction(root) {
  const cache = new Map();
  const load = (file) => {
    file = path.resolve(file);
    if (cache.has(file)) return cache.get(file).exports;
    const source = process.env.BACKUP_TEST_BASE_REV
      ? execFileSync('git', ['show', `${process.env.BACKUP_TEST_BASE_REV}:${path.relative(repo, file)}`], { cwd: repo, encoding: 'utf8' })
      : fs.readFileSync(file, 'utf8');
    const code = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, experimentalDecorators: true, esModuleInterop: true },
      fileName: file,
    }).outputText;
    const mod = { exports: {} };
    cache.set(file, mod);
    const localRequire = (name) => {
      if (name === '@nestjs/common') return { Injectable: () => (value) => value };
      if (name === 'yaml') return { parse: unused };
      if (name === 'arktype') return { type: { errors: class {} } };
      if (name === '@/common/constants') return { DATA_DIR: root, APP_DATA_DIR: root, APP_DIR: root };
      if (['@/core/config/configuration.service', '@/core/logger/logger.service', '../apps/app-files-manager'].includes(name)) return {};
      if (name.startsWith('@/')) return load(path.join(backend, 'src', `${name.slice(2)}.ts`));
      if (name.startsWith('.')) return load(path.resolve(path.dirname(file), `${name}.ts`));
      return require(name);
    };
    new Function('require', 'module', 'exports', code)(localRequire, mod, mod.exports);
    return mod.exports;
  };
  return {
    ...load(path.join(backend, 'src/modules/backups/backup.manager.ts')),
    ...load(path.join(backend, 'src/core/filesystem/filesystem.service.ts')),
    ...load(path.join(backend, 'src/core/archive/archive.service.ts')),
    ...load(path.join(backend, 'src/modules/app-stores/packaging.ts')),
  };
}

async function fixture(t) {
  const root = await fs.promises.mkdtemp(path.join(process.env.TMPDIR || os.tmpdir(), 'backup-source-'));
  t.after(() => fs.promises.rm(root, { recursive: true, force: true }));
  const { BackupManager, FilesystemService, ArchiveService, sourceDir } = loadProduction(root);
  const filesystem = new FilesystemService(logger);
  filesystem.createTempDirectory = () => fs.promises.mkdtemp(path.join(root, 'staging-'));
  const archive = new ArchiveService(logger);
  const appDataDir = path.join(root, 'app-data/store/demo');
  const appInstalledDir = path.join(root, 'apps/store/demo');
  const context = sourceDir(path.join(root, 'app-data'), 'store', 'demo', commit);
  const write = async (file, content) => {
    await fs.promises.mkdir(path.dirname(file), { recursive: true });
    await fs.promises.writeFile(file, content);
  };
  await write(path.join(context, 'Dockerfile'), 'FROM scratch\n');
  await write(path.join(context, 'lib/code.txt'), 'saved source');
  await write(path.join(appDataDir, 'data.txt'), 'saved data');
  await write(path.join(appInstalledDir, '.wizard-commit'), commit);
  await write(path.join(appInstalledDir, 'docker-compose.yml'), `services:\n  demo:\n    build: ${context}\n`);
  const manager = new BackupManager(archive, logger, { get: () => ({ dataDir: root, appDataDir: path.join(root, 'app-data') }) }, filesystem,
    { getAppPaths: () => ({ appDataDir, appInstalledDir }) });
  const backup = async () => {
    await manager.backupApp(urn);
    const files = await fs.promises.readdir(path.join(root, 'backups/store/demo'));
    assert.equal(files.length, 1);
    return files[0];
  };
  const current = async () => {
    await write(path.join(context, 'Dockerfile'), 'CURRENT');
    await write(path.join(context, 'current-only.txt'), 'keep me');
    await write(path.join(appDataDir, 'data.txt'), 'current data');
  };
  const assertCurrent = async () => {
    assert.equal(await fs.promises.readFile(path.join(context, 'Dockerfile'), 'utf8'), 'CURRENT');
    assert.equal(await fs.promises.readFile(path.join(context, 'current-only.txt'), 'utf8'), 'keep me');
    assert.equal(await fs.promises.readFile(path.join(appDataDir, 'data.txt'), 'utf8'), 'current data');
    assert.equal(await fs.promises.readFile(path.join(appInstalledDir, '.wizard-commit'), 'utf8'), commit);
  };
  return { root, context, appDataDir, appInstalledDir, filesystem, archive, manager, write, backup, current, assertCurrent };
}

for (const kind of ['file', 'directory', 'absolute-internal', 'chained-internal']) {
  test(`backup dereferences confined ${kind} symlink and restores portable bytes after source pruning`, async (t) => {
    const f = await fixture(t);
    const link = kind === 'directory' ? 'linked-dir' : 'linked-file';
    const target = kind === 'directory' ? 'lib' : kind === 'absolute-internal' ? path.join(f.context, 'lib/code.txt') : 'lib/code.txt';
    if (kind === 'chained-internal') await fs.promises.symlink(target, path.join(f.context, 'intermediate'));
    await fs.promises.symlink(kind === 'chained-internal' ? 'intermediate' : target, path.join(f.context, link));
    const filename = await f.backup();
    const entries = await f.archive.listTarGz(path.join(f.root, 'backups/store/demo', filename));
    assert(entries.every((entry) => entry.type === '-' || entry.type === 'd'), 'archive must contain no symlinks or hardlinks');
    await fs.promises.rm(f.context, { recursive: true });
    await f.manager.restoreApp(urn, filename);
    const restored = path.join(f.context, link, ...(kind === 'directory' ? ['code.txt'] : []));
    assert.equal(await fs.promises.readFile(restored, 'utf8'), 'saved source');
    assert.equal((await fs.promises.lstat(path.join(f.context, link))).isSymbolicLink(), false);
    assert.equal(await fs.promises.readFile(path.join(f.appDataDir, 'data.txt'), 'utf8'), 'saved data');
    assert.match(await fs.promises.readFile(path.join(f.appInstalledDir, 'docker-compose.yml'), 'utf8'), /demo@aaaaaaaaaaaa/);
  });
}

for (const kind of ['escape', 'chained-escape', 'cycle', 'hardlink', 'fifo']) {
  test(`backup rejects ${kind} source without changing installed bytes`, async (t) => {
    const f = await fixture(t);
    await f.write(path.join(f.root, 'outside.txt'), 'outside');
    if (kind === 'escape') await fs.promises.symlink(path.join(f.root, 'outside.txt'), path.join(f.context, 'link'));
    if (kind === 'chained-escape') {
      await fs.promises.symlink('second', path.join(f.context, 'first'));
      await fs.promises.symlink(path.join(f.root, 'outside.txt'), path.join(f.context, 'second'));
    }
    if (kind === 'cycle') await fs.promises.symlink('.', path.join(f.context, 'loop'));
    if (kind === 'hardlink') await fs.promises.link(path.join(f.context, 'Dockerfile'), path.join(f.context, 'hard'));
    if (kind === 'fifo') execFileSync('mkfifo', [path.join(f.context, 'pipe')]);
    await assert.rejects(f.backup());
    assert.equal(await fs.promises.readFile(path.join(f.context, 'Dockerfile'), 'utf8'), 'FROM scratch\n');
    assert.equal(await fs.promises.readFile(path.join(f.root, 'outside.txt'), 'utf8'), 'outside');
  });
}

test('same-commit restore replaces source completely only after staging succeeds', async (t) => {
  const f = await fixture(t);
  const filename = await f.backup();
  await f.current();
  await f.manager.restoreApp(urn, filename);
  assert.equal(await fs.promises.readFile(path.join(f.context, 'Dockerfile'), 'utf8'), 'FROM scratch\n');
  assert.equal(fs.existsSync(path.join(f.context, 'current-only.txt')), false);
  assert.equal(await fs.promises.readFile(path.join(f.appDataDir, 'data.txt'), 'utf8'), 'saved data');
  assert.deepEqual(await fs.promises.readdir(path.dirname(f.context)), [path.basename(f.context)]);
});

test('same-commit partial copy failure keeps the existing source and installed data', async (t) => {
  const f = await fixture(t);
  const filename = await f.backup();
  await f.current();
  const copy = f.filesystem.copyDirectory.bind(f.filesystem);
  f.filesystem.copyDirectory = async (src, dest, options) => {
    if (path.basename(src) === 'app-source') {
      await f.write(path.join(dest, 'partial.txt'), 'partial');
      return false;
    }
    return copy(src, dest, options);
  };
  await assert.rejects(f.manager.restoreApp(urn, filename), /source context/);
  await f.assertCurrent();
  assert.equal(fs.existsSync(path.join(f.context, 'partial.txt')), false);
  assert.deepEqual(await fs.promises.readdir(path.dirname(f.context)), [path.basename(f.context)]);
});

for (const phase of ['move-current', 'commit-stage']) {
  test(`restore rolls back when ${phase} rename fails`, async (t) => {
    const f = await fixture(t);
    const filename = await f.backup();
    await f.current();
    const rename = fs.promises.rename;
    let injected = false;
    t.after(() => { fs.promises.rename = rename; });
    fs.promises.rename = async (src, dest) => {
      if (!injected && (phase === 'move-current' ? src === f.context : dest === f.context)) {
        injected = true;
        throw Object.assign(new Error('injected rename failure'), { code: 'EIO' });
      }
      return rename(src, dest);
    };
    await assert.rejects(f.manager.restoreApp(urn, filename));
    assert.equal(injected, true, 'must exercise the production rename commit path');
    await f.assertCurrent();
    assert.deepEqual(await fs.promises.readdir(path.dirname(f.context)), [path.basename(f.context)]);
  });
}

test('backup revalidates a source link changed between admission and copy', async (t) => {
  const f = await fixture(t);
  const link = path.join(f.context, 'link');
  await fs.promises.symlink('lib/code.txt', link);
  await f.write(path.join(f.root, 'outside.txt'), 'outside');
  const copy = f.filesystem.copyDirectory.bind(f.filesystem);
  let changed = false;
  f.filesystem.copyDirectory = async (src, dest, options) => {
    if (src === f.context) {
      changed = true;
      await fs.promises.unlink(link);
      await fs.promises.symlink(path.join(f.root, 'outside.txt'), link);
    }
    return copy(src, dest, options);
  };
  await assert.rejects(f.backup());
  assert.equal(changed, true);
  assert.equal(fs.existsSync(path.join(f.root, 'backups/store/demo')), false);
});

test('a failed rollback retains the previous source on disk for recovery', async (t) => {
  const f = await fixture(t);
  const filename = await f.backup();
  await f.current();
  const rename = fs.promises.rename;
  t.after(() => { fs.promises.rename = rename; });
  fs.promises.rename = async (src, dest) => {
    if (dest === f.context) throw new Error('injected commit and rollback failure');
    return rename(src, dest);
  };
  await assert.rejects(f.manager.restoreApp(urn, filename));
  const siblings = await fs.promises.readdir(path.dirname(f.context));
  assert.equal(siblings.length, 1);
  assert(siblings[0].includes('restore-'));
  const previous = path.join(path.dirname(f.context), siblings[0], 'previous');
  assert.equal(await fs.promises.readFile(path.join(previous, 'Dockerfile'), 'utf8'), 'CURRENT');
  // Reinstall/update invokes this production cleanup even for the same commit.
  // Recovery bytes must live outside its app@commit source namespace.
  const { pruneSources, sourceDirName } = loadProduction(f.root);
  await pruneSources(path.join(f.root, 'app-data'), 'store', 'demo', sourceDirName('demo', commit));
  assert.equal(await fs.promises.readFile(path.join(previous, 'Dockerfile'), 'utf8'), 'CURRENT');
  assert.equal(await fs.promises.readFile(path.join(previous, 'current-only.txt'), 'utf8'), 'keep me');
  assert.equal(await fs.promises.readFile(path.join(f.appDataDir, 'data.txt'), 'utf8'), 'current data');
});

test('archive source symlinks and hardlinks remain rejected before extraction', async (t) => {
  const f = await fixture(t);
  const filename = await f.backup();
  await f.current();
  for (const type of ['l', 'h']) {
    f.archive.listTarGz = async () => [{ path: './app-source/link', type }];
    f.archive.extractTarGz = async () => { assert.fail('unsafe archive must not be extracted'); };
    await assert.rejects(f.manager.restoreApp(urn, filename), /unsupported file types/);
    await f.assertCurrent();
  }
});
