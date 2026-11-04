import { promises as fs } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { beforeEach, afterEach, expect, it } from 'vitest';
import { reconcileAppGrants, targets, validateAgentRuntime } from '../wizard-app-grants';
import type { App, Grants } from '../wizard-storage';

let root: string;
beforeEach(async () => { root = await fs.mkdtemp(path.join(process.env.TMPDIR!, 'app-runtime-')); });
afterEach(async () => { await fs.rm(root, { recursive: true, force: true }); });
const runtime = { id: 'pi', label: 'Pi', harness: 'pi', port: 18659, model: { provider: 'wizard-router', model: 'reply' } };
const app = (id: string, manifest: object): App => ({ urn: `${id}:wizardapps`, data: path.join(root, 'app-data/wizardapps', id), hostData: path.join(root, 'app-data/wizardapps', id), manifest: { id, ...manifest }, compose: { services: { [id]: { 'x-runtipi': { is_main: true, internal_port: id === 'wizard-pi-agent' ? 18659 : 8802 } } } } });

it('mints and revokes standard scoped runtime keys without storing one in registry metadata', async () => {
  const service = app('wizard-agent-service', { provides: ['agent-service'], mcp: { port: 8787 } });
  const pi = app('wizard-pi-agent', { type: 'agent', agent_runtime: runtime, uses: ['connection-router'] });
  const router = app('wizard-connection-router', { provides: ['connection-router'], mcp: { port: 8802 } });
  // The runtime endpoint is a standard API, not an MCP declaration.
  const grants: Grants = { schema: 1, updated_at: 0, seeded: [], grants: [], denied: [], mode: 'declared' };
  const save = async () => {};
  await reconcileAppGrants([service, pi, router], grants, root, save);
  const read = async (a: App) => JSON.parse(await fs.readFile(path.join(a.data, 'wizard/grants.json'), 'utf8'));
  const s = await read(service), p = await read(pi);
  const key = s.outgoing['wizard-pi-agent'];
  expect(p.enforce).toBe(true);
  expect(key).toMatch(/^[a-f0-9]{64}$/);
  expect(p.incoming).toContainEqual({ from: 'wizard-agent-service', sha256: createHash('sha256').update(key).digest('hex') });
  expect(s.provider_endpoints['wizard-pi-agent']).toBe('http://wizard-pi-agent:18659');
  expect(p.outgoing['slot:connection-router']).toMatch(/^[a-f0-9]{64}$/);
  expect(JSON.stringify(grants)).not.toContain(key);
  await reconcileAppGrants([service, pi, router], grants, root, save);
  expect((await read(service)).outgoing['wizard-pi-agent']).toBe(key);
  await reconcileAppGrants([service, pi, router], grants, root, save, 'wizard-pi-agent');
  expect((await read(service)).outgoing['wizard-pi-agent']).toBeUndefined();
});

it('rejects duplicate IDs and non-standard/private declarations for any harness', () => {
  const service = app('wizard-agent-service', { provides: ['agent-service'] });
  const pi = app('wizard-pi-agent', { type: 'agent', agent_runtime: runtime });
  expect(targets(service, [service, pi])[0]?.reason).toBe('agent-runtime');
  expect(() => targets(service, [service, pi, app('another-runtime', { type: 'agent', agent_runtime: runtime })])).toThrow('Duplicate');
  for (const change of [{ id: 'main' }, { port: 80 }, { url: 'http://evil' }])
    expect(() => validateAgentRuntime({ id: 'bad', type: 'agent', agent_runtime: { ...runtime, ...change } } as any)).toThrow();
  expect(() => validateAgentRuntime({ id: 'bad', type: 'agent', privacy: 'private', agent_runtime: runtime } as any)).toThrow();
});
