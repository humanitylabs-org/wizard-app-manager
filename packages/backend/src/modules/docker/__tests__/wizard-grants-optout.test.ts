import { describe, expect, it } from 'vitest';
import { type } from 'arktype';
import * as yaml from 'yaml';
import { dynamicComposeSchemaYaml, convertLegacyToYaml, convertYamlToLegacy } from '@runtipi/common/schemas';
import { DockerComposeBuilder } from '../builders/compose.builder';
import { mountAppGrants } from '../wizard-app-grants';

const helper = (extra: any = {}) => ({ image: 'example', user: '0:0', network_mode: 'none', cap_drop: ['ALL'], cap_add: ['CHOWN'],
  read_only: true, security_opt: ['no-new-privileges:true'], volumes: ['${APP_DATA_DIR}/data:/data'], environment: { KEEP: 'yes' },
  'x-runtipi': { wizard_grants: false }, ...extra });
const compose = (service: any) => ({ services: { init: service, engine: { image: 'example', user: '1000:1000' } }, 'x-runtipi': { schema_version: 2 } });
const render = (doc: any, arch = 'amd64', store = 'wizardapps') => yaml.parse(new DockerComposeBuilder().getDockerCompose(doc, {}, `demo:${store}` as any, '10.128.1.0/24', arch));

describe('explicit per-service connection attenuation', () => {
  it('keeps opted-out service byte-equivalent through the injector, input immutable; legacy sibling gets both mounts and env', () => {
    const doc = compose(helper()); const before = structuredClone(doc); const result = mountAppGrants(doc);
    expect(result.services.init).toEqual(before.services.init); expect(doc).toEqual(before);
    expect(result.services.engine.volumes).toEqual(['${APP_DATA_DIR}/wizard:/wizard:ro', '${APP_DATA_DIR}/wizard-requests:/wizard-requests']);
    expect(result.services.engine.environment).toEqual({ WIZARD_GRANTS_FILE: '/wizard/grants.json', WIZARD_CONNECTION_REQUESTS: '/wizard-requests/connections.json' });
  });
  it('does not infer an opt-out from root, oneshot, CHOWN, network-none or readonly', () => {
    const old = helper(); delete old['x-runtipi'];
    expect(mountAppGrants(compose(old)).services.init.volumes).toHaveLength(3);
  });
  it('retains all non-connection builder output and strips only build metadata, with no declaration leaking to Compose', () => {
    const raw = compose(helper()); const legacy = structuredClone(raw); delete legacy.services.init['x-runtipi'];
    const result = render(raw), old = render(legacy);
    expect(result.services.init['x-runtipi']).toBeUndefined();
    const expected = structuredClone(old.services.init);
    expected.volumes = expected.volumes.filter((v: string) => !v.includes(':/wizard'));
    delete expected.environment.WIZARD_GRANTS_FILE; delete expected.environment.WIZARD_CONNECTION_REQUESTS;
    expect(result.services.init).toEqual(expected); expect(result.services.engine).toEqual(old.services.engine);
    expect(result.networks).toEqual(old.networks); expect(result['x-wizard-volumes']).toEqual(old['x-wizard-volumes']);
  });
  it('honors amd64/arm64 overrides after merging without changing sibling defaults', () => {
    const doc: any = compose({ image: 'example' });
    doc['x-runtipi'].overrides = [{ architecture: 'amd64', services: { init: helper() } }];
    expect(render(doc).services.init.volumes).toEqual(['${APP_DATA_DIR}/data:/data']);
    expect(render(doc, 'arm64').services.init.volumes).toHaveLength(2);
  });
  it('leaves non-Wizard store output free of grants injection, including root services', () => {
    expect(render(compose(helper()), 'amd64', 'other').services.init.volumes).toEqual(['${APP_DATA_DIR}/data:/data']);
  });
  it('roundtrips false through YAML/legacy JSON and architecture overrides', () => {
    const doc: any = compose(helper());
    doc['x-runtipi'].overrides = [{ architecture: 'arm64', services: { init: { image: 'other', 'x-runtipi': { wizard_grants: false } } } }];
    const legacy: any = convertYamlToLegacy(doc);
    expect(legacy.services.find((s: any) => s.name === 'init').wizardGrants).toBe(false);
    expect(legacy.overrides[0].services[0].wizardGrants).toBe(false);
    const roundtrip: any = convertLegacyToYaml(legacy);
    expect(roundtrip.services.init['x-runtipi'].wizard_grants).toBe(false);
    expect(roundtrip['x-runtipi'].overrides[0].services.init['x-runtipi'].wizard_grants).toBe(false);
    const v1: any = convertLegacyToYaml({ services: [{ name: 'init', image: 'example', wizardGrants: false }] });
    expect(v1.services.init['x-runtipi'].wizard_grants).toBe(false);
  });
  for (const value of [true, 'false', 'none', 0, 1, null, [], {}]) {
    it(`rejects invalid declaration ${JSON.stringify(value)} in YAML, JSON, overrides and direct builder bypass`, () => {
      const doc: any = compose(helper({ 'x-runtipi': { wizard_grants: value } }));
      expect(dynamicComposeSchemaYaml(doc)).toBeInstanceOf(type.errors);
      expect(() => convertLegacyToYaml({ schemaVersion: 2, services: [{ name: 'init', image: 'example', wizardGrants: value }] })).toThrow();
      expect(() => convertLegacyToYaml({ services: [{ name: 'init', image: 'example', wizardGrants: value }] })).toThrow();
      expect(() => render(doc)).toThrow(/wizard_grants/);
      doc.services.init = { image: 'example' };
      doc['x-runtipi'].overrides = [{ architecture: 'amd64', services: { init: helper({ 'x-runtipi': { wizard_grants: value } }) } }];
      expect(dynamicComposeSchemaYaml(doc)).toBeInstanceOf(type.errors); expect(() => render(doc)).toThrow(/wizard_grants/);
    });
  }
  for (const [label, extra] of Object.entries({
    'reserved target': { volumes: ['other:/wizard'] }, 'long target': { volumes: [{ type: 'bind', source: '/other', target: '/wizard-requests/connections.json' }] },
    'normalized target': { volumes: ['other:/tmp/../wizard/grants.json'] },
    'canonical grants source': { volumes: ['${APP_DATA_DIR}/wizard:/alias:ro'] },
    'request source': { volumes: ['${APP_DATA_DIR}/wizard-requests:/alias'] }, 'app root source': { volumes: ['${APP_DATA_DIR}:/alias'] },
    'normalized source': { volumes: ['$APP_DATA_DIR/data/../wizard/grants.json:/alias'] },
    'env map': { environment: { WIZARD_GRANTS_FILE: '/alias' } }, 'env array': { environment: ['KEEP=yes', 'WIZARD_CONNECTION_REQUESTS=/alias'] },
    'bare inherited env': { environment: ['WIZARD_GRANTS_FILE'] }, 'env_file': { env_file: [] },
    'extends': { extends: { service: 'engine' } }, 'volumes_from': { volumes_from: ['engine'] },
  })) {
    it(`fails closed on opt-out contradiction: ${label}`, () => {
      expect(() => mountAppGrants(compose(helper(extra)))).toThrow(/opt-out/);
      expect(() => render(compose(helper(extra)))).toThrow(/opt-out/);
    });
  }
  it('keeps array environment, ordinary data, network topology and unrelated explicit capabilities', () => {
    const s = helper({ environment: ['KEEP=yes'], volumes: [{ type: 'bind', source: '${APP_DATA_DIR}/data', target: '/data', read_only: true }], networks: { mine: {} } });
    expect(mountAppGrants(compose(s)).services.init).toEqual(s);
  });
});
