import { describe, expect, test } from 'bun:test';
import { prepareMainPrivateData } from '../packages/backend/src/modules/docker/wizard-private-data';

const fixture = (user = '1000:1000', volume: any = '${APP_DATA_DIR}/data:/data') => ({
  data: '/app-data/wizardapps/wizard-recorder',
  compose: { services: { recorder: { user, volumes: [volume], 'x-runtipi': { is_main: true } } } },
});
const stat = (uid = 0, gid = 0, symlink = false) => ({ uid, gid, isDirectory: () => !symlink, isSymbolicLink: () => symlink });

function harness(owner = stat()) {
  const calls: any[] = [];
  return {
    calls,
    confined: async (...args: any[]) => { calls.push(['confined', ...args]); return '/confined/data'; },
    io: { lstat: async () => owner, lchown: async (...args: any[]) => { calls.push(['chown', ...args]); } } as any,
  };
}

describe('factory Recorder private data ownership', () => {
  test('creates and gives only the data root to declared non-root user before start', async () => {
    const h = harness(); await prepareMainPrivateData(fixture(), h.confined, h.io);
    expect(h.calls).toEqual([
      ['confined', '/app-data/wizardapps/wizard-recorder', 'data', true],
      ['chown', '/confined/data', 1000, 1000],
    ]);
  });
  test('idempotent for existing app-owned recordings; never recursively chowns or chmods', async () => {
    const h = harness(stat(1000, 1000)); await prepareMainPrivateData(fixture(), h.confined, h.io);
    expect(h.calls).toHaveLength(1);
  });
  test('refuses existing other owner or symlink; chown errors block start', async () => {
    for (const s of [stat(1234, 1234), stat(0, 0, true)]) {
      const h = harness(s); await expect(prepareMainPrivateData(fixture(), h.confined, h.io)).rejects.toThrow();
      expect(h.calls.filter((c) => c[0] === 'chown')).toHaveLength(0);
    }
    const h = harness(); h.io.lchown = async () => { throw new Error('denied'); };
    await expect(prepareMainPrivateData(fixture(), h.confined, h.io)).rejects.toThrow('denied');
  });
  test('uses existing confinement helper; refuses path validation errors', async () => {
    const h = harness(); await expect(prepareMainPrivateData(fixture(), async () => { throw new Error('symlink'); }, h.io)).rejects.toThrow('symlink');
    expect(h.calls).toHaveLength(0);
  });
  test('long syntax uses same rule; single uid supplies its gid', async () => {
    const h = harness(); await prepareMainPrivateData(fixture('1001', { type: 'bind', source: '${APP_DATA_DIR}/data', target: '/data' }), h.confined, h.io);
    expect(h.calls[1]).toEqual(['chown', '/confined/data', 1001, 1001]);
  });
  test('does not touch root/named users, readonly, nested or other-app mounts', async () => {
    for (const app of [fixture('0:0'), fixture('node'), fixture('1000:1000', '${APP_DATA_DIR}/data:/data:ro'), fixture('1000:1000', '${APP_DATA_DIR}/data/private:/data'), fixture('1000:1000', '/another-app/data:/data')]) {
      const h = harness(); await prepareMainPrivateData(app, h.confined, h.io); expect(h.calls).toHaveLength(0);
    }
  });
  test('does not take over a root used by another service', async () => {
    const app: any = fixture(); app.compose.services.other = { user: '1001:1001', volumes: ['${APP_DATA_DIR}/data:/state'] };
    const h = harness(); await prepareMainPrivateData(app, h.confined, h.io); expect(h.calls).toHaveLength(0);
  });
});
