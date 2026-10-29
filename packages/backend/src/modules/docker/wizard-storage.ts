/** Decisions #79/#80. File-backed storage, independent of the network-isolation flag.
 * Only Wizard manifests opt in; stock Runtipi recipes are untouched.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
const execFileAsync = promisify(execFile);
import * as yaml from 'yaml';
import { appProposals, sameAppGrant, reconcileAppGrants } from './wizard-app-grants';
import { withHostAuthority } from './host-policy-lock';
import { bindIdentity, volumeOf } from './wizard-host-guard';

export const COPY_IMAGE = 'alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc';
export const LEGACY_CLAUDE = 'wizard-ai-accounts-claude';
const ID = /^[a-z0-9][a-z0-9-]{1,40}$/;
const isId = (value: unknown): value is string => typeof value === 'string' && ID.test(value);
const VOLUME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
export type Share = { id: string; path: string };
export type Mount = { app: string; share: string; path: string };
export type Manifest = {
  id: string;
  shares?: Share[];
  mounts?: Mount[];
  uses?: string[];
  provides?: string[];
  requires?: { models?: unknown; services?: unknown };
  api?: { endpoint?: string };
};
export type Grant = { kind: string; from: string; to: string; share?: string; reason?: string; approved?: string; created_at?: number };
export type Grants = { schema: number; updated_at: number; seeded: string[]; grants: Grant[]; denied: Grant[]; [key: string]: unknown };
// Compose is extensible; preserve unknown fields rather than normalizing them away.
export type Compose = { services: Record<string, any>; volumes?: Record<string, any>; [key: string]: any };
export type App = { urn: string; manifest: Manifest; compose: Compose; data: string; hostData: string };
type Run = (file: string, args: string[]) => Promise<{ stdout: string; stderr?: string }>;
export const relativePath = (p: unknown): p is string =>
  typeof p === 'string' &&
  !!p &&
  !p.startsWith('/') &&
  !p.split('/').some((x) => !x || x === '.' || x === '..') &&
  !/[\\$:,]/.test(p) &&
  ![...p].some((x) => x.charCodeAt(0) < 32 || x.charCodeAt(0) === 127);
export const containerPath = (p: unknown): p is string => typeof p === 'string' && p.startsWith('/') && relativePath(p.slice(1));

export function validateStorage(m: Manifest, compose: Compose, legacy = false) {
  for (const [field, keys] of [
    ['shares', ['id', 'path']],
    ['mounts', ['app', 'share', 'path']],
  ] as const) {
    const rows = m[field] ?? [];
    if (!Array.isArray(rows)) throw new Error(`${field} must be a list`);
    const seen = new Set();
    for (const row of rows as any[]) {
      if (!row || typeof row !== 'object' || Object.keys(row).sort().join() !== [...keys].sort().join()) throw new Error(`Invalid ${field} shape`);
      if (!(field === 'shares' ? isId(row.id) && relativePath(row.path) : isId(row.app) && isId(row.share) && containerPath(row.path)))
        throw new Error(`Unsafe ${field} id/path`);
      const key = field === 'shares' ? row.id : row.path;
      if (seen.has(key)) throw new Error(`Duplicate ${field} id/path`);
      seen.add(key);
    }
  }
  if (compose.volumes != null && (typeof compose.volumes !== 'object' || Array.isArray(compose.volumes)))
    throw new Error('volumes must be a mapping');
  for (const [name, opts] of Object.entries(compose.volumes ?? {})) {
    if (!VOLUME.test(name) || name === '.' || name === '..') throw new Error('Unsafe volume id');
    if (opts != null && (typeof opts !== 'object' || Array.isArray(opts))) throw new Error('Invalid volume options');
    const legacyName = legacy && opts?.name === LEGACY_CLAUDE;
    if ((opts?.external && !legacyName) || opts?.driver || opts?.driver_opts || (opts && 'name' in opts && !legacyName))
      throw new Error(`Volume ${name}: fixed name/external/custom driver is not portable; use shares`);
  }
}
export const targetOf = (v: any): string | undefined => (typeof v === 'string' ? v.split(':')[1] : v?.target);

/** Pure compile pass, including long syntax and architecture overrides (caller applies those first). */
export function rewriteVolumes(input: Compose, appId: string): Compose {
  const doc = structuredClone(input);
  const plans: { name: string; old: string; relative: string }[] = [];
  const aliases: NonNullable<Compose['volumes']> = {};
  for (const [name, opts] of Object.entries(doc.volumes ?? {})) {
    if (!VOLUME.test(name) || name === '.' || name === '..') throw new Error('Unsafe volume id');
    const claude = appId === 'wizard-claude' && opts?.name === LEGACY_CLAUDE;
    // Agent Service consumers declare the legacy login as `external: true`; the
    // alias branch below keeps that declaration verbatim (prepare() verifies it).
    const legacyConsumer = opts?.name === LEGACY_CLAUDE && !claude && appId !== 'hermes-agent';
    if ((opts?.external && !legacyConsumer) || opts?.driver || opts?.driver_opts) throw new Error(`Nonportable volume ${name}`);
    if (opts && 'name' in opts && opts.name !== LEGACY_CLAUDE) throw new Error(`Fixed volume name ${name}`);
    // Agent Service generated consumers predate mounts declarations. Preserve
    // their exact mounts; prepare() verifies this name is a provider-folder alias
    // before start. Hermes retains its existing grant-based upgrade below.
    if (opts?.name === LEGACY_CLAUDE && !claude && appId !== 'hermes-agent') {
      aliases[name] = opts;
      continue;
    }
    const relative = claude ? 'data/claude' : `volumes/${name}`;
    plans.push({ name, old: opts?.name ?? name, relative });
    for (const svc of Object.values(doc.services)) {
      svc.volumes = (svc.volumes ?? []).map((v: any) => {
        if (typeof v === 'string') {
          const parts = v.split(':');
          if (parts.length > 1 && parts[0] === name) return [`\${APP_DATA_DIR}/${relative}`, ...parts.slice(1)].join(':');
        } else if (v?.source === name && (!v.type || v.type === 'volume')) {
          const { volume: _volume, ...rest } = v;
          return { ...rest, type: 'bind', source: `\${APP_DATA_DIR}/${relative}` };
        }
        return v;
      });
    }
  }
  if (Object.keys(aliases).length) doc.volumes = aliases;
  else delete doc.volumes;
  doc['x-wizard-volumes'] = plans;
  return doc;
}

export async function confined(base: string, relative: string, create = false): Promise<string> {
  if (!relativePath(relative)) throw new Error('Unsafe storage path');
  // Reject every symlink component, including the base, not just final realpath.
  const full = path.resolve(base, relative);
  let walk = path.parse(full).root;
  for (const part of full.slice(walk.length).split(path.sep)) {
    walk = path.join(walk, part);
    const st = await fs.lstat(walk).catch((e) => {
      if (e.code !== 'ENOENT') throw e;
      return null;
    });
    if (st?.isSymbolicLink() || (st && !st.isDirectory())) throw new Error(`Storage path is not a real directory: ${walk}`);
    if (!st && create)
      await fs.mkdir(walk, { mode: 0o700 }).catch((e) => {
        if (e.code !== 'EEXIST') throw e;
      });
  }
  return full;
}

export class WizardStorage {
  private queue: Promise<unknown> = Promise.resolve();
  constructor(
    readonly dirs: { dataDir: string; appDataDir: string },
    // Runtipi's app rows are authoritative. Files are recipes, not installs.
    readonly installedApps: () => Promise<{ appName: string; appStoreSlug: string }[]>,
    readonly run: Run = (f, a) => execFileAsync(f, a),
    // Isolated rehearsals can select a disposable legacy volume without ever
    // touching the owner's fixed-name login. Recipe compatibility stays literal.
    readonly legacyClaudeVolume: string = LEGACY_CLAUDE,
    readonly warn: (message: string) => void = console.warn,
  ) {
    if (!VOLUME.test(legacyClaudeVolume)) throw new Error('Unsafe legacy Claude volume name');
  }
  serial<T>(fn: () => Promise<T>): Promise<T> {
    return withHostAuthority(this.dirs.dataDir, async () => {
      const next = this.queue.then(fn);
      this.queue = next.catch(() => {
        /* A failed operation must not poison subsequent independent operations. */
      });
      return next;
    });
  }
  get grantsPath() {
    return path.join(this.dirs.dataDir, 'state', 'wizard-grants.json');
  }
  async readGrants(): Promise<Grants> {
    const raw = await fs.readFile(this.grantsPath, 'utf8').catch((e) => {
      if (e.code !== 'ENOENT') throw e;
      return null;
    });
    if (!raw) return { schema: 1, updated_at: 0, seeded: [], grants: [], denied: [] };
    const file = JSON.parse(raw);
    if (file.schema !== 1 || !Array.isArray(file.grants) || !Array.isArray(file.denied))
      throw new Error('Invalid wizard-grants.json; refusing to start');
    return file;
  }
  async writeGrants(file: Grants) {
    file.updated_at = Math.floor(Date.now() / 1000);
    await fs.mkdir(path.dirname(this.grantsPath), { recursive: true });
    const tmp = `${this.grantsPath}.tmp`;
    await fs.writeFile(tmp, `${JSON.stringify(file, null, 2)}\n`, { mode: 0o600 });
    await fs.rename(tmp, this.grantsPath);
    // Preserve the registry's other fields and do not publish keys.
    const mirrors = new Set([
      // WIZARD_OS_HOME adds an owner projection; it must not replace the
      // installed state projection consumed by older shells and restore paths.
      path.join(this.dirs.dataDir, 'state', 'wizard-os', 'apps.json'),
      ...(process.env.WIZARD_OS_HOME ? [path.join(process.env.WIZARD_OS_HOME, 'apps.json')] : []),
      path.join(this.dirs.appDataDir, 'wizard-os', 'apps.json'),
    ]);
    for (const mirror of mirrors) {
      const reg = await fs.readFile(mirror, 'utf8').catch((e) => {
        if (e.code !== 'ENOENT') throw e;
        return null;
      });
      if (reg) {
        await fs.writeFile(`${mirror}.tmp-grants`, `${JSON.stringify({ ...JSON.parse(reg), grants: file.grants }, null, 2)}\n`);
        await fs.rename(`${mirror}.tmp-grants`, mirror);
      }
    }
  }
  async apps(): Promise<App[]> {
    const out: App[] = [];
    const installed = await this.installedApps();
    const urns = new Set(installed.map((a) => `${a.appName}:${a.appStoreSlug}`));
    const root = path.join(this.dirs.dataDir, 'apps');
    const stores = await fs.readdir(root).catch((e) => {
      if (e.code !== 'ENOENT') throw e;
      return [];
    });
    for (const store of stores) {
      if (!ID.test(store)) continue;
      for (const id of await fs.readdir(path.join(root, store))) {
        if (!ID.test(id)) continue;
        if (!urns.has(`${id}:${store}`)) {
          this.warn(`Ignoring stale app directory ${id}:${store}: no installed app row`);
          continue;
        }
        const dir = path.join(root, store, id);
        const text = await fs.readFile(path.join(dir, 'wizard.md'), 'utf8').catch((e) => {
          if (e.code !== 'ENOENT') throw e;
          return null;
        });
        if (!text) continue;
        const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
        if (!m) throw new Error(`Invalid manifest ${id}`);
        const manifest = yaml.parse(m[1]!);
        if (manifest.id !== id) throw new Error('Installed manifest identity mismatch');
        const compose = yaml.parse(await fs.readFile(path.join(dir, 'docker-compose.yml'), 'utf8'));
        // Narrow compatibility shim for already-installed legacy recipes only.
        if (id === 'wizard-claude' && Object.values(compose.volumes ?? {}).some((v: any) => v?.name === LEGACY_CLAUDE))
          manifest.shares ??= [{ id: 'claude-login', path: 'claude' }];
        if (id === 'hermes-agent' && Object.values(compose.volumes ?? {}).some((v: any) => v?.name === LEGACY_CLAUDE))
          manifest.mounts ??= [{ app: 'wizard-claude', share: 'claude-login', path: '/claude' }];
        const data = path.join(this.dirs.appDataDir, store, id);
        const env = await fs.readFile(path.join(data, 'app.env'), 'utf8').catch((e) => {
          if (e.code !== 'ENOENT') throw e;
          return '';
        });
        const hostData = env.match(/^APP_DATA_DIR=['"]?([^\n'"]+)/m)?.[1] ?? data;
        out.push({ urn: `${id}:${store}`, manifest, compose, data, hostData });
      }
    }
    // Count database rows, including a second install whose recipe is missing.
    // Only share providers must be unique: an unrelated app installed in two
    // stores (e.g. a stopped older copy) must not block every lifecycle action.
    const providers = new Set(out.filter((a) => a.manifest.shares?.length).map((a) => a.manifest.id));
    for (const a of out) for (const m of a.manifest.mounts ?? []) providers.add(m.app);
    for (const a of out) if (Object.values(a.compose.volumes ?? {}).some((v: any) => v?.name === LEGACY_CLAUDE)) providers.add('wizard-claude');
    if ([...providers].some((id) => installed.filter((row) => row.appName === id).length > 1)) throw new Error('Ambiguous installed provider ids');
    return out;
  }
  proposals(apps: App[], file: Grants): Grant[] {
    const out: Grant[] = [];
    const key = (g: Grant) => `${g.kind}\0${g.from}\0${g.to}\0${g.share ?? ''}`;
    const known = new Set([...file.grants, ...file.denied].map(key));
    for (const a of apps)
      for (const m of a.manifest.mounts ?? []) {
        const p = apps.find((p) => p.manifest.id === m.app);
        const g = { kind: 'share', from: a.manifest.id, to: m.app, share: m.share, reason: 'mounts' };
        if (p?.manifest.shares?.some((s) => s.id === m.share) && !known.has(key(g))) {
          out.push(g);
          known.add(key(g));
        }
      }
    return [...out, ...appProposals(apps, file)];
  }
  async proposed(id?: string) {
    if (id && !ID.test(id)) throw new Error('Invalid app id');
    const apps = await this.apps();
    // The install sheet asks before the consumer is installed. Materialized store
    // recipes are candidates only: prepare() still acts solely on installed apps.
    if (id && ID.test(id) && !apps.some((a) => a.manifest.id === id)) {
      const repos = path.join(this.dirs.dataDir, 'repos');
      const stores = await fs.readdir(repos).catch((e) => {
        if (e.code !== 'ENOENT') throw e;
        return [];
      });
      for (const slug of stores.filter((s) => ID.test(s))) {
        const dir = path.join(repos, slug, 'apps', id);
        const text = await fs.readFile(path.join(dir, 'wizard.md'), 'utf8').catch((e) => {
          if (e.code !== 'ENOENT') throw e;
          return null;
        });
        if (!text) continue;
        const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
        const manifest = match ? yaml.parse(match[1]!) : null;
        if (!manifest || manifest.id !== id) throw new Error('Invalid candidate manifest');
        const compose = yaml.parse(await fs.readFile(path.join(dir, 'docker-compose.yml'), 'utf8'));
        validateStorage(manifest, compose);
        apps.push({ urn: `${id}:${slug}`, manifest, compose, data: '', hostData: '' });
        break;
      }
    }
    if (id) {
      const consumer = apps.find((a) => a.manifest.id === id);
      for (const dep of consumer?.manifest.uses ?? []) {
        if (!ID.test(dep) || apps.some((a) => a.manifest.id === dep)) continue;
        const dir = path.join(this.dirs.dataDir, 'repos/wizardapps/apps', dep);
        const text = await fs.readFile(path.join(dir, 'wizard.md'), 'utf8').catch((e) => {
          if (e.code !== 'ENOENT') throw e;
          return '';
        });
        if (!text) continue;
        const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
        const manifest = match ? yaml.parse(match[1]!) : null;
        if (!manifest || manifest.id !== dep) throw new Error('Invalid dependency manifest');
        const compose = yaml.parse(await fs.readFile(path.join(dir, 'docker-compose.yml'), 'utf8'));
        apps.push({ urn: `${dep}:wizardapps`, manifest, compose, data: '', hostData: '' });
      }
    }
    return this.proposals(apps, await this.readGrants()).filter((g) => !id || g.from === id);
  }
  async change(grant: Grant, approve: boolean): Promise<string> {
    return this.serial(async () => {
      const apps = await this.apps();
      const file = await this.readGrants();
      if (grant.kind === 'app') {
        if (!ID.test(grant.from) || !ID.test(grant.to.replace(/^slot:/, '')) || grant.from === grant.to) throw new Error('Invalid app grant');
        const consumer = apps.find((a) => a.manifest.id === grant.from);
        if (!consumer) throw new Error('Consumer not installed');
        // Explicit owner grants need not be manifest defaults. Revocations are remembered.
        file.grants = file.grants.filter((g) => !sameAppGrant(g, grant));
        file.denied = file.denied.filter((g) => !sameAppGrant(g, grant));
        (approve ? file.grants : file.denied).push({
          kind: 'app',
          from: grant.from,
          to: grant.to,
          reason: 'owner',
          approved: 'owner',
          created_at: Math.floor(Date.now() / 1000),
        });
        file.layer2_removing = ((file.layer2_removing as string[]) ?? []).filter((id) => id !== grant.from);
        await this.writeGrants(file);
        await reconcileAppGrants(apps, file, this.dirs.dataDir, (f) => this.writeGrants(f));
        return consumer.urn;
      }
      if (grant.kind !== 'share' || !ID.test(grant.from) || !ID.test(grant.to) || !ID.test(grant.share ?? '')) throw new Error('Invalid share grant');
      const consumer = apps.find((a) => a.manifest.id === grant.from);
      if (!consumer) throw new Error('Consumer not installed');
      if (approve && !consumer.manifest.mounts?.some((m) => m.app === grant.to && m.share === grant.share)) throw new Error('No declared mount');
      if (approve && !apps.some((a) => a.manifest.id === grant.to && a.manifest.shares?.some((s) => s.id === grant.share)))
        throw new Error('Provider/share not installed');
      const match = (g: Grant) => g.kind === 'share' && g.from === grant.from && g.to === grant.to && g.share === grant.share;
      file.grants = file.grants.filter((g) => !match(g));
      file.denied = file.denied.filter((g) => !match(g));
      const row = {
        kind: 'share',
        from: grant.from,
        to: grant.to,
        share: grant.share,
        reason: 'mounts',
        approved: 'owner',
        created_at: Math.floor(Date.now() / 1000),
      };
      (approve ? file.grants : file.denied).push(row);
      await this.writeGrants(file);
      return consumer.urn;
    });
  }
  async remove(id: string) {
    return this.serial(async () => {
      const file = await this.readGrants();
      const affected = file.grants.filter((g) => g.kind === 'share' && g.to === id).map((g) => g.from);
      file.grants = file.grants.filter((g) => g.from !== id && g.to !== id);
      file.seeded = (file.seeded ?? []).filter((x) => x !== id);
      file.layer2_removing = [...new Set([...((file.layer2_removing as string[]) ?? []), id])];
      await this.writeGrants(file);
      // The DB row still exists during uninstall; do not immediately re-seed it.
      await reconcileAppGrants(await this.apps(), file, this.dirs.dataDir, (f) => this.writeGrants(f), id);
      return affected;
    });
  }

  async reconcile() {
    return this.serial(async () => reconcileAppGrants(await this.apps(), await this.readGrants(), this.dirs.dataDir, (f) => this.writeGrants(f)));
  }

  /** Only the already-created local bind alias is safe for legacy consumers.
   * Inspect the daemon path (hostData), not the manager's mapped app-data path.
   * Never create/remove/retarget a volume here: running agents may hold it.
   */
  private async isClaudeAlias(app: App, relative = 'data/claude'): Promise<boolean> {
    const info = JSON.parse((await this.run('docker', ['volume', 'inspect', this.legacyClaudeVolume, '--format', '{{json .}}'])).stdout);
    return (
      info.Driver === 'local' &&
      info.Options?.type === 'none' &&
      info.Options?.o?.split(',').includes('bind') &&
      info.Options?.device === path.posix.join(app.hostData, relative)
    );
  }

  /** Copy with the daemon, not the manager's uid. Stage+rename and a durable intent
   * make failure/retry safe; an existing unmarked nonempty destination is never overwritten.
   * Never removes the source volume. Refuse a hot copy while any container is running on it.
   */
  async migrate(old: string, relative: string, app: App) {
    await confined(app.data, relative);
    const parent = path.posix.dirname(relative);
    const leaf = path.posix.basename(relative);
    const marker = path.join(app.data, parent, `.${leaf}.wizard-migrated`);
    const marked = await fs.lstat(marker).catch((e) => {
      if (e.code !== 'ENOENT') throw e;
      return null;
    });
    if (marked && !marked.isFile()) throw new Error('Unsafe storage migration marker');
    // A completed migration does not require a cold legacy volume. Agents may
    // still use its bind alias, and no login bytes need to be copied again.
    if (marked) return;
    const names = (await this.run('docker', ['volume', 'ls', '--format', '{{.Name}}'])).stdout.split(/\r?\n/);
    if (!names.includes(old)) {
      // An empty bind replaces Docker's new named volume. Initialise its owner
      // from the recipe so non-root services can chmod/create their store. Never
      // chown an existing directory or migrated owner data.
      const exists = await fs.lstat(path.join(app.data, relative)).catch((e) => {
        if (e.code !== 'ENOENT') throw e;
        return null;
      });
      if (!exists) {
        const folder = await confined(app.data, relative, true);
        const volume = relative.startsWith('volumes/') ? relative.slice(8) : '';
        const users = [
          ...new Set(
            Object.values(app.compose.services)
              .filter((svc) => (svc.volumes ?? []).some((v: any) => (typeof v === 'string' ? v.split(':')[0] === volume : v.source === volume)))
              .map((svc) => String(svc.user ?? '0:0')),
          ),
        ];
        const owner = users[0];
        if (users.length === 1 && owner && /^\d+:\d+$/.test(owner)) {
          const [uid, gid] = owner.split(':').map(Number);
          await fs.chown(folder, uid!, gid!);
        }
      }
      return;
    }
    if (old === this.legacyClaudeVolume && (await this.isClaudeAlias(app, relative))) {
      await confined(app.data, parent, true);
      await fs.writeFile(marker, '', { mode: 0o600, flag: 'wx' });
      return;
    }
    const active = (await this.run('docker', ['ps', '-q', '--filter', `volume=${old}`])).stdout.trim();
    if (active) throw new Error(`Storage migration blocked: stop containers using ${old}, then restart the app; old login was not changed`);
    const script = `set -eu
cd /state
[ ! -L "$1" ] || exit 41
[ ! -L ".$1.wizard-migrated" ] || exit 41
[ ! -f ".$1.wizard-migrated" ] || exit 0
if [ -e "$1" ] && [ "$(ls -A "$1")" ]; then
  [ -f ".$1.wizard-copy-ready" ] || { echo 'Nonempty unmarked destination: refusing login overwrite' >&2; exit 42; }
else
  [ ! -e ".$1.wizard-copy" ] || rm -rf ".$1.wizard-copy"
  mkdir ".$1.wizard-copy"
  cp -a /old/. ".$1.wizard-copy/"
  touch ".$1.wizard-copy-ready"
  [ ! -e "$1" ] || rmdir "$1"
  mv ".$1.wizard-copy" "$1"
fi
touch ".$1.wizard-migrated"
rm -f ".$1.wizard-copy-ready"`;
    await confined(app.data, parent === '.' ? 'volumes' : parent, true);
    await this.run('docker', [
      'run',
      '--rm',
      '--network',
      'none',
      '--read-only',
      '--mount',
      `type=volume,src=${old},dst=/old,readonly`,
      '--mount',
      `type=bind,src=${app.hostData}/${parent},dst=/state`,
      COPY_IMAGE,
      'sh',
      '-c',
      script,
      'copy',
      leaf,
    ]);
  }

  async prepare(urn: string, generated: string, apply?: (prepared: string, grantedBinds?: Set<string>) => Promise<void>): Promise<string> {
    return this.serial(async () => {
      const apps = await this.apps();
      const app = apps.find((a) => a.urn === urn);
      if (!app) {
        await apply?.(generated);
        return generated;
      }
      // apps() returns installed recipes only; candidates still fail rule 15.
      validateStorage(app.manifest, app.compose, true);
      if (
        !['wizard-claude', 'hermes-agent'].includes(app.manifest.id) &&
        Object.values(app.compose.volumes ?? {}).some((v) => v?.name === LEGACY_CLAUDE)
      ) {
        const provider = apps.find((a) => a.manifest.id === 'wizard-claude');
        if (!provider) throw new Error('Legacy Claude bind alias requires the installed provider');
        await confined(provider.data, 'data/claude');
        if (!(await this.isClaudeAlias(provider))) throw new Error('Legacy Claude volume must be a bind alias to the provider folder');
      }
      let doc: Compose = yaml.parse(generated);
      // Builder metadata persists the original names; a restarted/revoked consumer is
      // always reconstructed from its source, never from previously injected mounts.
      const plans = rewriteVolumes(app.compose, app.manifest.id)['x-wizard-volumes'];
      if (!doc['x-wizard-volumes']) doc = rewriteVolumes(doc, app.manifest.id);
      // Never trust an app-supplied x-wizard-* migration instruction.
      doc['x-wizard-volumes'] = plans;
      const file = await this.readGrants();
      file.layer2_removing = ((file.layer2_removing as string[]) ?? []).filter((id) => id !== app.manifest.id);
      await reconcileAppGrants(apps, file, this.dirs.dataDir, (f) => this.writeGrants(f));
      const provider = apps.find((a) => a.manifest.id === 'wizard-claude');
      const hermes = apps.find((a) => a.manifest.id === 'hermes-agent');
      // Only an existing fixed-volume pair inherits authority. New installs require consent.
      if (
        provider &&
        hermes &&
        !file.claude_share_upgrade_v1 &&
        [provider, hermes].every((a) => Object.values(a.compose.volumes ?? {}).some((v: any) => v?.name === LEGACY_CLAUDE)) &&
        (await this.run('docker', ['volume', 'ls', '--format', '{{.Name}}'])).stdout.split(/\r?\n/).includes(this.legacyClaudeVolume)
      ) {
        if (
          !file.denied.some((g) => g.kind === 'share' && g.from === 'hermes-agent' && g.to === 'wizard-claude' && g.share === 'claude-login') &&
          !file.grants.some((g) => g.kind === 'share' && g.from === 'hermes-agent' && g.to === 'wizard-claude' && g.share === 'claude-login')
        )
          file.grants.push({
            kind: 'share',
            from: 'hermes-agent',
            to: 'wizard-claude',
            share: 'claude-login',
            reason: 'mounts',
            approved: 'install',
            created_at: Math.floor(Date.now() / 1000),
          });
        file.claude_share_upgrade_v1 = true;
        await this.writeGrants(file);
      }
      for (const plan of doc['x-wizard-volumes'] ?? []) {
        if (plan.old === LEGACY_CLAUDE && app.manifest.id === 'hermes-agent') {
          // Never duplicate provider-owned credentials into consumer backups.
          for (const svc of Object.values(doc.services)) svc.volumes = (svc.volumes ?? []).filter((v: any) => targetOf(v) !== '/claude');
          continue;
        }
        await this.migrate(plan.old === LEGACY_CLAUDE ? this.legacyClaudeVolume : `${urn.replace(':', '_')}_${plan.old}`, plan.relative, app);
      }
      // A modern provider already uses data/claude and has no named-volume declaration.
      if (app.manifest.id === 'wizard-claude') await this.migrate(this.legacyClaudeVolume, 'data/claude', app);
      const previously = doc['x-wizard-share-mounts'] ?? [];
      for (const svc of Object.values(doc.services)) svc.volumes = (svc.volumes ?? []).filter((v: any) => !previously.includes(targetOf(v)));
      doc['x-wizard-share-mounts'] = [];
      const grantedBinds = new Set<string>();
      for (const m of app.manifest.mounts ?? []) {
        if (!file.grants.some((g) => g.kind === 'share' && g.from === app.manifest.id && g.to === m.app && g.share === m.share)) continue;
        const p = apps.find((p) => p.manifest.id === m.app);
        const share = p?.manifest.shares?.find((s) => s.id === m.share);
        if (!p || !share) throw new Error(`Granted share unavailable: ${m.app}/${m.share}`);
        validateStorage(p.manifest, p.compose, p.manifest.id === 'wizard-claude');
        const missing = await fs.lstat(path.join(p.data, 'data', share.path)).then(
          () => false,
          (e) => {
            if (e.code !== 'ENOENT') throw e;
            return true;
          },
        );
        const folder = await confined(p.data, `data/${share.path}`, true);
        if (p.manifest.id === 'wizard-claude' && share.id === 'claude-login') await this.migrate(this.legacyClaudeVolume, `data/${share.path}`, p);
        const main = Object.keys(app.compose.services).filter((n) => app.compose.services[n]['x-runtipi']?.is_main);
        if (main.length !== 1 || !doc.services[main[0]!]) throw new Error('Share consumer requires exactly one main service');
        const svc = doc.services[main[0]!];
        if ((svc.volumes ?? []).some((v: any) => targetOf(v) === m.path)) throw new Error(`Share mount conflicts at ${m.path}`);
        // New provider folders inherit the provider service uid, never consumer uid.
        const providerSvc =
          Object.values(p.compose.services).find((s) =>
            (s.volumes ?? []).some((v: any) => typeof v === 'string' && v.startsWith(`\${APP_DATA_DIR}/data/${share.path}:`)),
          ) ?? Object.values(p.compose.services).find((s) => s['x-runtipi']?.is_main);
        const user = String(providerSvc?.user ?? '0:0');
        if (!/^\d+(?::\d+)?$/.test(user)) throw new Error('Shared folder provider must declare numeric uid[:gid]');
        const [uid, gid] = user.split(':').map(Number);
        const stat = await fs.stat(folder);
        if (missing && (stat.uid !== uid || stat.gid !== (gid ?? uid))) await fs.chown(folder, uid!, gid ?? uid!);
        svc.volumes ??= [];
        svc.volumes.push({ type: 'bind', source: `${p.hostData}/data/${share.path}`, target: m.path, bind: { create_host_path: false } });
        grantedBinds.add(bindIdentity(main[0]!, volumeOf(svc.volumes[svc.volumes.length - 1])!));
        doc['x-wizard-share-mounts'].push(m.path);
      }
      const prepared = yaml.stringify(doc);
      // Hold the same grant lock through the actual create/start. Otherwise a
      // concurrent revoke can finish before a stale prepared mount is started.
      await apply?.(prepared, grantedBinds);
      return prepared;
    });
  }
}
