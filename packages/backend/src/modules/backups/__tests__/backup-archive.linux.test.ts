import fs from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';
import { describe, expect, it, vi } from 'vitest';
import { mock } from 'vitest-mock-extended';
import { BackupArchive } from '../backup-archive';

vi.unmock('fs');
vi.unmock('node:fs');

const createLogger = () => mock<LoggerService>();

const writeFile = async (filePath: string, content: string) => {
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true });
  await fs.promises.writeFile(filePath, content);
};

describe('BackupArchive Linux filesystem security', () => {
  it.runIf(process.platform === 'linux')('copies a regular file from its no-follow handle when the source is swapped', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source.txt');
    const destination = path.join(rootDir, 'quarantine', 'source.txt');
    const movedSource = path.join(rootDir, 'source-before-swap.txt');
    const externalFile = path.join(rootDir, 'external.txt');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const copySourceToQuarantine = (
      manager as unknown as { copySourceToQuarantine: (source: string, destination: string) => Promise<void> }
    ).copySourceToQuarantine.bind(manager);
    const originalCopyFile = fs.promises.copyFile;
    const copyFileSpy = vi.spyOn(fs.promises, 'copyFile');
    let swapped = false;

    try {
      await fs.promises.writeFile(source, 'safe');
      await fs.promises.writeFile(externalFile, 'external');
      copyFileSpy.mockImplementation(async (copySource, copyDestination, mode) => {
        if (!swapped) {
          swapped = true;
          await fs.promises.rename(source, movedSource);
          await fs.promises.symlink(externalFile, source);
        }
        await originalCopyFile(copySource, copyDestination, mode);
      });

      await copySourceToQuarantine(source, destination);

      await expect(fs.promises.readFile(destination, 'utf8')).resolves.toBe('safe');
    } finally {
      copyFileSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('traverses a directory from its no-follow handle when the source is swapped', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const destination = path.join(rootDir, 'quarantine', 'source');
    const movedSource = path.join(rootDir, 'source-before-swap');
    const externalDirectory = path.join(rootDir, 'external');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const copySourceToQuarantine = (
      manager as unknown as { copySourceToQuarantine: (source: string, destination: string) => Promise<void> }
    ).copySourceToQuarantine.bind(manager);
    const originalMkdir = fs.promises.mkdir;
    const mkdirSpy = vi.spyOn(fs.promises, 'mkdir');
    let swapped = false;

    try {
      await writeFile(path.join(source, 'safe.txt'), 'safe');
      await writeFile(path.join(externalDirectory, 'external.txt'), 'external');
      mkdirSpy.mockImplementation(async (directory, options) => {
        if (!swapped && directory === destination) {
          swapped = true;
          await fs.promises.rename(source, movedSource);
          await fs.promises.symlink(externalDirectory, source);
        }
        return originalMkdir(directory, options);
      });

      await copySourceToQuarantine(source, destination);

      await expect(fs.promises.readFile(path.join(destination, 'safe.txt'), 'utf8')).resolves.toBe('safe');
      await expect(fs.promises.access(path.join(destination, 'external.txt'))).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      mkdirSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('rejects a quarantine root that becomes a symlink before rewriting it', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const movedSource = path.join(rootDir, 'source-before-swap');
    const externalDirectory = path.join(rootDir, 'external');
    const externalFile = path.join(externalDirectory, 'external.txt');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const stageBackupDirectory = (
      manager as unknown as { stageBackupDirectory: (source: string, destination: string, name: string) => Promise<void> }
    ).stageBackupDirectory.bind(manager);
    const originalOpen = fs.promises.open;
    const openSpy = vi.spyOn(fs.promises, 'open');
    let swapped = false;

    try {
      await writeFile(path.join(source, 'safe.txt'), 'safe');
      await writeFile(externalFile, 'external');
      openSpy.mockImplementation(async (file, flags, mode) => {
        if (!swapped && file === source) {
          swapped = true;
          await fs.promises.rename(source, movedSource);
          await fs.promises.symlink(externalDirectory, source);
        }
        return originalOpen(file, flags, mode);
      });

      await expect(stageBackupDirectory(source, destination, 'app data')).rejects.toThrow('Backup contains unsupported file types');
      await expect(fs.promises.readFile(externalFile, 'utf8')).resolves.toBe('external');
      await expect(fs.promises.readdir(externalDirectory)).resolves.toEqual(['external.txt']);
    } finally {
      openSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('leaves live unix sockets out of an app data backup', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const gateway = path.join(source, 'data', 'wizard-os', 'agents', 'main-agent', 'gateway');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const servers: net.Server[] = [];

    try {
      await writeFile(path.join(gateway, 'agent.json'), '{}');
      for (const name of ['ingress.sock', 'gateway.sock']) {
        const server = net.createServer();
        await new Promise<void>((resolve, reject) => {
          server.once('error', reject);
          server.listen(path.join(gateway, name), resolve);
        });
        servers.push(server);
      }
      expect((await fs.promises.lstat(path.join(gateway, 'ingress.sock'))).isSocket()).toBe(true);

      await manager.stageBackupDirectory(source, destination, 'app data');

      const copied = path.join(destination, 'data', 'wizard-os', 'agents', 'main-agent', 'gateway');
      await expect(fs.promises.readFile(path.join(copied, 'agent.json'), 'utf8')).resolves.toBe('{}');
      await expect(fs.promises.readdir(copied)).resolves.toEqual(['agent.json']);
    } finally {
      await Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve))));
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('skips a socket that is unlinked right after open refuses it', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'loop-tick.1.sock');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const copySourceToQuarantine = (
      manager as unknown as { copySourceToQuarantine: (source: string, destination: string) => Promise<void> }
    ).copySourceToQuarantine.bind(manager);
    const openSpy = vi.spyOn(fs.promises, 'open').mockImplementation(async () => {
      throw Object.assign(new Error('ENXIO: no such device or address'), { code: 'ENXIO' });
    });

    try {
      await copySourceToQuarantine(source, path.join(rootDir, 'quarantine', 'loop-tick.1.sock'));
      await expect(fs.promises.access(path.join(rootDir, 'quarantine'))).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      openSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });
});
