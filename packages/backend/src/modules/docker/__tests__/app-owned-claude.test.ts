import { describe, expect, it } from 'vitest';
import { assertAppOwnedClaudeSource, type App } from '../wizard-storage';

const source = '${APP_DATA_DIR}/data/claude';
const app: App = {
  urn: 'wizard-claude:wizardapps',
  manifest: { id: 'wizard-claude', shares: [{ id: 'claude-login', path: 'claude' }] },
  compose: { services: { engine: { volumes: [`${source}:/claude`] } } },
  data: '/mapped/app-data/wizardapps/wizard-claude',
  hostData: '/srv/runtipi/app-data/wizardapps/wizard-claude',
};
const withMount = (mount: unknown) => ({ services: { engine: { volumes: [mount] } } });

describe('James: login belongs to the connection app, never the host agent', () => {
  it('accepts only this provider app-data in short and long compiled syntax', () => {
    for (const mount of [`${source}:/claude`, `${app.hostData}/data/claude:/claude`, { type: 'bind', source: `${app.hostData}/data/claude`, target: '/claude' }])
      expect(() => assertAppOwnedClaudeSource(app, withMount(mount))).not.toThrow();
  });
  it('blocks an old alongside host override before migration or grant writes', () => {
    for (const mount of ['/srv/owner/.claude:/claude', 'wizard-ai-accounts-claude:/claude', { type: 'bind', source: '/srv/owner/.claude', target: '/claude' }])
      expect(() => assertAppOwnedClaudeSource(app, withMount(mount))).toThrow('Stop the old credential owner');
  });
  it('does not change legacy fixed-volume packages or unrelated app mounts', () => {
    const legacy = { ...app, compose: { services: { engine: { volumes: ['old:/claude'] } } } };
    expect(() => assertAppOwnedClaudeSource(legacy, withMount('old:/claude'))).not.toThrow();
    expect(() => assertAppOwnedClaudeSource({ ...app, manifest: { id: 'other' } }, withMount('/srv/owner:/claude'))).not.toThrow();
  });
  it('rejects a second hidden host override even when the normal app-data mount is present', () => {
    expect(() => assertAppOwnedClaudeSource(app, { services: { engine: { volumes: [`${source}:/claude`, '/srv/owner/.claude:/claude'] } } })).toThrow();
  });
});
