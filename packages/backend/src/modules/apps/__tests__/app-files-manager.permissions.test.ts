import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import type { ConfigurationService } from '@/core/config/configuration.service';
import type { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import type { AppUrn } from '@runtipi/common/types';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AppFilesManager } from '../app-files-manager';

// The permission walker uses node:fs (memfs in the shared setup): run it on the real files too.
vi.unmock('fs');
vi.unmock('node:fs');

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

  it('sets owner-only grant keys with the wrong owner bits back to exactly 0700 / 0600', async () => {
    await fs.chmod(path.join(grants(), 'agent-service.json'), 0o700);
    await fs.chmod(grants(), 0o600);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(grants())).toBe(0o700);
    expect(await mode(path.join(grants(), 'agent-service.json'))).toBe(0o600);
  });

  it("keeps every agent's gateway folder 0700 and its sockets 0600 (wizard-messenger#352)", async () => {
    const { execFileSync } = await import('node:child_process');
    const gateway = path.join(app(), 'data/wizard-os/agents/daily/gateway');
    await fs.mkdir(gateway, { recursive: true });
    // Real socket files, bound by a relative name from inside the folder (a full temp path can pass the 108-byte
    // socket path limit). A bound socket's file stays after its process exits.
    execFileSync('python3', ['-c', 'import socket\nfor n in ("gateway.sock", "ingress.sock"): socket.socket(socket.AF_UNIX).bind(n)'], {
      cwd: gateway,
    });
    try {
      // What an earlier update left behind: the folder opened to everyone (the sockets are made 0600 by their service).
      await fs.chmod(gateway, 0o777);
      await fs.chmod(path.join(gateway, 'gateway.sock'), 0o600);
      await fs.chmod(path.join(gateway, 'ingress.sock'), 0o600);
      await fs.mkdir(path.join(app(), 'data/wizard-os/agents/daily/home'), { recursive: true });

      await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

      expect(await mode(gateway)).toBe(0o700);
      expect(await mode(path.join(gateway, 'gateway.sock'))).toBe(0o600);
      expect(await mode(path.join(gateway, 'ingress.sock'))).toBe(0o600);
      // Only the gateway folder is kept private; the rest of the agent folder keeps Runtipi's behaviour.
      expect(await mode(path.join(app(), 'data/wizard-os/agents/daily/home'))).toBe(0o777);
      expect(logger.error).not.toHaveBeenCalled();
      expect((await fs.lstat(path.join(gateway, 'ingress.sock'))).isSocket()).toBe(true);
    } finally {
      await fs.rm(gateway, { recursive: true, force: true });
    }
  });

  it('never follows a link where an agent gateway folder should be', async () => {
    await fs.mkdir(path.join(app(), 'data/wizard-os/agents/evil'), { recursive: true });
    await fs.symlink(outside, path.join(app(), 'data/wizard-os/agents/evil/gateway'));
    await fs.chmod(outside, 0o755);

    await manager.setAppDataDirPermissions('wizard-agent-service:wizardapps' as AppUrn);

    expect(await mode(outside)).toBe(0o755);
    expect(await mode(path.join(outside, 'target'))).toBe(0o600);
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
