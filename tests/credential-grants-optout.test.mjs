// Dependency-free source contract: execute the exact injector body, not a substitute.
// Full schema/converter/builder/storage/Compose acceptance remains in the forum lane.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { stripTypeScriptTypes } from 'node:module';
const source = await fs.readFile(new URL('../packages/backend/src/modules/docker/wizard-app-grants.ts', import.meta.url), 'utf8');
const start = source.indexOf('export function mountAppGrants('), end = source.indexOf('\ntype AppFile =', start);
assert(start > 0 && end > start);
const body = "import path from 'node:path';\n" + source.slice(start, end);
const { mountAppGrants } = await import('data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(body)).toString('base64'));
const helper = (extra = {}) => ({ image: 'example', user: '0:0', network_mode: 'none', read_only: true,
  cap_drop: ['ALL'], cap_add: ['CHOWN'], volumes: ['${APP_DATA_DIR}/data:/data'], 'x-runtipi': { wizard_grants: false }, ...extra });
const doc = (s) => ({ services: { init: s, engine: { image: 'example' } } });
test('attenuation preserves precise helper fields; sibling retains both mounts/env; input immutable', () => {
  const raw = doc(helper()); const before = structuredClone(raw); const out = mountAppGrants(raw);
  assert.deepEqual(out.services.init, before.services.init); assert.deepEqual(raw, before);
  assert.deepEqual(out.services.engine.volumes, ['${APP_DATA_DIR}/wizard:/wizard:ro', '${APP_DATA_DIR}/wizard-requests:/wizard-requests']);
  assert.deepEqual(out.services.engine.environment, { WIZARD_GRANTS_FILE: '/wizard/grants.json', WIZARD_CONNECTION_REQUESTS: '/wizard-requests/connections.json' });
});
test('omission retains historical root/oneshot behavior, not heuristics', () => {
  const s = helper(); delete s['x-runtipi']; assert.equal(mountAppGrants(doc(s)).services.init.volumes.length, 3);
});
for (const value of [true, 'false', 'none', 0, 1, null, [], {}]) test(`direct wrong type ${JSON.stringify(value)} refuses`, () => {
  assert.throws(() => mountAppGrants(doc(helper({ 'x-runtipi': { wizard_grants: value } }))), /must be false/);
});
for (const [label, extra] of Object.entries({
  'reserved target': { volumes: ['other:/wizard'] }, 'long target': { volumes: [{ source: '/other', target: '/wizard-requests/connections.json' }] },
  'normalized target': { volumes: ['other:/tmp/../wizard/grants.json'] },
  'canonical source alias': { volumes: ['${APP_DATA_DIR}/wizard:/alias:ro'] }, 'request source': { volumes: ['${APP_DATA_DIR}/wizard-requests:/alias'] },
  'app root': { volumes: ['${APP_DATA_DIR}:/alias'] }, 'normalized source': { volumes: ['$APP_DATA_DIR/data/../wizard/grants.json:/alias'] },
  'env map': { environment: { WIZARD_GRANTS_FILE: '/alias' } }, 'env array': { environment: ['WIZARD_CONNECTION_REQUESTS=/alias'] },
  'bare inherited env': { environment: ['WIZARD_GRANTS_FILE'] }, 'env_file': { env_file: [] },
  'extends': { extends: { service: 'engine' } }, 'volumes_from': { volumes_from: ['engine'] },
})) test(`contradictory attenuation refuses: ${label}`, () => assert.throws(() => mountAppGrants(doc(helper(extra))), /opt-out/));
test('ordinary array environment/long data mount remains unchanged', () => {
  const s = helper({ environment: ['KEEP=yes'], volumes: [{ type: 'bind', source: '${APP_DATA_DIR}/data', target: '/data', read_only: true }] });
  assert.deepEqual(mountAppGrants(doc(s)).services.init, s);
});
