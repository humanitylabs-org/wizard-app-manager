import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { composeEnv } from './wizard-host-guard';

const exec = promisify(execFile);
export type HelperRun = (args: string[], signal: AbortSignal) => Promise<{ stdout: string }>;
export const helperRun: HelperRun = (args, signal) => {
  signal.throwIfAborted();
  return exec('docker', args, {
    env: composeEnv(), signal, timeout: 6_000, killSignal: 'SIGKILL', maxBuffer: 1024 * 1024,
  });
};
const LABEL = 'wizard.bind-check';
const recordPath = (dataDir: string) => path.join(dataDir, 'user-config', '.wizard-bind-check.json');
const missing = (e: any) => /No such (container|object)/i.test(String(e.stderr));

/** Called only under the authority lease, including stop. The intent is synced
 * BEFORE engine creation. A dead backend's successor must reconcile it before
 * any mutation. Daemon failures leave this quarantine in place, never a bypass. */
export async function reapBindHelper(dataDir: string, run = helperRun) {
  const record = recordPath(dataDir);
  let entry: { name: string; nonce: string; phase: 'creating' | 'created' };
  try { entry = JSON.parse(await fs.readFile(record, 'utf8')); }
  catch (e: any) { if (e.code === 'ENOENT') return; throw e; }
  if (!/^wizard-bind-[a-f0-9-]{36}$/.test(entry.name) || entry.name !== `wizard-bind-${entry.nonce}`)
    throw new Error('Bind check quarantine is invalid');
  const signal = AbortSignal.timeout(20_000);
  let info: any;
  try { info = JSON.parse((await run(['inspect', '--type', 'container', entry.name], signal)).stdout)[0]; }
  catch (e) { if (!missing(e)) throw new Error('Bind check cleanup unconfirmed; mutations quarantined', { cause: e }); }
  if (!info && entry.phase !== 'created')
    throw new Error('Bind helper create outcome unknown; mutations quarantined');
  if (info) {
    if (info.Name !== `/${entry.name}` || info.Config?.Labels?.[LABEL] !== entry.nonce)
      throw new Error('Bind check cleanup identity refused; mutations quarantined');
    try { await run(['rm', '--force', '--volumes', info.Id], signal); }
    catch (e) { if (!missing(e)) throw new Error('Bind check cleanup unconfirmed; mutations quarantined', { cause: e }); }
  }
  // A readback by unique name must say absent, not merely return a successful rm.
  try {
    await run(['inspect', '--type', 'container', entry.name], signal);
    throw new Error('Bind check helper still exists; mutations quarantined');
  } catch (e) { if (!missing(e)) throw e; }
  await fs.unlink(record);
}

export async function runBindHelper(dataDir: string, args: string[], run = helperRun, signal?: AbortSignal): Promise<{ stdout: string }> {
  signal?.throwIfAborted();
  await reapBindHelper(dataDir, run);
  signal?.throwIfAborted();
  const nonce = randomUUID();
  const name = `wizard-bind-${nonce}`;
  const record = recordPath(dataDir);
  const fd = await fs.open(record, 'wx', 0o600);
  try { await fd.writeFile(JSON.stringify({ name, nonce, phase: 'creating' })); await fd.sync(); } finally { await fd.close(); }
  const dir = await fs.open(path.dirname(record), 'r');
  try { await dir.sync(); } finally { await dir.close(); }
  try {
    // The helper also has a DAEMON-SIDE lifetime bound. Killing a docker client
    // cannot leave arbitrary path inspection running forever. --rm reaps its exit;
    // the journal covers a crash before start and ambiguous create acknowledgments.
    const created = await run(['create', '--name', name, '--label', `${LABEL}=${nonce}`, ...args], AbortSignal.any([AbortSignal.timeout(8_000), ...(signal ? [signal] : [])]));
    const id = created.stdout.trim();
    if (!/^[a-f0-9]{64}$/.test(id)) throw new Error('Bind helper create identity unavailable');
    const journal = await fs.open(record, 'r+');
    try {
      await journal.truncate(0);
      await journal.writeFile(JSON.stringify({ name, nonce, phase: 'created' }));
      await journal.sync();
    } finally { await journal.close(); }
    return await run(['start', '--attach', id], AbortSignal.any([AbortSignal.timeout(18_000), ...(signal ? [signal] : [])]));
  } finally {
    // Cleanup uses a fresh budget, not the already aborted execution signal.
    await reapBindHelper(dataDir, run);
  }
}
