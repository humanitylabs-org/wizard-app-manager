import { AsyncLocalStorage } from 'node:async_hooks';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { constants } from 'node:fs';

// Same inode as wam.final_host_tokens.authority_lock. Never unlink the lock.
// A child holds flock until stdin closes; process death releases the kernel lock.
const held = new AsyncLocalStorage<Map<string, { active: boolean }>>();
export async function withHostAuthority<T>(dataDir: string, action: () => Promise<T>): Promise<T> {
  const file = path.join(dataDir, 'user-config', '.wizard-host-authority.lock');
  if (held.getStore()?.get(file)?.active) return action();
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o755 });
  const fd = await fs.open(file, constants.O_RDONLY | constants.O_CREAT | constants.O_NOFOLLOW, 0o644);
  // Inherit an already opened inode rather than letting flock follow a path.
  const proc = spawn('flock', ['-x', '3', 'sh', '-c', 'printf "locked\\n"; cat >/dev/null'], {
    stdio: ['pipe', 'pipe', 'pipe', fd.fd],
  });
  proc.stdin!.on('error', () => {
    /* acquisition/exit path reports lock failure */
  });
  const completion = new Promise<void>((resolve) => proc.once('close', () => resolve()));
  const lease = { active: false };
  try {
    await new Promise<void>((resolve, reject) => {
      proc.once('error', reject);
      proc.once('exit', () => reject(new Error('Host authority lock unavailable')));
      let line = '';
      proc.stdout!.on('data', (b) => {
        line += String(b);
        if (line === 'locked\n') resolve();
        else if (line.includes('\n') || line.length > 32) reject(new Error('Host authority lock protocol failed'));
      });
    });
    lease.active = true;
    const context = new Map(held.getStore());
    context.set(file, lease);
    return await held.run(context, action);
  } finally {
    lease.active = false;
    proc.stdin!.end();
    await fd.close();
    await completion;
  }
}
