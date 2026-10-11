import { test } from 'node:test';
import assert from 'node:assert/strict';
import { runtipiDomainUrl } from '../wizard-views/runtipi-open.js';

const row = { info: { id: 'files', no_gui: false }, app: { status: 'running', exposedLocal: true }, metadata: { localSubdomain: 'files-wizardapps' } };
const settings = { localDomain: 'whereismiguel.com', sslPort: 443 };

test('stock local domain is read from Runtipi, no custom route required', () => {
  assert.equal(runtipiDomainUrl(row, settings), 'https://files-wizardapps.whereismiguel.com/');
});
test('stock explicit domain and suffix take precedence', () => {
  assert.equal(runtipiDomainUrl({ ...row, info: { url_suffix: '/ui?x=1' }, app: { ...row.app, exposed: true, domain: 'files.example.org' } }, { ...settings, sslPort: 8443 }), 'https://files.example.org:8443/ui?x=1');
});
test('headless, stopped, restart-pending and unconfigured apps have no domain Open', () => {
  assert.equal(runtipiDomainUrl({ ...row, info: { no_gui: true } }, settings), '');
  assert.equal(runtipiDomainUrl({ ...row, app: { ...row.app, status: 'stopped' } }, settings), '');
  assert.equal(runtipiDomainUrl({ ...row, app: { ...row.app, pendingRestart: true } }, settings), '');
  assert.equal(runtipiDomainUrl({ ...row, app: { status: 'running' } }, settings), '');
  assert.equal(runtipiDomainUrl(row, {}), '');
});
test('malformed domains, ports and suffixes fail closed', () => {
  for (const domain of ['a.example.org/evil', 'user@a.example.org', 'javascript:alert(1)', 'https://a.example.org']) {
    assert.equal(runtipiDomainUrl({ ...row, app: { ...row.app, exposed: true, domain } }, settings), '');
  }
  for (const sslPort of [0, 65536, NaN, 4.5]) assert.equal(runtipiDomainUrl(row, { ...settings, sslPort }), '');
  for (const url_suffix of ['//evil.org', '/\\evil', 'relative', '/\n/evil.org', '/\t/evil.org', 12, {}]) assert.equal(runtipiDomainUrl({ ...row, info: { url_suffix } }, settings), '');
});
