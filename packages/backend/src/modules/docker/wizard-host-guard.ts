/** Final host-access gate at the privileged Compose consumer (spec/PRIVILEGE.md, Decision #105).
 *
 * The Python App Manager checks recipes and overrides on its own lifecycle paths. The backend API can also be
 * called directly (user-config overrides, restart, install), so the facade alone is not a boundary. Immediately
 * before Docker can create or change a container, this module reads Compose's OWN merged and interpolated model
 * (`docker-compose ... config --format json`, every --file and --env-file the real command uses) and refuses any
 * host-reaching setting the App Manager did not accept for exactly this app.
 *
 * Accepted settings come only from `user-config/wizard-host-approvals.json`, written by the App Manager service
 * account (wam/final_host_tokens.py) for risk sets that are intrinsically safe (manager-managed mounts) or covered by
 * an owner approval. A missing or unreadable file accepts nothing. Token rules are shared with the Python writer
 * through test vectors (tests/fixtures/final-host-tokens.json); unknown Compose keys fail closed.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { constants } from 'node:fs';

export const APPROVALS_FILE = 'wizard-host-approvals.json';
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const OURS = /^(?:wizard-(?:app|link|gw)-[a-z0-9-]+|wizard-core)$/;
const SAFE_SECURITY = new Set(['no-new-privileges:true', 'no-new-privileges=true', 'no-new-privileges']);
const DEFAULT_CAPS = new Set(
  'AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD NET_BIND_SERVICE NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_CHROOT'.split(' '),
);
const TOP_KEYS = new Set(['name', 'version', 'services', 'volumes', 'networks', 'secrets', 'configs']);
// Inert or container-scoped keys, including the ones Compose adds while normalizing. A Compose feature not listed
// here (for example use_api_socket, provider, volumes_from, gpus) is refused until reviewed.
const SERVICE_KEYS = new Set(
  (
    'image build command entrypoint user working_dir hostname domainname restart depends_on environment ports expose ' +
    'volumes networks network_mode pid ipc userns_mode uts privileged devices device_cgroup_rules cap_add cap_drop ' +
    'security_opt sysctls env_file secrets configs read_only init tmpfs shm_size pids_limit mem_limit memswap_limit ' +
    'mem_reservation cpus cpu_count cpu_percent cpu_period cpu_quota cpu_shares cpuset healthcheck labels logging ' +
    'stop_grace_period stop_signal stdin_open tty ulimits deploy platform pull_policy profiles group_add dns dns_search ' +
    'dns_opt extra_hosts isolation runtime cgroup cgroup_parent oom_kill_disable oom_score_adj storage_opt ' +
    'container_name scale attach annotations mac_address entrypoint post_start pre_stop'
  ).split(' '),
);
const FLAGGED_SERVICE_KEYS = ['devices', 'device_cgroup_rules', 'sysctls', 'runtime', 'cgroup_parent', 'storage_opt', 'isolation'];
const BUILD_KEYS = new Set([
  'context',
  'dockerfile',
  'dockerfile_inline',
  'args',
  'target',
  'cache_from',
  'cache_to',
  'labels',
  'no_cache',
  'pull',
  'platforms',
  'shm_size',
  'tags',
]);

export type GuardContext = {
  appId: string;
  store: string;
  /** Host path of this app's own data directory (manager configuration, never app.env). */
  appDataDir: string;
  /** Host path of the owner files root. */
  filesRoot: string;
  /** Host path prefix of Wizard source pins: <appDataRoot>/.wizard-source/<store>/<app>@<rev>. */
  sourceRoot: string;
  /** Directories Compose may read --env-file / env_file from (container paths). */
  envDirs: string[];
  /** Host path of the whole app-data root (WizardStorage share binds stay inside it). */
  appDataRoot?: string;
  /** Exact normalized mount tuples added under the current WizardStorage grant lock. */
  injected?: Set<string>;
  /** Runtipi's shared network Docker name. */
  sharedNetwork?: string;
};

const clean = (p: string) => path.posix.normalize(p).replace(/\/+$/, '') || '/';
const under = (p: string, root: string) => {
  const a = clean(p);
  const b = clean(root);
  return !a.split('/').includes('..') && (a === b || a.startsWith(`${b}/`));
};
const truthy = (v: unknown) => v === true || String(v).toLowerCase() === 'true';
const nonEmpty = (v: unknown) => (Array.isArray(v) ? v.length > 0 : v && typeof v === 'object' ? Object.keys(v).length > 0 : Boolean(v));

function canonical(v: any): any {
  if (Array.isArray(v)) return v.map(canonical);
  if (v && typeof v === 'object')
    return Object.fromEntries(
      Object.keys(v)
        .sort()
        .map((k) => [k, canonical(v[k])]),
    );
  return v;
}
/** Complete values, excluding inert image/command/environment updates. */
export const valueDigest = (v: unknown) =>
  createHash('sha256')
    .update(JSON.stringify(canonical(v)))
    .digest('hex');
export const bindIdentity = (service: string, v: NonNullable<ReturnType<typeof volumeOf>>) =>
  `${service}|${valueDigest({ source: clean(v.source), target: clean(v.target), read_only: v.readOnly })}`;

/** Unsupported means unreviewed, never owner-approvable. Check before secret-reading normalization too. */
export function unsupportedFeatures(config: any): string[] {
  const out: string[] = [];
  if (!config || typeof config !== 'object' || Array.isArray(config)) return ['unsupported:compose'];
  for (const key of Object.keys(config)) if (!TOP_KEYS.has(key) && !key.startsWith('x-')) out.push(`unsupported:top:${key}`);
  for (const kind of ['secrets', 'configs']) if (nonEmpty(config[kind])) out.push(`unsupported:${kind}`);
  for (const [name, v] of Object.entries((config.volumes ?? {}) as Record<string, any>)) {
    if (v && (v.external || (v.driver && v.driver !== 'local') || nonEmpty(v.driver_opts))) out.push(`unsupported:volume:${name}`);
    for (const k of Object.keys(v ?? {}))
      if (!['name', 'driver', 'driver_opts', 'external', 'labels'].includes(k)) out.push(`unsupported:volume:${name}:${k}`);
  }
  for (const [name, v] of Object.entries((config.networks ?? {}) as Record<string, any>)) {
    if (v && ((v.driver && v.driver !== 'bridge') || nonEmpty(v.driver_opts))) out.push(`unsupported:network:${name}`);
    for (const k of Object.keys(v ?? {}))
      if (!['name', 'driver', 'driver_opts', 'external', 'labels', 'attachable', 'internal', 'enable_ipv6', 'enable_ipv4', 'ipam'].includes(k))
        out.push(`unsupported:network:${name}:${k}`);
    if (v?.ipam && ((v.ipam.driver && v.ipam.driver !== 'default') || nonEmpty(v.ipam.options))) out.push(`unsupported:network:${name}:ipam`);
  }
  for (const [svc, s] of Object.entries((config.services ?? {}) as Record<string, any>)) {
    if (!s || typeof s !== 'object' || Array.isArray(s)) {
      out.push(`unsupported:${svc}:invalid`);
      continue;
    }
    for (const k of Object.keys(s)) if (!SERVICE_KEYS.has(k) && !k.startsWith('x-')) out.push(`unsupported:${svc}:${k}`);
    for (const k of ['build', 'post_start', 'pre_stop', 'secrets', 'configs', 'env_file', 'develop', 'annotations'])
      if (nonEmpty(s[k])) out.push(`unsupported:${svc}:${k}`);
    if (
      s.logging &&
      ((s.logging.driver && !['json-file', 'local', 'none'].includes(s.logging.driver)) ||
        Object.keys(s.logging.options ?? {}).some(
          (k) => !['max-size', 'max-file', 'compress', 'labels', 'labels-regex', 'env', 'env-regex', 'mode', 'max-buffer-size'].includes(k),
        ))
    )
      out.push(`unsupported:${svc}:logging`);
    if (nonEmpty(s.deploy?.resources?.reservations?.devices)) out.push(`unsupported:${svc}:deploy_devices`);
    if (s.deploy && Object.keys(s.deploy).some((k) => !['replicas', 'resources', 'restart_policy', 'labels'].includes(k)))
      out.push(`unsupported:${svc}:deploy`);
    for (const raw of Array.isArray(s.volumes) ? s.volumes : []) {
      if (typeof raw !== 'object' || !raw) continue;
      if (raw.type && !['bind', 'volume', 'tmpfs'].includes(raw.type)) out.push(`unsupported:${svc}:mount_type`);
      for (const k of Object.keys(raw))
        if (!['type', 'source', 'target', 'read_only', 'bind', 'volume', 'tmpfs', 'consistency'].includes(k))
          out.push(`unsupported:${svc}:mount:${k}`);
      if (
        raw.bind &&
        (Object.keys(raw.bind).some((k) => !['create_host_path', 'propagation'].includes(k)) ||
          (raw.bind.propagation && raw.bind.propagation !== 'rprivate'))
      )
        out.push(`unsupported:${svc}:bind_options`);
      if (raw.volume && Object.keys(raw.volume).some((k) => k !== 'nocopy')) out.push(`unsupported:${svc}:volume_options`);
      if (raw.tmpfs && Object.keys(raw.tmpfs).some((k) => !['size', 'mode'].includes(k))) out.push(`unsupported:${svc}:tmpfs_options`);
    }
  }
  return out.sort();
}

/** Normalize one service volume (long or short syntax) to {type, source, target, readOnly}. */
export function volumeOf(v: any): { type: string; source: string; target: string; readOnly: boolean } | null {
  if (typeof v === 'string') {
    const parts = v.split(':');
    if (parts.length === 1) return { type: 'volume', source: '', target: parts[0] ?? '', readOnly: false };
    const [source = '', target = '', mode = ''] = parts;
    const bind = /^[/.~]/.test(source) || source.includes('/') || source.includes('$');
    return { type: bind ? 'bind' : 'volume', source, target, readOnly: mode.split(',').includes('ro') };
  }
  if (!v || typeof v !== 'object') return null;
  return {
    type: String(v.type ?? (v.hostPath ? 'bind' : 'volume')),
    source: String(v.source ?? v.src ?? v.hostPath ?? ''),
    target: String(v.target ?? v.dst ?? v.containerPath ?? ''),
    readOnly: v.read_only === true || v.readOnly === true,
  };
}

export function finalHostTokens(config: any, ctx: GuardContext): string[] {
  const out = new Set<string>(unsupportedFeatures(config));
  if (!config || typeof config !== 'object' || Array.isArray(config)) return ['compose:invalid'];
  for (const key of Object.keys(config)) if (!TOP_KEYS.has(key) && !key.startsWith('x-')) out.add(`top:${key}`);
  for (const [name, raw] of Object.entries((config.networks ?? {}) as Record<string, any>)) {
    const spec = raw && typeof raw === 'object' ? raw : {};
    const dockerName = String(spec.name ?? name);
    if (spec.external) {
      if (!OURS.test(dockerName) && dockerName !== (ctx.sharedNetwork ?? 'runtipi_tipi_main_network')) out.add(`network:external:${dockerName}`);
    } else if ((spec.driver && spec.driver !== 'bridge') || nonEmpty(spec.driver_opts)) out.add(`network:driver:${name}`);
  }
  for (const [name, raw] of Object.entries((config.volumes ?? {}) as Record<string, any>)) {
    const spec = raw && typeof raw === 'object' ? raw : {};
    if (spec.external) out.add(`volume:external:${String(spec.name ?? name)}`);
    else if ((spec.driver && spec.driver !== 'local') || nonEmpty(spec.driver_opts)) out.add(`volume:driver:${name}`);
  }
  for (const kind of ['secrets', 'configs']) {
    for (const [name, raw] of Object.entries((config[kind] ?? {}) as Record<string, any>)) {
      if (raw && typeof raw === 'object' && raw.file != null) out.add(`${kind}:file:${name}:${clean(String(raw.file))}`);
    }
  }
  const services = config.services ?? {};
  if (!services || typeof services !== 'object' || Array.isArray(services)) return [...out, 'services:invalid'].sort();
  for (const [svc, raw] of Object.entries(services as Record<string, any>)) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      out.add(`${svc}|invalid`);
      continue;
    }
    const s = raw as Record<string, any>;
    for (const key of Object.keys(s)) if (!SERVICE_KEYS.has(key) && !key.startsWith('x-')) out.add(`${svc}|key:${key}`);
    if (truthy(s.privileged)) out.add(`${svc}|privileged`);
    const nm = s.network_mode == null ? '' : String(s.network_mode);
    if (nm && nm !== 'none' && !nm.startsWith('service:')) out.add(`${svc}|network_mode:${nm}`);
    for (const key of ['pid', 'ipc', 'userns_mode', 'uts', 'cgroup']) {
      const v = s[key] == null ? '' : String(s[key]).toLowerCase();
      if (v && v !== 'private' && v !== 'shareable' && !v.startsWith('service:')) out.add(`${svc}|${key}:${v}`);
    }
    for (const key of FLAGGED_SERVICE_KEYS) if (nonEmpty(s[key])) out.add(`${svc}|${key}:${valueDigest(s[key])}`);
    for (const cap of Array.isArray(s.cap_add) ? s.cap_add : s.cap_add ? ['invalid'] : []) {
      const c = String(cap).toUpperCase().replace(/^CAP_/, '');
      if (!DEFAULT_CAPS.has(c)) out.add(`${svc}|cap:${c}`);
    }
    for (const opt of Array.isArray(s.security_opt) ? s.security_opt : s.security_opt ? ['invalid'] : []) {
      if (!SAFE_SECURITY.has(String(opt))) out.add(`${svc}|security_opt:${String(opt)}`);
    }
    if (nonEmpty(s.deploy?.resources?.reservations?.devices)) out.add(`${svc}|deploy_devices`);
    for (const f of Array.isArray(s.env_file) ? s.env_file : s.env_file ? [s.env_file] : []) {
      const file = clean(String(typeof f === 'object' && f ? (f.path ?? '') : f));
      if (!ctx.envDirs.some((d) => under(file, d))) out.add(`${svc}|env_file:${file}`);
    }
    if (s.build) {
      const b = typeof s.build === 'object' ? s.build : { context: s.build };
      const context = String(b.context ?? '.');
      const remote = /^(?:https?:\/\/|git:\/\/|git@)/.test(context);
      const pinned = context.startsWith(`${clean(ctx.sourceRoot)}/${ctx.appId}@`) && !context.split('/').includes('..');
      if (!remote && !pinned && !under(context, ctx.appDataDir)) out.add(`${svc}|build:context:${clean(context)}`);
      for (const key of Object.keys(b)) if (!BUILD_KEYS.has(key)) out.add(`${svc}|build:${key}`);
      if (b.network && b.network !== 'default' && b.network !== 'none') out.add(`${svc}|build:network:${b.network}`);
    }
    for (const raw of Array.isArray(s.volumes) ? s.volumes : s.volumes ? ['invalid'] : []) {
      const v = volumeOf(raw);
      if (!v) {
        out.add(`${svc}|volume:invalid`);
        continue;
      }
      if (/docker\.sock|containerd\.sock|podman\.sock|\/run\/docker|\/run\/containerd/.test(v.source)) {
        out.add(`${svc}|socket:${clean(v.source)}:${clean(v.target)}:${v.readOnly ? 'ro' : 'rw'}`);
        continue;
      }
      if (v.type === 'volume' || v.type === 'tmpfs') continue; // app-private named volume (top level checked above)
      if (v.type !== 'bind') {
        out.add(`${svc}|mount:${v.type}`);
        continue;
      }
      const source = clean(v.source);
      if (!source.startsWith('/') || source.includes('$')) {
        out.add(`${svc}|bind:${source}:${v.target}:${v.readOnly ? 'ro' : 'rw'}`);
        continue;
      }
      if (under(source, ctx.appDataDir) || under(source, ctx.filesRoot)) continue;
      if (v.readOnly && source.startsWith(`${clean(ctx.sourceRoot)}/${ctx.appId}@`) && !source.split('/').includes('..')) continue;
      // Grant shares are manager-generated binds between app-data trees, never
      // a recipe or user override path (a matching target outside fails).
      if (ctx.appDataRoot && under(source, ctx.appDataRoot) && ctx.injected?.has(bindIdentity(svc, v))) continue;
      out.add(`${svc}|bind:${source}:${v.target}:${v.readOnly ? 'ro' : 'rw'}`);
    }
  }
  return [...out].sort();
}

export async function readHostApprovals(dataDir: string): Promise<Record<string, string[]>> {
  try {
    const manager = (await fs.stat(path.join(dataDir, '.env'))).uid;
    const parent = path.join(dataDir, 'user-config');
    const st = await fs.lstat(parent);
    if (!st.isDirectory() || st.isSymbolicLink() || st.mode & 0o022 || ![0, manager].includes(st.uid)) return {};
    const file = await fs.open(path.join(parent, APPROVALS_FILE), constants.O_RDONLY | constants.O_NOFOLLOW);
    let doc: any;
    try {
      const st = await file.stat();
      if (!st.isFile() || st.nlink !== 1 || st.mode & 0o022 || ![0, manager].includes(st.uid)) return {};
      doc = JSON.parse(await file.readFile('utf8'));
    } finally {
      await file.close();
    }
    if (doc?.schema !== 1 || !doc.apps || typeof doc.apps !== 'object') return {};
    const out: Record<string, string[]> = {};
    for (const [key, list] of Object.entries(doc.apps as Record<string, unknown>))
      if (Array.isArray(list)) out[key] = list.filter((x): x is string => typeof x === 'string');
    return out;
  } catch {
    return {}; // missing or unreadable: nothing accepted
  }
}

export class HostAccessRefused extends Error {
  constructor(
    readonly app: string,
    readonly tokens: string[],
  ) {
    super(
      `${app} refused before container start: its host access is unapproved or unsupported (${tokens.join('; ')}). ` +
        'Retry through Wizard to get an owner approval link.',
    );
  }
}

export function assertFinalHostPolicy(config: unknown, ctx: GuardContext, approvals: Record<string, string[]>) {
  if (!ID.test(ctx.appId) || !ID.test(ctx.store)) throw new HostAccessRefused(`${ctx.appId}:${ctx.store}`, ['invalid app id']);
  const accepted = new Set(approvals[`${ctx.store}/${ctx.appId}`] ?? []);
  const missing = finalHostTokens(config, ctx).filter((t) => t.startsWith('unsupported:') || !accepted.has(t));
  if (missing.length) throw new HostAccessRefused(`${ctx.appId}:${ctx.store}`, missing);
}

/** Compose's own model of the actual command, NOT the generated file alone. */
export function injectedBinds(generated: any, source: any): Set<string> {
  const out = new Set<string>();
  for (const [svc, s] of Object.entries((generated?.services ?? {}) as Record<string, any>)) {
    const recipe = new Set(
      (Array.isArray(source?.[svc]?.volumes) ? source[svc].volumes : []).map((v: any) => volumeOf(v)?.target).filter(Boolean) as string[],
    );
    for (const raw of Array.isArray(s?.volumes) ? s.volumes : []) {
      const v = volumeOf(raw);
      if (v?.type === 'bind' && v.target && !recipe.has(v.target)) out.add(bindIdentity(svc, v));
    }
  }
  return out;
}

/** Commands that can create or change containers. Everything else only operates existing ones. */
export const MUTATING = new Set(['up', 'create', 'run', 'build', 'watch']);

/** Process environment for docker-compose: never the manager's own credentials (no ${POSTGRES_PASSWORD}). */
export function composeEnv(env: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  const keep = ['PATH', 'HOME', 'DOCKER_HOST', 'DOCKER_CONFIG', 'DOCKER_CERT_PATH', 'DOCKER_TLS_VERIFY', 'DOCKER_CONTEXT', 'TZ', 'LANG'];
  return Object.fromEntries(keep.filter((k) => env[k] !== undefined).map((k) => [k, env[k]])) as NodeJS.ProcessEnv;
}
