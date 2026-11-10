import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import type { AppUrn } from '@runtipi/common/types';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppFilesManager } from '../app-files-manager';

// Real files (node:fs/promises is not the mocked 'fs'): the permissions come from the real find / chmod.
const mode = async (p: string) => (await fs.lstat(p)).mode & 0o777;

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
    // Recipe config without private_data: the default for every app, the Agent Service included.
    const filesystem = {
      isFile: vi.fn().mockResolvedValue(true),
      readJsonFile: vi.fn().mockResolvedValue({ id: 'app' }),
    } as unknown as FilesystemService;
    manager = new AppFilesManager(configuration, filesystem, logger);
  });

  afterEach(async () => {
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

  it('leaves other apps without a grants folder fully open, as before', async () => {
    const other = path.join(root, 'wizardapps', 'messenger');
    await fs.mkdir(path.join(other, 'data'), { recursive: true });
    await fs.writeFile(path.join(other, 'data/db.sqlite'), '', { mode: 0o600 });

    await manager.setAppDataDirPermissions('messenger:wizardapps' as AppUrn);

    expect(await mode(path.join(other, 'data/db.sqlite'))).toBe(0o777);
    expect(await mode(grants())).toBe(0o700);
  });
});
