import path from 'node:path';
import { volumeOf, type GuardContext, HostAccessRefused } from './wizard-host-guard';

// Trusted, digest-pinned helper. No app image/code, no network, no file reads;
// it inspects ONLY each selected bind pathname and its host-side ancestry.
export const BIND_CHECK_IMAGE = 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc';
export const BIND_ANCESTRY_SCRIPT = `set -eu
set -f
manager="$1"; source="$2"; current=/host
check_parent() {
  [ ! -L "$current" ] && [ -d "$current" ] || exit 42
  uid=$(stat -c %u -- "$current")
  mode=$(stat -c %a -- "$current")
  [ "$uid" = 0 ] || [ "$uid" = "$manager" ] || exit 42
  [ "$((0$mode & 022))" = 0 ] || exit 42
}
check_parent
rest="$source"
while [ -n "$rest" ]; do
  rest="\${rest#/}"
  [ -n "$rest" ] || break
  part="\${rest%%/*}"
  if [ "$rest" = "$part" ]; then rest=; else rest="\${rest#*/}"; fi
  current="$current/$part"
  [ ! -L "$current" ] || exit 42
  if [ ! -e "$current" ]; then printf 'missing\\n'; exit 0; fi
  if [ -n "$rest" ]; then check_parent; fi
done
[ -d "$current" ] || [ -f "$current" ] || exit 42
printf 'safe\\n'
`;
export type BindRun = (file: string, args: string[]) => Promise<{ stdout: string }>;
const clean = (p: string) => path.posix.normalize(p).replace(/\/+$/, '') || '/';
const under = (p: string, root: string) => root === '/' || p === root || p.startsWith(`${root}/`);


/** A rootful app can chmod/chown through a writable ancestor bind even when
 * host ownership currently looks protected. Refuse overlapping layouts, including
 * already-running deployments, rather than infer durable authority from UID alone. */
export function assertNoWritableBindAncestors(model: any, existing: string[] = [], existingSources: string[] = []) {
  const mounts = (Object.values(model.services ?? {}) as any[]).flatMap((s) => (s.volumes ?? []).map(volumeOf)).filter(Boolean);
  const candidateWritable = mounts.filter((v) => v!.type === 'bind' && !v!.readOnly).map((v) => clean(v!.source));
  const writable = [...existing.map(clean), ...candidateWritable];
  // A new broad writable parent must not invalidate another running app's
  // checked ancestry either. Exact shared endpoints remain supported.
  for (const current of existingSources.map(clean))
    if (candidateWritable.some((ancestor) => ancestor !== current && under(current, ancestor)))
      throw new HostAccessRefused('bind', ['writable bind ancestor overlap is unsupported']);
  for (const v of mounts) if (v!.type === 'bind') {
    const source = clean(v!.source);
    if (writable.some((ancestor) => ancestor !== source && under(source, ancestor)))
      throw new HostAccessRefused('bind', ['writable bind ancestor overlap is unsupported']);
  }
}

/** Stable path semantics, not check-realpath-then-use. Every HOST ancestor of
 * the bind endpoint must be owned by root/manager and not group/world writable.
 * The endpoint itself can remain app-owned/writable; its protected parent keeps
 * an app from swapping it for a link. Reject unsupported agent-writable ancestry.
 * No ephemeral /proc/fd source: Docker persists sources and reopens on restart.
 */
export async function verifyBindSources(
  model: any,
  ctx: GuardContext,
  managerUid: number,
  run: BindRun,
  create?: (source: string) => Promise<void>,
): Promise<void> {
  assertNoWritableBindAncestors(model);
  for (const service of Object.values(model.services ?? {}) as any[])
    for (const mount of service.volumes ?? []) {
      const v = volumeOf(mount);
      if (v?.type !== 'bind') continue;
      if (
        !mount ||
        typeof mount !== 'object' ||
        !v.source.startsWith('/') ||
        [...v.source].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) === 127)
      )
        throw new HostAccessRefused(ctx.appId, ['unnormalized bind']);
      const source = clean(v.source);
      const check = async () => {
        try {
          const args = [
            '--rm',
            '--restart=no',
            '--stop-timeout=1',
            '--network',
            'none',
            '--read-only',
            '--cap-drop',
            'ALL',
            '--security-opt',
            'no-new-privileges:true',
            '--mount',
            'type=bind,source=/,target=/host,readonly,bind-propagation=rprivate',
            BIND_CHECK_IMAGE,
            'timeout',
            '-s',
            'KILL',
            '12',
            'sh',
            '-c',
            BIND_ANCESTRY_SCRIPT,
            'bind-check',
            String(managerUid),
            source,
          ];
          return (await run('docker', args)).stdout.trim();
        } catch {
          throw new HostAccessRefused(ctx.appId, ['bind source has unsafe or unverifiable host ancestry']);
        }
      };
      let state = await check();
      if (
        state === 'missing' &&
        create &&
        under(source, clean(ctx.appDataDir)) &&
        source !== clean(ctx.appDataDir) &&
        mount.bind?.create_host_path !== false
      ) {
        // The helper proved the nearest existing host ancestor protected. Only our
        // own app's normal missing data directories may be created, never media,
        // shares, source snapshots, or an approved arbitrary host path.
        await create(source);
        state = await check();
      }
      if (state !== 'safe') throw new HostAccessRefused(ctx.appId, ['bind source unavailable or unsafe']);
      mount.bind = { create_host_path: false, propagation: 'rprivate' };
    }
}

/** Compose interpolates again when consuming stdin. Escape values, not map keys,
 * so resolved credentials containing dollar signs remain byte-for-byte literal. */
export function executableModel(v: any): any {
  // A string replacement treats $$ as the special "literal $" token. Use a
  // callback so every input dollar really becomes two for Compose's next pass.
  if (typeof v === 'string') return v.replaceAll('$', () => '$$');
  if (Array.isArray(v)) return v.map(executableModel);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, executableModel(x)]));
  return v;
}
