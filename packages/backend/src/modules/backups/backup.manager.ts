import fs from 'node:fs';
import path from 'node:path';
import { extractAppUrn } from '@/common/helpers/app-helpers';
import { pLimit, sanitizeFilename } from '@/common/helpers/file-helpers';
import { ArchiveService, type ArchiveEntry } from '@/core/archive/archive.service';
import { ConfigurationService } from '@/core/config/configuration.service';
import { FilesystemService } from '@/core/filesystem/filesystem.service';
import { LoggerService } from '@/core/logger/logger.service';
import { Injectable } from '@nestjs/common';
import type { AppUrn } from '@runtipi/common/types';
import { AppFilesManager } from '../apps/app-files-manager';
import { SOURCE_LIMIT_BYTES, sourceDir } from '../app-stores/packaging';

@Injectable()
export class BackupManager {
  constructor(
    private readonly archiveManager: ArchiveService,
    private readonly logger: LoggerService,
    private readonly config: ConfigurationService,
    private readonly filesystem: FilesystemService,
    private readonly appFilesManager: AppFilesManager,
  ) {}

  private getBackupPaths(appUrn: AppUrn) {
    const { dataDir } = this.config.get('directories');
    const { appStoreId, appName } = extractAppUrn(appUrn);

    return {
      appName,
      appStoreId,
      backupDir: path.join(dataDir, 'backups', appStoreId, appName),
      userConfigDir: path.join(dataDir, 'user-config', appStoreId, appName),
    };
  }

  private getValidatedBackupFilename(filename: string) {
    const sanitizedFilename = sanitizeFilename(filename);

    if (sanitizedFilename !== filename || !sanitizedFilename.endsWith('.tar.gz')) {
      throw new Error('Invalid backup filename');
    }

    return sanitizedFilename;
  }

  private getBackupFilePath(appUrn: AppUrn, filename: string) {
    const { backupDir } = this.getBackupPaths(appUrn);
    const sanitizedFilename = this.getValidatedBackupFilename(filename);

    return {
      backupDir,
      sanitizedFilename,
      backupPath: this.filesystem.getSafeFilePath(path.join(backupDir, sanitizedFilename)),
    };
  }

  public backupApp = async (appUrn: AppUrn) => {
    const { appName, appStoreId, backupDir, userConfigDir } = this.getBackupPaths(appUrn);
    const backupName = `${appName}-${appStoreId}-${Date.now()}`;

    const tempDir = await this.filesystem.createTempDirectory(appUrn);

    if (!tempDir) {
      throw new Error('Failed to create temp directory');
    }

    this.logger.info('Copying files to backup location...');

    await this.filesystem.createDirectory(tempDir);

    const { appDataDir, appInstalledDir } = this.appFilesManager.getAppPaths(appUrn);

    await this.filesystem.copyDirectory(appDataDir, path.join(tempDir, 'app-data'), {
      recursive: true,
      filter: (src) => src !== path.join(appDataDir, 'backups') && !src.startsWith(`${path.join(appDataDir, 'backups')}${path.sep}`),
    });

    await this.filesystem.copyDirectory(appInstalledDir, path.join(tempDir, 'app'));

    // Source/build code lives beside per-app data and is pruned on update.
    // Include the installed pin's context so an offline restore can build/start it.
    const commit = (await this.filesystem.readTextFile(path.join(appInstalledDir, '.wizard-commit')))?.trim();
    if (commit && /^[0-9a-f]{40}$/.test(commit)) {
      const context = sourceDir(this.config.get('directories').appDataDir, appStoreId, appName, commit);
      if (await this.filesystem.isDirectory(context)) {
        const validateSourceEntry = await this.validateBackupSource(context);
        const archivedSource = path.join(tempDir, 'app-source');
        // cp otherwise preserves links (and may rewrite relative targets as absolute
        // source paths). Archive only independent bytes, never source-bound links.
        if (!(await this.filesystem.copyDirectory(context, archivedSource, { dereference: true, filter: validateSourceEntry }))) {
          throw new Error('Failed to include installed source context in backup');
        }
        await this.validateRestoreDirectory(archivedSource, { allowSymlinks: false, rejectHardLinks: true });
      }
    }

    if (await this.filesystem.isDirectory(userConfigDir)) {
      this.logger.info('Including user configuration in backup...');
      await this.filesystem.copyDirectory(userConfigDir, path.join(tempDir, 'user-config'));
    }

    this.logger.info('Creating archive...');

    // Create the archive
    const { stdout, stderr } = await this.archiveManager.createTarGz(tempDir, `${path.join(tempDir, backupName)}.tar.gz`);
    this.logger.debug('--- archiveManager.createTarGz ---');
    this.logger.debug('stderr:', stderr);
    this.logger.debug('stdout:', stdout);

    this.logger.info('Moving archive to backup directory...', backupDir);

    // Move the archive to the backup directory
    await this.filesystem.createDirectory(backupDir);
    await this.filesystem.copyFile(`${path.join(tempDir, backupName)}.tar.gz`, path.join(backupDir, `${backupName}.tar.gz`));

    // Remove the temp backup folder
    await this.filesystem.removeDirectory(tempDir);

    this.logger.info('Backup completed!');
  };

  public restoreApp = async (appUrn: AppUrn, filename: string) => {
    const restoreDir = await this.filesystem.createTempDirectory(appUrn);

    if (!restoreDir) {
      throw new Error('Failed to create temp directory');
    }

    const { backupDir, backupPath: archive } = this.getBackupFilePath(appUrn, filename);
    const { userConfigDir } = this.getBackupPaths(appUrn);

    if (!archive.startsWith(backupDir)) {
      throw new Error('Invalid backup file path');
    }

    this.logger.info('Restoring app from backup...');

    // Verify the app has a backup
    if (!(await this.filesystem.isFile(archive))) {
      throw new Error('The backup file does not exist');
    }

    // Unzip the archive
    await this.filesystem.createDirectory(restoreDir);

    try {
      this.validateRestoreArchiveEntries(await this.archiveManager.listTarGz(archive));

      this.logger.info('Extracting archive...');
      const { stderr, stdout } = await this.archiveManager.extractTarGz(archive, restoreDir);
      this.logger.debug('--- archiveManager.extractTarGz ---');
      this.logger.debug('stderr:', stderr);
      this.logger.debug('stdout:', stdout);

      const runValidationFsOperation = pLimit(16);

      await this.validateRestoreDirectory(path.join(restoreDir, 'app-data'), { allowSymlinks: true }, undefined, runValidationFsOperation);
      await this.validateRestoreDirectory(
        path.join(restoreDir, 'app'),
        { allowSymlinks: true, rejectHardLinks: true },
        undefined,
        runValidationFsOperation,
      );
      await this.validateRestoreDirectory(
        path.join(restoreDir, 'user-config'),
        {
          allowSymlinks: false,
          optional: true,
          rejectHardLinks: true,
        },
        undefined,
        runValidationFsOperation,
      );

      const { appInstalledDir, appDataDir } = this.appFilesManager.getAppPaths(appUrn);

      const archivedSource = path.join(restoreDir, 'app-source');
      await this.validateRestoreDirectory(
        archivedSource,
        { allowSymlinks: false, optional: true, rejectHardLinks: true },
        undefined,
        runValidationFsOperation,
      );
      if (await this.filesystem.isDirectory(archivedSource)) {
        const commit = (await this.filesystem.readTextFile(path.join(restoreDir, 'app', '.wizard-commit')))?.trim();
        if (!commit || !/^[0-9a-f]{40}$/.test(commit)) {
          throw new Error('Backup source context has no valid installed commit');
        }
        const { appName, appStoreId } = extractAppUrn(appUrn);
        const context = sourceDir(this.config.get('directories').appDataDir, appStoreId, appName, commit);
        // Fail before replacing app/data if the saved code cannot be recovered.
        await this.restoreSourceContext(archivedSource, context);
      }

      // Remove old data directories
      await this.filesystem.removeDirectory(appDataDir);
      await this.filesystem.removeDirectory(appInstalledDir);
      await this.filesystem.removeDirectory(userConfigDir);

      await this.filesystem.createDirectory(appDataDir);
      await this.filesystem.createDirectory(appInstalledDir);
      await this.filesystem.createDirectory(userConfigDir);

      // Copy data from the backup folder
      await this.filesystem.copyDirectory(path.join(restoreDir, 'app-data'), appDataDir);
      await this.filesystem.copyDirectory(path.join(restoreDir, 'app'), appInstalledDir);

      if (await this.filesystem.isDirectory(path.join(restoreDir, 'user-config'))) {
        await this.filesystem.copyDirectory(path.join(restoreDir, 'user-config'), userConfigDir);
      }
    } finally {
      await this.filesystem.removeDirectory(restoreDir);
    }
  };

  // This admission is only for a local source checkout. Restore archives still
  // reject every link before extraction. Resolve the full chain, not just the
  // first readlink target, and reject cycles before cp can recurse into them.
  private async validateBackupSource(directory: string) {
    const root = await fs.promises.realpath(this.filesystem.getSafeFilePath(directory));
    // Count each output path, including aliases: confined directory links can
    // expand a small checkout into exponentially many portable copies.
    const admissionBudget = { entries: 0, bytes: 0 };
    const copyBudget = { entries: 0, bytes: 0 };
    const validateEntry = async (entryPath: string, budget: { entries: number; bytes: number }) => {
      if (++budget.entries > 100_000) throw new Error('Backup source exceeds 100000 entry limit');
      const linkStats = await fs.promises.lstat(entryPath);
      if (linkStats.isSymbolicLink()) {
        const target = await fs.promises.readlink(entryPath);
        if (!this.isPathInsideOrEqual(path.resolve(directory), path.resolve(path.dirname(entryPath), target))) {
          throw new Error('Backup source link points outside the app');
        }
      }
      const resolved = await fs.promises.realpath(entryPath);
      if (!this.isPathInsideOrEqual(root, resolved)) {
        throw new Error('Backup source link points outside the app');
      }
      const stats = await fs.promises.stat(entryPath);
      if ((!stats.isDirectory() && !stats.isFile()) || (stats.isFile() && stats.nlink > 1)) {
        throw new Error('Backup contains unsupported file types');
      }
      if (stats.isFile()) {
        budget.bytes += stats.size;
        if (budget.bytes > SOURCE_LIMIT_BYTES) throw new Error('Backup source exceeds source byte limit');
      }
      return { resolved, stats };
    };
    const walk = async (entryPath: string, ancestors: Set<string>): Promise<void> => {
      const { resolved, stats } = await validateEntry(entryPath, admissionBudget);
      if (stats.isDirectory()) {
        if (ancestors.has(resolved)) {
          throw new Error('Backup source contains a directory link cycle');
        }
        const nextAncestors = new Set(ancestors).add(resolved);
        for (const name of await fs.promises.readdir(entryPath)) {
          await walk(path.join(entryPath, name), nextAncestors);
        }
      }
    };
    await walk(directory, new Set());
    // Recheck each entry as cp visits it, not only during the initial walk.
    return async (entryPath: string) => {
      await validateEntry(entryPath, copyBudget);
      return true;
    };
  }

  private async restoreSourceContext(archivedSource: string, destination: string) {
    const context = this.filesystem.getSafeFilePath(destination);
    if (!(await this.filesystem.createDirectory(path.dirname(context)))) {
      throw new Error('Failed to restore installed source context');
    }
    // Sibling staging guarantees same-filesystem renames. Keep the previous
    // context until the fully copied replacement has actually been committed.
    // Stay outside app@commit: pruneSources must not sweep retained recovery bytes.
    const staging = await fs.promises.mkdtemp(path.join(path.dirname(context), `.restore-${path.basename(context)}-`));
    const stagedSource = path.join(staging, 'source');
    const previous = path.join(staging, 'previous');
    let movedCurrent = false;
    let committed = false;
    try {
      if (!(await this.filesystem.copyDirectory(archivedSource, stagedSource))) {
        throw new Error('Failed to restore installed source context');
      }
      const current = await fs.promises.lstat(context).catch((error: NodeJS.ErrnoException) => {
        if (error.code === 'ENOENT') return null;
        throw error;
      });
      if (current) {
        await fs.promises.rename(context, previous);
        movedCurrent = true;
      }
      try {
        await fs.promises.rename(stagedSource, context);
        committed = true;
      } catch (error) {
        if (movedCurrent) {
          // If even rollback fails, retain previous on disk for recovery. Never
          // clean up the only surviving copy of the installed context.
          await fs.promises.rename(previous, context);
          movedCurrent = false;
        }
        throw error;
      }
    } finally {
      if (committed || !movedCurrent) {
        await this.filesystem.removeDirectory(staging);
      } else {
        this.logger.error(`Source rollback failed; previous context retained at ${previous}`);
      }
    }
  }

  private validateRestoreArchiveEntries(entries: ArchiveEntry[]) {
    for (const entry of entries) {
      if (entry.type !== '-' && entry.type !== 'd') {
        throw new Error('Backup contains unsupported file types');
      }

      const entryPath = this.normalizeArchiveEntryPath(entry.path);

      if (entryPath === '.') {
        continue;
      }

      const rootName = entryPath.split('/')[0];

      if (!rootName) {
        throw new Error('Backup contains unsupported file types');
      }
    }
  }

  private normalizeArchiveEntryPath(entryPath: string) {
    if (path.posix.isAbsolute(entryPath)) {
      throw new Error('Backup contains unsupported file types');
    }

    const normalizedPath = path.posix.normalize(entryPath.replace(/^(\.\/)+/, ''));

    if (normalizedPath === '..' || normalizedPath.startsWith('../')) {
      throw new Error('Backup contains unsupported file types');
    }

    return normalizedPath;
  }

  private async validateRestoreDirectory(
    directory: string,
    options: { allowSymlinks: boolean; optional?: boolean; rejectHardLinks?: boolean },
    rootDirectory = directory,
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
    const rootPath = path.resolve(rootDirectory);
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

          const linkTarget = await runFsOperation(() => fs.promises.readlink(entryPath));
          const resolvedTarget = path.resolve(path.dirname(entryPath), linkTarget);

          if (!this.isPathInsideOrEqual(rootPath, resolvedTarget)) {
            throw new Error('Backup contains unsupported file types');
          }

          continue;
        }

        if (entry.isDirectory()) {
          await this.validateRestoreDirectory(entryPath, options, rootDirectory, runFsOperation);
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

  private isPathInsideOrEqual(parentPath: string, childPath: string): boolean {
    const relativePath = path.relative(parentPath, childPath);

    return relativePath === '' || (!relativePath.startsWith('..') && !path.isAbsolute(relativePath));
  }

  /**
   * Delete a backup file
   * @param appUrn - The app id
   * @param filename - The filename of the backup
   */
  public async deleteBackup(appUrn: AppUrn, filename: string) {
    const { backupPath } = this.getBackupFilePath(appUrn, filename);

    if (await this.filesystem.pathExists(backupPath)) {
      await this.filesystem.removeFile(backupPath);
    }
  }

  /**
   * Clean up old backups based on retention policy
   * @param appUrn - The app id
   * @param maxBackups - Maximum number of backups to keep (0 means no limit)
   */
  public async cleanupOldBackups(appUrn: AppUrn, maxBackups: number) {
    if (!maxBackups) {
      return;
    }

    const backups = await this.listBackupsByAppId(appUrn);

    if (backups.length <= maxBackups) {
      return;
    }

    backups.sort((a, b) => b.date - a.date);

    const backupsToDelete = backups.slice(maxBackups);
    this.logger.info(`Cleaning up ${backupsToDelete.length} old backup(s) for ${appUrn}...`);

    await Promise.all(backupsToDelete.map((backup) => this.deleteBackup(appUrn, backup.id)));
    this.logger.info(`Cleanup completed for ${appUrn}`);
  }

  /**
   * Delete all backups for an app
   * @param appUrn - The app id
   */
  public async deleteAppBackupsByUrn(appUrn: AppUrn): Promise<void> {
    const backups = await this.listBackupsByAppId(appUrn);

    await Promise.all(backups.map((backup) => this.deleteBackup(appUrn, backup.id)));
  }

  /**
   * List the backups for an app
   * @param appUrn - The app id
   * @returns The list of backups
   */
  public async listBackupsByAppId(appUrn: AppUrn) {
    const { backupDir: backupsDir } = this.getBackupPaths(appUrn);

    if (!(await this.filesystem.isDirectory(backupsDir))) {
      return [];
    }

    try {
      const list = await this.filesystem.listFiles(backupsDir);

      const backups = await Promise.all(
        list.map(async (backup) => {
          const backupPath = path.join(backupsDir, backup);

          if (!(await this.filesystem.isFile(backupPath))) {
            return null;
          }

          const stats = await this.filesystem.getStats(backupPath);
          return { id: backup, size: stats.size, date: stats.mtime.getTime() };
        }),
      );

      return backups.filter((backup) => backup !== null);
    } catch (error) {
      this.logger.error(`Error listing backups for app ${appUrn}:`, error);
      return [];
    }
  }

  /**
   * Get the file path for a backup
   * @param appUrn - The app id
   * @param filename - The filename of the backup
   * @returns The backup file path
   */
  public async getBackupPath(appUrn: AppUrn, filename: string): Promise<string> {
    const { backupPath } = this.getBackupFilePath(appUrn, filename);

    if (!(await this.filesystem.isFile(backupPath))) {
      throw new Error('The backup file does not exist');
    }

    return backupPath;
  }

  /**
   * Upload a backup file
   * @param appUrn - The app id
   * @param filename - The filename of the backup
   * @param fileBuffer - The file buffer
   */
  public async uploadBackup(appUrn: AppUrn, filename: string, fileBuffer: Buffer): Promise<void> {
    const { backupDir, backupPath, sanitizedFilename } = this.getBackupFilePath(appUrn, filename);

    // Create backup directory if it doesn't exist
    await this.filesystem.createDirectory(backupDir);

    // Check if file already exists
    if (await this.filesystem.pathExists(backupPath)) {
      throw new Error('A backup with this filename already exists');
    }

    // Write the file
    await this.filesystem.writeBinaryFile(backupPath, fileBuffer);

    this.logger.info(`Backup uploaded successfully: ${sanitizedFilename}`);
  }
}
