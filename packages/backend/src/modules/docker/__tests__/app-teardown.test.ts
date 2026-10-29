import { describe, it, expect, vi } from 'vitest';
import { createAppUrn } from '@/common/helpers/app-helpers';
import { appProject, teardownApp } from '../app-teardown';
import { UninstallAppCommand } from '@/modules/app-lifecycle/commands/uninstall-app-command';
import { DockerService } from '../docker.service';
import { AppFilesManager } from '@/modules/apps/app-files-manager';
import { LoggerService } from '@/core/logger/logger.service';

function fixture() {
  let exists = true;
  let running = true;
  const absent = () => Object.assign(new Error('absent'), { statusCode: 404 });
  const labels = { 'com.docker.compose.project': 'demo_wizardapps', 'com.docker.compose.service': 'web', 'runtipi.appurn': 'demo:wizardapps' };
  const inspect = vi.fn(async () => {
    if (!exists) throw absent();
    return { Id: 'pinned-id', Image: 'image-id', Config: { Labels: labels }, State: { Running: running } };
  });
  const stop = vi.fn(async () => { running = false; });
  const remove = vi.fn(async () => { exists = false; });
  const docker: any = {
    listContainers: vi.fn(async () => exists ? [{ Id: 'pinned-id', State: running ? 'running' : 'exited' }] : []),
    getContainer: vi.fn((id) => { expect(id).toBe('pinned-id'); return { inspect, stop, remove }; }),
    listNetworks: vi.fn(async () => []), listVolumes: vi.fn(async () => ({ Volumes: [] })),
  };
  return { docker, labels, inspect, stop, remove };
}

describe('daemon-owned teardown', () => {
  it('uses exact reserved project identity and pins effects by ID without Compose', async () => {
    const f = fixture();
    await teardownApp('demo:wizardapps', 'down --remove-orphans', f.docker);
    expect(f.docker.listContainers).toHaveBeenCalledWith({ all: true, filters: { label: ['com.docker.compose.project=demo_wizardapps'] } });
    expect(f.stop).toHaveBeenCalledWith({ t: 10 });
    expect(f.remove).toHaveBeenCalledWith({ force: false, v: false });
    expect(f.inspect).toHaveBeenCalledTimes(2);
  });
  it('explicit destructive uninstall alone removes anonymous volumes', async () => {
    const f = fixture();
    await teardownApp('demo:wizardapps', 'down --volumes --remove-orphans', f.docker);
    expect(f.remove).toHaveBeenCalledWith({ force: false, v: true });
  });
  it('stop retains the container and acknowledges non-running state', async () => {
    const f = fixture();
    await teardownApp('demo:wizardapps', 'stop', f.docker);
    expect(f.remove).not.toHaveBeenCalled();
    expect(f.inspect).toHaveBeenCalledTimes(2);
  });
  it('refuses mismatched runtime identity before any stop', async () => {
    const f = fixture();
    f.labels['com.docker.compose.project'] = 'other_wizardapps';
    await expect(teardownApp('demo:wizardapps', 'down', f.docker)).rejects.toThrow('ambiguous');
    expect(f.stop).not.toHaveBeenCalled();
  });
  it('does not let mutable nonreserved app labels prevent risk-reducing stop', async () => {
    const f = fixture();
    f.labels['runtipi.appurn'] = 'other:wizardapps';
    await teardownApp('demo:wizardapps', 'down', f.docker);
    expect(f.stop).toHaveBeenCalled();
    expect(f.remove).toHaveBeenCalled();
  });

  it('propagates unacknowledged stop instead of deleting records', async () => {
    const f = fixture();
    f.stop.mockRejectedValue(new Error('daemon unavailable'));
    await expect(teardownApp('demo:wizardapps', 'down', f.docker)).rejects.toThrow('daemon unavailable');
    expect(f.remove).not.toHaveBeenCalled();
  });
  it('uninstall stops before an unreadable recipe inventory and retains control/data on failure', async () => {
    const calls: string[] = [];
    const service = { composeApp: vi.fn(async () => { calls.push('stop'); }), storage: { apps: async () => { calls.push('inventory'); throw new Error('unreadable recipe'); } } };
    const files = { deleteAppFolder: vi.fn(), deleteAppDataDir: vi.fn() };
    const ref: any = { get: (token: any) => new Map<any, any>([[DockerService, service], [AppFilesManager, files], [LoggerService, { info: vi.fn() }]]).get(token) };
    expect(await new UninstallAppCommand(ref, {} as any).execute(createAppUrn('demo', 'wizardapps'))).toMatchObject({ success: false });
    expect(calls).toEqual(['stop', 'inventory']);
    expect(files.deleteAppFolder).not.toHaveBeenCalled();
    expect(files.deleteAppDataDir).not.toHaveBeenCalled();
  });

  it('does not accept raw project or option injection', () => {
    expect(() => appProject('demo:wizardapps --all')).toThrow('Invalid');
  });
});
