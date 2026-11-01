#!/usr/bin/env node
/** Synthetic Linux/Docker gate. Never run on an owner install or the VPS.
 * Explicit opt-in + exact daemon context + a fresh QA namespace are mandatory.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { initializeAppDataDirectory, writeAppEnvironment } from '../packages/backend/src/core/filesystem/app-data-permissions.ts';
const exec = promisify(execFile);
const IMAGE = 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc';
assert.equal(process.env.WIZARD_LIFECYCLE_SYNTHETIC, '1', 'explicit synthetic-only opt-in required');
assert.ok(process.env.WIZARD_QA_DOCKER_CONTEXT, 'exact daemon context required');
assert.ok(process.env.TMPDIR?.startsWith('/srv/work/'), 'forum /srv/work task-local TMPDIR required');
const context = process.env.WIZARD_QA_DOCKER_CONTEXT;
const docker = async (...args) => (await exec('docker', ['--context', context, ...args], { maxBuffer: 4 * 1024 * 1024 })).stdout;
const namespace = `lifecycle-qa-${randomUUID().slice(0, 12)}`;
const root = await fs.mkdtemp(path.join(process.env.TMPDIR, `${namespace}-`));
const base = path.join(root, 'app-data');
const volumes = [];
const containers = [];
const allContainers = [];
const results = [];
const check = async (name, fn) => {
  await fn();
  results.push({ name, pass: true });
};
const run = async (mount, script, user = '0:0', args = []) =>
  docker('run', '--rm', '--network', 'none', '--read-only', '--user', user, '--mount', mount, IMAGE, 'sh', '-ec', script, 'synthetic', ...args);
try {
  await docker('info', '--format', '{{.OSType}}/{{.Architecture}}').then((s) => assert.match(s, /^linux\/(x86_64|amd64)/));
  for (const uid of [1101, 2202]) {
    const app = `app-${uid}`;
    const appRoot = path.join(base, 'stock', app);
    const privateDir = path.join(appRoot, 'private');
    await initializeAppDataDirectory(base, 'stock', app);
    // Existing supported recipe init-service pattern. No inferred image uid,
    // no global chmod and no owner data: only this newly-created fixture leaf.
    await run(`type=bind,src=${appRoot},dst=/state`, 'mkdir /state/private; chown "$1:$1" /state/private; chmod 700 /state/private', '0:0', [
      String(uid),
    ]);
    const mount = `type=bind,src=${privateDir},dst=/data`;
    await check(`fresh uid ${uid} writes via explicit init-service ownership`, async () => {
      await run(mount, 'umask 077; printf FAKE_ONLY > /data/secret; test "$(stat -c %a /data/secret)" = 600', `${uid}:${uid}`);
    });
    for (const phase of ['install', 'restart', 'update']) {
      await check(`${phase} uid ${uid}: existing 0600/0700 retained`, async () => {
        await initializeAppDataDirectory(base, 'stock', app);
        await writeAppEnvironment(base, 'stock', app, 'SYNTHETIC=ONLY');
        assert.equal((await fs.stat(path.join(appRoot, 'app.env'))).mode & 0o777, 0o600);
        await run(
          mount,
          'test "$(stat -c %a /data)" = 700; test "$(stat -c %a /data/secret)" = 600; printf FAKE_AGAIN >> /data/secret',
          `${uid}:${uid}`,
        );
      });
    }
    const name = `${namespace}-${uid}`;
    containers.push(name);
    allContainers.push(name);
    // Continuously verify the writer inode from the container as well as after
    // every host lifecycle call; no credential contents leave the container.
    await docker(
      'run',
      '-d',
      '--name',
      name,
      '--network',
      'none',
      '--read-only',
      '--user',
      `${uid}:${uid}`,
      '--mount',
      mount,
      IMAGE,
      'sh',
      '-ec',
      'umask 077; while :; do printf FAKE_ONLY > /data/next; mv /data/next /data/secret; test "$(stat -c %a /data/secret)" = 600 || exit 41; test "$(stat -c %a /data)" = 700 || exit 42; done',
    );
    await check(`live uid ${uid}: no mode opening while atomic writer runs`, async () => {
      for (let i = 0; i < 100; i++) {
        await initializeAppDataDirectory(base, 'stock', app);
        await docker('exec', name, 'sh', '-ec', 'test "$(stat -c %a /data/secret)" = 600; test "$(stat -c %a /data)" = 700');
      }
      assert.equal((await docker('inspect', name, '--format', '{{.State.Running}}')).trim(), 'true');
    });
    await docker('rm', '-f', name);
    containers.splice(containers.indexOf(name), 1);
  }
  const volume = `${namespace}-volume`;
  volumes.push(volume);
  await docker('volume', 'create', '--label', `wizard.synthetic=${namespace}`, volume);
  const vm = `type=volume,src=${volume},dst=/volume`;
  await run(vm, 'umask 077; mkdir /volume/private; printf FAKE > /volume/private/secret; chmod 700 /volume/private');
  await check('actual named volume is not swept by app-data lifecycle', async () => {
    await initializeAppDataDirectory(base, 'stock', 'app-1101');
    await run(vm, 'test "$(stat -c %a /volume/private)" = 700; test "$(stat -c %a /volume/private/secret)" = 600');
  });
  await check('legacy nonsecret data and symlink targets remain unchanged', async () => {
    const app = path.join(base, 'stock', 'app-1101');
    const legacy = path.join(app, 'legacy');
    await fs.writeFile(legacy, 'NONSECRET', { mode: 0o664 });
    await fs.chmod(legacy, 0o664);
    const outside = path.join(root, 'outside');
    await fs.mkdir(outside, { mode: 0o700 });
    await fs.symlink(outside, path.join(app, 'outside-link'));
    await initializeAppDataDirectory(base, 'stock', 'app-1101');
    assert.equal((await fs.stat(legacy)).mode & 0o777, 0o664);
    assert.equal((await fs.stat(outside)).mode & 0o777, 0o700);
    await fs.symlink(outside, path.join(base, 'stock', 'symlink-app'));
    await assert.rejects(initializeAppDataDirectory(base, 'stock', 'symlink-app'));
    await assert.rejects(initializeAppDataDirectory(base, 'stock', '../outside'));
    assert.deepEqual(await fs.readdir(outside), []);
  });
} finally {
  for (const name of containers) await docker('rm', '-f', name);
  for (const name of volumes) await docker('volume', 'rm', name);
  // Dedicated root helper removes only this newly-created synthetic tree's
  // contents, including UID-owned 0700 leaves. Never mounts source/owner state.
  await run(`type=bind,src=${root},dst=/fixture`, 'rm -rf /fixture/app-data /fixture/outside');
  await fs.rm(root, { recursive: true, force: true });
  const remainingContainers = await docker('container', 'ls', '-a', '--format', '{{.Names}}');
  const remainingVolumes = await docker('volume', 'ls', '--format', '{{.Name}}');
  for (const name of allContainers) assert.ok(!remainingContainers.split(/\r?\n/).includes(name));
  for (const name of volumes) assert.ok(!remainingVolumes.split(/\r?\n/).includes(name));
}
