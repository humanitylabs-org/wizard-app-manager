import { describe, it, expect, vi } from 'vitest';
import { appProject, teardownApp } from '../app-teardown';

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
    expect(f.remove).toHaveBeenCalledWith({ force: false, v: true });
    expect(f.inspect).toHaveBeenCalledTimes(2);
  });
  it('stop retains the container and acknowledges non-running state', async () => {
    const f = fixture();
    await teardownApp('demo:wizardapps', 'stop', f.docker);
    expect(f.remove).not.toHaveBeenCalled();
    expect(f.inspect).toHaveBeenCalledTimes(2);
  });
  it('refuses mismatched runtime identity before any stop', async () => {
    const f = fixture();
    f.labels['runtipi.appurn'] = 'other:wizardapps';
    await expect(teardownApp('demo:wizardapps', 'down', f.docker)).rejects.toThrow('ambiguous');
    expect(f.stop).not.toHaveBeenCalled();
  });
  it('propagates unacknowledged stop instead of deleting records', async () => {
    const f = fixture();
    f.stop.mockRejectedValue(new Error('daemon unavailable'));
    await expect(teardownApp('demo:wizardapps', 'down', f.docker)).rejects.toThrow('daemon unavailable');
    expect(f.remove).not.toHaveBeenCalled();
  });
  it('does not accept raw project or option injection', () => {
    expect(() => appProject('demo:wizardapps --all')).toThrow('Invalid');
  });
});
