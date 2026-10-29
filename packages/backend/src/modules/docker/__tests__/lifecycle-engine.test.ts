import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as yaml from 'yaml';
import { DockerService } from '../docker.service';
import { StopAppCommand } from '@/modules/app-lifecycle/commands/stop-app-command';
import { RestartAppCommand } from '@/modules/app-lifecycle/commands/restart-app-command';
import { UninstallAppCommand } from '@/modules/app-lifecycle/commands/uninstall-app-command';
import { LoggerService } from '@/core/logger/logger.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { AppHelpers } from '@/modules/apps/app.helpers';
import { teardownApp } from '../app-teardown';
import { BIND_CHECK_IMAGE } from '../compose-consumption';
import { runBindHelper, reapBindHelper } from '../bind-helper';

vi.mock('node:fs', async (load) => load<typeof import('node:fs')>());
const exec = promisify(execFile);
const enabled = process.env.WIZARD_LIFECYCLE_ENGINE_PROOF === '1';
const cases: { root: string; urn: string; volume: string }[] = [];
const docker = async (...args: string[]) => (await exec('docker', args, { timeout: 45_000 })).stdout.trim();

afterEach(async () => {
  for (const c of cases.splice(0)) {
    await teardownApp(c.urn, 'down --remove-orphans');
    await docker('volume', 'rm', c.volume).catch((e) => { if (!String(e.stderr).includes('no such volume')) throw e; });
    await reapBindHelper(c.root);
    await fs.rm(c.root, { recursive: true, force: true });
  }
});

async function fixture() {
  if (!(await fs.readFile('/run/wizard-lifecycle-disposable', 'utf8')).startsWith('synthetic')) throw new Error('Disposable VM required');
  const root = await fs.mkdtemp('/opt/lifecycle/case-');
  const id = path.basename(root).toLowerCase();
  const urn = `${id}:wizardapps` as const;
  const project = `${id}_wizardapps`;
  const volume = `${project}_canary`;
  cases.push({ root, urn, volume });
  const appDataRoot = path.join(root, 'app-data');
  const appData = path.join(appDataRoot, 'wizardapps', id);
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
  service.config = { get: () => ({ dataDir: root, appDataDir: appDataRoot }) };
  service.logger = { info: vi.fn() };
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
    await f.service.composeApp(f.urn, 'up --detach');
    const id = (await f.ids())[0]!;
    expect(await docker('exec', id, 'sh', '-ec', 'n=0; while [ ! -f /data/written ] && [ $n -lt 50 ]; do n=$((n+1)); sleep 0.1; done; cat /data/written')).toBe('synthetic');
    const after = await fs.stat(secret);
    expect([after.uid, after.gid, after.mode]).toEqual([before.uid, before.gid, before.mode]);
    expect(await fs.readFile(secret, 'utf8')).toBe('synthetic');
    expect((await fs.stat(path.join(f.appData, 'data/written'))).uid).toBe(12345);
  }, 120_000);

  it('daemon lifetime and journal recovery reap a helper after backend process death', async () => {
    const f = await fixture();
    const code = `import { runBindHelper } from './src/modules/docker/bind-helper.ts'; await runBindHelper(${JSON.stringify(f.root)}, ['--rm', '--network=none', ${JSON.stringify(BIND_CHECK_IMAGE)}, 'timeout', '-s', 'KILL', '12', 'sleep', '300']);`;
    const child = spawn(process.execPath, ['--import', 'tsx', '--input-type=module', '-e', code], { stdio: 'ignore' });
    const exited = new Promise((resolve) => child.once('close', resolve));
    try {
      let id = '';
      for (let i = 0; i < 50 && !id; i++) {
        id = await docker('ps', '-q', '--filter', 'label=wizard.bind-check');
        if (!id) await new Promise((r) => setTimeout(r, 100));
      }
      expect(id).not.toBe('');
      child.kill('SIGKILL'); await exited;
      // Wait on the exact daemon object, then reconcile durable intent. No client
      // remaining alive is necessary for the helper's own timeout/AutoRemove.
      await docker('wait', id).catch((e) => { if (!String(e.stderr).includes('No such container')) throw e; });
      await reapBindHelper(f.root);
      expect(await docker('ps', '-aq', '--filter', 'label=wizard.bind-check')).toBe('');
      await f.service.composeApp(f.urn, 'up --detach');
    } finally { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); await exited; }
  }, 120_000);

  it('bounds a hung helper and acknowledges cleanup before a subsequent consume', async () => {
    const f = await fixture();
    await expect(runBindHelper(f.root, ['--rm', '--network=none', BIND_CHECK_IMAGE, 'timeout', '-s', 'KILL', '12', 'sleep', '300'])).rejects.toThrow();
    expect(await docker('ps', '-aq', '--filter', 'label=wizard.bind-check')).toBe('');
    await expect(fs.stat(path.join(f.root, 'user-config/.wizard-bind-check.json'))).rejects.toMatchObject({ code: 'ENOENT' });
    await f.service.composeApp(f.urn, 'up --detach');
    expect(await f.ids()).toHaveLength(1);
  }, 120_000);
});
