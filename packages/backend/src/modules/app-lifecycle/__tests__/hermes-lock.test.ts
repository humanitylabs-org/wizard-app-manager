import { expect, test } from 'vitest';
import { assertHermesUpdate } from '../hermes-lock';

const approved = () => ({
  schema: 1,
  automatic_updates: 'paused',
  minimum_release_age_days: 7,
  state: 'approved',
  promotion: {
    id: 'fixture',
    published_at: '2026-10-01T00:00:00Z',
    approved_at: '2026-10-08T00:00:00Z',
    recommendation: 'https://example.invalid/recommendation',
    approval: 'https://example.invalid/approval',
    acceptance: 'https://example.invalid/acceptance',
    canary: 'https://example.invalid/canary',
    bug_review: 'https://example.invalid/bugs',
    rollback: 'https://example.invalid/rollback',
  },
});
const now = Date.parse('2026-10-25T00:00:00Z');

test('reviewed policy and matching execution id are both necessary', () => {
  expect(() => assertHermesUpdate('hermes-agent:wizardapps', 'fixture')).toThrow('paused');
  expect(() => assertHermesUpdate('hermes-agent:wizardapps', undefined, now, approved())).toThrow('paused');
  expect(() => assertHermesUpdate('hermes-agent:wizardapps', 'other', now, approved())).toThrow('paused');
  expect(() => assertHermesUpdate('hermes-agent:wizardapps', 'fixture', now, approved())).not.toThrow();
  expect(() => assertHermesUpdate('files:wizardapps')).not.toThrow();
});

test('seven full publication days, nonfuture approval and evidence remain mandatory', () => {
  for (const approvedAt of ['2026-10-07T23:59:59Z', '2026-11-01T00:00:00Z', 'invalid']) {
    const doc = approved();
    doc.promotion.approved_at = approvedAt;
    expect(() => assertHermesUpdate('hermes-agent:wizardapps', 'fixture', now, doc)).toThrow('paused');
  }
  const doc = approved();
  doc.promotion.canary = '';
  expect(() => assertHermesUpdate('hermes-agent:wizardapps', 'fixture', now, doc)).toThrow('paused');
});
