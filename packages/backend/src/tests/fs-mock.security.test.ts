import fs, { constants } from 'node:fs';
import realFs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { initializeAppDataDirectory, writeAppEnvironment } from '@/core/filesystem/app-data-permissions';

// Do not model /proc/self/fd, O_NOFOLLOW or ownership in memfs. These cases
// deliberately exercise the real async filesystem beneath disposable TMPDIR.
let root: string;
beforeEach(async () => {
  root = await realFs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'fs-mock-security-'));
});
afterEach(async () => {
  if (root) await realFs.rm(root, { recursive: true, force: true });
});

test('named fs constants retain the native descriptor security flags', async () => {
  const native = await vi.importActual<typeof import('node:fs')>('node:fs');
  for (const name of ['O_RDONLY', 'O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_WRONLY', 'O_CREAT', 'O_EXCL'] as const) {
    expect(constants[name], name).toBe(native.constants[name]);
    expect(typeof constants[name], name).toBe('number');
  }
  expect(constants.O_DIRECTORY).not.toBe(0);
  expect(constants.O_NOFOLLOW).not.toBe(0);
});

test('default fs stays in memfs while node:fs/promises stays on the real filesystem', async () => {
  const mocked = path.join(root, 'mock-only');
  const actual = path.join(root, 'real-only');
  await fs.promises.mkdir(root, { recursive: true });
  await fs.promises.writeFile(mocked, 'SYNTHETIC_MEMFS_ONLY');
  await realFs.writeFile(actual, 'SYNTHETIC_REAL_ONLY');
  expect(await fs.promises.readFile(mocked, 'utf8')).toBe('SYNTHETIC_MEMFS_ONLY');
  await expect(realFs.stat(mocked)).rejects.toMatchObject({ code: 'ENOENT' });
  expect(await realFs.readFile(actual, 'utf8')).toBe('SYNTHETIC_REAL_ONLY');
  await expect(fs.promises.stat(actual)).rejects.toMatchObject({ code: 'ENOENT' });
});

test('real lifecycle helpers preserve private inode modes and ownership with the global mock active', async () => {
  const base = path.join(root, 'app-data');
  const app = path.join(base, 'store', 'demo');
  await initializeAppDataDirectory(base, 'store', 'demo');
  const privateDir = path.join(app, 'private');
  const secret = path.join(privateDir, 'synthetic-secret');
  await realFs.mkdir(privateDir, { mode: 0o700 });
  await realFs.writeFile(secret, 'FAKE_ONLY', { mode: 0o600 });
  const before = await realFs.stat(secret);
  for (const phase of ['install', 'restart', 'update']) {
    await initializeAppDataDirectory(base, 'store', 'demo');
    await writeAppEnvironment(base, 'store', 'demo', 'FAKE_ENV=ONLY');
    const after = await realFs.stat(secret);
    expect(after.ino, phase).toBe(before.ino);
    expect(after.uid, phase).toBe(before.uid);
    expect(after.gid, phase).toBe(before.gid);
    expect(after.mode & 0o777, phase).toBe(0o600);
    expect((await realFs.stat(privateDir)).mode & 0o777, phase).toBe(0o700);
    expect((await realFs.stat(path.join(app, 'app.env'))).mode & 0o777, phase).toBe(0o600);
  }
});

test('real no-follow refuses a symlink ancestor and env target without changing outside bytes or metadata', async () => {
  const base = path.join(root, 'app-data');
  const outside = path.join(root, 'outside');
  await realFs.mkdir(outside, { mode: 0o700 });
  const target = path.join(outside, 'target');
  await realFs.writeFile(target, 'FAKE_OUTSIDE', { mode: 0o600 });
  const before = await realFs.stat(target);
  await realFs.mkdir(path.join(base, 'store'), { recursive: true });
  await realFs.symlink(outside, path.join(base, 'store', 'linked'));
  await expect(initializeAppDataDirectory(base, 'store', 'linked')).rejects.toThrow();
  await initializeAppDataDirectory(base, 'store', 'demo');
  await realFs.symlink(target, path.join(base, 'store', 'demo', 'app.env'));
  await expect(initializeAppDataDirectory(base, 'store', 'demo')).rejects.toThrow();
  await expect(writeAppEnvironment(base, 'store', 'demo', 'FAKE_REPLACEMENT')).rejects.toThrow('Unsafe app environment file');
  const after = await realFs.stat(target);
  expect([after.ino, after.uid, after.gid, after.mode]).toEqual([before.ino, before.uid, before.gid, before.mode]);
  expect(await realFs.readFile(target, 'utf8')).toBe('FAKE_OUTSIDE');
  expect(await realFs.readdir(outside)).toEqual(['target']);
});
