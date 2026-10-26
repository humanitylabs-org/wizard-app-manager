/** App-to-app layer 2. Shares use the same store/lock but never carry keys.
 * No Docker-network mutations: isolation remains deferred.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import { randomBytes, createHash } from 'node:crypto';
import * as yaml from 'yaml';
import { confined, type App, type Grant, type Grants } from './wizard-storage';

const ID = /^[a-z0-9][a-z0-9-]{1,40}$/;
export const appGrant = (g: Grant) => !g.kind || g.kind === 'app';
export const sameAppGrant = (a: Grant, b: Grant) => appGrant(a) && appGrant(b) && a.from === b.from && a.to === b.to;
const hash = (key: string) => createHash('sha256').update(key).digest('hex');
export function targets(app: App, apps: App[]): Grant[] {
  const uses = app.manifest.uses ?? [];
  if (!Array.isArray(uses) || uses.some((x) => typeof x !== 'string' || !ID.test(x.replace(/^slot:/, '')))) throw new Error('Invalid uses');
  const edges: Grant[] = uses.map((to) => ({
    kind: 'app',
    from: app.manifest.id,
    to: to.startsWith('slot:') || apps.some((a) => a.manifest.id === to) ? to : `slot:${to}`,
    reason: 'uses',
  }));
  if (app.manifest.requires?.models || app.manifest.requires?.services)
    edges.push({ kind: 'app', from: app.manifest.id, to: 'slot:connection-router', reason: 'requires' });
  return edges.filter((g, i) => g.to !== g.from && edges.findIndex((x) => x.to === g.to) === i);
}
export function providers(to: string, apps: App[]): App[] {
  return apps.filter((a) => (to.startsWith('slot:') ? a.manifest.provides?.includes(to.slice(5)) : a.manifest.id === to));
}
export function appProposals(apps: App[], file: Grants): Grant[] {
  return apps
    .flatMap((a) => targets(a, apps))
    .filter((g) => providers(g.to, apps).length && ![...file.grants, ...file.denied].some((old) => sameAppGrant(old, g)));
}
/** Prefer an explicit advertised ingress; otherwise the main service's shared
 * network alias and internal port. Never use a public/owner URL from apps.json.
 */
export function providerEndpoint(app: App): string | undefined {
  const explicit = app.manifest.api?.endpoint;
  if (explicit != null) {
    const u = new URL(explicit);
    if (!['http:', 'https:'].includes(u.protocol) || u.username || u.password || u.search || u.hash || /[\s$]/.test(explicit))
      throw new Error('Invalid api.endpoint');
    return explicit.replace(/\/$/, '');
  }
  const mains = Object.entries(app.compose.services).filter(([, s]) => s['x-runtipi']?.is_main);
  if (mains.length !== 1) return undefined;
  const [name, svc] = mains[0]!;
  const port = Number(svc['x-runtipi'].internal_port);
  if (!Number.isInteger(port) || port < 1 || port > 65535 || svc.network_mode) return undefined;
  const networks = Object.values(svc.networks ?? {}) as any[];
  const alias = networks.flatMap((n) => n?.aliases ?? []).find((s) => typeof s === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_.-]*$/.test(s));
  // Compose collapses duplicate logical networks by physical name. Its injected
  // tipi_main_network attachment wins and can discard the recipe alias on main.
  // Use the daemon's unique project/service container alias in that case, without
  // changing any networks (Layer 1 remains off).
  const duplicateMain = Object.entries(app.compose.networks ?? {}).some(
    ([key, net]: [string, any]) => key !== 'tipi_main_network' && net?.name?.includes('runtipi_tipi_main_network') && key in (svc.networks ?? {}),
  );
  const container = svc.container_name ?? `${app.urn.replace(':', '_')}-${name}-1`;
  return `http://${duplicateMain ? container : (alias ?? name)}:${port}`;
}
export function mountAppGrants(input: any): any {
  const doc = structuredClone(input);
  for (const svc of Object.values(doc.services) as any[]) {
    const target = (v: any) => (typeof v === 'string' ? v.split(':')[1] : v.target);
    const source = (v: any) => (typeof v === 'string' ? v.split(':')[0] : v.source);
    if ((svc.volumes ?? []).some((v: any) => target(v) === '/wizard' && source(v) !== '${APP_DATA_DIR}/wizard'))
      throw new Error('Reserved grants mount /wizard');
    svc.volumes = [...(svc.volumes ?? []).filter((v: any) => target(v) !== '/wizard'), '${APP_DATA_DIR}/wizard:/wizard:ro'];
    if (Array.isArray(svc.environment)) {
      svc.environment = [...svc.environment.filter((e: string) => !e.startsWith('WIZARD_GRANTS_FILE=')), 'WIZARD_GRANTS_FILE=/wizard/grants.json'];
    } else svc.environment = { ...svc.environment, WIZARD_GRANTS_FILE: '/wizard/grants.json' };
  }
  return doc;
}
type AppFile = {
  outgoing: Record<string, string>;
  incoming: { from: string; sha256: string }[];
  provider_endpoints?: Record<string, string>;
  enforce: boolean;
  [key: string]: any;
};
async function read(file: string): Promise<AppFile | null> {
  const text = await fs.readFile(file, 'utf8').catch((e) => {
    if (e.code !== 'ENOENT') throw e;
    return null;
  });
  if (text === null) return null;
  const d = JSON.parse(text);
  if (
    !d ||
    !d.outgoing ||
    Array.isArray(d.outgoing) ||
    typeof d.outgoing !== 'object' ||
    Object.values(d.outgoing).some((v) => typeof v !== 'string' || !v) ||
    !Array.isArray(d.incoming) ||
    d.incoming.some((v: any) => !v || typeof v.from !== 'string' || !/^[a-f0-9]{64}$/.test(v.sha256)) ||
    typeof d.enforce !== 'boolean'
  )
    throw new Error('Invalid app grants file; refusing to replace');
  return d;
}
async function atomic(file: string, value: unknown, mode = 0o644) {
  const bytes = `${JSON.stringify(value, null, 2)}\n`;
  if (
    (await fs.readFile(file, 'utf8').catch((e) => {
      if (e.code !== 'ENOENT') throw e;
      return null;
    })) === bytes
  )
    return;
  const tmp = `${file}.tmp-${process.pid}-${randomBytes(6).toString('hex')}`;
  try {
    await fs.writeFile(tmp, bytes, { mode, flag: 'wx' });
    await fs.rename(tmp, file);
  } finally {
    await fs.rm(tmp, { force: true });
  }
}
/** Discover previous owner directory mounts, including the Calendar bridge.
 * Translate host paths into the manager's mapped root. No symlinks or arbitrary
 * outside-root writes. External files must first be moved under runtipi by owner.
 */
async function legacyPaths(app: App, dataDir: string): Promise<string[]> {
  const config = path.join(dataDir, 'user-config', app.urn.split(':')[1]!, app.manifest.id, 'docker-compose.yml');
  const text = await fs.readFile(config, 'utf8').catch((e) => {
    if (e.code !== 'ENOENT') throw e;
    return '';
  });
  if (!text) return [];
  const doc = yaml.parse(text);
  const out: string[] = [];
  const suffix = `/app-data/${app.urn.split(':')[1]}/${app.manifest.id}`;
  const hostRoot = app.hostData.endsWith(suffix) ? app.hostData.slice(0, -suffix.length) : dataDir;
  for (const svc of Object.values(doc?.services ?? {}) as any[]) {
    const env = Array.isArray(svc.environment) ? Object.fromEntries(svc.environment.map((e: string) => e.split(/=(.*)/s))) : (svc.environment ?? {});
    const file = env.WIZARD_GRANTS_FILE ?? '/wizard/grants.json';
    for (const v of svc.volumes ?? []) {
      const [src, dst] = typeof v === 'string' ? v.split(':') : [v.source, v.target];
      if (!src || !dst || !file.startsWith(`${dst}/`)) continue;
      const source = src.replaceAll('${ROOT_FOLDER_HOST}', hostRoot).replaceAll('${APP_DATA_DIR}', app.hostData);
      if (!source.startsWith(`${hostRoot}/`)) throw new Error('Legacy grants mount outside runtipi root; explicit migration required');
      const local = path.join(dataDir, source.slice(hostRoot.length), file.slice(dst.length));
      await confined(dataDir, path.relative(dataDir, path.dirname(local)));
      out.push(local);
    }
  }
  return [...new Set(out)];
}
/** Called only under WizardStorage.serial. Keys exist solely in consumer files.
 * Keep unmanaged incoming rows (e.g. the gateway) and existing enforcement policy.
 * Save hash metadata before publication so retries can remove partial old writes.
 */
export async function reconcileAppGrants(
  installedApps: App[],
  file: Grants,
  dataDir: string,
  save: (file: Grants) => Promise<void>,
  exclude?: string,
) {
  file.layer2_removing = ((file.layer2_removing as string[]) ?? []).filter((id) => installedApps.some((a) => a.manifest.id === id));
  const apps = installedApps.filter(
    (a) => a.manifest.id !== exclude && !(file.layer2_removing as string[]).includes(a.manifest.id) && a.urn.endsWith(':wizardapps'),
  );
  const involvesLink = (g: Grant) =>
    installedApps.some((a) => a.source === 'link' && a.manifest.id === g.from) || providers(g.to, installedApps).some((a) => a.source === 'link');
  // Installing a link is not approval for its declared capabilities. Remove old
  // automatic app/share rows too, without touching explicit owner decisions.
  file.grants = file.grants.filter((g) => g.approved !== 'install' || !involvesLink(g));
  const previous = (file.layer2 ?? {}) as {
    managed?: Record<string, string[]>;
    adopted?: string[];
    endpoints?: Record<string, Record<string, string>>;
  };
  const adopted = new Set(previous.adopted ?? []);
  const docs = new Map<string, { app: App; paths: string[]; doc: AppFile; old: AppFile }>();
  for (const app of apps) {
    await confined(app.data, 'wizard', true);
    await fs.chmod(path.join(app.data, 'wizard'), 0o755);
    const canonical = path.join(app.data, 'wizard/grants.json');
    const legacy = (await legacyPaths(app, dataDir)).filter((p) => p !== canonical);
    const existing = await read(canonical);
    const old = existing ??
      (await Promise.all(legacy.map(read))).find(Boolean) ?? { outgoing: {}, incoming: [], provider_endpoints: {}, enforce: false };
    docs.set(app.manifest.id, {
      app,
      paths: [canonical, ...legacy],
      old,
      doc: {
        ...old,
        outgoing: {},
        incoming: [...old.incoming],
        provider_endpoints: Object.fromEntries(Object.entries(old.provider_endpoints ?? {}).filter(([to]) => !(to in old.outgoing))),
      },
    });
    if (!adopted.has(app.manifest.id)) {
      for (const to of Object.keys(old.outgoing)) {
        const g = { kind: 'app', from: app.manifest.id, to, reason: 'owner', approved: 'owner', created_at: Math.floor(Date.now() / 1000) };
        if (
          !involvesLink(g) &&
          (ID.test(to) || /^slot:[a-z0-9][a-z0-9-]{1,40}$/.test(to)) &&
          ![...file.grants, ...file.denied].some((x) => sameAppGrant(x, g))
        )
          file.grants.push(g);
      }
      adopted.add(app.manifest.id);
    }
  }
  file.grants.push(
    ...appProposals(apps, file)
      .filter((g) => !involvesLink(g))
      .map((g) => ({ ...g, approved: 'install', created_at: Math.floor(Date.now() / 1000) })),
  );
  const managed: Record<string, string[]> = {};
  const endpoints: Record<string, Record<string, string>> = {};
  const adoptedHashes: Record<string, string[]> = {};
  for (const d of docs.values())
    for (const [to, key] of Object.entries(d.old.outgoing)) {
      for (const provider of providers(to, apps)) {
        adoptedHashes[provider.manifest.id] ??= [];
        adoptedHashes[provider.manifest.id]?.push(hash(key));
      }
    }
  for (const [id, d] of docs) {
    const oldHashes = [...(previous.managed?.[id] ?? []), ...(adoptedHashes[id] ?? [])];
    d.doc.incoming = d.doc.incoming.filter((i) => !oldHashes.includes(i.sha256));
  }
  for (const g of file.grants.filter(appGrant)) {
    const consumer = docs.get(g.from);
    const dests = providers(g.to, apps).filter((p) => p.manifest.id !== g.from);
    if (!consumer || !dests.length) continue;
    const key = consumer.old.outgoing[g.to] ?? randomBytes(32).toString('hex');
    consumer.doc.outgoing[g.to] = key;
    const digest = hash(key);
    for (const provider of dests) {
      const p = docs.get(provider.manifest.id)!;
      // Retain legacy identities for their existing keys (old providers may check
      // calendar), and also publish the canonical ID for updated providers.
      for (const row of [...p.old.incoming.filter((r) => r.sha256 === digest), { from: g.from, sha256: digest }]) {
        if (!p.doc.incoming.some((r) => r.sha256 === digest && r.from === row.from)) p.doc.incoming.push(row);
      }
      managed[provider.manifest.id] ??= [];
      managed[provider.manifest.id]?.push(digest);
      const oldEndpoint = consumer.old.provider_endpoints?.[g.to];
      const ownerOverride = oldEndpoint && oldEndpoint !== previous.endpoints?.[g.from]?.[g.to];
      const endpoint = ownerOverride ? oldEndpoint : providerEndpoint(provider);
      if (endpoint) {
        consumer.doc.provider_endpoints![g.to] = endpoint;
        if (!ownerOverride) {
          endpoints[g.from] ??= {};
          endpoints[g.from]![g.to] = endpoint;
        }
      }
    }
  }
  file.seeded = [...new Set([...(file.seeded ?? []), ...apps.map((a) => a.manifest.id)])];
  // Retain the previous hash set until every provider has received revocation.
  const pending = Object.fromEntries(
    [...new Set([...Object.keys(previous.managed ?? {}), ...Object.keys(managed)])].map((id) => [
      id,
      [...new Set([...(previous.managed?.[id] ?? []), ...(managed[id] ?? [])])],
    ]),
  );
  file.layer2 = { managed: pending, adopted: [...adopted], endpoints: previous.endpoints ?? {} };
  await save(file);
  // Providers first: no consumer receives a newly minted key before its hash.
  for (const d of docs.values()) for (const p of d.paths) await atomic(p, { ...d.doc, outgoing: d.old.outgoing });
  for (const d of docs.values()) for (const p of d.paths) await atomic(p, d.doc);
  file.layer2 = { managed, adopted: [...adopted], endpoints };
  await save(file);
}
