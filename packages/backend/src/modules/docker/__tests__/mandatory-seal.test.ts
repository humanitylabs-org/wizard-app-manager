import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { execFile, spawn } from 'node:child_process';
import { promisify } from 'node:util';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAppUrn } from '@/common/helpers/app-helpers';
import { WizardStorage } from '../wizard-storage';
import * as yaml from 'yaml';
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
  const urn = createAppUrn(main, `seal${path.basename(root).toLowerCase().replaceAll(/[^a-z0-9]/g, '').slice(-10)}`);
  const network = (suffix: string) => `${main}-${suffix}`;
  const qaLabel = { 'wizard.qa.manager-seal': process.env.MANAGER_SEAL_QA_OWNER ?? 'unit', 'wizard.qa.manager-seal.fixture': urn };
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

async function installedFixture(f: Awaited<ReturnType<typeof fixture>>, baseline = f.doc) {
  const store = f.urn.split(':')[1];
  const provider = 'synthetic-provider';
  const recipe = structuredClone(baseline);
  recipe.services[f.main]['x-runtipi'] = { is_main: true };
  const rows = [{ appName: f.main, appStoreSlug: store }, { appName: provider, appStoreSlug: store }];
  for (const [id, manifest, compose] of [
    [f.main, { id: f.main, mounts: [{ app: provider, share: 'synthetic-share', path: '/managed-share' }] }, recipe],
    [provider, { id: provider, shares: [{ id: 'synthetic-share', path: 'shared' }] }, { services: { provider: { image: 'synthetic', user: `${process.getuid?.()}:${process.getgid?.()}`, 'x-runtipi': { is_main: true } } } }],
  ] as const) {
    const dir = path.join(f.root, 'apps', store, id);
    await fs.mkdir(dir, { recursive: true });
    await fs.writeFile(path.join(dir, 'wizard.md'), `---\n${yaml.stringify(manifest)}---\n`);
    await fs.writeFile(path.join(dir, 'docker-compose.yml'), yaml.stringify(compose));
  }
  const source = `${f.root}/${store}/${provider}/data/shared`;
  return { rows, source, grant: { kind: 'share', from: f.main, to: provider, share: 'synthetic-share' } };
}

function seamManager(root: string, doc: any) {
  const generated = path.join(root, 'generated.yml');
  const logger: any = { info() {}, debug() {}, warn() {}, error() {} };
  const files: any = { getGeneratedDockerComposeYaml: async () => ({ path: generated, content: JSON.stringify(doc) }) };
  const manager = new DockerService(logger, { get: () => ({ dataDir: root, appDataDir: root }) } as any, files, {} as any, {} as any,
    { getApps: async () => [] } as any);
  const run = vi.spyOn(manager as any, 'runComposeApp').mockResolvedValue({ success: true, stdout: '', stderr: '' });
  const sealed = vi.spyOn(manager as any, 'sealedLaunch').mockImplementation(async (...args: any[]) => { await args[3](['validated-snapshot']); });
  return { manager, run, sealed };
}

// Fixture-local projection: network actors do not inherit container labels.
function ownedEventTrace(urn: string, project = urn.replace(':', '_')) {
  const events: any[] = [];
  const ownedIds = new Set<string>();
  const earlyConnections = new Map<string, any[]>();
  return { events, accept(event: any) {
    if (event.Type === 'container' && event.Action === 'create' &&
        (event.Actor.Attributes['com.docker.compose.project'] === project || event.Actor.Attributes['wizard.qa.manager-seal.fixture'] === urn)) {
      ownedIds.add(event.Actor.ID); events.push(event);
      events.push(...(earlyConnections.get(event.Actor.ID) ?? [])); earlyConnections.delete(event.Actor.ID);
    } else if (event.Type === 'network' && event.Action === 'connect') {
      const id = event.Actor.Attributes.container;
      if (ownedIds.has(id)) events.push(event);
      else earlyConnections.set(id, [...(earlyConnections.get(id) ?? []), event]);
    }
  } };
}

// Shared by the real fixture and the unlabeled-project-extra regression.
function reconcileEventTrace(events: any[], project: string, services: any, expected: Set<string>, sentinels: string[]) {
  const creates = events.filter((e) => e.Type === 'container' && e.Action === 'create');
  expect(creates.map((e) => e.Actor.ID).sort()).toEqual([...expected, ...sentinels].sort());
  const created = new Map<string, string>();
  for (const e of creates) {
    if (sentinels.includes(e.Actor.ID)) continue;
    expect(e.Actor.Attributes['com.docker.compose.project']).toBe(project);
    const svc = e.Actor.Attributes['com.docker.compose.service'];
    expect(Object.hasOwn(services, svc)).toBe(true); // unknown/removed transient service fails too
    created.set(e.Actor.ID, svc);
  }
  expect([...created.keys()].sort()).toEqual([...expected].sort());
  const connections = events.filter((e) => e.Type === 'network' && e.Action === 'connect');
  expect(connections.length).toBeGreaterThan(0);
  for (const event of connections) {
    const svc = created.get(event.Actor.Attributes.container);
    expect(svc).toBeTruthy(); // sentinels must not attach to any network
    expect(Object.keys(services[svc!].networks).map((n) => `${project}_${n}`)).toContain(event.Actor.Attributes.name);
  }
  for (const [id, svc] of created) {
    const actual = connections.filter((e) => e.Actor.Attributes.container === id).map((e) => e.Actor.Attributes.name);
    expect(actual.sort()).toEqual(Object.keys(services[svc].networks).map((n) => `${project}_${n}`).sort());
  }
}

async function finalEventCutoff(probe: () => Promise<void>, drain: () => Promise<void>, stop: () => Promise<void>, reconcile: () => void) {
  await probe();
  await drain();
  await stop(); // stdout is closed before assertions/evidence use the retained array
  reconcile();
}

describe('reviewed manager admission regressions', () => {
  it('retains even an early attach to an unlabeled public network for an owned identity, excluding unrelated trace rows', () => {
    const trace = ownedEventTrace('synthetic:fixture');
    const attach = { Type: 'network', Action: 'connect', Actor: { ID: 'unlabeled-network', Attributes: { container: 'owned', name: 'public-bridge' } } };
    trace.accept(attach);
    trace.accept({ Type: 'container', Action: 'create', Actor: { ID: 'unrelated', Attributes: {} } });
    trace.accept({ ...attach, Actor: { ID: 'other-network', Attributes: { container: 'unrelated' } } });
    trace.accept({ Type: 'container', Action: 'create', Actor: { ID: 'owned', Attributes: { 'wizard.qa.manager-seal.fixture': 'synthetic:fixture' } } });
    expect(trace.events).toHaveLength(2);
    expect(trace.events).toContainEqual(attach);
  });
  it('R2 retains a project-owned unlabeled transient intruder and its early public connect, then fails exact reconciliation', () => {
    const project = 'synthetic_fixture';
    const trace = ownedEventTrace('synthetic:fixture', project);
    const attach = { Type: 'network', Action: 'connect', Actor: { ID: 'public', Attributes: { container: 'intruder-id', name: 'unlabeled-public' } } };
    trace.accept(attach);
    trace.accept({ Type: 'container', Action: 'create', Actor: { ID: 'intruder-id', Attributes: {
      'com.docker.compose.project': project, 'com.docker.compose.service': 'intruder',
    } } });
    trace.accept({ Type: 'container', Action: 'create', Actor: { ID: 'unrelated', Attributes: { 'com.docker.compose.project': `${project}-other` } } });
    expect(trace.events).toHaveLength(2);
    expect(trace.events).toContainEqual(attach);
    expect(() => reconcileEventTrace(trace.events, project, {}, new Set(), [])).toThrow();
    // Even if an inspection had seen the extra ID, its unknown service fails.
    expect(() => reconcileEventTrace(trace.events, project, {}, new Set(['intruder-id']), [])).toThrow();
  });
  it('R2 drains and awaits shutdown after the final missing-policy probe before reconciling late events', async () => {
    const order: string[] = [];
    const events: string[] = [];
    await finalEventCutoff(async () => { order.push('missing-policy-probe'); events.push('late-create'); },
      async () => { order.push('drain'); }, async () => {
        await Promise.resolve(); events.push('last-stdout'); order.push('closed');
      }, () => { order.push('reconcile'); expect(events).toEqual(['late-create', 'last-stdout']); });
    expect(order).toEqual(['missing-policy-probe', 'drain', 'closed', 'reconcile']);
  });
  it('R1 no-op stage preserves the prior rollback anchor and exact policy bytes', async () => {
    const f = await fixture();
    const v1 = await f.seal.enroll(f.urn, f.doc);
    const policy = path.join(f.root, 'state/mandatory-seals', sealHash(f.urn), 'policy.json');
    const initialBytes = await fs.readFile(policy, 'utf8');
    expect(await f.seal.stage(f.urn, v1.sha256, f.doc, f.doc)).toEqual(v1);
    expect(await fs.readFile(policy, 'utf8')).toBe(initialBytes);
    expect((await f.seal.read(f.urn))?.previous).toBeUndefined();
    const v2 = structuredClone(f.doc); v2.services[f.main].image = 'synthetic-reviewed:v2';
    const staged = await f.seal.stage(f.urn, v1.sha256, v2, v2);
    const before = await fs.readFile(policy, 'utf8');
    const stat = await fs.stat(policy);
    expect(await f.seal.stage(f.urn, staged.sha256, v2, v2)).toEqual(staged);
    expect(await fs.readFile(policy, 'utf8')).toBe(before);
    expect((await fs.stat(policy)).ino).toBe(stat.ino);
    expect((await f.seal.read(f.urn))?.previous?.sha256).toBe(v1.sha256);
  });
  it('R1 v1 stage-v2 recover-v1 stage-v3 refuses without evicting the installed v1 anchor', async () => {
    const f = await fixture();
    const v1 = await f.seal.enroll(f.urn, f.doc);
    const v2 = structuredClone(f.doc); v2.services[f.main].image = 'synthetic-reviewed:v2';
    const staged = await f.seal.stage(f.urn, v1.sha256, v2, v2);
    expect(f.seal.select(staged, f.doc, []).services[f.main].image).toBe(f.doc.services[f.main].image);
    const v3 = structuredClone(v2); v3.services[f.main].image = 'synthetic-reviewed:v3';
    await expect(new MandatorySeal(f.root).stage(f.urn, staged.sha256, v3, v3)).rejects.toThrow();
    expect(await f.seal.read(f.urn)).toEqual(staged);
    expect(f.seal.select((await f.seal.read(f.urn))!, f.doc, []).services[f.main].image).toBe(f.doc.services[f.main].image);
  });
  it('projects installed consumer approval, revoke and reapproval onto the security base under the grant lock', async () => {
    const f = await fixture();
    const installed = await installedFixture(f);
    f.doc['x-wizard-share-mounts'] = ['/managed-share'];
    f.doc.services[f.main].volumes.push(`${installed.source}:/managed-share`);
    const store = new WizardStorage({ dataDir: f.root, appDataDir: f.root }, async () => installed.rows);
    const record = await f.seal.enroll(f.urn, f.doc);
    const project = (doc: any, targets: string[]) => f.seal.select(record, doc, targets);
    const mounts: any[] = [];
    const launch = async (prepared: string) => { mounts.push(yaml.parse(prepared).services[f.main].volumes); };
    await store.change(installed.grant, true);
    await store.prepare(f.urn, JSON.stringify(f.doc), launch, project);
    expect(mounts.at(-1)).toContainEqual(expect.objectContaining({ source: installed.source, target: '/managed-share' }));
    await store.change(installed.grant, false);
    await store.prepare(f.urn, JSON.stringify(f.doc), launch, project);
    expect(mounts.at(-1).some((v: any) => (typeof v === 'string' ? v.split(':')[1] : v.target) === '/managed-share')).toBe(false);
    await store.change(installed.grant, true);
    await store.prepare(f.urn, JSON.stringify(f.doc), launch, project);
    expect(mounts.at(-1)).toContainEqual(expect.objectContaining({ source: installed.source }));
    let revoke!: Promise<void>; let revoked = false;
    await store.prepare(f.urn, JSON.stringify(f.doc), async () => {
      revoke = store.change(installed.grant, false).then(() => { revoked = true; });
      await new Promise((r) => setTimeout(r, 20));
      expect(revoked).toBe(false); // reconciliation/launch exclusion is real
    }, project);
    await revoke;
    expect(revoked).toBe(true);
    expect(() => f.seal.select(record, f.doc, ['/home/synthetic'])).toThrow();
  });
  it('normalizes whitespace once, guards up and refuses prefixed, unknown and shortcut activation', async () => {
    const f = await fixture();
    await f.seal.enroll(f.urn, f.doc);
    const { manager, run, sealed } = seamManager(f.root, f.doc);
    await manager.composeApp(f.urn, ' \tup  --detach\n');
    expect(sealed).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledWith(f.urn, ['up', '--detach'], ['validated-snapshot']);
    for (const command of [' start', ' restart', ' create', ' run', ' watch', '--ansi never up --detach', '--profile any up', 'unknown', '']) {
      await expect(manager.composeApp(f.urn, command)).rejects.toThrow();
    }
    expect(run).toHaveBeenCalledOnce();
  });
  it('shares enrollment exclusion with a separate CLI process; no stale ordinary launch waits through enrollment', async () => {
    const f = await fixture();
    const modulePath = path.resolve('src/modules/docker/mandatory-seal.ts');
    const child = spawn('bun', ['-e', `import { MandatorySeal } from ${JSON.stringify(modulePath)}; await new MandatorySeal(${JSON.stringify(f.root)}).exclusive(${JSON.stringify(f.urn)}, async () => { console.log('LOCKED'); for await (const _ of Bun.stdin.stream()) {} });`]);
    try {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('CLI fence readiness timeout')), 5000);
        child.once('error', (e) => { clearTimeout(timer); reject(e); });
        child.stdout.once('data', (b) => { clearTimeout(timer); String(b).includes('LOCKED') ? resolve() : reject(new Error('CLI fence not ready')); });
        child.once('exit', () => { clearTimeout(timer); reject(new Error('CLI fence exited before readiness')); });
      });
      const { manager, run } = seamManager(f.root, f.doc);
      await expect(manager.composeApp(f.urn, 'up --detach')).rejects.toThrow();
      await expect(f.seal.enroll(f.urn, f.doc)).rejects.toThrow();
      expect(run).not.toHaveBeenCalled();
      const exited = new Promise<void>((resolve) => child.once('exit', () => resolve()));
      child.stdin.end(); await exited;
      await f.seal.enroll(f.urn, f.doc);
      await manager.composeApp(f.urn, 'up --detach');
      expect(run).toHaveBeenCalledWith(f.urn, ['up', '--detach'], ['validated-snapshot']);
    } finally { if (child.exitCode === null) { child.stdin.end(); child.kill(); } }
  });
  it('blocks enrollment during an ordinary launch and rereads after admission, not before storage queueing', async () => {
    const f = await fixture();
    const { manager } = seamManager(f.root, f.doc);
    let entered!: () => void; let release!: () => void;
    const ready = new Promise<void>((r) => { entered = r; });
    const wait = new Promise<void>((r) => { release = r; });
    const launch = manager.composeApp(f.urn, 'up --detach', async () => { entered(); await wait; });
    await ready;
    await expect(f.seal.enroll(f.urn, f.doc)).rejects.toThrow();
    release(); await launch;
    await f.seal.enroll(f.urn, f.doc);
  });
  it('admits a CAS-reviewed runtime revision and prior-policy rollback without changing the privacy envelope', async () => {
    const f = await fixture();
    const old = await f.seal.enroll(f.urn, f.doc);
    const next = structuredClone(f.doc); next.services[f.main].image = 'synthetic-reviewed:v2';
    const staged = await f.seal.stage(f.urn, old.sha256, next, next);
    expect(f.seal.select(staged, next, []).services[f.main].image).toBe('synthetic-reviewed:v2');
    expect(f.seal.select(staged, f.doc, []).services[f.main].image).toBe(f.doc.services[f.main].image);
    expect((await f.seal.read(f.urn))?.previous?.sha256).toBe(old.sha256);
    await expect(f.seal.stage(f.urn, old.sha256, next, next)).rejects.toThrow();
    const mismatched = structuredClone(next); mismatched.services[f.main].image = 'not-the-requested-image';
    await expect(f.seal.stage(f.urn, staged.sha256, mismatched, next)).rejects.toThrow();
    for (const mutate of [
      (d: any) => { d.services[f.main].environment.LITERAL = 'changed'; },
      (d: any) => { d.services[f.main].volumes.push('/other:/other'); },
      (d: any) => { d.services[f.main].networks.public = {}; },
      (d: any) => { d.services[f.main].privileged = true; },
    ]) {
      const changed = structuredClone(next); mutate(changed);
      await expect(f.seal.stage(f.urn, staged.sha256, changed, changed)).rejects.toThrow();
      expect(() => f.seal.select(staged, changed, [])).toThrow();
    }
  });
});

describe('sticky mandatory enrollment', () => {
  it('escapes literal dollars before Compose reconsumes resolved snapshot values', async () => {
    const { seal } = await fixture();
    const snapshot = JSON.parse(seal.snapshot({ services: { main: { environment: { TOKEN: '$LITERAL${also_literal}' } } } }));
    expect(snapshot.services.main.environment.TOKEN).toBe('$$LITERAL$${also_literal}');
  });
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
    const f = await fixture();
    const { root, urn, doc, seal, main, qaLabel } = f;
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
    const installed = await installedFixture(f, baseline);
    const repository: any = { getApps: async () => installed.rows };
    const createManager = () => new DockerService(logger, config, files, {} as any, apps, repository);
    let manager = createManager();
    doc['x-wizard-share-mounts'] = ['/managed-share'];
    doc.services[main].volumes.push(`${installed.source}:/managed-share`);
    let record = await seal.enroll(urn, doc, baseline);
    await manager.storage.change(installed.grant, true);
    let shareApproved = true;
    let expectedImage = doc.services[main].image;
    const expectedContainers = new Set<string>();
    // Docker network-event label filters match the NETWORK, not its container.
    // They would miss a transient attach to an unlabeled public bridge. Consume
    // only create/connect metadata and retain only this fixture's identities;
    // buffer early connects until their container create identifies ownership.
    // No config/env, logs, exec or contents of other containers are accessed.
    const trace = ownedEventTrace(urn);
    const events = trace.events;
    let pending = ''; let traceError: Error | undefined;
    let traceBytes = 0;
    const stream = spawn('docker', ['events', '--since', String(Math.floor(Date.now() / 1000)),
      '--filter', 'type=container', '--filter', 'type=network', '--filter', 'event=create', '--filter', 'event=connect', '--format', '{{json .}}']);
    stream.on('error', (e) => { traceError = e; });
    stream.stdout.on('data', (b) => {
      traceBytes += b.length;
      if (traceBytes > 4 * 1024 * 1024) { traceError = new Error('scoped trace exceeded byte bound'); stream.kill(); return; }
      pending += b.toString();
      const lines = pending.split('\n'); pending = lines.pop()!;
      try {
        for (const line of lines.filter(Boolean)) {
          trace.accept(JSON.parse(line));
        }
      }
      catch { traceError = new Error('invalid Docker event trace'); }
    });
    let streamClosed = false;
    const closed = new Promise<void>((resolve) => stream.once('close', () => { streamClosed = true; resolve(); }));
    const stopStream = async () => {
      if (streamClosed) return;
      stream.kill('SIGTERM');
      const timer = setTimeout(() => {
        traceError ??= new Error('event stream required forced shutdown');
        stream.kill('SIGKILL');
      }, 5000);
      try { await closed; }
      finally { clearTimeout(timer); }
    };
    const bound = setTimeout(() => { traceError = new Error('event trace deadline'); stream.kill(); }, 210_000);
    const sentinelIds: string[] = [];
    const barrier = async () => {
      const id = (await exec('docker', ['create', '--label', `wizard.qa.manager-seal=${qaLabel['wizard.qa.manager-seal']}`,
        '--label', `wizard.qa.manager-seal.fixture=${urn}`, doc.services[main].image])).stdout.trim();
      sentinelIds.push(id);
      const deadline = Date.now() + 10_000;
      while (!events.some((e) => e.Type === 'container' && e.Action === 'create' && e.Actor.ID === id)) {
        if (traceError || streamClosed || Date.now() > deadline) throw traceError ?? new Error('event stream barrier not observed');
        await new Promise((r) => setTimeout(r, 25));
      }
      await exec('docker', ['rm', id]);
    };
    const inspect = async () => {
      const ids = (await exec('docker', ['ps', '-aq', '--no-trunc', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      const rows = await Promise.all(ids.map(async (id) => {
        const { stdout } = await exec('docker', ['inspect', '--format', '{{json .NetworkSettings.Networks}}', id]);
        const { stdout: service } = await exec('docker', ['inspect', '--format', '{{index .Config.Labels "com.docker.compose.service"}}', id]);
        return { id, service: service.trim(), networks: Object.keys(JSON.parse(stdout)) };
      }));
      expect(rows).toHaveLength(Object.keys(doc.services).length);
      for (const row of rows) expectedContainers.add(row.id);
      for (const row of rows) expect(row.networks.sort()).toEqual(Object.keys(doc.services[row.service].networks).map((n) => `${project}_${n}`).sort());
      const client = rows.find((row) => row.service === main)!;
      const mounts = JSON.parse((await exec('docker', ['inspect', '--format', '{{json .Mounts}}', client.id])).stdout);
      expect(mounts.some((m: any) => m.Destination === '/managed-share' && m.Source === installed.source)).toBe(shareApproved);
      expect((await exec('docker', ['inspect', '--format', '{{.Image}}', client.id])).stdout.trim()).toBe(expectedImage);
      // Read only this explicitly synthetic value, never Config.Env or any
      // provider/owner credential. It detects snapshot double interpolation.
      const literalValue = await exec('docker', ['exec', client.id, 'python3', '-c', 'import os; print(os.environ["LITERAL"])']);
      expect(literalValue.stdout.trim()).toBe('$DO_NOT_INTERPOLATE');
    };
    try {
      // Original confirmed bypass: optional config disabled, direct manager up.
      await barrier();
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      await inspect();
      for (const command of [' start', ' restart', ' create', '--ansi never up --detach', 'unknown'])
        await expect(manager.composeApp(urn, command)).rejects.toThrow();
      await manager.storage.change(installed.grant, false); shareApproved = false;
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans'); await inspect();
      await manager.storage.change(installed.grant, true); shareApproved = true;
      await manager.composeApp(urn, ' \tup  --detach --force-recreate --remove-orphans'); await inspect();
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
      // Unsupported requested values refuse, not a silent metadata-only update.
      const changedEnv = structuredClone(baseline);
      changedEnv.services[main].environment = { LITERAL: 'downgraded' };
      await fs.writeFile(generated, JSON.stringify(changedEnv));
      await expect(manager.prepareApp(urn)).rejects.toThrow();
      await fs.writeFile(generated, JSON.stringify(baseline));
      const next = structuredClone(doc); const nextCatalogue = structuredClone(baseline);
      const v2 = process.env.MANAGER_SEAL_TEST_IMAGE_V2!;
      expect(v2).toMatch(/^sha256:/); expect(v2).not.toBe(expectedImage);
      next.services[main].image = v2; nextCatalogue.services[main].image = v2;
      record = await seal.stage(urn, record.sha256, next, nextCatalogue);
      await fs.writeFile(generated, JSON.stringify(nextCatalogue));
      await manager.prepareApp(urn);
      await manager.composeApp(urn, 'up --detach --force-recreate --remove-orphans');
      expectedImage = v2;
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
      expectedImage = doc.services[main].image;
      await inspect();
      expect(await fs.readFile(path.join(root, 'home/history.txt'), 'utf8')).toBe('Synthetic continuity only.\n');
      await finalEventCutoff(async () => {
        // Last negative probe belongs to the same captured/reconciled window.
        await fs.unlink(path.join(root, 'state/mandatory-seals', sealHash(urn), 'policy.json'));
        let stopped = false;
        await expect(manager.composeApp(urn, 'up --detach', async () => { stopped = true; })).rejects.toThrow();
        expect(stopped).toBe(false);
        await inspect();
      }, async () => {
        await barrier();
        expect(traceError).toBeUndefined(); expect(streamClosed).toBe(false);
      }, stopStream, () => {
        expect(traceError).toBeUndefined(); expect(pending).toBe(''); expect(streamClosed).toBe(true);
        reconcileEventTrace(events, project, doc.services, expectedContainers, sentinelIds);
      });
    } finally {
      // Exact synthetic project only. Never use global prune or stop.
      clearTimeout(bound); await stopStream().catch((e) => { traceError ??= e; });
      const traceDir = process.env.MANAGER_SEAL_TRACE_DIR ?? root;
      await fs.mkdir(traceDir, { recursive: true });
      await fs.writeFile(path.join(traceDir, `${project}-events.json`), JSON.stringify({ readiness: 'sentinel-create', error: traceError?.message,
        expectedContainers: [...expectedContainers], events }, null, 2));
      for (const id of sentinelIds) await exec('docker', ['rm', '-f', id]).catch(() => {});
      const ids = (await exec('docker', ['ps', '-aq', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      if (ids.length) await exec('docker', ['rm', '-f', ...ids]);
      const nets = (await exec('docker', ['network', 'ls', '-q', '--filter', `label=com.docker.compose.project=${project}`])).stdout.trim().split(/\s+/).filter(Boolean);
      if (nets.length) await exec('docker', ['network', 'rm', ...nets]);
    }
  }, 180_000);
});
