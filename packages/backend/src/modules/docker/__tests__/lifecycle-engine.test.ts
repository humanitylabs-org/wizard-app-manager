import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as yaml from 'yaml';
import { createAppUrn } from '@/common/helpers/app-helpers';
import { DockerService } from '../docker.service';
import { StopAppCommand } from '@/modules/app-lifecycle/commands/stop-app-command';
import { RestartAppCommand } from '@/modules/app-lifecycle/commands/restart-app-command';
import { UninstallAppCommand } from '@/modules/app-lifecycle/commands/uninstall-app-command';
import { LoggerService } from '@/core/logger/logger.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { AppHelpers } from '@/modules/apps/app.helpers';
import { teardownApp } from '../app-teardown';
import { BIND_METADATA_SUPERVISOR, runBindHelper, reapBindHelper, bindMetadataImage, helperRun } from '../bind-helper';
// App/canary fixture only, NEVER the production metadata helper artifact.
const BIND_CHECK_IMAGE = 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc';

vi.mock('node:fs', async (load) => load<typeof import('node:fs')>());
const exec = promisify(execFile);
const enabled = process.env.WIZARD_LIFECYCLE_ENGINE_PROOF === '1';
const cases: { root: string; urn: string; volume: string; appData: string }[] = [];
const docker = async (...args: string[]) => (await exec('docker', args, { timeout: 45_000 })).stdout.trim();
const anonymousVolumes = new Set<string>();
const anonymousImages = new Set<string>();
const persistedContainers = new Set<string>();

// One synthetic app/container, one persisted contender, one anonymous volume and
// image per case. API requests are <=15s; helper recovery retains its own <=20s
// budget; each CLI call is <=45s. This hook permits their sequential worst case,
// including Docker's legitimate 10s stop grace, without changing product deadlines.
const CLEANUP_BUDGET_MS = 8 * 15_000 + 20_000 + 4 * 45_000 + 10_000;
let cleanupFailure: AggregateError | undefined;
const removeVolume = async (name: string) => {
  try { await docker('volume', 'rm', name); }
  catch (error: any) {
    // Accept only the complete daemon missing-volume diagnostic for THIS name.
    // Permission, in-use, transport and mixed/multiline errors must still fail.
    if (error.code !== 1 || String(error.stderr).trim().toLowerCase() !== `error: no such volume: ${name}`.toLowerCase()) throw error;
  }
};

afterEach(async () => {
  const failures: Error[] = [];
  const collect = async (phase: string, action: () => Promise<unknown>) => {
    try { await action(); }
    catch (cause) {
      const error = new Error(`Lifecycle fixture cleanup failed: ${phase}`, { cause });
      failures.push(error);
      console.error(error, cause);
    }
  };
  for (const id of persistedContainers) await collect('persisted container', () => docker('rm', '-f', id));
  persistedContainers.clear();
  for (const c of cases.splice(0)) {
    await collect('app teardown', () => teardownApp(c.urn, 'down --remove-orphans'));
    await collect('canary volume', () => removeVolume(c.volume));
    await collect('bind helper recovery', () => reapBindHelper(c.root));
    await collect('case directory', () => fs.rm(c.root, { recursive: true, force: true }));
    await collect('app data', () => fs.rm(c.appData, { recursive: true, force: true }));
  }
  for (const name of anonymousVolumes) await collect('anonymous volume', () => removeVolume(name));
  anonymousVolumes.clear();
  for (const image of anonymousImages) await collect('anonymous image', () => docker('image', 'rm', image));
  anonymousImages.clear();
  if (failures.length) {
    // Vitest retains the original body failure separately. Preserve the first AND
    // all cleanup failures, and prohibit another engine fixture after unsafe cleanup.
    await collect('cleanup failure marker', () => fs.writeFile('/opt/lifecycle/cleanup-failure.json', JSON.stringify(failures.map((error) => ({ message: error.message, cause: String(error.cause) })))));
    cleanupFailure = new AggregateError(failures, 'Lifecycle fixture cleanup incomplete', { cause: failures[0] });
    throw cleanupFailure;
  }
}, CLEANUP_BUDGET_MS + 5_000);

async function fixture() {
  if (cleanupFailure) throw cleanupFailure;
  if (!(await fs.readFile('/run/wizard-lifecycle-disposable', 'utf8')).startsWith('synthetic')) throw new Error('Disposable VM required');
  const root = await fs.mkdtemp('/opt/lifecycle/case-');
  const id = path.basename(root).toLowerCase();
  const urn = createAppUrn(id, 'wizardapps');
  const project = `${id}_wizardapps`;
  const volume = `${project}_canary`;
  const appDataRoot = '/opt/lifecycle/app-data';
  const appData = path.join(appDataRoot, 'wizardapps', id);
  cases.push({ root, urn, volume, appData });
  await fs.mkdir(appData, { recursive: true, mode: 0o755 });
  await fs.mkdir(path.join(root, 'user-config'), { mode: 0o755 });
  await fs.writeFile(path.join(root, '.env'), 'SYNTHETIC=1\n');
  const file = path.join(root, 'compose.yml');
  const model: any = { services: { web: {
    image: BIND_CHECK_IMAGE, command: ['sh', '-c', 'while :; do sleep 1; done'],
    cap_drop: ['ALL'], network_mode: 'none', labels: { 'runtipi.appurn': urn },
    volumes: ['canary:/canary'],
  } }, volumes: { canary: {} } };
  const write = async () => fs.writeFile(file, yaml.stringify(model));
  await write();
  const service: any = Object.create(DockerService.prototype);
  service.config = {
    get: () => ({ dataDir: root, appDataDir: appDataRoot }),
    getConfig: () => ({ directories: { dataDir: root, appDataDir: appDataRoot }, rootFolderHost: root, userSettings: { appDataPath: '/opt/lifecycle' } }),
  };
  service.logger = { info: vi.fn(), debug: vi.fn(), warn: vi.fn(), error: vi.fn() };
  // Repository/storage generation is outside this seam. Consume real immutable
  // files through the production final gate, Compose executable and daemon APIs.
  service.prepareApp = async (_urn: string, apply: any) => apply(await fs.readFile(file, 'utf8'));
  service.getBaseComposeArgsApp = async () => ({ args: ['--project-name', project, '--file', file], isCustomConfig: false });
  const files = {
    getInstalledAppInfo: async () => ({ force_pull: false }),
    deleteAppFolder: vi.fn(), deleteAppDataDir: vi.fn(),
  };
  Object.defineProperty(service, 'storage', { value: { apps: async () => [], remove: async () => [] } });
  const logger = { info: vi.fn(), error: vi.fn(), warn: vi.fn() };
  const ref: any = { get: (token: any) => new Map<any, any>([
    [DockerService, service], [LoggerService, logger], [AppFilesManager, files], [AppHelpers, {}],
  ]).get(token) };
  const ids = async () => (await docker('ps', '-aq', '--no-trunc', '--filter', `label=com.docker.compose.project=${project}`)).split(/\s+/).filter(Boolean);
  const hookAbsent = async () => expect(await docker('run', '--rm', '--network=none', '-v', `${volume}:/canary:ro`, BIND_CHECK_IMAGE,
    'sh', '-ec', 'test ! -e /canary/hook; printf absent')).toBe('absent');
  return { root, urn, project, volume, appData, file, model, write, service, files, ref, ids, hookAbsent };
}

describe.runIf(enabled)('production lifecycle with real engine (disposable guest only)', () => {
  it.each(['stop', 'down --remove-orphans'] as const)('retains image-VOLUME anonymous identity and sentinel bytes on %s', async (command) => {
    const f = await anonymousFixture();
    await f.service.composeApp(f.urn, command);
    // Read back the ORIGINAL daemon volume name, never a replacement volume or
    // next-up mount. Removed anonymous volumes are not automatically remounted.
    expect(JSON.parse(await docker('volume', 'inspect', f.anonymous))[0].Name).toBe(f.anonymous);
    expect(await docker('run', '--rm', '--network=none', '--mount', `type=volume,source=${f.anonymous},target=/recover,readonly`,
      BIND_CHECK_IMAGE, 'cat', '/recover/sentinel')).toBe('anonymous-persistent-sentinel');
  }, 120_000);

  it('explicit --volumes uninstall deletes the image-VOLUME anonymous mount', async () => {
    const f = await anonymousFixture();
    await f.service.composeApp(f.urn, 'down --volumes --remove-orphans');
    await expect(docker('volume', 'inspect', f.anonymous)).rejects.toMatchObject({ stderr: expect.stringMatching(/no such volume/i) });
  }, 120_000);

  it.each(['stopped-writable-parent', 'stopped-readonly-child'] as const)('refuses %s persisted restart-always overlap before app create', async (kind) => {
    const f = await fixture();
    const parent = path.join(f.appData, 'data');
    const child = path.join(parent, 'child');
    await fs.mkdir(child, { recursive: true });
    const parentFirst = kind === 'stopped-writable-parent';
    const persisted = await docker('create', '--restart=always', '--network=none', '--mount',
      `type=bind,source=${parentFirst ? parent : child},target=/persisted${parentFirst ? '' : ',readonly'}`,
      BIND_CHECK_IMAGE, 'sleep', '300');
    persistedContainers.add(persisted);
    await docker('start', persisted); await docker('stop', '-t', '1', persisted);
    const info = JSON.parse(await docker('inspect', persisted))[0];
    expect(info.State.Running).toBe(false);
    expect(info.HostConfig.RestartPolicy.Name).toBe('always');
    f.model.services.web.volumes = [{ type: 'bind', source: parentFirst ? child : parent, target: '/data' }];
    await f.write();
    const beforeStart = vi.fn();
    await expect(f.service.composeApp(f.urn, 'up --detach', beforeStart)).rejects.toThrow('overlap');
    expect(beforeStart).not.toHaveBeenCalled();
    expect(await f.ids()).toEqual([]);
    // No daemon restart needed: conservative all-container inventory is the gate.
    expect(JSON.parse(await docker('inspect', persisted))[0].State.Running).toBe(false);
  }, 120_000);

  it.each(['stop', 'uninstall', 'restart'] as const)('ignores hooks added after startup during %s teardown', async (action) => {
    const f = await fixture();
    await f.service.composeApp(f.urn, 'up --detach');
    expect(await f.ids()).toHaveLength(1);
    const old = (await f.ids())[0];
    f.model.services.web.pre_stop = [{ command: ['sh', '-c', 'touch /canary/hook'] }];
    await f.write();
    if (action === 'restart') {
      const command = new RestartAppCommand(f.ref, {} as any);
      vi.spyOn(command as any, 'ensureAppDir').mockResolvedValue(undefined);
      expect(await command.execute(f.urn, { skipEnv: true, skipPull: true })).toMatchObject({ success: false, deploymentUnchanged: true });
      expect((JSON.parse(await docker('inspect', old!)))[0].State.Running).toBe(true);
      // A later valid restart uses the same safe teardown primitive.
      delete f.model.services.web.pre_stop; await f.write();
      expect(await command.execute(f.urn, { skipEnv: true, skipPull: true })).toMatchObject({ success: true });
      expect((await f.ids())[0]).not.toBe(old);
    } else if (action === 'stop') {
      expect(await new StopAppCommand(f.ref, {} as any).execute(f.urn, {})).toMatchObject({ success: true });
      expect(await f.ids()).toEqual([]);
    } else {
      expect(await new UninstallAppCommand(f.ref, {} as any).execute(f.urn)).toMatchObject({ success: true });
      expect(f.files.deleteAppDataDir).toHaveBeenCalledWith(f.urn);
      expect(await f.ids()).toEqual([]);
    }
    await f.hookAbsent();
  }, 120_000);

  it.each(['override-only', 'stale-recipe'] as const)('fences concurrent revoke and stops existing privilege despite %s drift', async (kind) => {
    const f = await fixture();
    await fs.writeFile(path.join(f.root, 'user-config/wizard-host-approvals.json'), JSON.stringify({ schema: 1, apps: { [`wizardapps/${f.urn.split(':')[0]}`]: ['web|privileged'] } }));
    const override = path.join(f.root, 'override.yml');
    if (kind === 'override-only') {
      await fs.writeFile(override, 'services:\n  web:\n    privileged: true\n');
      f.service.getBaseComposeArgsApp = async () => ({ args: ['--project-name', f.project, '--file', f.file, '--file', override], isCustomConfig: false });
    } else { f.model.services.web.privileged = true; await f.write(); }
    const done = path.join(f.root, 'revoked');
    const py = `import sys; sys.path.insert(0, ${JSON.stringify(path.resolve('../..'))}); from pathlib import Path; from types import SimpleNamespace; from wam import host_policy; c=SimpleNamespace(home=Path(${JSON.stringify(path.join(f.root, 'home'))}),runtipi_root=Path(${JSON.stringify(f.root)})); host_policy.revoke(c, ${JSON.stringify(f.urn.split(':')[0])}); Path(${JSON.stringify(done)}).touch()`;
    let revoke: ReturnType<typeof exec> | undefined;
    await f.service.composeApp(f.urn, 'up --detach', async () => {
      // This runs AFTER real final validation, with the shared authority lock held.
      revoke = exec('python3', ['-c', py], { timeout: 45_000 });
      await new Promise((r) => setTimeout(r, 100));
      await expect(fs.stat(done)).rejects.toMatchObject({ code: 'ENOENT' });
    });
    await revoke;
    expect(JSON.parse(await fs.readFile(path.join(f.root, 'user-config/wizard-host-approvals.json'), 'utf8')).apps).toEqual({});
    const id = (await f.ids())[0]!;
    expect(JSON.parse(await docker('inspect', id))[0].HostConfig.Privileged).toBe(true);
    // Current recipe no longer declares the running privilege, and gains a hook.
    delete f.model.services.web.privileged;
    f.model.services.web.pre_stop = [{ command: ['sh', '-c', 'touch /canary/hook'] }];
    await f.write();
    if (kind === 'override-only') await fs.writeFile(override, 'services: {}\n');
    expect(await new StopAppCommand(f.ref, {} as any).execute(f.urn, {})).toMatchObject({ success: true });
    expect(await f.ids()).toEqual([]);
    await f.hookAbsent();
  }, 120_000);

  it('provisions only a missing data endpoint for a real nonroot app, preserving existing secrets', async () => {
    const f = await fixture();
    const secret = path.join(f.appData, 'app-secret');
    await fs.writeFile(secret, 'synthetic', { mode: 0o640 });
    const before = await fs.stat(secret);
    f.model.services.web.user = '12345:12345';
    f.model.services.web.volumes = [{ type: 'bind', source: path.join(f.appData, 'data'), target: '/data' }];
    f.model.services.web.command = ['sh', '-c', 'echo synthetic > /data/written; while :; do sleep 1; done'];
    await f.write();
    try { await f.service.composeApp(f.urn, 'up --detach'); }
    catch (primary) {
      // Fixed synthetic paths only: capture ancestry metadata before afterEach
      // removes it. Never chmod an ancestor or pre-create the requested endpoint.
      const ancestry = [];
      let current = '/';
      for (const part of path.join(f.appData, 'data').split('/').filter(Boolean)) {
        current = path.join(current, part);
        try {
          const stat = await fs.lstat(current);
          ancestry.push({ path: current, uid: stat.uid, gid: stat.gid, mode: stat.mode & 0o7777, symlink: stat.isSymbolicLink() });
        } catch (error: any) { ancestry.push({ path: current, code: error.code }); }
      }
      console.error('Synthetic missing-endpoint ancestry (primary failure retained)', ancestry);
      throw primary;
    }
    const id = (await f.ids())[0]!;
    expect(await docker('exec', id, 'sh', '-ec', 'n=0; while [ ! -f /data/written ] && [ $n -lt 50 ]; do n=$((n+1)); sleep 0.1; done; cat /data/written')).toBe('synthetic');
    const after = await fs.stat(secret);
    expect([after.uid, after.gid, after.mode]).toEqual([before.uid, before.gid, before.mode]);
    expect(await fs.readFile(secret, 'utf8')).toBe('synthetic');
    expect((await fs.stat(path.join(f.appData, 'data/written'))).uid).toBe(12345);
  }, 120_000);

  it('daemon lifetime and journal recovery reap a helper after backend process death', async () => {
    const f = await fixture();
    const loader = pathToFileURL(createRequire(path.join(process.cwd(), 'package.json')).resolve('tsx')).href;
    const moduleUrl = pathToFileURL(path.resolve('src/modules/docker/bind-helper.ts')).href;
    const ready = 'PRIVSEP_HELPER_READY';
    const code = `const ns = await import(${JSON.stringify(moduleUrl)});
      const api = ns.runBindHelper ? ns : ns.default;
      if (typeof api?.runBindHelper !== 'function' || typeof api?.helperRun !== 'function') throw new Error('bind helper export shape unavailable');
      await api.runBindHelper(${JSON.stringify(f.root)}, { image: await api.bindMetadataImage(), managerUid: 0, source: '/' },
        (args, signal) => {
          // Test-only replacement of the body, preserving the production
          // daemon watchdog/custody. This is a death test, NOT seccomp proof.
          const at = args.indexOf('/app/libexec/wizard-bind-metadata');
          const command = at < 0 ? args : [...args.slice(0, at), '/bin/sleep', '300'];
          const pending = api.helperRun(command, signal);
          if (args[0] === 'start') console.log(${JSON.stringify(ready)}); return pending; });`;
    // Vitest can itself run under Bun. The helper child must be explicit Node with
    // the workspace-resolved loader, never process.execPath or an implicit loader.
    const child = spawn('node', ['--import', loader, '--input-type=module', '-e', code], { stdio: ['ignore', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', closed = false;
    let spawnError: Error | undefined;
    child.stdout.on('data', (data) => { stdout = (stdout + String(data)).slice(-8192); });
    child.stderr.on('data', (data) => { stderr = (stderr + String(data)).slice(-8192); });
    child.once('error', (error) => { spawnError = error; });
    const exited = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve) => child.once('close', (code, signal) => { closed = true; resolve({ code, signal }); }));
    const reapChild = async () => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      try { return await Promise.race([exited, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('Helper child was not reaped')), 3_000); })]); }
      finally { clearTimeout(timer); }
    };
    let primary: unknown;
    try {
      let id = '', name = '';
      const deadline = Date.now() + 5_000;
      while (Date.now() < deadline && !id) {
        if (closed || spawnError) throw new Error(`Helper child exited before readiness: ${stderr}`, { cause: spawnError });
        if (stdout.split(/\r?\n/).includes(ready)) {
          const journal = JSON.parse(await fs.readFile(path.join(f.root, 'user-config/.wizard-bind-check.json'), 'utf8'));
          expect(journal.phase).toBe('created');
          const info = JSON.parse(await docker('inspect', '--type', 'container', journal.name))[0];
          expect(info.Config.Labels['wizard.bind-check']).toBe(journal.nonce);
          expect(info.Image).toBe(await bindMetadataImage());
          expect(info.Config.Entrypoint).toEqual(['/bin/busybox']);
          expect(info.Config.Cmd).toEqual(['sh', '-c', BIND_METADATA_SUPERVISOR, 'wizard-bind-metadata-supervisor', '/bin/sleep', '300']);
          if (info.State.Running) { id = info.Id; name = journal.name; }
        }
        if (!id) await new Promise((r) => setTimeout(r, 100));
      }
      expect(id, `Helper readiness timeout (not an early exit): ${stderr}`).not.toBe('');
      // Read exact NUL-delimited argv rather than width-truncated ps args. The
      // packaged shell must still be init, with the actual timeout target its
      // non-init child. No dependency on a host init executable/configuration.
      expect((await docker('exec', id, '/bin/busybox', 'cat', '/proc/1/cmdline')).split('\0').slice(0, -1))
        .toEqual(['/bin/busybox', 'sh', '-c', BIND_METADATA_SUPERVISOR, 'wizard-bind-metadata-supervisor', '/bin/sleep', '300']);
      const processes = (await docker('exec', id, '/bin/busybox', 'ps', '-o', 'pid,ppid,comm')).split('\n').slice(1).map((line) => line.trim().split(/\s+/));
      const targets = processes.filter(([pid, ppid, comm]) => Number(pid) > 1 && ppid === '1' && comm === 'sleep');
      expect(targets).toHaveLength(1);
      child.kill('SIGKILL'); expect((await reapChild()).signal).toBe('SIGKILL');
      // Wait on the exact daemon object, then reconcile durable intent. No client
      // remaining alive is necessary for the helper's own timeout/AutoRemove.
      const missingExact = (e: any, target: string) => e.code === 1 && ['container', 'object'].some((kind) =>
        String(e.stderr).trim().toLowerCase() === `error: no such ${kind}: ${target}`.toLowerCase());
      const killedAt = Date.now();
      await docker('wait', id).then((exit) => expect(exit).toBe('137')).catch((e) => { if (!missingExact(e, id)) throw e; });
      expect(Date.now() - killedAt).toBeLessThan(18_000); // watchdog 12s; CLI hard bound remains 45s
      // AutoRemove must finish BEFORE a surviving test process reconciles the
      // journal. Otherwise reapBindHelper could hide a broken daemon watchdog.
      for (const target of [id, name]) {
        const absentBy = Date.now() + 5_000;
        for (;;) {
          try { await docker('inspect', '--type', 'container', target); }
          catch (e) { if (missingExact(e, target)) break; throw e; }
          if (Date.now() >= absentBy) throw new Error('Daemon watchdog did not remove its exact helper');
          await new Promise((r) => setTimeout(r, 100));
        }
      }
      await reapBindHelper(f.root);
      expect(await docker('ps', '-aq', '--filter', 'label=wizard.bind-check')).toBe('');
      await f.service.composeApp(f.urn, 'up --detach');
    } catch (error) { primary = error; throw error; }
    finally {
      if (!closed) child.kill('SIGKILL');
      try {
        const status = await reapChild();
        console.error('Synthetic helper child exit', { ...status, stderr });
      } catch (cleanup) {
        if (primary) throw new AggregateError([primary, cleanup], 'Helper body and child cleanup failed', { cause: primary });
        throw cleanup;
      }
    }
  }, 120_000);

  it('bounds a hung helper and acknowledges cleanup before a subsequent consume', async () => {
    const f = await fixture();
    await expect(runBindHelper(f.root, { image: await bindMetadataImage(), managerUid: 0, source: '/' }, (args, signal) => {
      // Custody/watchdog-only test seam; hostile post-filter tests are separate.
      const at = args.indexOf('/app/libexec/wizard-bind-metadata');
      return helperRun(at < 0 ? args : [...args.slice(0, at), '/bin/sleep', '300'], signal);
    })).rejects.toThrow();
    expect(await docker('ps', '-aq', '--filter', 'label=wizard.bind-check')).toBe('');
    await expect(fs.stat(path.join(f.root, 'user-config/.wizard-bind-check.json'))).rejects.toMatchObject({ code: 'ENOENT' });
    await f.service.composeApp(f.urn, 'up --detach');
    expect(await f.ids()).toHaveLength(1);
  }, 120_000);
});

async function anonymousFixture() {
  const f = await fixture();
  const image = `${f.project}:anonymous`;
  await fs.writeFile(path.join(f.root, 'Anonymous.Dockerfile'), `FROM ${BIND_CHECK_IMAGE}\nVOLUME /anonymous\n`);
  await docker('build', '--network=none', '-f', path.join(f.root, 'Anonymous.Dockerfile'), '-t', image, f.root);
  anonymousImages.add(image);
  expect(JSON.parse(await docker('image', 'inspect', image))[0].Config.Volumes).toHaveProperty('/anonymous');
  f.model.services.web.image = image;
  f.model.services.web.command = ['sh', '-ec', 'printf anonymous-persistent-sentinel > /anonymous/sentinel; while :; do sleep 1; done'];
  await f.write();
  await f.service.composeApp(f.urn, 'up --detach');
  const id = (await f.ids())[0]!;
  const mounts = JSON.parse(await docker('inspect', id))[0].Mounts;
  const anonymous = mounts.find((m: any) => m.Type === 'volume' && m.Destination === '/anonymous')?.Name;
  expect(anonymous).toMatch(/^[a-f0-9]{64}$/);
  anonymousVolumes.add(anonymous);
  expect(await docker('exec', id, 'sh', '-ec', 'n=0; while [ ! -f /anonymous/sentinel ] && [ $n -lt 50 ]; do n=$((n+1)); sleep 0.1; done; cat /anonymous/sentinel')).toBe('anonymous-persistent-sentinel');
  return { ...f, anonymous };
}
