import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
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
const fullId = (id: unknown): id is string => typeof id === 'string' && /^[a-f0-9]{64}$/.test(id);
const missing = (e: any, target: string) => e.code === 1 && ['container', 'object'].some((kind) =>
  String(e.stderr).trim().toLowerCase() === `error: no such ${kind}: ${target}`.toLowerCase());
export const BIND_METADATA_PATH = '/app/libexec/wizard-bind-metadata';
// BusyBox timeout execs its target. A non-tail-exec shell must remain PID1 so
// timeout's same-namespace watcher can KILL the target (never namespace init).
// No selected value enters shell text; the fixed helper/UID/path are positional.
export const BIND_METADATA_SUPERVISOR = '/bin/busybox timeout -s KILL 12 "$@"; rc=$?; exit "$rc"';
export type BindMetadataRequest = { image: string; managerUid: number; source: string };

/** Only the attested running manager chooses the artifact. Never Config.Image,
 * an environment override, app image, mutable tag or separately published image. */
export async function bindMetadataImage(run = helperRun, signal = AbortSignal.timeout(8_000)): Promise<string> {
  const hostname = os.hostname();
  if (!/^[a-f0-9]{12}(?:[a-f0-9]{52})?$/.test(hostname)) throw new Error('Cannot attest backend identity');
  const self = JSON.parse((await run(['inspect', '--type', 'container', hostname], signal)).stdout)[0];
  if (!fullId(self?.Id) || !self.Id.startsWith(hostname) || self.Config?.Hostname !== hostname || !self.State?.Running ||
      !/^sha256:[a-f0-9]{64}$/.test(self.Image)) throw new Error('Cannot attest backend image');
  const image = JSON.parse((await run(['image', 'inspect', self.Image], signal)).stdout)[0];
  if (image?.Id !== self.Image || image.Config?.Labels?.['org.wizard.bind-metadata'] !== BIND_METADATA_PATH)
    throw new Error('Backend image lacks packaged metadata helper');
  // The image's fixed packaging label is bound to inspect.Image; also refuse a
  // missing/replaced/shadowed helper in this running manager. The child has no
  // manager source overlays: Docker recreates the fixed path from that image.
  if ((self.Mounts ?? []).some((m: any) => typeof m.Destination !== 'string' ||
      m.Destination === '/' || BIND_METADATA_PATH === m.Destination || BIND_METADATA_PATH.startsWith(`${m.Destination}/`)))
    throw new Error('Backend metadata helper path is shadowed');
  for (const parent of ['/app', '/app/libexec']) {
    const st = await fs.lstat(parent);
    if (!st.isDirectory() || st.uid !== 0 || (st.mode & 0o022)) throw new Error('Backend metadata helper ancestry is unsafe');
  }
  const st = await fs.lstat(BIND_METADATA_PATH);
  if (!st.isFile() || st.uid !== 0 || (st.mode & 0o7777) !== 0o555)
    throw new Error('Backend metadata helper is unavailable');
  return self.Image;
}

export function bindMetadataArgs(request: BindMetadataRequest): string[] {
  const { image, managerUid, source } = request;
  if (!/^sha256:[a-f0-9]{64}$/.test(image) || !Number.isInteger(managerUid) || managerUid < 0 || managerUid >= 0xffffffff ||
      typeof source !== 'string' || Buffer.byteLength(source) > 4089 || !source.startsWith('/') ||
      [...source].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127) || path.posix.normalize(source) !== source || (source !== '/' && source.endsWith('/')))
    throw new Error('Invalid bind metadata request');
  // No explicit propagation on this root inspection mount: Docker 20.10 uses
  // automatic rslave, or its private-source fallback. App mounts stay rprivate.
  return ['--rm', '--restart=no', '--stop-timeout=1', '--network=none', '--read-only',
    '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--user=0:0',
    '--entrypoint=/bin/busybox', '--mount', 'type=bind,source=/,target=/host,readonly',
    image, 'sh', '-c', BIND_METADATA_SUPERVISOR, 'wizard-bind-metadata-supervisor', BIND_METADATA_PATH, String(managerUid), source];
}

/** Called only under the authority lease, including stop. The intent is synced
 * BEFORE engine creation. A dead backend's successor must reconcile it before
 * any mutation. Daemon failures leave this quarantine in place, never a bypass. */
export async function reapBindHelper(dataDir: string, run = helperRun) {
  const record = recordPath(dataDir);
  let entry: { name: string; nonce: string; phase: 'creating' | 'created'; id?: string };
  try { entry = JSON.parse(await fs.readFile(record, 'utf8')); }
  catch (e: any) { if (e.code === 'ENOENT') return; throw e; }
  if (!/^wizard-bind-[a-f0-9-]{36}$/.test(entry.name) || entry.name !== `wizard-bind-${entry.nonce}` ||
      !['creating', 'created'].includes(entry.phase) || (entry.phase === 'created' && !fullId(entry.id)))
    throw new Error('Bind check quarantine is invalid');
  const signal = AbortSignal.timeout(20_000);
  let info: any;
  try { info = JSON.parse((await run(['inspect', '--type', 'container', entry.name], signal)).stdout)[0]; }
  catch (e) { if (!missing(e, entry.name)) throw new Error('Bind check cleanup unconfirmed; mutations quarantined', { cause: e }); }
  if (!info && entry.phase !== 'created')
    throw new Error('Bind helper create outcome unknown; mutations quarantined');
  if (info) {
    if (!fullId(info.Id) || (entry.id && info.Id !== entry.id) || info.Name !== `/${entry.name}` || info.Config?.Labels?.[LABEL] !== entry.nonce)
      throw new Error('Bind check cleanup identity refused; mutations quarantined');
    try { await run(['rm', '--force', '--volumes', info.Id], signal); }
    catch (e) { if (!missing(e, info.Id)) throw new Error('Bind check cleanup unconfirmed; mutations quarantined', { cause: e }); }
  }
  // A readback by unique name must say absent, not merely return a successful rm.
  try {
    await run(['inspect', '--type', 'container', entry.name], signal);
    throw new Error('Bind check helper still exists; mutations quarantined');
  } catch (e) { if (!missing(e, entry.name)) throw e; }
  await fs.unlink(record);
}

export async function runBindHelper(dataDir: string, request: BindMetadataRequest, run = helperRun, signal?: AbortSignal): Promise<{ stdout: string }> {
  const args = bindMetadataArgs(request);
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
  let outcome: { result: { stdout: string } } | { primary: Error };
  let phase: 'create' | 'journal' | 'start' = 'create';
  try {
    // The helper also has a DAEMON-SIDE lifetime bound. Killing a docker client
    // cannot leave arbitrary path inspection running forever. --rm reaps its exit;
    // the journal covers a crash before start and ambiguous create acknowledgments.
    const created = await run(['create', '--name', name, '--label', `${LABEL}=${nonce}`, ...args], AbortSignal.any([AbortSignal.timeout(8_000), ...(signal ? [signal] : [])]));
    const id = created.stdout.trim();
    if (!/^[a-f0-9]{64}$/.test(id)) throw new Error('Bind helper create identity unavailable');
    const identity = JSON.parse((await run(['inspect', '--type', 'container', id], AbortSignal.any([AbortSignal.timeout(6_000), ...(signal ? [signal] : [])]))).stdout)[0];
    if (identity?.Id !== id || identity.Name !== `/${name}` || identity.Config?.Labels?.[LABEL] !== nonce)
      throw new Error('Bind helper create identity mismatch');
    phase = 'journal';
    const journal = await fs.open(record, 'r+');
    try {
      await journal.truncate(0);
      await journal.writeFile(JSON.stringify({ name, nonce, phase: 'created', id }));
      await journal.sync();
    } finally { await journal.close(); }
    phase = 'start';
    outcome = { result: await run(['start', '--attach', id], AbortSignal.any([AbortSignal.timeout(18_000), ...(signal ? [signal] : [])])) };
  } catch (error: any) {
    // Capture bounded safe phase/exit metadata BEFORE cleanup can also fail.
    // Never retain arbitrary child output/argv/path in the reported diagnostic.
    const exit = Number.isInteger(error?.code) ? String(error.code).slice(0, 12) : 'unknown';
    outcome = { primary: new Error(`Bind metadata execution failed (phase=${phase}, exit=${exit})`) };
  }
  // Both outcomes flow through cleanup with a fresh budget. Propagate only
  // afterwards: no finally throw can overwrite a return or the primary error.
  try { await reapBindHelper(dataDir, run); }
  catch (cleanup) {
    if ('primary' in outcome) throw new AggregateError([outcome.primary, cleanup], 'Bind metadata execution and cleanup failed; mutations quarantined', { cause: outcome.primary });
    throw cleanup;
  }
  if ('primary' in outcome) throw outcome.primary;
  return outcome.result;
}
