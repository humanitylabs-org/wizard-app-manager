/** Linux manager app-data operations. Never walk/chmod app-owned descendants.
 * Pin each directory with O_NOFOLLOW, including ancestors, so a renamed parent
 * cannot redirect an operation outside the opened directory. No secret reads.
 */
import { constants } from 'node:fs';
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import type { FileHandle } from 'node:fs/promises';

const directoryFlags = constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW;
const component = /^[a-zA-Z0-9_-]+$/;

export async function withAppDataDirectory<T>(
  base: string,
  store: string,
  app: string,
  operation: (directory: FileHandle) => Promise<T>,
): Promise<T> {
  if (!path.isAbsolute(base) || base.split('/').some((p) => p === '..' || p === '.') || !component.test(store) || !component.test(app)) {
    throw new Error('Unsafe app-data path');
  }
  let directory = await fs.open('/', directoryFlags);
  try {
    for (const part of [...base.split('/').filter(Boolean), store, app]) {
      const child = `/proc/self/fd/${directory.fd}/${part}`;
      // Create only. Existing directory permissions/ownership are authoritative.
      await fs.mkdir(child, { mode: 0o755 }).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== 'EEXIST') throw error;
      });
      const next = await fs.open(child, directoryFlags);
      await directory.close();
      directory = next;
    }
    return await operation(directory);
  } finally {
    await directory.close();
  }
}

/** Compatibility entry point for lifecycle callers. No recursive chmod, chown,
 * enumeration or secret-content access, before OR after compose up. app.env is
 * manager-owned and is tightened (never opened to other users), even skipEnv.
 */
export async function initializeAppDataDirectory(base: string, store: string, app: string): Promise<void> {
  await withAppDataDirectory(base, store, app, async (directory) => {
    const environment = await fs
      .open(`/proc/self/fd/${directory.fd}/app.env`, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
      .catch((error: NodeJS.ErrnoException) => {
        if (error.code !== 'ENOENT') throw error;
        return null;
      });
    if (!environment) return;
    try {
      const metadata = await environment.stat();
      if (!metadata.isFile() || metadata.nlink !== 1) throw new Error('Unsafe app environment file');
      await environment.chmod(0o600);
    } finally {
      await environment.close();
    }
  });
}

/** app.env is manager-owned, not a consumer mount. Atomic replacement ensures
 * a permissive old inode is never written with new secret bytes. The temporary
 * inode is owner-only from creation; parent and destination symlinks are refused.
 */
export async function writeAppEnvironment(base: string, store: string, app: string, content: string): Promise<void> {
  await withAppDataDirectory(base, store, app, async (directory) => {
    const prefix = `/proc/self/fd/${directory.fd}`;
    const destination = `${prefix}/app.env`;
    const previous = await fs.lstat(destination).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') throw error;
      return null;
    });
    if (previous && (!previous.isFile() || previous.nlink !== 1)) throw new Error('Unsafe app environment file');
    const temporary = `${prefix}/.app-env-${randomUUID()}`;
    let handle: FileHandle | undefined;
    try {
      handle = await fs.open(temporary, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
      await handle.writeFile(`${content}\n`, 'utf8');
      await handle.sync();
      await handle.close();
      handle = undefined;
      await fs.rename(temporary, destination);
      await directory.sync();
    } finally {
      await handle?.close();
      await fs.unlink(temporary).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== 'ENOENT') throw error;
      });
    }
  });
}
