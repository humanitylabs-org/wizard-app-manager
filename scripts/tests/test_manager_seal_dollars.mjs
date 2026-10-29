// Bounded dependency-free method regression, NOT full-module/Compose/Docker proof.
// Execute exact snapshot method + literal/SECTIONS from source; no replacement
// serializer or simulated Compose interpolation. Real Compose lives in Vitest.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const sourcePath = process.env.MANAGER_SEAL_DOLLAR_SOURCE ?? fileURLToPath(new URL('../../packages/backend/src/modules/docker/mandatory-seal.ts', import.meta.url));
const source = fs.readFileSync(sourcePath, 'utf8');
const sections = source.match(/^const SECTIONS = (.*) as const;$/m);
const literal = source.match(/^const literal = (.*);$/m);
const snapshot = source.match(/  snapshot\(effective: Compose\) \{([\s\S]*?)\n  \}\n\}\s*$/);
assert.ok(sections && literal && snapshot, 'exact source extraction contract');
const seal = vm.runInNewContext(`const SECTIONS = ${sections[1]}; const literal = ${literal[1].replace('(v: unknown)', '(v)')}; new (class { snapshot(effective) {${snapshot[1]}\n} })()`);
const values = ['$$DO_NOT_INTERPOLATE', '$${DO_NOT_INTERPOLATE}', '$$$$', '$$', '$$$$DO_NOT_INTERPOLATE', 'quote"\\unicode-λ$$'];
for (const value of values) {
  test(`rendered dollar bytes stay exact: ${JSON.stringify(value)}`, () => {
    const rendered = { services: { main: { environment: { LITERAL: value }, command: [value], labels: { synthetic: value } } } };
    const before = JSON.stringify(rendered);
    const output = JSON.parse(seal.snapshot(rendered));
    assert.deepEqual(output, rendered);
    assert.equal(JSON.stringify(rendered), before, 'does not mutate validated input');
    assert.equal(seal.snapshot(output), seal.snapshot(rendered), 'repeated snapshot is stable');
  });
}
test('only launch sections survive and every selected section retains exact bytes', () => {
  const sections = ['services', 'networks', 'volumes', 'configs', 'secrets'];
  const rendered = Object.fromEntries(sections.map((key) => [key, { synthetic: { value: '$$PUBLIC' } }]));
  assert.deepEqual(JSON.parse(seal.snapshot({ ...rendered, name: 'discard', 'x-synthetic': '$$PUBLIC' })), rendered);
});
