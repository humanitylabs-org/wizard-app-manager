/**
 * Exit promptly when Docker stops the container (wizard-app-manager#738).
 *
 * The backend runs as PID 1. Node itself installs a SIGTERM/SIGINT handler (to restore the terminal) that re-raises
 * the signal with its default action, and the kernel ignores a default-action signal sent to PID 1. So without a
 * handler of our own `docker stop runtipi` did nothing until Docker's grace period ran out and it killed the
 * process: a minute of every WizardOS update and reboot. On the first stop signal we close the app and exit; a
 * watchdog exits anyway if closing hangs, well inside Docker's grace period (which stays as the backstop).
 */
type Signals = { on: (signal: 'SIGTERM' | 'SIGINT', fn: () => void) => unknown; exit: (code?: number) => unknown };

export const STOP_SIGNALS = ['SIGTERM', 'SIGINT'] as const;
export const STOP_WATCHDOG_MS = 4000;

export function exitOnStopSignals(
  close: () => Promise<unknown> | unknown,
  proc: Signals = process,
  watchdogMs = STOP_WATCHDOG_MS,
  timer: (fn: () => void, ms: number) => { unref?: () => unknown } = setTimeout,
) {
  let stopping = false;
  const stop = () => {
    if (stopping) return;
    stopping = true;
    timer(() => proc.exit(0), watchdogMs).unref?.();
    Promise.resolve()
      .then(close)
      .catch((err) => console.error('Error while stopping', err))
      .finally(() => proc.exit(0));
  };
  for (const signal of STOP_SIGNALS) proc.on(signal, stop);
  return stop;
}
