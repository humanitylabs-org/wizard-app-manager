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

  // wizard-app-manager#971: a running private agent keeps writing its home inside the Agent Service's app data while the
  // Service is stopped for its update backup; a SQLite journal deleted between the folder listing and its open failed
  // the whole backup ("Failed to copy app data for backup"), so the update rolled back.
  it.runIf(process.platform === 'linux')('skips files and folders deleted while the backup walks the app data', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const home = path.join(source, 'data', 'wizard-os', 'agents', 'private-agent', 'home');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const originalOpen = fs.promises.open;
    const gone = new Set(['state.db-journal', 'tmp-run', 'stale-link']);
    const openSpy = vi.spyOn(fs.promises, 'open').mockImplementation(async (file, flags, mode) => {
      const name = path.basename(String(file));
      if (gone.has(name)) {
        // the agent removes it right after the walk listed it
        const real = await fs.promises.readlink(path.dirname(String(file))).catch(() => path.dirname(String(file)));
        await fs.promises.rm(path.join(real, name), { recursive: true, force: true });
      }
      return originalOpen(file, flags, mode);
    });

    try {
      await writeFile(path.join(home, 'state.db'), 'db');
      await writeFile(path.join(home, 'state.db-journal'), 'journal');
      await writeFile(path.join(home, 'tmp-run', 'part'), 'temp');
      await fs.promises.symlink('state.db', path.join(home, 'stale-link'));

      await manager.stageBackupDirectory(source, destination, 'app data');

      const copied = path.join(destination, 'data', 'wizard-os', 'agents', 'private-agent', 'home');
      await expect(fs.promises.readFile(path.join(copied, 'state.db'), 'utf8')).resolves.toBe('db');
      await expect(fs.promises.readdir(copied)).resolves.toEqual(['state.db']);
    } finally {
      openSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  // An agent's uv cache hard-links each package file between its archive and wheel folders (and the installed copy), so
  // the Agent Service's app data holds hundreds of hard links that all live inside it. Refusing every hard link failed
  // each Agent Service update backup ("Failed to copy app data for backup"), and with it the WizardOS update.
  it.runIf(process.platform === 'linux')('copies hard links whose names all sit inside the app data as plain files', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const cache = path.join(source, 'home', '.cache', 'uv');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);

    try {
      await writeFile(path.join(cache, 'archive-v0', 'pkg', 'mod.py'), 'code');
      await fs.promises.mkdir(path.join(cache, 'wheels-v6', 'pkg'), { recursive: true });
      await fs.promises.link(path.join(cache, 'archive-v0', 'pkg', 'mod.py'), path.join(cache, 'wheels-v6', 'pkg', 'mod.py'));
      await fs.promises.mkdir(path.join(source, 'home', 'lazy-packages', 'pkg'), { recursive: true });
      await fs.promises.link(path.join(cache, 'archive-v0', 'pkg', 'mod.py'), path.join(source, 'home', 'lazy-packages', 'pkg', 'mod.py'));

      await manager.stageBackupDirectory(source, destination, 'app data');

      for (const copy of ['.cache/uv/archive-v0/pkg/mod.py', '.cache/uv/wheels-v6/pkg/mod.py', 'lazy-packages/pkg/mod.py']) {
        const copied = path.join(destination, 'home', copy);
        await expect(fs.promises.readFile(copied, 'utf8')).resolves.toBe('code');
        expect((await fs.promises.lstat(copied)).nlink).toBe(1);
      }
    } finally {
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('still refuses a file that is also linked from outside the app data', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);

    try {
      await writeFile(path.join(rootDir, 'host-secret'), 'secret');
      await fs.promises.mkdir(path.join(source, 'a'), { recursive: true });
      await fs.promises.link(path.join(rootDir, 'host-secret'), path.join(source, 'a', 'one'));
      await fs.promises.link(path.join(rootDir, 'host-secret'), path.join(source, 'two'));

      await expect(manager.stageBackupDirectory(source, destination, 'app data')).rejects.toThrow('Failed to copy app data for backup');
      await expect(fs.promises.access(destination)).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  // wizard-app-manager#1039 (Codex P1 on #1037): a private agent's data can change while it is backed up. A folder the
  // scan listed as a directory and that is then swapped for a symlink to the outside must not let the scan count the
  // outside name of a hard link as if it sat inside, or that file is accepted into the backup.
  it.runIf(process.platform === 'linux')('does not follow a folder swapped for a symlink while counting hard links', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const outside = path.join(rootDir, 'outside');
    const swappedDir = path.join(source, 'd');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const originalReaddir = fs.promises.readdir;
    let swapped = false;
    const swap = () => {
      if (swapped) return;
      swapped = true;
      fs.rmSync(swappedDir, { recursive: true });
      fs.symlinkSync(outside, swappedDir);
    };
    // Swap right after the source folder itself is listed (by path or through its handle), before the scan reaches `d`.
    const readdirSpy = vi.spyOn(fs.promises, 'readdir').mockImplementation((async (...args: Parameters<typeof originalReaddir>) => {
      const result = await originalReaddir(...args);
      if (!swapped && fs.realpathSync(String(args[0])) === source) swap();
      return result;
    }) as typeof originalReaddir);

    try {
      await writeFile(path.join(outside, 'secret'), 'host secret');
      await fs.promises.mkdir(swappedDir, { recursive: true });
      await fs.promises.link(path.join(outside, 'secret'), path.join(source, 'secret'));

      await expect(manager.stageBackupDirectory(source, destination, 'app data')).rejects.toThrow('Failed to copy app data for backup');
      expect(swapped).toBe(true);
      await expect(fs.promises.access(path.join(destination, 'secret'))).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      readdirSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  // wizard-app-manager#1039 (Codex P2 on #1037): a cancelled update or backup is seen only through the copy filter. The
  // hard-link scan must ask it before reading anything, and must not walk the excluded top-level `backups` folder.
  it.runIf(process.platform === 'linux')('asks the copy filter before scanning and skips what it excludes', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const source = path.join(rootDir, 'source');
    const destination = path.join(rootDir, 'backup', 'app-data');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);
    const lstatSpy = vi.spyOn(fs.promises, 'lstat');

    try {
      await writeFile(path.join(source, 'data', 'a'), 'a');
      await fs.promises.link(path.join(source, 'data', 'a'), path.join(source, 'data', 'b'));
      for (let i = 0; i < 20; i += 1) await writeFile(path.join(source, 'backups', `old-${i}`, 'archive.tar.gz'), 'old');

      // Cancelled: the filter throws on its first call, so nothing may be read.
      const cancelled = new AbortController();
      cancelled.abort();
      await expect(
        manager.stageBackupDirectory(source, destination, 'app data', () => {
          cancelled.signal.throwIfAborted();
          return true;
        }),
      ).rejects.toThrow('Failed to copy app data for backup');
      expect(lstatSpy).not.toHaveBeenCalled();

      // Not cancelled: nothing inside the excluded backups folder is read or asked about.
      const asked: string[] = [];
      await manager.stageBackupDirectory(source, destination, 'app data', (filterSource) => {
        asked.push(filterSource);
        return filterSource !== path.join(source, 'backups');
      });
      expect(asked.filter((p) => p.startsWith(`${path.join(source, 'backups')}/`))).toEqual([]);
      expect(lstatSpy.mock.calls.filter(([p]) => String(p).endsWith('archive.tar.gz'))).toEqual([]);
      await expect(fs.promises.readFile(path.join(destination, 'data', 'b'), 'utf8')).resolves.toBe('a');
      await expect(fs.promises.access(path.join(destination, 'backups'))).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      lstatSpy.mockRestore();
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });

  it.runIf(process.platform === 'linux')('still fails when the app data folder itself is missing', async () => {
    const rootDir = await fs.promises.mkdtemp('/tmp/runtipi-backup-source-');
    const logger = createLogger();
    const manager = new BackupArchive(new FilesystemService(logger), logger);

    try {
      await expect(manager.stageBackupDirectory(path.join(rootDir, 'missing'), path.join(rootDir, 'backup', 'app-data'), 'app data')).rejects.toThrow(
        'Failed to copy app data for backup',
      );
    } finally {
      await fs.promises.rm(rootDir, { recursive: true, force: true });
    }
  });
});
