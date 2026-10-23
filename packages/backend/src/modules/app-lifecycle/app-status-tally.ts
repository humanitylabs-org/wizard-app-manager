import type { AppStatus } from '@/core/database/drizzle/types';

export type ContainerFacts = {
  appUrn: string;
  state: string;
  exitCode?: number | null;
  restartPolicy?: string | null;
};

export type StatusTally = { running: number; total: number; completedJobs: number };

/**
 * A one-shot job (compose `restart: "no"`) that exited 0 has finished its work,
 * e.g. a data-init sidecar that fixes folder ownership. It is neither running
 * nor broken, so it must not make a healthy app look stopped.
 */
export const isCompletedOneShot = (c: ContainerFacts) =>
  c.state === 'exited' && c.exitCode === 0 && (!c.restartPolicy || c.restartPolicy === 'no');

export function tallyContainers(containers: ContainerFacts[]) {
  const map = new Map<string, StatusTally>();
  for (const c of containers) {
    const tally = map.get(c.appUrn) ?? { running: 0, total: 0, completedJobs: 0 };
    map.set(c.appUrn, tally);
    if (isCompletedOneShot(c)) {
      tally.completedJobs++;
      continue;
    }
    tally.total++;
    if (c.state === 'running') tally.running++;
  }
  return map;
}

/** Running only when every long-lived container runs; an app of finished jobs alone is stopped. */
export const statusFromTally = (tally: StatusTally | undefined): AppStatus =>
  tally && tally.total > 0 && tally.running === tally.total ? 'running' : 'stopped';
