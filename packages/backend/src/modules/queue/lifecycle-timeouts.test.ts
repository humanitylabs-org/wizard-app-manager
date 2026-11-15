import { describe, expect, it } from 'vitest';
import { LONG_APP_COMMANDS, longCommandMinutes, transitionalGraceSeconds } from './lifecycle-timeouts';

describe('lifecycle timeouts (WAM#711)', () => {
  it('gives install, update, backup and restore at least 30 minutes', () => {
    expect([...LONG_APP_COMMANDS].sort()).toEqual(['backup', 'install', 'restore', 'update']);
    expect(longCommandMinutes(5)).toBe(30);
    expect(longCommandMinutes(60)).toBe(60);
  });

  it('lets the status sync leave a long-running update alone past the 5-minute queue timeout', () => {
    expect(transitionalGraceSeconds('updating', 5)).toBe(30 * 60);
    expect(transitionalGraceSeconds('installing', 5)).toBe(30 * 60);
    expect(transitionalGraceSeconds('backing_up', 5)).toBe(30 * 60);
    expect(transitionalGraceSeconds('restoring', 5)).toBe(30 * 60);
    expect(transitionalGraceSeconds('starting', 5)).toBe(5 * 60);
    expect(transitionalGraceSeconds('stopping', 5)).toBe(5 * 60);
  });
});
