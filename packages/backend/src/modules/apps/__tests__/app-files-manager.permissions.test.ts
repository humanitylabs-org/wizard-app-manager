import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import type { AppUrn } from '@runtipi/common/types';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppFilesManager } from '../app-files-manager';

// Real synthetic files; only the explicitly injected race/procfs failures below are mocked.
const mode = async (p: string) => (await fs.lstat(p)).mode & 0o7777;

describe('setAppDataDirPermissions', () => {
  let root: string;
  let outside: string;
  let manager: AppFilesManager;
  let logger: LoggerService;
  const app = () => path.join(root, 'wizardapps', 'wizard-agent-service');
  const grants = () => path.join(app(), 'data/wizard-os/grants');

  beforeEach(async () => {
    root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'app-data-'));
    outside = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'outside-'));
    await fs.mkdir(grants(), { recursive: true });
    await fs.writeFile(path.join(grants(), 'agent-service.json'), '{"outgoing":{}}\n', { mode: 0o600 });
    await fs.chmod(grants(), 0o700);
    await fs.mkdir(path.join(app(), 'data/state'), { recursive: true });
    await fs.writeFile(path.join(app(), 'data/state/notes.json'), '{}', { mode: 0o644 });
    await fs.writeFile(path.join(outside, 'target'), 'x', { mode: 0o600 });
    logger = { debug: vi.fn(), error: vi.fn(), warn: vi.fn() } as unknown as LoggerService;
    const configuration = {
      getConfig: vi.fn().mockReturnValue({ directories: { appDataDir: root, dataDir: root, appDir: root } }),
    } as unknown as ConfigurationService;
    manager = new AppFilesManager(configuration, {} as FilesystemService, logger);
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    await fs.rm(root, { recursive: true, force: true });
    await fs.rm(outside, { recursive: true, force: true });
  });

  it('opens the app data folder to everyone but keeps the gateway grant keys owner-only', async () => {
    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(app())).toBe(0o777);
    expect(await mode(path.join(app(), 'data/state'))).toBe(0o777);
    expect(await mode(path.join(app(), 'data/state/notes.json'))).toBe(0o777);
    expect(await mode(path.join(app(), 'data/wizard-os'))).toBe(0o777);
    expect(await mode(grants())).toBe(0o700);
    expect(await mode(path.join(grants(), 'agent-service.json'))).toBe(0o600);
    expect(logger.error).not.toHaveBeenCalled();
  });

  it('brings grant keys an earlier start opened back to 0700 / 0600', async () => {
    await fs.chmod(grants(), 0o777);
    await fs.chmod(path.join(grants(), 'agent-service.json'), 0o777);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(grants())).toBe(0o700);
    expect(await mode(path.join(grants(), 'agent-service.json'))).toBe(0o600);
  });

  it('never follows a link out of the app data folder', async () => {
    await fs.symlink(path.join(outside, 'target'), path.join(app(), 'data/state/link'));
    await fs.symlink(outside, path.join(app(), 'data/state/dir-link'));
    // A link where the grants folder should be: nothing behind it is touched.
    await fs.rm(grants(), { recursive: true });
    await fs.symlink(outside, grants());

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(path.join(outside, 'target'))).toBe(0o600);
    expect(await mode(outside)).toBe(0o700);
    expect(await mode(path.join(app(), 'data/state/notes.json'))).toBe(0o777);
  });

  it.each(['data', 'data/wizard-os'])('does not follow an intermediate %s link', async (component) => {
    const link = path.join(app(), component);
    const targetGrants = path.join(outside, component === 'data' ? 'wizard-os/grants' : 'grants');
    await fs.mkdir(targetGrants, { recursive: true });
    await fs.chmod(targetGrants, 0o755);
    const target = path.join(targetGrants, 'agent-service.json');
    await fs.writeFile(target, '{}', { mode: 0o644 });
    await fs.rm(link, { recursive: true });
    await fs.symlink(outside, link);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(targetGrants)).toBe(0o755);
    expect(await mode(target)).toBe(0o644);
    expect(logger.error).not.toHaveBeenCalled();
  });

  it.each([0o700, 0o400])('normalizes an owner-only file mode %i to exact 0600', async (initialMode) => {
    const key = path.join(grants(), 'agent-service.json');
    await fs.chmod(key, initialMode);
    const before = await fs.stat(key);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(key)).toBe(0o600);
    const after = await fs.stat(key);
    expect([after.uid, after.gid]).toEqual([before.uid, before.gid]);
  });

  it('skips hardlinks outside the app and warns once', async () => {
    const target = path.join(outside, 'target');
    await fs.chmod(target, 0o644);
    await fs.link(target, path.join(grants(), 'hardlink-a'));
    await fs.link(target, path.join(grants(), 'hardlink-b'));

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(target)).toBe(0o644);
    expect(logger.warn).toHaveBeenCalledExactlyOnceWith('Skipped hardlinked private grant files; permissions unchanged');
  });

  it('keeps file opens pinned when grants is replaced after enumeration', async () => {
    const target = path.join(outside, 'agent-service.json');
    await fs.writeFile(target, '{}', { mode: 0o644 });
    await fs.chmod(path.join(grants(), 'agent-service.json'), 0o644);
    const readDirectory = fs.readdir.bind(fs);
    vi.spyOn(fs, 'readdir').mockImplementationOnce((async (directory, options) => {
      const entries = await readDirectory(directory, { withFileTypes: true });
      await fs.rename(grants(), `${grants()}-pinned`);
      await fs.symlink(outside, grants());
      return entries;
    }) as typeof fs.readdir);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(target)).toBe(0o644);
    expect(await mode(path.join(`${grants()}-pinned`, 'agent-service.json'))).toBe(0o600);
    expect(logger.error).not.toHaveBeenCalled();
  });

  it.each(['absent', 'mismatch'])('fails closed when procfd verification is %s', async (failure) => {
    await fs.chmod(grants(), 0o755);
    const key = path.join(grants(), 'agent-service.json');
    await fs.chmod(key, 0o644);
    const stat = fs.stat.bind(fs);
    vi.spyOn(fs, 'stat').mockImplementation((async (file, options) => {
      if (String(file).startsWith('/proc/self/fd/')) {
        if (failure === 'absent') throw Object.assign(new Error('Synthetic missing procfs'), { code: 'ENOENT' });
        const actual = await stat(file);
        return { ...actual, ino: actual.ino + 1 };
      }
      return stat(file, options);
    }) as typeof fs.stat);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(grants())).toBe(0o755);
    expect(await mode(key)).toBe(0o644);
    expect(logger.error).toHaveBeenCalledWith(expect.stringContaining('Error restoring private permissions'));
  });

  it('leaves other apps without a grants folder fully open, as before', async () => {
    const other = path.join(root, 'wizardapps', 'messenger');
    await fs.mkdir(path.join(other, 'data'), { recursive: true });
    await fs.writeFile(path.join(other, 'data/db.sqlite'), '', { mode: 0o600 });

    await manager.setAppDataDirPermissions('messenger:wizardapps' as AppUrn);

    expect(await mode(path.join(other, 'data/db.sqlite'))).toBe(0o777);
    expect(await mode(grants())).toBe(0o700);
  });
});
