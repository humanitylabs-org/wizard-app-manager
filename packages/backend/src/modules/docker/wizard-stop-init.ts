import * as yaml from 'yaml';

/**
 * Python servers that run as a container's first process ignore `docker stop` (wizard-app-manager#738).
 *
 * The kernel ignores SIGTERM sent to PID 1 unless the process handles it, and a Python server with no handler does
 * not. So every Wizard MCP front (`python -c "..."`) and similar Python server waited out Docker's 10 s stop timeout
 * and was killed, on every app restart, update, reboot and WizardOS update. Docker's own init (tini, `init: true`)
 * runs as PID 1 instead and passes the signal on, so Python stops at once with its default action.
 *
 * Only services that start Python directly (exec-form or plain `command` whose program is python/python3/pythonX.Y)
 * and set neither `init` nor `entrypoint` get it: an image's own init system is never wrapped, and an app (or an
 * owner's user-config override, merged after this file) that sets `init` keeps its choice.
 */
const PYTHON = /^(?:.*\/)?python(?:\d+(?:\.\d+)*)?$/;

type Service = { command?: unknown; entrypoint?: unknown; init?: unknown };

function program(command: unknown): string | undefined {
  if (Array.isArray(command)) return typeof command[0] === 'string' ? command[0] : undefined;
  if (typeof command === 'string') return command.trim().split(/\s+/)[0];
  return undefined;
}

export function startsPythonAsPid1(service: Service): boolean {
  if (!service || typeof service !== 'object') return false;
  if (service.init !== undefined || service.entrypoint !== undefined) return false;
  const prog = program(service.command);
  return prog !== undefined && PYTHON.test(prog);
}

/** The generated compose with `init: true` on each service that starts Python as PID 1; unchanged text otherwise. */
export function addStopInit(generated: string): string {
  const doc = yaml.parseDocument(generated);
  const services = doc.get('services');
  if (!yaml.isMap(services)) return generated;
  let changed = false;
  for (const item of services.items) {
    const svc = item.value;
    if (!yaml.isMap(svc)) continue;
    if (!startsPythonAsPid1(svc.toJSON() as Service)) continue;
    svc.set('init', true);
    changed = true;
  }
  return changed ? doc.toString() : generated;
}
