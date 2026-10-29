import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { reapBindHelper, runBindHelper, type HelperRun } from '../bind-helper';

vi.mock('node:fs', async (load) => load<typeof import('node:fs')>());
const roots: string[] = [];
afterEach(async () => { for (const r of roots.splice(0)) await fs.rm(r, { recursive: true, force: true }); });
async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'bind-helper-'));
  roots.push(root); await fs.mkdir(path.join(root, 'user-config'));
  let info: any;
  const calls: string[][] = [];
  const run: HelperRun = vi.fn(async (args, signal) => {
    expect(signal).toBeInstanceOf(AbortSignal);
    calls.push(args);
    if (args[0] === 'create') {
      // Intent is durable before any engine request.
      const record = JSON.parse(await fs.readFile(path.join(root, 'user-config/.wizard-bind-check.json'), 'utf8'));
      expect(record.phase).toBe('creating');
      info = { Id: 'a'.repeat(64), Name: `/${record.name}`, Config: { Labels: { 'wizard.bind-check': record.nonce } } };
      return { stdout: info.Id + '\n' };
    }
    if (args[0] === 'start') return { stdout: 'safe\n' };
    if (args[0] === 'rm') { info = undefined; return { stdout: '' }; }
    if (!info) throw Object.assign(new Error('absent'), { stderr: 'Error: No such container' });
    return { stdout: JSON.stringify([info]) };
  });
  return { root, run, calls, record: path.join(root, 'user-config/.wizard-bind-check.json') };
}
describe('bounded helper custody', () => {
  it('acknowledges removal and absence before clearing durable intent', async () => {
    const f = await fixture();
    expect(await runBindHelper(f.root, ['--rm', 'image', 'timeout', '12', 'sh'], f.run)).toEqual({ stdout: 'safe\n' });
    expect(f.calls.map((c) => c[0])).toEqual(['create', 'start', 'inspect', 'rm', 'inspect']);
    await expect(fs.stat(f.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
  it('a failed/hung client still reaps its named helper under fresh cleanup budget', async () => {
    const f = await fixture();
    const run: HelperRun = async (args, signal) => {
      if (args[0] === 'start') throw new Error('execution timeout');
      return f.run(args, signal);
    };
    await expect(runBindHelper(f.root, ['--rm', 'image'], run)).rejects.toThrow('execution timeout');
    expect(f.calls.map((c) => c[0])).toEqual(['create', 'inspect', 'rm', 'inspect']);
    await expect(fs.stat(f.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
  it('daemon cleanup failure leaves a quarantine and refuses later mutations', async () => {
    const f = await fixture();
    const broken: HelperRun = async (args, signal) => {
      if (args[0] === 'rm') throw new Error('daemon unavailable');
      return f.run(args, signal);
    };
    await expect(runBindHelper(f.root, ['--rm', 'image'], broken)).rejects.toThrow('quarantined');
    expect(JSON.parse(await fs.readFile(f.record, 'utf8')).phase).toBe('created');
    await reapBindHelper(f.root, f.run);
    await expect(fs.stat(f.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
  it('uncertain create + absent readback stays fenced, not declared safe', async () => {
    const f = await fixture();
    const uncertain: HelperRun = async (args, signal) => {
      if (args[0] === 'create') throw new Error('client died before acknowledgement');
      return f.run(args, signal);
    };
    await expect(runBindHelper(f.root, ['--rm', 'image'], uncertain)).rejects.toThrow('outcome unknown');
    expect(JSON.parse(await fs.readFile(f.record, 'utf8')).phase).toBe('creating');
    await expect(reapBindHelper(f.root, uncertain)).rejects.toThrow('quarantined');
  });
});
