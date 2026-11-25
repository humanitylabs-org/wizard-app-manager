import fs from 'node:fs';
import path from 'node:path';
import { pLimit } from '@/common/helpers/file-helpers';
import type { ArchiveEntry } from '@/core/archive/archive.service';
import type { FilesystemService } from '@/core/filesystem/filesystem.service';
import type { LoggerService } from '@/core/logger/logger.service';

export const migrationMetadataFilename = 'runtipi-migrations.json';

export class BackupArchive {
  constructor(
    private readonly filesystem: FilesystemService,
    private readonly logger: LoggerService,
  ) {}

  public validateRestoreArchiveEntries(entries: ArchiveEntry[]) {
    const entryPaths = new Map<string, string>();

    for (const entry of entries) {
      if (entry.type !== '-' && entry.type !== 'd' && entry.type !== 'l') {
        throw new Error('Backup contains unsupported file types');
      }

      const entryPath = this.normalizeArchiveEntryPath(entry.path);

      if (entryPaths.has(entryPath)) {
        throw new Error('Backup contains duplicate file paths');
      }
      entryPaths.set(entryPath, entry.type);

      if (entryPath === '.') {
        if (entry.type !== 'd') {
          throw new Error('Backup contains unsupported file types');
        }
        continue;
      }

      const rootName = entryPath.split('/')[0];

      const isMetadata = entryPath === migrationMetadataFilename && entry.type === '-';
      // Wizard: app-source holds the installed pin's source/build context (BackupManager), never links.
      const isSourceEntry = rootName === 'app-source';
      const isAppEntry = rootName === 'app-data' || rootName === 'app' || rootName === 'user-config' || isSourceEntry;
      if ((!isMetadata && !isAppEntry) || (isSourceEntry && entry.type === 'l')) {
        throw new Error('Backup contains unsupported file types');
      }
      if (isAppEntry && entryPath === rootName && entry.type !== 'd') {
        throw new Error('Backup contains unsupported file types');
      }
    }

    for (const entryPath of entryPaths.keys()) {
      let parent = path.posix.dirname(entryPath);

      while (parent !== '.') {
        const parentType = entryPaths.get(parent);
        if (parentType && parentType !== 'd') {
          throw new Error('Backup contains unsupported file types');
        }
        parent = path.posix.dirname(parent);
      }
    }
  }

  private normalizeArchiveEntryPath(entryPath: string) {
    if (path.posix.isAbsolute(entryPath)) {
      throw new Error('Backup contains unsupported file types');
    }

    const normalizedPath = path.posix.normalize(entryPath.replace(/^(\.\/)+/, '')).replace(/\/+$/, '') || '.';

    if (normalizedPath === '..' || normalizedPath.startsWith('../')) {
      throw new Error('Backup contains unsupported file types');
    }

    return normalizedPath;
  }

  public async validateRestoreDirectory(
    directory: string,
    options: { allowSymlinks: boolean; optional?: boolean; rejectHardLinks?: boolean },
    runFsOperation = pLimit(16),
  ) {
    const directoryStats = await runFsOperation(() => fs.promises.lstat(directory)).catch((error: NodeJS.ErrnoException) => {
      if (options.optional && error.code === 'ENOENT') {
        return null;
      }

      throw error;
    });

    if (!directoryStats) {
      return;
    }

    if (!directoryStats.isDirectory()) {
      throw new Error('Backup contains unsupported file types');
    }

    const entries = await runFsOperation(() => fs.promises.readdir(directory, { withFileTypes: true }));
    let nextEntryIndex = 0;

    const workers = Array.from({ length: Math.min(16, entries.length) }, async () => {
      while (nextEntryIndex < entries.length) {
        const entry = entries[nextEntryIndex];
        nextEntryIndex += 1;

        if (!entry) {
          continue;
        }

        const entryPath = path.join(directory, entry.name);

        if (entry.isSymbolicLink()) {
          if (!options.allowSymlinks) {
            throw new Error('Backup contains unsupported file types');
          }

          continue;
        }

        if (entry.isDirectory()) {
          await this.validateRestoreDirectory(entryPath, options, runFsOperation);
          continue;
        }

        if (entry.isFile()) {
          if (options.rejectHardLinks) {
            const stats = await runFsOperation(() => fs.promises.lstat(entryPath));

            if (stats.nlink > 1) {
              throw new Error('Backup contains unsupported file types');
            }
          }

          continue;
        }

        throw new Error('Backup contains unsupported file types');
      }
    });

    await Promise.all(workers);
  }

  public async stageBackupDirectory(source: string, destination: string, name: string, filter?: fs.CopyOptions['filter']) {
    const quarantine = path.join(path.dirname(destination), '.backup-quarantine', path.basename(destination));
    try {
      // Wizard: hard links whose every name lives inside this folder (a uv/pip cache, a lock file) are the app's own
      // data and are copied as plain files. A file also linked from outside the folder is still refused.
      const internalLinks = await this.internalHardLinks(source, filter, quarantine);
      await this.copySourceToQuarantine(source, quarantine, filter, source, false, internalLinks);
    } catch (error) {
      this.logger.error(`Error copying ${name} to backup quarantine:`, error);
      throw new Error(`Failed to copy ${name} for backup`, { cause: error });
    }

    const quarantineStats = await fs.promises.lstat(quarantine);
    if (!quarantineStats.isDirectory()) {
      throw new Error('Backup contains unsupported file types');
    }

    await this.validateRestoreDirectory(quarantine, { allowSymlinks: true, rejectHardLinks: true });

    const copiedToArchive = await this.filesystem.copyDirectory(quarantine, destination, { dereference: false, verbatimSymlinks: true });
    if (!copiedToArchive) {
      throw new Error(`Failed to copy ${name} for backup`);
    }

    if (!(await this.filesystem.removeDirectory(path.dirname(quarantine)))) {
      throw new Error('Failed to remove backup quarantine');
    }
  }

  // Wizard: every hard-linked file under `root` whose names all sit under `root`, keyed by device and inode, with its
  // link count. Symlinks are never followed, so a link can only count names really inside the folder.
  // wizard-app-manager#1039: app data can change during the walk (private agents), so on Linux each folder is opened
  // with O_NOFOLLOW and read through its own handle, like the copy: a folder swapped for a symlink is never entered.
  // The copy filter is asked before anything is read, so a cancel stops the walk and excluded folders are skipped.
  private async internalHardLinks(
    root: string,
    filter?: fs.CopyOptions['filter'],
    destination = root,
    runFsOperation = pLimit(16),
  ): Promise<Map<string, number>> {
    const found = new Map<string, { nlink: number; seen: number }>();
    const count = (stats: fs.Stats | null) => {
      if (!stats?.isFile() || stats.nlink < 2) return;
      const key = `${stats.dev}:${stats.ino}`;
      const known = found.get(key);
      if (known) known.seen += 1;
      else found.set(key, { nlink: stats.nlink, seen: 1 });
    };
    const linux = process.platform === 'linux';
    const openFlags = fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK | (fs.constants.O_DIRECTORY ?? 0);

    // `directory` is where to read; `filterSource`/`filterDestination` are the paths the copy filter sees.
    const walk = async (directory: string, filterSource: string, filterDestination: string): Promise<void> => {
      if (filter && !(await filter(filterSource, filterDestination))) return;

      // Anything unreadable here is left for the copy itself to report; this walk only finds links it can prove inside.
      let handle: fs.promises.FileHandle | null = null;
      let readFrom = directory;
      if (linux) {
        handle = await fs.promises.open(directory, openFlags).catch(() => null);
        if (!handle) return;
        readFrom = `/proc/self/fd/${handle.fd}`;
      }
      try {
        const stats = handle ? await handle.stat().catch(() => null) : await fs.promises.lstat(directory).catch(() => null);
        if (!stats?.isDirectory()) return;

        const entries = await runFsOperation(() => fs.promises.readdir(readFrom, { withFileTypes: true })).catch(() => []);
        const files: string[] = [];
        for (const entry of entries) {
          const childSource = path.join(filterSource, entry.name);
          const childDestination = path.join(filterDestination, entry.name);
          if (entry.isDirectory()) {
            // One folder at a time keeps a single open handle per depth.
            await walk(path.join(readFrom, entry.name), childSource, childDestination);
            continue;
          }
          if (!entry.isFile()) continue;
          if (filter && !(await filter(childSource, childDestination))) continue;
          files.push(path.join(readFrom, entry.name));
        }
        await Promise.all(files.map(async (file) => count(await runFsOperation(() => fs.promises.lstat(file)).catch(() => null))));
      } finally {
        await handle?.close().catch(() => undefined);
      }
    };
    await walk(root, root, destination);

    const internal = new Map<string, number>();
    for (const [key, { nlink, seen }] of found) {
      if (seen === nlink) internal.set(key, nlink);
    }
    return internal;
  }

  // A file with more than one name is copied only when the walk found all of its names inside the folder and its link
  // count has not changed since.
  private internalHardLink(stats: { dev: number; ino: number; nlink: number }, internalLinks?: Map<string, number>): boolean {
    return internalLinks?.get(`${stats.dev}:${stats.ino}`) === stats.nlink;
  }

  private async copySourceToQuarantine(
    source: string,
    destination: string,
    filter?: fs.CopyOptions['filter'],
    filterSource = source,
    child = false,
    internalLinks?: Map<string, number>,
  ): Promise<void> {
    if (process.platform !== 'linux') {
      await this.copySourceToQuarantineOutsideLinux(source, destination, filter, filterSource, internalLinks);
      return;
    }

    if (filter && !(await filter(filterSource, destination))) {
      return;
    }

    const openFlags = fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK;
    let sourceHandle: fs.promises.FileHandle;

    try {
      sourceHandle = await fs.promises.open(source, openFlags);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      // Wizard: a live Unix socket (e.g. an agent gateway socket) can't be opened or restored; it is runtime state, not data.
      if (code === 'ENXIO' && (await this.socketOrGone(source))) {
        return;
      }
      // Wizard (wizard-app-manager#971): an entry deleted after its folder was listed (a running agent's SQLite journal,
      // a temp file) is no longer data. Skipping it keeps one live writer from failing the whole backup, and with it
      // the app's update. The top folder itself missing still fails.
      if (code === 'ENOENT' && child) {
        return;
      }
      if (code !== 'ELOOP') {
        throw error;
      }

      let target: string;
      try {
        target = await fs.promises.readlink(source);
      } catch (linkError) {
        if ((linkError as NodeJS.ErrnoException).code === 'ENOENT' && child) {
          return;
        }
        throw linkError;
      }
      await fs.promises.mkdir(path.dirname(destination), { recursive: true });
      await fs.promises.symlink(target, destination);
      return;
    }

    try {
      const stats = await sourceHandle.stat();
      const sourceHandlePath = `/proc/self/fd/${sourceHandle.fd}`;

      if (stats.isFile()) {
        if (stats.nlink > 1 && !this.internalHardLink(stats, internalLinks)) {
          throw new Error('Backup contains unsupported file types');
        }

        await this.copyFileFromHandle(sourceHandlePath, destination);
        return;
      }
      if (!stats.isDirectory()) {
        throw new Error('Backup contains unsupported file types');
      }

      await fs.promises.mkdir(destination, { recursive: true });
      const directory = await fs.promises.opendir(sourceHandlePath);
      for await (const entry of directory) {
        // Wizard: sockets are runtime state; skip them by their directory entry type so one unlinked mid-walk can't fail the backup.
        if (entry.isSocket()) {
          continue;
        }
        await this.copySourceToQuarantine(
          path.join(sourceHandlePath, entry.name),
          path.join(destination, entry.name),
          filter,
          path.join(filterSource, entry.name),
          true,
          internalLinks,
        );
      }
      // Wizard: keep a folder's own mode (e.g. an app's 0700 private folder), as `cp -r` did before 4.11.
      await fs.promises.chmod(destination, stats.mode & 0o7777);
    } finally {
      await sourceHandle.close().catch((error) => this.logger.warn(`Failed to close backup file ${source}`, error));
    }
  }

  // ENXIO on open means a socket; if it was unlinked right after (rotating agent sockets), there is nothing to copy either.
  private async socketOrGone(source: string): Promise<boolean> {
    try {
      return (await fs.promises.lstat(source)).isSocket();
    } catch (error) {
      return (error as NodeJS.ErrnoException).code === 'ENOENT';
    }
  }

  public async copyRestoreArchive(source: string, destination: string): Promise<void> {
    if (process.platform !== 'linux') {
      const stats = await fs.promises.lstat(source);
      if (!stats.isFile()) {
        throw new Error('The backup file does not exist');
      }

      await fs.promises.copyFile(source, destination);
      return;
    }

    const openFlags = fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK;
    let sourceHandle: fs.promises.FileHandle;

    try {
      sourceHandle = await fs.promises.open(source, openFlags);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code === 'ENOENT' || code === 'ELOOP') {
        throw new Error('The backup file does not exist');
      }
      throw error;
    }

    try {
      if (!(await sourceHandle.stat()).isFile()) {
        throw new Error('The backup file does not exist');
      }

      await this.copyFileFromHandle(`/proc/self/fd/${sourceHandle.fd}`, destination);
    } finally {
      await sourceHandle.close().catch((error) => this.logger.warn(`Failed to close backup file ${source}`, error));
    }
  }

  private async copyFileFromHandle(sourceHandlePath: string, destination: string): Promise<void> {
    await fs.promises.mkdir(path.dirname(destination), { recursive: true });
    await fs.promises.copyFile(sourceHandlePath, destination);
  }

  private async copySourceToQuarantineOutsideLinux(
    source: string,
    destination: string,
    filter?: fs.CopyOptions['filter'],
    filterSource = source,
    internalLinks?: Map<string, number>,
  ): Promise<void> {
    if (filter && !(await filter(filterSource, destination))) {
      return;
    }

    const stats = await fs.promises.lstat(source);
    if (stats.isSocket()) {
      return;
    }
    if (stats.isSymbolicLink()) {
      await fs.promises.mkdir(path.dirname(destination), { recursive: true });
      await fs.promises.symlink(await fs.promises.readlink(source), destination);
      return;
    }

    if (stats.isFile()) {
      if (stats.nlink > 1 && !this.internalHardLink(stats, internalLinks)) {
        throw new Error('Backup contains unsupported file types');
      }

      await fs.promises.mkdir(path.dirname(destination), { recursive: true });
      await fs.promises.copyFile(source, destination);
      return;
    }

    if (!stats.isDirectory()) {
      throw new Error('Backup contains unsupported file types');
    }

    await fs.promises.mkdir(destination, { recursive: true });
    const entries = await fs.promises.readdir(source, { withFileTypes: true });
    for (const dirent of entries) {
      if (dirent.isSocket()) {
        continue;
      }
      const entry = dirent.name;
      const entrySource = path.join(source, entry);
      const entryDestination = path.join(destination, entry);
      const entryFilterSource = path.join(filterSource, entry);
      await this.copySourceToQuarantineOutsideLinux(entrySource, entryDestination, filter, entryFilterSource, internalLinks);
    }
    await fs.promises.chmod(destination, stats.mode & 0o7777);
  }
}
