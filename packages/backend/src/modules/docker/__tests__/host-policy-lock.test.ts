import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { describe, expect, it } from 'vitest';
import { withHostAuthority } from '../host-policy-lock';
const exec = promisify(execFile);

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
});
