import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { assertFinalHostPolicy, composeEnv, finalHostTokens, HostAccessRefused, injectedBinds, readHostApprovals } from '../wizard-host-guard';

// Shared with wam/final_host_tokens.py (tests/test_final_host_gate.py): both sides must agree token for token.
const fixture = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../../../../tests/fixtures/final-host-tokens.json'), 'utf8'));
const c = fixture.context;
const ctx = {
  appId: c.app_id,
  store: c.store,
  appDataDir: c.app_data_dir,
  filesRoot: c.files_root,
  sourceRoot: c.source_root,
  envDirs: c.env_dirs,
  appDataRoot: c.app_data_root,
};

describe('final host gate', () => {
  for (const vector of fixture.cases) {
    it(`tokens: ${vector.name}`, () => {
      expect(finalHostTokens(vector.config, ctx)).toEqual([...vector.tokens].sort());
    });
  }

  it('refuses unapproved host access and accepts exactly the recorded tokens', () => {
    const risky = fixture.cases.find((v: any) => v.name === 'privileged host namespaces');
    expect(() => assertFinalHostPolicy(risky.config, ctx, {})).toThrow(HostAccessRefused);
    expect(() => assertFinalHostPolicy(risky.config, ctx, { 'wizardapps/demo': risky.tokens.slice(1) })).toThrow(HostAccessRefused);
    expect(() => assertFinalHostPolicy(risky.config, ctx, { 'wizardapps/other': risky.tokens })).toThrow(HostAccessRefused);
    expect(() => assertFinalHostPolicy(risky.config, ctx, { 'wizardapps/demo': risky.tokens })).not.toThrow();
    const benign = fixture.cases.find((v: any) => v.name === 'benign app');
    expect(() => assertFinalHostPolicy(benign.config, ctx, {})).not.toThrow();
  });

  it('missing or malformed approvals accept nothing', async () => {
    expect(await readHostApprovals('/nonexistent-wizard-test')).toEqual({});
  });

  it('only manager-injected share binds inside app data are exempt', () => {
    const generated = { services: { web: { volumes: ['/srv/rt/app-data/wizardapps/files/data/share:/share', '/etc:/share2'] } } };
    const injected = injectedBinds(generated, { web: { volumes: [] } });
    expect(finalHostTokens(generated, { ...ctx, injected })).toEqual(['web|bind:/etc:/share2:rw']);
    expect(finalHostTokens(generated, { ...ctx, appDataRoot: undefined, injected })).toHaveLength(2);
  });

  it('docker-compose never inherits manager credentials', () => {
    const env = composeEnv({ PATH: '/bin', POSTGRES_PASSWORD: 'sentinel', JWT_SECRET: 'sentinel', RABBITMQ_PASSWORD: 'sentinel' });
    expect(env).toEqual({ PATH: '/bin' });
  });
});
