// Bounded exact-method routing checks with dependency facades; NOT Compose,
// full-module, filesystem-lock or Docker execution. No imports of live services.
import assert from 'node:assert/strict';
import { AsyncLocalStorage, AsyncResource } from 'node:async_hooks';
import fs from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = new URL('../../', import.meta.url);
const source = fs.readFileSync(process.env.MANAGER_SEAL_REMEDIATION_SOURCE ?? fileURLToPath(new URL('packages/backend/src/modules/docker/docker.service.ts', root)), 'utf8');
const sealSource = fs.readFileSync(new URL('packages/backend/src/modules/docker/mandatory-seal.ts', root), 'utf8');
function slice(text, start, end) {
  const a = text.indexOf(start), b = text.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `source extraction ${start}`);
  return text.slice(a, b);
}
const constants = slice(source, 'const composeArgv', '@Injectable()');
const base = slice(source, '  public getBaseComposeArgsApp', '  public async prepareApp');
const compose = slice(source, '  public async composeApp', '  private async sealedLaunch');
const field = source.match(/^  private readonly remediationContext = .*;$/m)?.[0] ?? '';
const exists = slice(sealSource, '  async exists(', '  async read(');
const compiled = stripTypeScriptTypes(`${constants}\nclass Service {\n${field}\n${base}\n${compose}\n}\nclass Presence { ${exists} }\n({Service, Presence})`);

function fixture({ enrolled = true, valid = true } = {}) {
  const calls = [], optionalReads = [], held = new Set();
  const state = { enrolled, valid, snapshots: [], escaped: undefined };
  class MandatorySeal {
    constructor() { this.enrollment = async () => '/public-synthetic-marker'; }
    async read() { if (state.enrolled && !state.valid) throw new Error('invalid policy'); return state.enrolled ? {} : null; }
    async exclusive(urn, fn) { assert.ok(!held.has(urn), 'must not relock enclosing launch'); held.add(urn); try { return await fn(); } finally { held.delete(urn); } }
  }
  const fakeFs = { lstat: async () => { if (!state.enrolled) throw Object.assign(new Error(), { code: 'ENOENT' }); } };
  const { Service, Presence } = vm.runInNewContext(compiled, { AsyncLocalStorage, MandatorySeal, fs: fakeFs, fail: () => new Error('unsafe path') });
  MandatorySeal.prototype.exists = Presence.prototype.exists;
  const manager = new Service();
  manager.config = { get: () => ({ dataDir: '/public-synthetic' }) };
  manager.appsService = { getApp: async () => ({ app: { userConfigEnabled: true } }) };
  manager.appFilesManager = {
    getAppEnv: async () => ({ path: '/generated.env', content: 'PUBLIC=1' }),
    getGeneratedDockerComposeYaml: async () => ({ path: '/generated.json', content: '{}' }),
    getUserEnv: async () => { optionalReads.push('env'); return { path: '/hostile.env', content: 'PUBLIC=2' }; },
    getUserComposeFile: async () => { optionalReads.push('compose'); return { path: '/hostile.yml', content: 'network_mode: host\nprivileged: true' }; },
  };
  manager.prepareLocked = async (urn, seal, apply) => {
    await seal.read(urn);
    const args = state.enrolled ? ['--project-name', urn, '-f', `/validated-${state.snapshots.length}.json`] : undefined;
    state.snapshots.push(args);
    await apply(args);
  };
  manager.runComposeApp = async (urn, argv, args) => {
    const selected = args ?? (await manager.getBaseComposeArgsApp(urn)).args;
    calls.push({ urn, argv: Array.from(argv), args: Array.from(selected) });
    // A routing observable, not a substitute Compose implementation: refusing
    // hostile input makes old down fail before the callback's forced exception.
    if (selected.includes('/hostile.yml') && enrolled) throw new Error('optional input reached remediation');
    return { success: true };
  };
  return { manager, state, calls, held, optionalReads };
}
const urn = 'agent:synthetic';
test('forced rollback: nested down receives exact snapshot; original failure survives; recovery uses fresh snapshot', async () => {
  const f = fixture();
  await assert.rejects(f.manager.composeApp(urn, 'up --detach', async () => {
    await f.manager.composeApp(urn, 'down --remove-orphans');
    throw new Error('synthetic forced rollback');
  }), /synthetic forced rollback/);
  assert.deepEqual(f.calls[0], { urn, argv: ['down', '--remove-orphans'], args: Array.from(f.state.snapshots[0]) });
  assert.equal(f.calls.length, 1, 'failed launch must not run up');
  assert.equal(f.held.size, 0, 'failure releases exclusion');
  assert.deepEqual(f.optionalReads, []);
  await f.manager.composeApp(urn, 'up --detach');
  assert.deepEqual(f.calls[1].args, Array.from(f.state.snapshots[1]));
});
for (const action of ['stop', 'down --remove-orphans', 'kill', 'rm --force']) {
  test(`standalone ${action}: sticky enrollment excludes optional files even with corrupt policy`, async () => {
    const f = fixture({ valid: false });
    await f.manager.composeApp(urn, action);
    assert.deepEqual(f.calls[0].args, ['--env-file', '/generated.env', '--project-name', 'agent_synthetic', '-f', '/generated.json']);
    assert.deepEqual(f.optionalReads, [], 'does not even read hostile optional files');
    await assert.rejects(f.manager.composeApp(urn, 'up --detach'), /invalid policy/);
    assert.equal(f.calls.length, 1, 'corrupt policy cannot activate');
  });
}
test('ordinary app remediation retains optional env/compose inputs', async () => {
  const f = fixture({ enrolled: false });
  await f.manager.composeApp(urn, 'down --remove-orphans');
  assert.ok(f.calls[0].args.includes('/hostile.yml'));
  assert.ok(f.calls[0].args.includes('/hostile.env'));
});
test('callback context is URN-bound, expires after callback and is not shared with concurrent callers', async () => {
  const f = fixture();
  let unblock, entered;
  const wait = new Promise(r => { unblock = r; });
  const ready = new Promise(r => { entered = r; });
  const launch = f.manager.composeApp(urn, 'up --detach', async () => {
    f.state.escaped = AsyncResource.bind(() => f.manager.composeApp(urn, 'down'));
    await f.manager.composeApp('other:synthetic', 'stop');
    entered(); await wait;
    await f.manager.composeApp(urn, 'down');
  });
  await ready;
  await f.manager.composeApp(urn, 'stop');
  unblock(); await launch;
  await f.state.escaped();
  const ordinaryArgs = ['--env-file', '/generated.env', '--project-name', 'agent_synthetic', '-f', '/generated.json'];
  assert.deepEqual(f.calls[0].args, ['--env-file', '/generated.env', '--project-name', 'other_synthetic', '-f', '/generated.json'], 'different URN cannot inherit snapshot');
  assert.deepEqual(f.calls[1].args, ordinaryArgs, 'concurrent independent caller cannot inherit snapshot');
  assert.deepEqual(f.calls[2].args, Array.from(f.state.snapshots[0]), 'nested same-URN down inherits snapshot');
  assert.deepEqual(f.calls[4].args, ordinaryArgs, 'completed callback cannot retain snapshot');
});
test('unknown/activation shortcuts remain refused; pull remains guarded', async () => {
  const f = fixture();
  for (const command of ['restart', 'start', 'create', 'run main', '--profile hostile up', '']) await assert.rejects(f.manager.composeApp(urn, command));
  assert.equal(f.calls.length, 0);
  await f.manager.composeApp(urn, 'pull');
  assert.deepEqual(f.calls[0].args, Array.from(f.state.snapshots[0]));
});
