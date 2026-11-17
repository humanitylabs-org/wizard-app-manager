import { describe, expect, it } from 'vitest';
import { exitOnStopSignals, STOP_SIGNALS } from '../stop-signals';

// wizard-app-manager#738: runtipi (PID 1) never exited on `docker stop`, so every shutdown waited a minute.
function fakeProcess() {
  const handlers: Record<string, (() => void)[]> = {};
  const exits: number[] = [];
  const timers: { fn: () => void; ms: number; unref: boolean }[] = [];
  return {
    handlers,
    exits,
    timers,
    proc: {
      on: (signal: string, fn: () => void) => {
        if (!handlers[signal]) handlers[signal] = [];
        handlers[signal].push(fn);
      },
      exit: (code?: number) => {
        exits.push(code ?? 0);
      },
    },
    timer: (fn: () => void, ms: number) => {
      const t = { fn, ms, unref: false };
      timers.push(t);
      return {
        unref: () => {
          t.unref = true;
        },
      };
    },
  };
}

const settle = () => new Promise((r) => setTimeout(r, 0));

describe('runtipi exits on the stop signal (#738)', () => {
  it('listens for SIGTERM and SIGINT', () => {
    const f = fakeProcess();
    exitOnStopSignals(() => undefined, f.proc, 4000, f.timer);
    expect(Object.keys(f.handlers).sort()).toEqual([...STOP_SIGNALS].sort());
  });

  it('closes the app once, then exits 0', async () => {
    const f = fakeProcess();
    let closed = 0;
    exitOnStopSignals(
      async () => {
        closed++;
      },
      f.proc,
      4000,
      f.timer,
    );
    f.handlers.SIGTERM?.[0]?.();
    f.handlers.SIGINT?.[0]?.();
    f.handlers.SIGTERM?.[0]?.();
    await settle();
    expect(closed).toBe(1);
    expect(f.exits).toEqual([0]);
  });

  it('exits even when closing fails', async () => {
    const f = fakeProcess();
    exitOnStopSignals(() => Promise.reject(new Error('queue gone')), f.proc, 4000, f.timer);
    f.handlers.SIGTERM?.[0]?.();
    await settle();
    expect(f.exits).toEqual([0]);
  });

  it('a watchdog exits when closing hangs, without keeping the process alive itself', async () => {
    const f = fakeProcess();
    exitOnStopSignals(() => new Promise(() => {}), f.proc, 4000, f.timer);
    f.handlers.SIGTERM?.[0]?.();
    await settle();
    expect(f.exits).toEqual([]);
    expect(f.timers).toHaveLength(1);
    expect(f.timers[0]?.ms).toBe(4000);
    expect(f.timers[0]?.unref).toBe(true);
    f.timers[0]?.fn();
    expect(f.exits).toEqual([0]);
  });

  it('a stop before the app exists still exits', async () => {
    const f = fakeProcess();
    const app: { close: () => Promise<void> } | undefined = undefined;
    exitOnStopSignals(() => (app as { close: () => Promise<void> } | undefined)?.close(), f.proc, 4000, f.timer);
    f.handlers.SIGTERM?.[0]?.();
    await settle();
    expect(f.exits).toEqual([0]);
  });
});
