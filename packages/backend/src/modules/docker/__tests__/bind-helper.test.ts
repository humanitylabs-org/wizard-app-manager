import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { bindMetadataArgs, bindMetadataImage, reapBindHelper, runBindHelper, type HelperRun } from '../bind-helper';

vi.mock('node:fs', async (load) => load<typeof import('node:fs')>());
const roots: string[] = [];
const request = { image: 'sha256:' + 'a'.repeat(64), managerUid: 0, source: '/safe' };
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
    if (!info) throw Object.assign(new Error('absent'), { code: 1, stderr: `Error: No such container: ${args.at(-1)}` });
    return { stdout: JSON.stringify([info]) };
  });
  return { root, run, calls, record: path.join(root, 'user-config/.wizard-bind-check.json') };
}
describe('bounded helper custody', () => {
  it('constructs only the fixed confined command and automatic root propagation', () => {
    const args = bindMetadataArgs(request);
    expect(args).toEqual(['--rm', '--restart=no', '--stop-timeout=1', '--network=none', '--read-only',
      '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--user=0:0', '--entrypoint=/bin/busybox',
      '--mount', 'type=bind,source=/,target=/host,readonly', request.image, 'timeout', '-s', 'KILL', '12',
      '/app/libexec/wizard-bind-metadata', '0', '/safe']);
    for (const bad of [{ image: 'node:latest' }, { image: 'sha256:short' }, { managerUid: -1 },
      { managerUid: 0xffffffff }, { managerUid: 1.5 }, { source: '/a/../b' }, { source: '/a\n' },
      { source: '/a/' }, { source: '/' + 'a'.repeat(4090) }])
      expect(() => bindMetadataArgs({ ...request, ...bad })).toThrow('Invalid bind metadata request');
  });

  it('uses the running manager inspect.Image, never the floating Config.Image', async () => {
    const hostname = 'b'.repeat(12), id = 'b'.repeat(64);
    const spy = vi.spyOn(os, 'hostname').mockReturnValue(hostname);
    const statSpy = vi.spyOn(fs, 'lstat').mockImplementation(async (name: any) => ({
      uid: 0, mode: String(name).endsWith('wizard-bind-metadata') ? 0o555 : 0o755,
      isDirectory: () => !String(name).endsWith('wizard-bind-metadata'),
      isFile: () => String(name).endsWith('wizard-bind-metadata'),
    }) as any);
    try {
      const run: HelperRun = vi.fn(async (args) => {
        if (args[0] === 'image') {
          expect(args).toEqual(['image', 'inspect', request.image]);
          return { stdout: JSON.stringify([{ Id: request.image, Config: { Labels: { 'org.wizard.bind-metadata': '/app/libexec/wizard-bind-metadata' } } }]) };
        }
        return { stdout: JSON.stringify([{ Id: id, Image: request.image, State: { Running: true }, Config: { Hostname: hostname, Image: 'node:latest' } }]) };
      });
      expect(await bindMetadataImage(run)).toBe(request.image);
    } finally { statSpy.mockRestore(); spy.mockRestore(); }
  });

  it('refuses unsupported self identity before using an image', async () => {
    const spy = vi.spyOn(os, 'hostname').mockReturnValue('owner-chosen-hostname');
    const run: HelperRun = vi.fn();
    try { await expect(bindMetadataImage(run)).rejects.toThrow('Cannot attest backend identity'); expect(run).not.toHaveBeenCalled(); }
    finally { spy.mockRestore(); }
  });

  it('refuses a malformed or different full self/image identity and missing helper declaration', async () => {
    const hostname = 'b'.repeat(12);
    const spy = vi.spyOn(os, 'hostname').mockReturnValue(hostname);
    const self = { Id: 'b'.repeat(64), Image: request.image, State: { Running: true }, Config: { Hostname: hostname } };
    try {
      for (const change of [{ Id: hostname }, { Id: 'c'.repeat(64) }, { Image: 'node:latest' }, { State: { Running: false } }]) {
        const run: HelperRun = async () => ({ stdout: JSON.stringify([{ ...self, ...change }]) });
        await expect(bindMetadataImage(run)).rejects.toThrow('Cannot attest backend image');
      }
      const run: HelperRun = async (args) => ({ stdout: JSON.stringify([args[0] === 'image' ? { Id: request.image, Config: {} } : self]) });
      await expect(bindMetadataImage(run)).rejects.toThrow('lacks packaged metadata helper');
    } finally { spy.mockRestore(); }
  });

  it('does not start a full ID which fails name/nonce attestation', async () => {
    const f = await fixture();
    const run: HelperRun = async (args, signal) => {
      const value = await f.run(args, signal);
      if (args[0] === 'inspect' && args.at(-1) === 'a'.repeat(64)) {
        const info = JSON.parse(value.stdout); info[0].Name = '/unrelated';
        return { stdout: JSON.stringify(info) };
      }
      return value;
    };
    await expect(runBindHelper(f.root, request, run)).rejects.toThrow('phase=create');
    expect(f.calls.some((args) => args[0] === 'start')).toBe(false);
  });

  it('preserves first safe failure when cleanup fails too, without child output', async () => {
    const f = await fixture();
    const run: HelperRun = async (args, signal) => {
      if (args[0] === 'start') throw Object.assign(new Error('untrusted-secret'), { code: 137, stderr: 'untrusted-secret' });
      if (args[0] === 'rm') throw new Error('cleanup unavailable');
      return f.run(args, signal);
    };
    const error = await runBindHelper(f.root, request, run).catch((e) => e);
    expect(error).toBeInstanceOf(AggregateError);
    expect(error.cause.message).toBe('Bind metadata execution failed (phase=start, exit=137)');
    expect(String(error.cause)).not.toContain('untrusted-secret');
    expect(JSON.parse(await fs.readFile(f.record, 'utf8')).id).toBe('a'.repeat(64));
  });

  it('never treats an unrelated/mixed missing diagnostic as absence', async () => {
    const f = await fixture();
    await fs.writeFile(f.record, JSON.stringify({ name: 'wizard-bind-' + 'a'.repeat(36), nonce: 'a'.repeat(36), phase: 'created', id: 'a'.repeat(64) }));
    const run: HelperRun = async () => { throw Object.assign(new Error('failed'), { code: 1, stderr: 'Error: No such container: unrelated\ntransport failure' }); };
    await expect(reapBindHelper(f.root, run)).rejects.toThrow('quarantined');
    expect(await fs.readFile(f.record, 'utf8')).toContain('created');
  });

  it('refuses a journal full-ID mismatch without removing any object', async () => {
    const f = await fixture();
    const run: HelperRun = async (args, signal) => {
      const value = await f.run(args, signal);
      if (args[0] === 'start') {
        const record = JSON.parse(await fs.readFile(f.record, 'utf8')); record.id = 'c'.repeat(64);
        await fs.writeFile(f.record, JSON.stringify(record));
      }
      return value;
    };
    await expect(runBindHelper(f.root, request, run)).rejects.toThrow('identity refused');
    expect(f.calls.some((args) => args[0] === 'rm')).toBe(false);
  });

  it('acknowledges removal and absence before clearing durable intent', async () => {
    const f = await fixture();
    expect(await runBindHelper(f.root, request, f.run)).toEqual({ stdout: 'safe\n' });
    expect(f.calls.map((c) => c[0])).toEqual(['create', 'inspect', 'start', 'inspect', 'rm', 'inspect']);
    await expect(fs.stat(f.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
  it('a failed/hung client still reaps its named helper under fresh cleanup budget', async () => {
    const f = await fixture();
    const run: HelperRun = async (args, signal) => {
      if (args[0] === 'start') throw new Error('execution timeout');
      return f.run(args, signal);
    };
    await expect(runBindHelper(f.root, request, run)).rejects.toThrow('Bind metadata execution failed (phase=start, exit=unknown)');
    expect(f.calls.map((c) => c[0])).toEqual(['create', 'inspect', 'inspect', 'rm', 'inspect']);
    await expect(fs.stat(f.record)).rejects.toMatchObject({ code: 'ENOENT' });
  });
  it('daemon cleanup failure leaves a quarantine and refuses later mutations', async () => {
    const f = await fixture();
    const broken: HelperRun = async (args, signal) => {
      if (args[0] === 'rm') throw new Error('daemon unavailable');
      return f.run(args, signal);
    };
    await expect(runBindHelper(f.root, request, broken)).rejects.toThrow('quarantined');
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
    await expect(runBindHelper(f.root, request, uncertain)).rejects.toThrow('execution and cleanup failed');
    expect(JSON.parse(await fs.readFile(f.record, 'utf8')).phase).toBe('creating');
    await expect(reapBindHelper(f.root, uncertain)).rejects.toThrow('quarantined');
  });
});
