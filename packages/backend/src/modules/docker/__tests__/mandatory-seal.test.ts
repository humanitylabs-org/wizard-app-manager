import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { afterEach, describe, expect, it } from 'vitest';
import { MandatorySeal, sealHash, validateSeal } from '../mandatory-seal';
import { DockerService } from '../docker.service';

const exec = promisify(execFile);
const LABEL = 'wizard.private-network-policy';
const POLICY = 'tinfoil-sealed-v1';
const roots: string[] = [];
async function fixture() {
  const root = await fs.mkdtemp(path.join(process.env.TMPDIR ?? os.tmpdir(), 'mandatory-seal-'));
  roots.push(root);
  const main = 'agent-synthetic';
  const urn = `${main}:seal${path.basename(root).toLowerCase().replaceAll(/[^a-z0-9]/g, '').slice(-10)}` as const;
  const network = (suffix: string) => `${main}-${suffix}`;
  const qaLabel = { 'wizard.qa.manager-seal': process.env.MANAGER_SEAL_QA_OWNER ?? 'unit' };
  const service = (nets: string[]) => ({ image: process.env.MANAGER_SEAL_TEST_IMAGE ?? 'python:3.12-slim',
    entrypoint: ['python3', '-c'], command: ['import time; time.sleep(3600)'], restart: 'no',
    networks: Object.fromEntries(nets.map((s) => [network(s), {}])),
    cap_drop: ['ALL'], security_opt: ['no-new-privileges:true'],
    labels: { [LABEL]: POLICY, ...qaLabel } });
  const doc: any = { services: {
    [main]: { ...service(['seal']), environment: { PUBLIC_KEY: 'wizard-sealed-provider-key-held-by-injector', LITERAL: '$DO_NOT_INTERPOLATE' },
      volumes: [`${root}/home:/home/synthetic`] },
    [`${main}-tinfoil`]: service(['provider', 'keyed']),
    [`${main}-search`]: service(['provider', 'keyed']),
    [`${main}-key`]: { ...service(['seal', 'keyed']), volumes: [`${root}/key:/run/provider-key/key:ro`] },
    [`${main}-edge`]: service(['seal', 'provider', 'edge-uplink']),
  }, networks: Object.fromEntries(['seal', 'provider', 'keyed'].map((s) => [network(s), {
    internal: true, enable_ipv6: false, driver_opts: { 'com.docker.network.bridge.inhibit_ipv4': 'true' }, labels: { [LABEL]: POLICY, ...qaLabel },
  }])) };
  doc.networks[network('edge-uplink')] = { internal: false, enable_ipv6: false, labels: qaLabel };
  await fs.mkdir(path.join(root, 'home'));
  await fs.writeFile(path.join(root, 'home/history.txt'), 'Synthetic continuity only.\n');
  await fs.writeFile(path.join(root, 'key'), 'synthetic-not-a-provider-credential\n', { mode: 0o600 });
  const seal = new MandatorySeal(root);
  return { root, main, urn, doc, seal, qaLabel };
}
afterEach(async () => { for (const root of roots.splice(0)) await fs.rm(root, { recursive: true, force: true }); });

describe('sticky mandatory enrollment', () => {
  it('is independent of optional configuration and survives a new manager instance', async () => {
    const { root, urn, doc, seal } = await fixture();
    expect(await seal.read(urn)).toBeNull();
    const record = await seal.enroll(urn, doc);
    await fs.mkdir(path.join(root, 'user-config'), { recursive: true });
    await fs.rm(path.join(root, 'user-config'), { recursive: true });
    expect(await new MandatorySeal(root).read(urn)).toEqual(record);
    await expect(seal.enroll(urn, { ...doc, services: {} })).rejects.toThrow();
    const changed = structuredClone(doc);
    changed.services['agent-synthetic'].command = ['unreviewed-command'];
    await expect(seal.enroll(urn, changed)).rejects.toThrow();
    expect(await seal.read(urn)).toEqual(record);
  });
  it('a missing or corrupt required policy is not an unenrolled ordinary app', async () => {
    const { root, urn, doc, seal } = await fixture();
    await seal.enroll(urn, doc);
    const file = path.join(root, 'state/mandatory-seals', sealHash(urn), 'policy.json');
    await fs.unlink(file);
    await expect(seal.read(urn)).rejects.toThrow();
    await expect(seal.enroll(urn, doc)).rejects.toThrow();
    await fs.writeFile(file, '{}', { mode: 0o600 });
    await expect(seal.read(urn)).rejects.toThrow();
  });
  it('rejects external/unsealed networks and network namespace or privilege escapes', async () => {
    const { urn, doc, main } = await fixture();
    for (const mutate of [
      (d: any) => { d.services[main].network_mode = 'host'; },
      (d: any) => { d.services[main].privileged = true; },
      (d: any) => { d.services[main].networks.public = {}; },
      (d: any) => { d.networks[`${main}-seal`].internal = false; },
      (d: any) => { d.networks[`${main}-seal`].external = true; },
      (d: any) => { d.services[main].cap_add = ['NET_ADMIN']; },
    ]) {
      const copy = structuredClone(doc); mutate(copy);
      expect(() => validateSeal(copy, urn)).toThrow();
    }
  });
});

// This is actual DockerService.composeApp and actual Compose+Docker, not a
// renderer-only/driver refusal test. The synthetic repository facades stand in
// for DB rows; WizardStorage.prepare and the shared launch seam are real.
// Full authenticated disposable API acceptance is a separate, still-required gate.
const dockerDescribe = process.env.RUN_MANAGER_SEAL_DOCKER === '1' ? describe : describe.skip;
dockerDescribe('mandatory seal direct manager lifecycle (real Docker)', () => {
  it('disabled, removed and malicious optional config cannot attach a transient public network', async () => {
    const { root, urn, doc, seal, main, qaLabel } = await fixture();
    const project = urn.replace(':', '_');
    const generated = path.join(root, 'generated.yml');
    const user = path.join(root, 'optional.yml');
    const env = path.join(root, 'app.env');
    const disabled = { value: true };
    const baseline = structuredClone(doc);
    // Reproduce the generator's ordinary per-app bridge on every service and
    // its shared/public main bridge on the client, using owned synthetic names.
    baseline.networks.public_app = { labels: qaLabel };
    baseline.networks.public_main = { labels: qaLabel };
    for (const service of Object.values(baseline.services) as any[]) service.networks.public_app = {};
    baseline.services[main].networks.public_main = {};
    await fs.writeFile(generated, JSON.stringify(baseline));
    await fs.writeFile(env, 'DO_NOT_INTERPOLATE=untrusted\n');
    await fs.writeFile(user, 'services: {}\n');
    const files: any = {
      getAppEnv: async () => ({ path: env, content: '' }),
      getUserEnv: async () => ({ path: env, content: 'untrusted' }),
      getGeneratedDockerComposeYaml: async () => ({ path: generated, content: await fs.readFile(generated, 'utf8') }),
      getUserComposeFile: async () => ({ path: user, content: await fs.readFile(user, 'utf8').catch(() => null) }),
    };
    const logger: any = { info() {}, debug() {}, warn() {}, error() {} };
    const config: any = { get: () => ({ dataDir: root, appDataDir: root }) };
    const apps: any = { getApp: async () => ({ app: { userConfigEnabled: !disabled.value } }) };
    const repository: any = { getApps: async () => [] };
    const createManager = () => new DockerService(logger, config, files, {} as any, apps, repository);
    let manager = createManager();
    const record = await seal.enroll(urn, doc);
    const since = Math.floor(Date.now() / 1000);
    const inspect = async () => {
      const ids = (await exec('docker', ['ps', '-aq', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      const rows = await Promise.all(ids.map(async (id) => {
        const { stdout } = await exec('docker', ['inspect', '--format', '{{json .NetworkSettings.Networks}}', id]);
        const { stdout: service } = await exec('docker', ['inspect', '--format', '{{index .Config.Labels "com.docker.compose.service"}}', id]);
        return { id, service: service.trim(), networks: Object.keys(JSON.parse(stdout)) };
      }));
      expect(rows).toHaveLength(Object.keys(doc.services).length);
      for (const row of rows) expect(row.networks.sort()).toEqual(Object.keys(doc.services[row.service].networks).map((n) => `${project}_${n}`).sort());
    };
    try {
      // Original confirmed bypass: optional config disabled, direct manager up.
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      const changedRuntime = structuredClone(baseline);
      changedRuntime.services[main].image = 'synthetic-unapproved-image:must-not-pull';
      await fs.writeFile(generated, JSON.stringify(changedRuntime));
      let runtimeStopped = false;
      await expect(manager.composeApp(urn, 'up --detach', async () => { runtimeStopped = true; })).rejects.toThrow();
      expect(runtimeStopped).toBe(false);
      await fs.writeFile(generated, JSON.stringify(baseline));
      await fs.unlink(user);
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      disabled.value = false;
      await fs.writeFile(user, JSON.stringify({ services: { [main]: { network_mode: 'host', privileged: true }, intruder: { image: doc.services[main].image } }, networks: { public: {} } }));
      manager = createManager(); // daemon object restart re-reads persistent seal
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      // Replacing the generated catalogue recipe cannot erase enrollment.
      baseline.services[main].environment = { LITERAL: 'downgraded' };
      await fs.writeFile(generated, JSON.stringify(baseline));
      await manager.prepareApp(urn);
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      expect(await seal.read(urn)).toEqual(record);
      // Forced failure after an update/restart down; recovery goes back through
      // the same guard, never an unsealed fallback launch.
      await expect(manager.composeApp(urn, 'up --detach', async () => {
        await manager.composeApp(urn, 'down --remove-orphans');
        throw new Error('synthetic forced rollback');
      })).rejects.toThrow('synthetic forced rollback');
      await fs.writeFile(generated, JSON.stringify(baseline));
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      expect(await fs.readFile(path.join(root, 'home/history.txt'), 'utf8')).toBe('Synthetic continuity only.\n');
      // Historical events, including containers removed by force-recreate:
      // final-state-only inspection is explicitly insufficient.
      const { stdout: history } = await exec('docker', ['events', '--since', String(since), '--until', String(Math.ceil(Date.now() / 1000)), '--filter', `label=com.docker.compose.project=${project}`, '--format', '{{json .}}']);
      const events = history.trim().split('\n').filter(Boolean).map((s) => JSON.parse(s));
      const created = new Map(events.filter((e) => e.Type === 'container' && e.Action === 'create').map((e) => [e.Actor.ID, e.Actor.Attributes['com.docker.compose.service']]));
      const connections = events.filter((e) => e.Type === 'network' && e.Action === 'connect');
      expect(connections.length).toBeGreaterThan(0);
      for (const event of connections) {
        const svc = created.get(event.Actor.Attributes.container) as string;
        expect(svc).toBeTruthy();
        expect(Object.keys(doc.services[svc].networks).map((n) => `${project}_${n}`)).toContain(event.Actor.Attributes.name);
      }
      // Policy corruption refuses before a restart's down callback or any up.
      await fs.unlink(path.join(root, 'state/mandatory-seals', sealHash(urn), 'policy.json'));
      let stopped = false;
      await expect(manager.composeApp(urn, 'up --detach', async () => { stopped = true; })).rejects.toThrow();
      expect(stopped).toBe(false);
      await inspect();
    } finally {
      // Exact synthetic project only. Never use global prune or stop.
      const ids = (await exec('docker', ['ps', '-aq', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      if (ids.length) await exec('docker', ['rm', '-f', ...ids]);
      const nets = (await exec('docker', ['network', 'ls', '-q', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      if (nets.length) await exec('docker', ['network', 'rm', ...nets]);
    }
  }, 180_000);
});
