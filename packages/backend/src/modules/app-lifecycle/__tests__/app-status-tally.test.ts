import { describe, expect, it } from 'vitest';
import { statusFromTally, tallyContainers } from '../app-status-tally';

const app = 'wizard-chatgpt:core';

describe('app status tally', () => {
  it('keeps an app running when its one-shot init job has finished', () => {
    const t = tallyContainers([
      { appUrn: app, state: 'running' },
      { appUrn: app, state: 'running' },
      { appUrn: app, state: 'exited', exitCode: 0, restartPolicy: 'no' },
    ]);
    expect(statusFromTally(t.get(app))).toBe('running');
  });

  it('marks stopped when a restartable service exited, even with code 0', () => {
    const t = tallyContainers([
      { appUrn: app, state: 'running' },
      { appUrn: app, state: 'exited', exitCode: 0, restartPolicy: 'unless-stopped' },
    ]);
    expect(statusFromTally(t.get(app))).toBe('stopped');
  });

  it('marks stopped when a one-shot job failed', () => {
    const t = tallyContainers([
      { appUrn: app, state: 'running' },
      { appUrn: app, state: 'exited', exitCode: 1, restartPolicy: 'no' },
    ]);
    expect(statusFromTally(t.get(app))).toBe('stopped');
  });

  it('marks stopped when only finished jobs remain', () => {
    const t = tallyContainers([{ appUrn: app, state: 'exited', exitCode: 0, restartPolicy: 'no' }]);
    expect(statusFromTally(t.get(app))).toBe('stopped');
  });

  it('marks stopped when the app has no containers', () => {
    expect(statusFromTally(undefined)).toBe('stopped');
  });

  it('counts an uninspected exited container as stopped', () => {
    const t = tallyContainers([
      { appUrn: app, state: 'running' },
      { appUrn: app, state: 'exited' },
    ]);
    expect(statusFromTally(t.get(app))).toBe('stopped');
  });
});
