// Tiny exact-source method regressions using dependency facades, not installed
// Nest/HTTP/DB/queue acceptance. The Vitest companion uses the real DTO schema.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { stripTypeScriptTypes } from 'node:module';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const root = process.env.MANAGER_SEAL_SAFETY_ROOT ?? fileURLToPath(new URL('../../', import.meta.url));
const read = rel => fs.readFileSync(path.join(root, 'packages/backend/src', rel), 'utf8');
function between(text, start, end) {
  const a = text.indexOf(start), b = text.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, `method extraction failed: ${start}`);
  return text.slice(a, b);
}
const bootstrap = between(read('app.service.ts'), '  public async bootstrap()', '  public async getVersion()');
const handler = read('modules/app-lifecycle/handlers/install-app.handler.ts');
const execute = handler.slice(handler.indexOf('  async execute('), handler.lastIndexOf('\n}'));
assert.ok(execute.startsWith('  async execute('));
const compile = (method, globals) => vm.runInNewContext(stripTypeScriptTypes(`(class Subject { ${method} })`), globals);

test('bootstrap: no daemon-wide mutation; migration, assets and TLS still complete', async () => {
  const calls = [];
  const Subject = compile(bootstrap, {
    ONE_DAY_IN_SECONDS: 86400, process: { env: { TIPI_VERSION: 'synthetic' } },
    Sentry: { captureException: e => { throw e; } },
  });
  const subject = new Subject();
  // Do not throw here: product bootstrap catches errors. Assert both authority
  // AND completion so an early catch cannot make this negative spuriously green.
  subject.docker = { pruneNetworks: async () => calls.push('UNSCOPED_PRUNE') };
  subject.databaseService = { migrate: async () => calls.push('migrate') };
  subject.configuration = { getConfig: () => ({ version: 'synthetic', userSettings: {}, __prod__: true }), initSentry: () => {} };
  subject.logger = { info() {}, debug() {}, flush: async () => {} , error: e => { throw e; } };
  subject.cache = { get: () => 'synthetic' };
  subject.appStoreService = { migrateLegacyRepo: async () => calls.push('legacy') };
  subject.repoQueue = { publish: () => calls.push('clone'), publishRepeatable: () => calls.push('repo-repeat') };
  subject.marketplaceService = { initialize: async () => calls.push('marketplace') };
  subject.systemEventsQueue = { publishRepeatable: () => calls.push('system-repeat') };
  subject.copyAssets = async () => calls.push('assets');
  subject.generateTlsCertificates = async () => calls.push('tls');
  subject.appLifecycleService = { restartRunningApps: () => calls.push('restart') };
  await subject.bootstrap();
  assert.ok(!calls.includes('UNSCOPED_PRUNE'), 'bootstrap invoked daemon-wide network prune');
  assert.deepEqual(calls, ['migrate', 'legacy', 'clone', 'marketplace', 'repo-repeat', 'system-repeat', 'assets', 'tls']);
});

async function install(form, override) {
  const published = [], success = [], writes = [];
  let finish;
  const complete = new Promise(resolve => { finish = resolve; });
  class ValidationErrors {}
  const Subject = compile(execute, {
    // A facade, deliberately NOT a claim about ArkType or HTTP validation.
    appFormSchema: input => ({ skipRun: false, openPort: true, ...input }),
    type: { errors: ValidationErrors }, HttpStatus: { BAD_REQUEST: 400, NOT_FOUND: 404 },
    TranslatableError: Error, extractAppUrn: () => ({ appName: 'fixture', appStoreId: 'synthetic' }),
    generateRequestId: () => 'synthetic-request',
  });
  const subject = new Subject();
  subject.appRepository = { getAppByUrn: async () => null, createApp: async input => { writes.push(input); return { id: 7 }; } };
  subject.statusManager = { emitEvent() {}, emitSuccess: async input => { success.push(input); finish(); } };
  subject.marketplaceService = { getAppInfoFromAppStoreOrInstalled: async () => ({ port: 10240, tipi_version: 1 }) };
  subject.validationService = { validateInstallSettings: async () => {} };
  subject.appEventsQueue = { publish: input => { published.push(input); return Promise.resolve({ success: true }); } };
  subject.startAppHandler = { execute: () => { throw new Error('new install directly started'); } };
  const result = await subject.execute('fixture:synthetic', { form, skipRun: override });
  await complete;
  assert.equal(result.requestId, 'synthetic-request');
  assert.equal(writes.length, 1);
  assert.equal(published.length, 1);
  assert.equal(success.length, 1);
  return { event: published[0], terminal: success[0] };
}
for (const [name, form, override, expected] of [
  ['HTTP form true survives omitted override', { skipRun: true, skipPull: true, openPort: false }, undefined, true],
  ['HTTP form false remains false', { skipRun: false }, undefined, false],
  ['ordinary omission defaults false', {}, undefined, false],
  ['trusted false overrides form true', { skipRun: true }, false, false],
  ['trusted true overrides form false', { skipRun: false }, true, true],
  ['trusted true overrides omission', {}, true, true],
]) {
  test(`install: ${name}; queue and final status agree`, async () => {
    const { event, terminal } = await install(form, override);
    assert.equal(event.form.skipRun, expected, 'validated form skipRun lost before queue');
    assert.equal(terminal.status, expected ? 'stopped' : 'running', 'terminal status disagrees with effective skipRun');
    if (form.skipPull !== undefined) assert.equal(event.form.skipPull, form.skipPull);
    if (form.openPort !== undefined) assert.equal(event.form.openPort, form.openPort);
  });
}
