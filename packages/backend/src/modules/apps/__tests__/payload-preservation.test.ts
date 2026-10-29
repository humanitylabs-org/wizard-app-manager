import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AppFilesManager } from '../app-files-manager';

vi.mock('node:fs', async (load) => load<typeof import('node:fs')>());
const roots: string[] = [];
afterEach(async () => { vi.restoreAllMocks(); for (const r of roots.splice(0)) await fs.rm(r, { recursive: true, force: true }); });
describe('existing payload preservation', () => {
  it('never mutates payload metadata, including deterministic symlink substitution during control sealing', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'payload-preservation-'));
    roots.push(root);
    const dataRoot = path.join(root, 'app-data');
    const app = path.join(dataRoot, 'wizardapps/demo');
    const payload = path.join(app, 'data');
    const sentinel = path.join(root, 'external-sentinel');
    await fs.mkdir(payload, { recursive: true });
    await fs.mkdir(sentinel);
    const secret = path.join(sentinel, 'app-secret');
    await fs.writeFile(secret, 'synthetic-secret', { mode: 0o600 });
    await fs.writeFile(path.join(root, '.env'), 'SYNTHETIC=1');
    const before = await fs.stat(secret);
    const manager: any = Object.create(AppFilesManager.prototype);
    manager.config = { get: () => ({ dataDir: root, appDataDir: dataRoot }) };
    manager.getAppPaths = () => ({ appDataDir: app });
    // Substitute at a deterministic suspension point inside the real method,
    // before it could have enumerated any app-controlled payload tree.
    const chmod = fs.chmod.bind(fs);
    let swapped = false;
    const calls: string[] = [];
    vi.spyOn(fs, 'chmod').mockImplementation(async (target, mode) => {
      calls.push(String(target));
      if (target === app && !swapped) {
        swapped = true;
        await fs.rename(payload, payload + '.saved');
        await fs.symlink(sentinel, payload, 'dir');
      }
      return chmod(target, mode);
    });
    await manager.setAppDataDirPermissions('demo:wizardapps');
    expect(swapped).toBe(true);
    expect(calls).toEqual([dataRoot, path.dirname(app), app]);
    const after = await fs.stat(secret);
    expect([after.uid, after.gid, after.mode]).toEqual([before.uid, before.gid, before.mode]);
    expect(await fs.readFile(secret, 'utf8')).toBe('synthetic-secret');
    expect((await fs.lstat(payload)).isSymbolicLink()).toBe(true);
  });
});
