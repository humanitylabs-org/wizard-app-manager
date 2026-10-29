import { AsyncLocalStorage } from 'node:async_hooks';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { constants } from 'node:fs';

// Same inode as wam.final_host_tokens.authority_lock. Never unlink the lock.
// flock's FD-only form locks the shared open-file description. After the
// acquisition process exits, OUR descriptor keeps the lease until action drains;
// backend death closes it in the kernel. No shell/holder process can lose it early.
const held = new AsyncLocalStorage<Map<string, { active: boolean }>>();
export async function withHostAuthority<T>(dataDir: string, action: () => Promise<T>, signal?: AbortSignal): Promise<T> {
  signal?.throwIfAborted();
  const file = path.join(dataDir, 'user-config', '.wizard-host-authority.lock');
  if (held.getStore()?.get(file)?.active) return action();
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o755 });
  const fd = await fs.open(file, constants.O_RDONLY | constants.O_CREAT | constants.O_NOFOLLOW, 0o644);
  const lease = { active: false };
  try {
    signal?.throwIfAborted();
    // With command arguments, `flock -x 3 sh ...` locks a FILE NAMED "3".
    // FD-only acquisition MUST have no command. fd 3 is already open/no-follow.
    const proc = spawn('flock', ['-x', '3'], { stdio: ['ignore', 'ignore', 'ignore', fd.fd] });
    let spawnError: Error | undefined;
    proc.once('error', (error) => { spawnError = error; });
    const completion = new Promise<number | null>((resolve) => proc.once('close', (code) => resolve(code)));
    const abort = () => { proc.kill('SIGKILL'); };
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    let code: number | null;
    try {
      code = await completion;
    } finally {
      signal?.removeEventListener('abort', abort);
    }
    signal?.throwIfAborted();
    if (spawnError || code !== 0) throw new Error('Host authority lock unavailable', { cause: spawnError });
    lease.active = true;
    const context = new Map(held.getStore());
    context.set(file, lease);
    // Cancellation during action never releases another writer into unfinished
    // mutations. Drain action first, then report cancellation and close the fd.
    const result = await held.run(context, action);
    signal?.throwIfAborted();
    return result;
  } finally {
    lease.active = false;
    await fd.close();
  }
}
