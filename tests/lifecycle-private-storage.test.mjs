import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import {
  initializeAppDataDirectory,
  withAppDataDirectory,
  writeAppEnvironment,
} from '../packages/backend/src/core/filesystem/app-data-permissions.ts';

const sourceRoot = process.env.LIFECYCLE_SOURCE_ROOT ?? path.resolve(import.meta.dirname, '..');
const appSource = await fs.readFile(path.join(sourceRoot, 'packages/backend/src/modules/apps/app-files-manager.ts'), 'utf8');
const AsyncFunction = Object.getPrototypeOf(async () => undefined).constructor;
// Execute exact production method bodies without installing Nest/the full UI.
// Full decorated-class/lifecycle command tests remain in the forum Vitest gate.
function method(name) {
  const start = appSource.indexOf(`  public async ${name}(`);
  assert.notEqual(start, -1);
  const bodyStart = appSource.indexOf('{', start);
  let depth = 1;
  let end = bodyStart + 1;
  while (depth) {
    if (appSource[end] === '{') depth++;
    if (appSource[end] === '}') depth--;
    end++;
  }
  return new AsyncFunction(
    'appUrn',
    'env',
    'initializeAppDataDirectory',
    'writeAppEnvironment',
    'extractAppUrn',
    'execFileAsync',
    'path',
    appSource.slice(bodyStart + 1, end - 1),
  );
}
const methods = Object.fromEntries(['createAppDataDir', 'setAppDataDirPermissions', 'writeAppEnv'].map((name) => [name, method(name)]));
function manager(base) {
  const result = {
    configuration: { getConfig: () => ({ directories: { appDataDir: base } }) },
    getAppPaths: () => ({ appDataDir: path.join(base, 'stock', 'fixture') }),
    filesystem: {
      createDirectory: async (p) => {
        await fs.mkdir(p, { recursive: true });
        return true;
      },
      writeTextFile: async (p, data) => {
        await fs.writeFile(p, `${data}\n`);
        return true;
      },
    },
    logger: {
      error() {
        /* Synthetic fixture deliberately discards secret-free errors. */
      },
    },
  };
  for (const [name, fn] of Object.entries(methods))
    result[name] = (urn, env) =>
      fn.call(
        result,
        urn ?? 'fixture:stock',
        env,
        initializeAppDataDirectory,
        writeAppEnvironment,
        () => ({ appStoreId: 'stock', appName: 'fixture' }),
        promisify(execFile),
        path,
      );
  return result;
}
async function fixture(t) {
  const root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'lifecycle-permissions-'));
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const base = path.join(root, 'app-data');
  const app = path.join(base, 'stock', 'fixture');
  await fs.mkdir(path.join(app, 'private'), { recursive: true, mode: 0o700 });
  await fs.chmod(path.join(app, 'private'), 0o700);
  await fs.writeFile(path.join(app, 'private', 'synthetic-secret'), 'FAKE_ONLY', { mode: 0o600 });
  return { root, base, app, manager: manager(base) };
}
const mode = async (p) => (await fs.stat(p)).mode & 0o777;
for (const phase of ['restart', 'install', 'update']) {
  test(`${phase}: existing private file and directory modes are never widened`, async (t) => {
    const f = await fixture(t);
    const before = await fs.stat(f.app);
    await f.manager.setAppDataDirPermissions();
    assert.equal(await mode(path.join(f.app, 'private')), 0o700);
    assert.equal(await mode(path.join(f.app, 'private', 'synthetic-secret')), 0o600);
    const after = await fs.stat(f.app);
    assert.equal(after.uid, before.uid);
    assert.equal(after.gid, before.gid);
    assert.equal(after.mode, before.mode);
  });
}
test('live atomic writer: every post-lifecycle read remains owner-only', async (t) => {
  const f = await fixture(t);
  const secret = path.join(f.app, 'private', 'synthetic-secret');
  for (let n = 0; n < 50; n++) {
    const tmp = `${secret}.next`;
    await fs.writeFile(tmp, `FAKE_${n}`, { mode: 0o600 });
    await Promise.all([fs.rename(tmp, secret), f.manager.setAppDataDirPermissions()]);
    // Parent initialization cannot chmod the old OR new inode, even transiently.
    assert.equal(await mode(secret), 0o600);
    assert.equal(await mode(path.dirname(secret)), 0o700);
  }
});
test('legacy nonsecret data retains modes, ownership, and bytes', async (t) => {
  const f = await fixture(t);
  const legacy = path.join(f.app, 'legacy');
  await fs.writeFile(legacy, 'NONSECRET', { mode: 0o664 });
  await fs.chmod(legacy, 0o664);
  const before = await fs.stat(legacy);
  await f.manager.setAppDataDirPermissions();
  assert.equal(await mode(legacy), 0o664);
  assert.equal((await fs.stat(legacy)).uid, before.uid);
  assert.equal(await fs.readFile(legacy, 'utf8'), 'NONSECRET');
});
test('fresh app root is create-only; no fixed container UID is assigned', async (t) => {
  const f = await fixture(t);
  await initializeAppDataDirectory(f.base, 'newstore', 'fresh');
  const p = path.join(f.base, 'newstore', 'fresh');
  assert.equal((await fs.stat(p)).uid, process.getuid());
  await fs.chmod(p, 0o700);
  await initializeAppDataDirectory(f.base, 'newstore', 'fresh');
  assert.equal(await mode(p), 0o700);
});
test('app.env replacement starts private under umask 000 and repairs legacy mode', async (t) => {
  const f = await fixture(t);
  const env = path.join(f.app, 'app.env');
  await fs.writeFile(env, 'OLD_FAKE', { mode: 0o777 });
  await fs.chmod(env, 0o777);
  const old = process.umask(0);
  try {
    assert.equal(await f.manager.writeAppEnv('fixture:stock', 'FAKE=ONLY'), true);
  } finally {
    process.umask(old);
  }
  assert.equal(await mode(env), 0o600);
  assert.equal(await fs.readFile(env, 'utf8'), 'FAKE=ONLY\n');
  await f.manager.setAppDataDirPermissions();
  assert.equal(await mode(env), 0o600);
  assert.deepEqual(
    (await fs.readdir(f.app)).filter((s) => s.startsWith('.app-env-')),
    [],
  );
});
test('skipEnv lifecycle tightens only manager-owned app.env without reading bytes', async (t) => {
  const f = await fixture(t);
  const env = path.join(f.app, 'app.env');
  await fs.writeFile(env, 'OLD_FAKE', { mode: 0o777 });
  await fs.chmod(env, 0o777);
  const inode = (await fs.stat(env)).ino;
  await f.manager.setAppDataDirPermissions();
  assert.equal(await mode(env), 0o600);
  assert.equal((await fs.stat(env)).ino, inode);
  assert.equal(await fs.readFile(env, 'utf8'), 'OLD_FAKE');
});
test('failed private replacement preserves old bytes/mode and removes pending inode', async (t) => {
  const f = await fixture(t);
  const env = path.join(f.app, 'app.env');
  await writeAppEnvironment(f.base, 'stock', 'fixture', 'OLD_FAKE');
  const rename = fs.rename;
  fs.rename = async () => {
    throw new Error('synthetic rename failure');
  };
  try {
    assert.equal(await f.manager.writeAppEnv('fixture:stock', 'NEW_FAKE'), false);
  } finally {
    fs.rename = rename;
  }
  assert.equal(await fs.readFile(env, 'utf8'), 'OLD_FAKE\n');
  assert.equal(await mode(env), 0o600);
  assert.deepEqual(
    (await fs.readdir(f.app)).filter((s) => s.startsWith('.app-env-')),
    [],
  );
});
test('lifecycle rejects manager env symlink/hardlink and FIFO without changing target', async (t) => {
  const f = await fixture(t);
  const outside = path.join(f.root, 'outside-file');
  const env = path.join(f.app, 'app.env');
  await fs.writeFile(outside, 'FAKE_ONLY', { mode: 0o640 });
  await fs.symlink(outside, env);
  await assert.rejects(f.manager.setAppDataDirPermissions());
  await fs.unlink(env);
  await fs.link(outside, env);
  await assert.rejects(f.manager.setAppDataDirPermissions());
  await fs.unlink(env);
  await promisify(execFile)('mkfifo', [env]);
  await assert.rejects(f.manager.setAppDataDirPermissions());
  assert.equal(await mode(outside), 0o640);
});
for (const bad of ['../escape', '/absolute', 'nested/path', '.', '..', 'x:y', 'x\\y', '']) {
  test(`confines invalid app/store component ${JSON.stringify(bad)}`, async (t) => {
    const f = await fixture(t);
    await assert.rejects(initializeAppDataDirectory(f.base, 'stock', bad), /Unsafe/);
    await assert.rejects(initializeAppDataDirectory(f.base, bad, 'fixture'), /Unsafe/);
  });
}
for (const which of ['base', 'store', 'app']) {
  test(`rejects ${which} directory symlink without touching target`, async (t) => {
    const f = await fixture(t);
    const outside = path.join(f.root, 'outside');
    await fs.mkdir(outside, { mode: 0o700 });
    const target = which === 'base' ? f.base : which === 'store' ? path.join(f.base, 'stock') : f.app;
    await fs.rename(target, `${target}-old`);
    await fs.symlink(outside, target);
    await assert.rejects(f.manager.setAppDataDirPermissions());
    assert.equal(await mode(outside), 0o700);
    assert.deepEqual(await fs.readdir(outside), []);
  });
}
test('app.env symlink/hardlink rejected; outside target unchanged', async (t) => {
  const f = await fixture(t);
  const outside = path.join(f.root, 'outside-file');
  const env = path.join(f.app, 'app.env');
  await fs.writeFile(outside, 'FAKE_OUTSIDE', { mode: 0o640 });
  await fs.symlink(outside, env);
  assert.equal(await f.manager.writeAppEnv('fixture:stock', 'DO_NOT_WRITE'), false);
  await fs.unlink(env);
  await fs.link(outside, env);
  assert.equal(await f.manager.writeAppEnv('fixture:stock', 'DO_NOT_WRITE'), false);
  assert.equal(await mode(outside), 0o640);
  assert.equal(await fs.readFile(outside, 'utf8'), 'FAKE_OUTSIDE');
});
test('pinned parent cannot be redirected by a renamed ancestor', async (t) => {
  const f = await fixture(t);
  const outside = path.join(f.root, 'outside');
  await fs.mkdir(outside);
  await withAppDataDirectory(f.base, 'stock', 'fixture', async (directory) => {
    await fs.rename(f.app, `${f.app}-saved`);
    await fs.symlink(outside, f.app);
    await fs.writeFile(`/proc/self/fd/${directory.fd}/pinned`, 'FAKE_ONLY', { mode: 0o600 });
  });
  assert.deepEqual(await fs.readdir(outside), []);
  assert.equal(await mode(`${f.app}-saved/pinned`), 0o600);
});
test('base must be absolute and cannot contain dot traversal components', async () => {
  for (const base of ['relative', '/any/../outside', '/any/./outside']) {
    await assert.rejects(initializeAppDataDirectory(base, 'stock', 'fixture'), /Unsafe/);
  }
});
test('named-volume analogue and descendant symlink targets are never swept', async (t) => {
  const f = await fixture(t);
  const volume = path.join(f.root, 'named-volume');
  await fs.mkdir(volume, { mode: 0o700 });
  await fs.writeFile(path.join(volume, 'secret'), 'FAKE', { mode: 0o600 });
  await fs.symlink(volume, path.join(f.app, 'volume-link'));
  await f.manager.setAppDataDirPermissions();
  assert.equal(await mode(volume), 0o700);
  assert.equal(await mode(path.join(volume, 'secret')), 0o600);
});
test('install/update have no post-start permission-reset caller', async () => {
  for (const file of ['install-app-command.ts', 'update-app-command.ts']) {
    const s = await fs.readFile(path.join(sourceRoot, 'packages/backend/src/modules/app-lifecycle/commands', file), 'utf8');
    assert.doesNotMatch(s, /appFilesManager\.setAppDataDirPermissions/);
  }
});
