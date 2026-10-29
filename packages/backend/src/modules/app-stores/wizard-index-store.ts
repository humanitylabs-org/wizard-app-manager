/**
 * Wizard App Manager: an app store backed by the Wizard App Index instead of a cloned git catalog.
 *
 * Browsing and search go straight to the index (https://apps.aiwizards.com), so nothing is downloaded until an
 * app is opened, installed or updated. Then exactly that one repo is fetched at the commit the index approved,
 * re-checked here, and written into Runtipi's normal per-app layout (repos/<slug>/apps/<id>/...). From that point
 * on Runtipi's own install / update / backup / networking code handles it unchanged.
 */
import path from 'node:path';
import { fetchPinnedCommit, resolveImages } from './curator-mirrors';
import { execFileAsync } from '@/common/helpers/exec-helpers';
import { extractAppUrn } from '@/common/helpers/app-helpers';
import type { AppUrn } from '@runtipi/common/types';
import * as yaml from 'yaml';
import { AppStoreFilesManager } from './app-store-files-manager';
import { prepareChatsModule } from './chats-module';
import { preparePackage, sourceDir } from './packaging';
import { hermesRecipe } from '../app-lifecycle/hermes-lock';

export const WIZARD_STORE_SLUG = 'wizardapps';
export const DEFAULT_WIZARD_INDEX = 'https://apps.aiwizards.com';
export const WIZARD_CATEGORIES = ['agent', 'interface', 'app', 'system'] as const;

export const isIndexStoreUrl = (url: string) => !/\.git$|github\.com|gitlab|codeberg|\/tree\//.test(url) && /^https?:\/\/[^/]+\/?$/.test(url);

export type IndexRecord = {
  repo: string;
  publisher?: string;
  id: string;
  name: string;
  summary: string;
  type?: string;
  category?: string;
  version?: string;
  logo?: string | null;
  url?: string;
  provides?: string[];
  uses?: string[];
  architectures?: string[];
  install: { git: string; commit: string };
  checked_at?: number;
};

type IndexSearch = { apps: IndexRecord[]; next_offset: number | null };

const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const SHA = /^[0-9a-f]{40}$/;
const CHECK_TTL = 10 * 60 * 1000;

// wizard-os#138: until app pages stop acting as the owner (wizard-os#133), the Store lists and installs first-party
// apps only. Same rule and override as wam/index.py: WAM_PUBLISHERS (comma-separated GitHub owners) replaces the
// list; WAM_PUBLISHERS=* turns the guard off.
export const FIRST_PARTY_PUBLISHERS = ['humanitylabs-org', 'oscarhenrycollins', 'jdefleur', 'defleur-agents'];
export const allowedPublisher = (rec: { repo?: string; publisher?: string }, env = process.env.WAM_PUBLISHERS ?? '') => {
  const raw = env.trim();
  if (raw === '*') return true;
  const allow = raw
    ? raw
        .split(',')
        .map((x) => x.trim().toLowerCase())
        .filter(Boolean)
    : FIRST_PARTY_PUBLISHERS;
  const owner = String(rec.publisher || String(rec.repo ?? '').split('/')[0]).toLowerCase();
  return allow.includes(owner);
};

/** Container settings that would give an app control of the host. Same rule as the index checker. */
export const composeRisks = (doc: unknown): string[] => {
  const risks: string[] = [];
  const services = (doc as { services?: Record<string, Record<string, unknown>> })?.services ?? {};
  const bad = ['ALL', 'SYS_ADMIN', 'SYS_MODULE', 'SYS_PTRACE', 'SYS_RAWIO', 'NET_ADMIN', 'DAC_READ_SEARCH', 'BPF', 'PERFMON'];
  for (const [name, s] of Object.entries(services)) {
    if (!s || typeof s !== 'object') continue;
    if (s.privileged) risks.push(`${name}: privileged`);
    for (const k of ['network_mode', 'pid', 'ipc', 'userns_mode', 'uts']) {
      if (String(s[k] ?? '').toLowerCase() === 'host') risks.push(`${name}: ${k}: host`);
    }
    for (const c of (s.cap_add as string[] | undefined) ?? []) {
      if (bad.includes(String(c).toUpperCase().replace(/^CAP_/, ''))) risks.push(`${name}: cap_add ${c}`);
    }
    if (Array.isArray(s.devices) && s.devices.length) risks.push(`${name}: host devices`);
    for (const opt of (s.security_opt as string[] | undefined) ?? []) if (/unconfined/i.test(String(opt))) risks.push(`${name}: security_opt ${opt}`);
    for (const v of (s.volumes as unknown[] | undefined) ?? []) {
      const src = typeof v === 'string' ? v.split(':')[0] : String((v as { source?: string })?.source ?? '');
      if (/docker\.sock|containerd\.sock|podman\.sock/.test(src ?? '')) risks.push(`${name}: mounts the container runtime socket`);
      else if (src === '/' || /^\/(etc|root|boot|proc|sys|dev|var\/run|run|home|usr|lib|bin|sbin)(\/|$)/.test(src ?? ''))
        risks.push(`${name}: mounts host path ${src}`);
    }
  }
  return risks;
};

const frontMatter = (text: string): Record<string, unknown> => {
  const m = text.match(/^---\n([\s\S]*?)\n---\n?/);
  if (!m?.[1]) throw new Error('wizard.md has no front matter');
  return (yaml.parse(m[1]) ?? {}) as Record<string, unknown>;
};

const body = (text: string) => text.replace(/^---\n[\s\S]*?\n---\n?/, '').trim();

export class WizardIndexStore extends AppStoreFilesManager {
  private lastCheck = new Map<string, { at: number; rec: IndexRecord | null }>();
  private inflight = new Map<string, Promise<void>>();

  get indexUrl() {
    const own = isIndexStoreUrl(this.storeConfig.url) ? this.storeConfig.url : '';
    return (process.env.WIZARD_INDEX_URL || own || DEFAULT_WIZARD_INDEX).replace(/\/+$/, '');
  }

  private async get<T>(pathAndQuery: string): Promise<T> {
    const res = await fetch(`${this.indexUrl}${pathAndQuery}`, {
      headers: { accept: 'application/json', 'user-agent': 'wizard-app-manager' },
      signal: AbortSignal.timeout(15000),
    });
    if (!res.ok) throw new Error(`index ${pathAndQuery} -> ${res.status}`);
    return (await res.json()) as T;
  }

  /** Search the index. `cursor` is the index offset as a string. */
  public async search(params: { search?: string | null; category?: string | null; pageSize?: number; cursor?: string | null }) {
    const q = new URLSearchParams({
      q: params.search ?? '',
      limit: String(Math.min(params.pageSize ?? 24, 50)),
      offset: params.cursor && /^\d+$/.test(params.cursor) ? params.cursor : '0',
    });
    if (params.category && (WIZARD_CATEGORIES as readonly string[]).includes(params.category)) q.set('category', params.category);
    const { architecture } = this.configuration.getConfig();
    if (architecture) q.set('arch', architecture);
    const res = await this.get<IndexSearch>(`/v1/search?${q}`);
    const data = res.apps.filter((a) => ID.test(a.id) && allowedPublisher(a)).map((a) => this.toSimple(a));
    return { data, total: data.length + (res.next_offset != null ? 1 : 0), nextCursor: res.next_offset != null ? String(res.next_offset) : null };
  }

  public toSimple(a: IndexRecord) {
    return {
      id: a.id,
      urn: `${a.id}:${this.storeConfig.slug}` as AppUrn,
      name: a.name,
      short_desc: a.summary,
      categories: [(WIZARD_CATEGORIES as readonly string[]).includes(a.category ?? '') ? a.category : 'app'] as never,
      deprecated: false,
      created_at: (a.checked_at ?? 0) * 1000,
      supported_architectures: (a.architectures?.length ? a.architectures : ['amd64', 'arm64']) as ('amd64' | 'arm64')[],
      available: true,
    };
  }

  /** The record the index lists for this id right now (cached briefly). Null if it is no longer listed. */
  private async lookup(appName: string, fresh = false): Promise<IndexRecord | null> {
    const hit = this.lastCheck.get(appName);
    if (!fresh && hit && Date.now() - hit.at < CHECK_TTL) return hit.rec;
    try {
      const res = await this.get<IndexSearch>(`/v1/search?${new URLSearchParams({ id: appName, limit: '5' })}`);
      const listed = res.apps.find((a) => a.id === appName && allowedPublisher(a)) ?? null;
      const rec =
        listed && appName === 'hermes-agent'
          ? {
              ...listed,
              repo: hermesRecipe.repo,
              install: { ...listed.install, git: `https://github.com/${hermesRecipe.repo}`, commit: hermesRecipe.commit },
            }
          : listed;
      this.lastCheck.set(appName, { at: Date.now(), rec });
      return rec;
    } catch (e) {
      this.logger.warn(`Wizard index lookup for ${appName} failed: ${String(e)}`);
      return hit?.rec ?? null;
    }
  }

  private async localCommit(appName: string) {
    const f = path.join(this.getAppStoreFolder(), appName, '.wizard-commit');
    return (await this.filesystem.isFile(f)) ? ((await this.filesystem.readTextFile(f)) ?? '').trim() : null;
  }

  /** True when the prepared recipe at `commit` is a source/build package whose app-data source copy is missing. */
  private async sourceMissing(appName: string, commit: string) {
    const wizardText = await this.filesystem.readTextFile(path.join(this.getAppStoreFolder(), appName, 'wizard.md'));
    const pkg = String(((frontMatter(wizardText ?? '').runtime ?? {}) as { package?: unknown }).package ?? '');
    if (pkg !== 'source' && pkg !== 'build') return false;
    const { appDataDir } = this.configuration.get('directories');
    return !(await this.filesystem.isDirectory(sourceDir(appDataDir, this.storeConfig.slug, appName, commit)));
  }

  /** Make sure repos/<slug>/apps/<id> holds the commit the index lists now. Fetches only this app. */
  public async ensureApp(appName: string, fresh = false) {
    const running = this.inflight.get(appName);
    if (running) return running;
    const job = this.materialize(appName, fresh).finally(() => this.inflight.delete(appName));
    this.inflight.set(appName, job);
    return job;
  }

  private async materialize(appName: string, fresh: boolean) {
    if (!ID.test(appName)) return;
    // Written only by the owner-local Store installer after resolving an authorized
    // exact release. Never replace a prepared private/channel selection with an
    // index lookup (including during copyAppFromRepoToInstalled's fresh lookup).
    const selected = path.join(this.getAppStoreFolder(), appName, '.wizard-selection');
    if (await this.filesystem.isFile(selected)) {
      const pin = JSON.parse((await this.filesystem.readTextFile(selected)) ?? '{}');
      const source = JSON.parse((await this.filesystem.readTextFile(path.join(this.getAppStoreFolder(), appName, '.wizard-source'))) ?? '{}');
      if (!SHA.test(pin.commit ?? '') || pin.commit !== (await this.localCommit(appName)) || pin.repo !== source.repo)
        throw new Error('invalid owner-local selection pin');
      if (appName === 'hermes-agent' && (pin.commit !== hermesRecipe.commit || pin.repo !== hermesRecipe.repo))
        throw new Error('Hermes selection differs from the shared exact lock');
      return;
    }
    const rec = await this.lookup(appName, fresh);
    if (!rec || !SHA.test(rec.install?.commit ?? '')) return;
    const have = await this.localCommit(appName);
    // Same commit is done, unless its pinned source copy (source/build packaging) is gone: then the compose's
    // ${APP_SOURCE_DIR} would be empty and every update fails with "invalid spec: :/app:ro".
    if (have === rec.install.commit && !(await this.sourceMissing(appName, have))) return;

    const { dataDir } = this.configuration.get('directories');
    const src = path.join(dataDir, 'repos', `.${this.storeConfig.slug}-src`, `${appName}@${rec.install.commit.slice(0, 12)}`);
    await this.fetchCommit(rec.install.git, rec.install.commit, src);

    const wizardText = await this.filesystem.readTextFile(path.join(src, 'wizard.md'));
    if (!wizardText) throw new Error(`${rec.repo}: no wizard.md`);
    const d = frontMatter(wizardText);
    if (d.id !== appName) throw new Error(`${rec.repo}: wizard.md id ${String(d.id)} != ${appName}`);
    const rt = (d.runtime ?? {}) as { config?: string; compose?: string; package?: unknown };
    if (!rt.config || !rt.compose) throw new Error(`${rec.repo}: wizard.md runtime.config/compose missing`);
    const cfgText = await this.filesystem.readTextFile(path.join(src, rt.config));
    const composeText = await this.filesystem.readTextFile(path.join(src, rt.compose));
    if (!cfgText || !composeText) throw new Error(`${rec.repo}: runtime files missing at ${rec.install.commit}`);
    const compose = yaml.parse(composeText);
    const risks = composeRisks(compose);
    if (risks.length) throw new Error(`${rec.repo} asks for host-level access: ${risks.join('; ')}`);

    await resolveImages(compose);
    // Packaging (source | build | image): copy the pinned code where the app's containers can mount or build it.
    const { appDataDir } = this.configuration.get('directories');
    await preparePackage({
      pkg: rt.package,
      compose,
      src,
      target: sourceDir(appDataDir, this.storeConfig.slug, appName, rec.install.commit),
      appName,
      commit: rec.install.commit,
    });

    const dst = path.join(this.getAppStoreFolder(), appName);
    let oldTipi = 0;
    const oldCfg = path.join(dst, 'config.json');
    if (await this.filesystem.isFile(oldCfg)) {
      try {
        oldTipi = Number(JSON.parse((await this.filesystem.readTextFile(oldCfg)) ?? '{}').tipi_version) || 0;
      } catch {
        oldTipi = 0;
      }
    }

    const cfg = JSON.parse(cfgText);
    cfg.id = appName;
    cfg.categories = [(WIZARD_CATEGORIES as readonly string[]).includes(String(d.category)) ? d.category : d.type === 'agent' ? 'agent' : 'app'];
    // A new commit is always an update, even if the author forgot to raise tipi_version.
    cfg.tipi_version = Math.max(Number(cfg.tipi_version) || 1, have ? oldTipi + 1 : oldTipi);
    cfg.short_desc = cfg.short_desc || String(d.summary ?? '');
    const now = Date.now();
    for (const k of ['created_at', 'updated_at']) if (typeof cfg[k] === 'number' && cfg[k] >= now) cfg[k] = now - 1000;

    // Box-only by default: publish the main port on loopback. Tailscale or Cloudflare Tunnel carry it further.
    for (const svc of Object.values((compose?.services ?? {}) as Record<string, Record<string, unknown>>)) {
      const xr = (svc?.['x-runtipi'] ?? {}) as { is_main?: boolean; internal_port?: number | string };
      if (xr.is_main && xr.internal_port && !(Array.isArray(svc.ports) && svc.ports.length)) {
        svc.ports = [`127.0.0.1:\${APP_PORT}:${xr.internal_port}`];
      }
    }

    const tmp = `${dst}.tmp-${process.pid}`;
    await this.filesystem.removeDirectory(tmp);
    await this.filesystem.createDirectories([path.join(tmp, 'metadata')]);
    await this.filesystem.writeTextFile(path.join(tmp, 'config.json'), `${JSON.stringify(cfg, null, 2)}\n`);
    await this.filesystem.writeTextFile(path.join(tmp, 'docker-compose.yml'), yaml.stringify(compose, { lineWidth: 0 }));
    const desc = body(wizardText);
    await this.filesystem.writeTextFile(
      path.join(tmp, 'metadata', 'description.md'),
      `${desc.startsWith('# ') ? '' : `# ${String(d.name ?? appName)}\n\n`}${desc}\n`,
    );
    await this.filesystem.writeTextFile(path.join(tmp, 'wizard.md'), wizardText);
    await prepareChatsModule(src, tmp, d, rec.install.commit);
    if (typeof d.skill === 'string') {
      const skill = await this.filesystem.readTextFile(path.join(src, d.skill));
      if (skill) await this.filesystem.writeTextFile(path.join(tmp, 'SKILL.md'), skill);
    }
    const cfgDir = path.dirname(path.join(src, rt.config));
    for (const cand of [
      path.join(cfgDir, 'metadata', 'logo.jpg'),
      typeof d.icon === 'string' ? path.join(src, d.icon) : '',
      path.join(cfgDir, 'logo.jpg'),
      path.join(src, 'metadata', 'logo.jpg'),
    ]) {
      if (cand && (await this.filesystem.isFile(cand))) {
        await this.filesystem.copyFile(cand, path.join(tmp, 'metadata', 'logo.jpg'));
        break;
      }
    }
    const data = path.join(cfgDir, 'data');
    if (await this.filesystem.isDirectory(data)) await this.filesystem.copyDirectory(data, path.join(tmp, 'data'));
    await this.filesystem.writeTextFile(path.join(tmp, '.wizard-commit'), `${rec.install.commit}\n`);
    await this.filesystem.writeTextFile(
      path.join(tmp, '.wizard-source'),
      `${JSON.stringify({ repo: rec.repo, git: rec.install.git, index: this.indexUrl, source: 'standard' })}\n`,
    );

    await this.filesystem.removeDirectory(dst);
    await execFileAsync('mv', [tmp, dst]);
    await this.filesystem.removeDirectory(src);
    this.logger.info(`Wizard app ${appName} ready at ${rec.install.commit.slice(0, 12)} (${rec.repo})`);
  }

  private async fetchCommit(url: string, commit: string, dest: string) {
    await fetchPinnedCommit(url, commit, dest);
  }

  // ---- Runtipi store interface: same as a git store, but each app is fetched on demand ----

  private async ensureUrn(appUrn: AppUrn, fresh = false) {
    try {
      await this.ensureApp(extractAppUrn(appUrn).appName, fresh);
    } catch (e) {
      this.logger.error(`Wizard app ${appUrn}: ${String(e)}`);
    }
  }

  public override async getAppInfoFromAppStore(appUrn: AppUrn) {
    await this.ensureUrn(appUrn);
    return super.getAppInfoFromAppStore(appUrn);
  }

  public override async getAppInfoFromAppStoreOrInstalled(appUrn: AppUrn) {
    await this.ensureUrn(appUrn);
    return super.getAppInfoFromAppStoreOrInstalled(appUrn);
  }

  public override async getSourceDockerComposeYaml(appUrn: AppUrn) {
    await this.ensureUrn(appUrn);
    return super.getSourceDockerComposeYaml(appUrn);
  }

  public override async copyAppFromRepoToInstalled(appUrn: AppUrn) {
    await this.ensureApp(extractAppUrn(appUrn).appName, true);
    return super.copyAppFromRepoToInstalled(appUrn);
  }

  /** Update checks only touch installed apps, so this stays cheap however big the index gets. */
  public override async getAppUpdateInfo(appUrn: AppUrn) {
    await this.ensureUrn(appUrn);
    return super.getAppUpdateInfo(appUrn);
  }

  public override async getConfigJson(appUrn: AppUrn) {
    await this.ensureUrn(appUrn);
    return super.getConfigJson(appUrn);
  }

  /** Only apps already fetched on this box; the full catalog lives in the index. */
  public override async getAvailableAppUrns() {
    const dir = this.getAppStoreFolder();
    if (!(await this.filesystem.isDirectory(dir))) return [];
    const names = await this.filesystem.listFiles(dir);
    return names.filter((n) => ID.test(n)).map((n) => `${n}:${this.storeConfig.slug}` as AppUrn);
  }

  public override async getAppImage(appUrn: AppUrn) {
    const local = await super.getAppImage(appUrn);
    const { appInstalledDir, appRepoDir } = this.getAppPaths(appUrn);
    const hasOwn =
      (await this.filesystem.isFile(path.join(appInstalledDir, 'metadata', 'logo.jpg'))) ||
      (await this.filesystem.isFile(path.join(appRepoDir, 'metadata', 'logo.jpg')));
    if (hasOwn) return local;
    // Store tiles for apps not fetched yet: use the logo the index lists, cached on disk.
    const { appName } = extractAppUrn(appUrn);
    const { dataDir } = this.configuration.get('directories');
    const cached = path.join(dataDir, 'repos', `.${this.storeConfig.slug}-logos`, `${appName}.img`);
    try {
      if (!(await this.filesystem.isFile(cached))) {
        const rec = await this.lookup(appName);
        if (!rec?.logo || !/^https:\/\//.test(rec.logo)) return local;
        const res = await fetch(rec.logo, { signal: AbortSignal.timeout(15000) });
        if (!res.ok) return local;
        const buf = Buffer.from(await res.arrayBuffer());
        if (buf.length > 2_000_000) return local;
        await this.filesystem.createDirectory(path.dirname(cached));
        await this.filesystem.writeBinaryFile(cached, buf);
      }
      return { image: await this.filesystem.readBinaryFile(cached), etag: await this.filesystem.getFileEtag(cached) };
    } catch {
      return local;
    }
  }
}
