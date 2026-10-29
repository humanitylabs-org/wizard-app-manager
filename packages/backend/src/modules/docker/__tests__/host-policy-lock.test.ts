import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFile, spawn, type ChildProcess } from 'node:child_process';
import { once } from 'node:events';
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { promisify } from 'node:util';
import { describe, expect, it, vi } from 'vitest';
import { withHostAuthority } from '../host-policy-lock';
const exec = promisify(execFile);
const probe = `import fcntl,sys
with open(sys.argv[1], 'r') as f:
 try:
  fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)
  print('acquired')
 except BlockingIOError:
  print('blocked')`;

async function pythonHolder(root: string) {
  const file = path.join(root, 'user-config', '.wizard-host-authority.lock');
  await fs.mkdir(path.dirname(file), { recursive: true });
  await fs.writeFile(file, '');
  const proc = spawn('python3', ['-u', '-c', `import fcntl,sys
with open(sys.argv[1], 'r') as f:
 fcntl.flock(f, fcntl.LOCK_EX)
 print('locked', flush=True)
 sys.stdin.read()`, file], { stdio: ['pipe', 'pipe', 'pipe'] });
  const closed = once(proc, 'close');
  await once(proc.stdout!, 'data');
  return { file, proc, closed };
}

async function acquisitionChild(file: string): Promise<number> {
  const lock = await fs.stat(file);
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    for (const entry of await fs.readdir('/proc')) {
      if (!/^\d+$/.test(entry)) continue;
      try {
        const status = await fs.readFile(`/proc/${entry}/status`, 'utf8');
        if (!new RegExp(`^PPid:\\s+${process.pid}$`, 'm').test(status)) continue;
        // Python's holder also opens this inode on fd 3. Identify flock itself.
        const executable = await fs.readlink(`/proc/${entry}/exe`);
        const argv = (await fs.readFile(`/proc/${entry}/cmdline`, 'utf8')).split('\0').filter(Boolean);
        if (path.basename(executable) !== 'flock' || argv.length !== 3 || argv[1] !== '-x' || argv[2] !== '3') continue;
        const inherited = await fs.stat(`/proc/${entry}/fd/3`);
        if (inherited.dev === lock.dev && inherited.ino === lock.ino) return Number(entry);
      } catch { /* unrelated process exited */ }
    }
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  throw new Error('real flock child did not enter acquisition');
}

async function stop(proc: ChildProcess, closed: Promise<unknown>) {
  if (proc.exitCode === null && proc.signalCode === null) proc.kill('SIGKILL');
  await closed;
}

function waitForLease(proc: ChildProcess): Promise<void> {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';
    const finish = (error?: Error) => {
      clearTimeout(timer);
      proc.stdout!.off('data', output);
      proc.stderr!.off('data', diagnostic);
      proc.off('error', failed);
      proc.off('close', earlyClose);
      if (error) reject(error);
      else resolve();
    };
    const output = (chunk: Buffer) => {
      stdout += chunk.toString();
      if (stdout.split(/\r?\n/).includes('locked')) finish();
    };
    const diagnostic = (chunk: Buffer) => { stderr = (stderr + chunk.toString()).slice(-8192); };
    const failed = (error: Error) => finish(error);
    const earlyClose = (code: number | null, signal: NodeJS.Signals | null) =>
      finish(new Error(`lease child exited before ready (${code ?? signal}): ${stderr}`));
    const timer = setTimeout(() => finish(new Error(`lease child never became ready: ${stderr}`)), 5000);
    proc.stdout!.on('data', output);
    proc.stderr!.on('data', diagnostic);
    proc.once('error', failed);
    proc.once('close', earlyClose);
  });
}

// Keep both fs APIs on the real isolated filesystem: flock inherits this fd.
vi.mock('node:fs', async (importOriginal) => importOriginal<typeof import('node:fs')>());

describe('host authority cross-process lock', () => {
  it('fences Python flock on the same inode, supports reentry and releases before returning', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const script = `import fcntl,sys
with open(sys.argv[1], 'r') as f:
 try:
  fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)
  print('acquired')
 except BlockingIOError:
  print('blocked')`;
    const file = path.join(root, 'user-config', '.wizard-host-authority.lock');
    try {
      await withHostAuthority(root, async () => {
        expect((await exec('python3', ['-c', script, file])).stdout.trim()).toBe('blocked');
        await withHostAuthority(root, async () => {
          expect((await exec('python3', ['-c', script, file])).stdout.trim()).toBe('blocked');
        });
      });
      expect((await exec('python3', ['-c', script, file])).stdout.trim()).toBe('acquired');
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('a concurrent config writer waits until validated consumption finishes', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const order: string[] = [];
    let entered!: () => void;
    let release!: () => void;
    const started = new Promise<void>((resolve) => {
      entered = resolve;
    });
    const barrier = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      const consumer = withHostAuthority(root, async () => {
        order.push('checked');
        entered();
        await barrier;
        order.push('consumed');
      });
      await started;
      const writer = withHostAuthority(root, async () => {
        order.push('writer');
      });
      // No timer assumption: the kernel lock remains held until the barrier opens.
      expect(order).toEqual(['checked']);
      release();
      await Promise.all([consumer, writer]);
      expect(order).toEqual(['checked', 'consumed', 'writer']);
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('waits behind a real Python holder before entering TypeScript', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const holder = await pythonHolder(root);
    let entered = false;
    const writer = withHostAuthority(root, async () => { entered = true; });
    try {
      await acquisitionChild(holder.file);
      expect(entered).toBe(false);
      holder.proc.stdin!.end();
      await holder.closed;
      await writer;
      expect(entered).toBe(true);
    } finally {
      await stop(holder.proc, holder.closed);
      await writer;
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it.each(['cancel', 'holder-failure'] as const)('fails closed on pending acquisition %s', async (mode) => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const holder = await pythonHolder(root);
    const controller = new AbortController();
    let entered = false;
    const writer = withHostAuthority(root, async () => { entered = true; }, controller.signal);
    const rejected = expect(writer).rejects.toThrow();
    try {
      const pid = await acquisitionChild(holder.file);
      expect(pid).not.toBe(holder.proc.pid);
      expect(holder.proc.exitCode).toBeNull();
      expect(holder.proc.signalCode).toBeNull();
      if (mode === 'cancel') controller.abort(new Error('cancelled'));
      else process.kill(pid, 'SIGKILL');
      await rejected;
      expect(entered).toBe(false);
      // Acquisition failure must leave the independent Python lease intact.
      expect(holder.proc.exitCode).toBeNull();
      expect(holder.proc.signalCode).toBeNull();
      expect((await exec('python3', ['-c', probe, holder.file])).stdout.trim()).toBe('blocked');
      holder.proc.stdin!.end();
      await holder.closed;
      expect((await exec('python3', ['-c', probe, holder.file])).stdout.trim()).toBe('acquired');
    } finally {
      controller.abort();
      await stop(holder.proc, holder.closed);
      await writer.catch(() => {});
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('keeps the lease until a cancelled action drains and releases after an action error', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const controller = new AbortController();
    const file = path.join(root, 'user-config', '.wizard-host-authority.lock');
    try {
      await expect(withHostAuthority(root, async () => {
        await Promise.resolve();
        await withHostAuthority(root, async () => {
          controller.abort(new Error('cancelled action'));
          expect((await exec('python3', ['-c', probe, file])).stdout.trim()).toBe('blocked');
        });
      }, controller.signal)).rejects.toThrow('cancelled action');
      expect((await exec('python3', ['-c', probe, file])).stdout.trim()).toBe('acquired');
      await expect(withHostAuthority(root, async () => { throw new Error('action failed'); })).rejects.toThrow('action failed');
      expect((await exec('python3', ['-c', probe, file])).stdout.trim()).toBe('acquired');
      await expect(fs.lstat(path.resolve('3'))).rejects.toMatchObject({ code: 'ENOENT' });
    } finally {
      await fs.rm(root, { recursive: true, force: true });
    }
  });

  it('kernel-releases the actual TypeScript lease after its owning process crashes', async () => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'host-authority-'));
    const file = path.join(root, 'user-config', '.wizard-host-authority.lock');
    const module = pathToFileURL(fileURLToPath(new URL('../host-policy-lock.ts', import.meta.url))).href;
    const loader = pathToFileURL(createRequire(import.meta.url).resolve('tsx')).href;
    const code = `const authority = await import(${JSON.stringify(module)});
      const withHostAuthority = authority.withHostAuthority ?? authority.default?.withHostAuthority;
      if (typeof withHostAuthority !== 'function') throw new Error('missing real withHostAuthority export');
      await withHostAuthority(${JSON.stringify(root)}, async () => {
        console.log('locked'); await new Promise(() => { setInterval(() => {}, 1000); });
      });`;
    // Vitest may run under Bun; tsx's Node loader needs the Node executable
    // and the exact workspace loader, not Vitest's ambient runtime/lookup.
    const proc = spawn('node', ['--import', loader, '--input-type=module', '-e', code], { stdio: ['ignore', 'pipe', 'pipe'] });
    const closed = once(proc, 'close');
    void closed.catch(() => {});
    try {
      await waitForLease(proc);
      expect((await exec('python3', ['-c', probe, file])).stdout.trim()).toBe('blocked');
      await stop(proc, closed);
      expect((await exec('python3', ['-c', probe, file])).stdout.trim()).toBe('acquired');
    } finally {
      await stop(proc, closed);
      await fs.rm(root, { recursive: true, force: true });
    }
  }, 15000);
});
