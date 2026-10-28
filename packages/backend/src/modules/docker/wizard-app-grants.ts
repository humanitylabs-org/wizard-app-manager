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
    .filter((g) => providers(g.to, apps).length && ![...file.grants, ...file.denied, ...pendingOf(file)].some((old) => sameAppGrant(old, g)));
}

// ---- Connection policy (owner-controllable, seamless by default) -------------------------------------------------
// Declared connections are approved at install and when an update adds them, with a quiet notice for the owner.
// An app may also ask for a connection at runtime (its own /wizard-requests/connections.json): approved the same way.
// The owner's Disconnect (a `denied` row) is sticky: no update or request adds it again. A connection TO an app the
// owner marked sensitive is never approved automatically: it waits in `pending` until the owner approves or denies.
// Only the owner changes `denied`, `sensitive` and `pending` decisions (the AuthGuard'ed App Manager API / wam).
export type Notice = { id: string; at: number; kind: 'added' | 'requested' | 'pending'; from: string; to: string; text: string; delivered?: boolean };
export const REQUESTS_DIR = 'wizard-requests';
export const REQUESTS_FILE = 'connections.json';
const NOTICES_KEPT = 50;
const TARGET = /^(slot:)?[a-z0-9][a-z0-9-]{1,40}$/;
export const pendingOf = (file: Grants): Grant[] => (Array.isArray(file.pending) ? (file.pending as Grant[]) : []);
export const sensitiveOf = (file: Grants): string[] =>
  Array.isArray(file.sensitive) ? (file.sensitive as unknown[]).filter((x): x is string => typeof x === 'string' && ID.test(x)) : [];
export const noticesOf = (file: Grants): Notice[] => (Array.isArray(file.notices) ? (file.notices as Notice[]) : []);
/** The owner's connection mode. Open (the default): every app reaches every app except the pairs the owner disconnected
 * and unapproved connections to sensitive apps (wam/network.py open_topology). Declared: only declared / approved ones. */
export type ConnectionMode = 'open' | 'declared';
export const modeOf = (file: Grants): ConnectionMode => (file.mode === 'declared' ? 'declared' : 'open');
/** Open mode: marking an app sensitive keeps the connections it has (every installed app reaches it). Each becomes an
 * `approved: open` grant, which only keeps that pair open: it never links in declared mode and carries no key. */
export function keepOpenConnections(file: Grants, apps: App[], sensitive: string, now = Math.floor(Date.now() / 1000)) {
  if (modeOf(file) !== 'open') return;
  const rows = [...file.grants, ...file.denied, ...pendingOf(file)];
  for (const a of apps) {
    const from = a.manifest.id;
    if (from === sensitive || !a.urn.endsWith(':wizardapps')) continue;
    if (rows.some((g) => appGrant(g) && g.from === from && providers(g.to, apps).some((p) => p.manifest.id === sensitive))) continue;
    file.grants.push({ kind: 'app', from, to: sensitive, reason: 'open', approved: 'open', created_at: now });
  }
}
const nameOf = (id: string, apps: App[]) => {
  const a = apps.find((x) => x.manifest.id === id);
  return (a?.manifest as { name?: string } | undefined)?.name || id;
};
const targetName = (to: string, apps: App[]) => {
  const ps = providers(to, apps);
  return ps.length ? ps.map((p) => nameOf(p.manifest.id, apps)).join(', ') : to.replace(/^slot:/, '');
};
export function addNotice(file: Grants, apps: App[], kind: Notice['kind'], from: string, to: string, now = Math.floor(Date.now() / 1000)) {
  const a = nameOf(from, apps);
  const b = targetName(to, apps);
  const outbound =
    kind === 'pending' && sensitiveOf(file).includes(from) && !providers(to, apps).some((p) => sensitiveOf(file).includes(p.manifest.id));
  const text =
    kind !== 'pending'
      ? `${a} now connects to ${b}.`
      : outbound
        ? `${a}, which you marked sensitive, wants to connect to ${b}. Approve or deny it in App Manager → Connections.`
        : `${a} wants to connect to ${b}, which you marked sensitive. Approve or deny it in App Manager → Connections.`;
  const n: Notice = { id: randomBytes(6).toString('hex'), at: now, kind, from, to, text, delivered: false };
  file.notices = [...noticesOf(file), n].slice(-NOTICES_KEPT);
  return n;
}
/** A connection request an app left in its own requests folder. Read without following links, size-capped; only
 * well-formed ids/slots survive. The folder is the app's own (only its containers mount it), so `from` is the app. */
export async function readRequests(app: App): Promise<string[]> {
  const dir = path.join(app.data, REQUESTS_DIR);
  const st = await fs.lstat(dir).catch(() => null);
  if (!st?.isDirectory() || st.isSymbolicLink()) return [];
  let fh: fs.FileHandle | undefined;
  try {
    fh = await fs.open(path.join(dir, REQUESTS_FILE), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const fst = await fh.stat();
    if (!fst.isFile() || fst.size > 16384) return [];
    const doc = JSON.parse(await fh.readFile('utf8'));
    const list = Array.isArray(doc) ? doc : Array.isArray(doc?.connections) ? doc.connections : [];
    const out = list
      .slice(0, 32)
      .map((x: unknown) => (typeof x === 'string' ? x : typeof (x as { to?: unknown })?.to === 'string' ? (x as { to: string }).to : ''))
      .filter((to: string) => TARGET.test(to) && to.replace(/^slot:/, '') !== app.manifest.id);
    return [...new Set<string>(out)];
  } catch {
    return [];
  } finally {
    await fh?.close();
  }
}
/** A requested target, named as a grant is (targets()): an installed app id, else a slot. */
const requestTarget = (to: string, ids: Set<string>) => (to.startsWith('slot:') || ids.has(to) ? to : `slot:${to}`);
const sensitiveTarget = (to: string, apps: App[], sensitive: string[]) => providers(to, apps).some((p) => sensitive.includes(p.manifest.id));
/** Apply the owner's policy to everything new. `requests`: runtime requests by consumer id. `seededBefore`: apps the
 * App Manager had already seen (a declaration that appears for one of them came with an update: tell the owner). */
export function applyConnectionPolicy(
  apps: App[],
  file: Grants,
  requests: Map<string, string[]>,
  seededBefore: Set<string>,
  now = Math.floor(Date.now() / 1000),
) {
  const sensitive = sensitiveOf(file);
  file.sensitive = sensitive;
  file.pending = pendingOf(file);
  const declared = new Set(apps.flatMap((a) => targets(a, apps)).map((g) => `${g.from}\0${g.to}`));
  const ids = new Set(apps.map((a) => a.manifest.id));
  // An automatic grant follows its declaration: when an update drops it, the grant goes, so declaring it again later is
  // a new connection the owner hears about. Grants of apps outside this reconcile (being removed) are kept.
  file.grants = file.grants.filter((g) => !(appGrant(g) && g.approved === 'install' && ids.has(g.from) && !declared.has(`${g.from}\0${g.to}`)));
  // A pending request lapses when its declaration (or runtime request) is gone; when the provider is no longer
  // sensitive it is approved like any other new connection.
  const asked = new Set([...requests].flatMap(([from, tos]) => tos.map((to) => `${from}\0${requestTarget(to, ids)}`)));
  const keep: Grant[] = [];
  const open = modeOf(file) === 'open';
  // Open-mode grants keep a sensitive app's existing connections; once it isn't sensitive they mean nothing.
  file.grants = file.grants.filter((g) => !(appGrant(g) && g.approved === 'open' && !sensitiveTarget(g.to, apps, sensitive)));
  const reaches = (from: string, to: string) => (x: Grant) =>
    appGrant(x) && x.from === from && providers(x.to, apps).some((q) => q.manifest.id === to);
  // Open mode: a connection the app already has (an `approved: open` grant kept when its provider was marked sensitive)
  // is not new, so declaring or asking for it never waits; it gets an ordinary grant (and its key) instead.
  const alreadyOpen = (g: Grant) =>
    open && providers(g.to, apps).some((p) => file.grants.some((x) => x.approved === 'open' && reaches(g.from, p.manifest.id)(x)));
  // Open mode: networks are two-way, so a sensitive app's own new connection to an app that may not reach it yet is a
  // new connection TO the sensitive app as well: it waits for the owner (Approve stores an owner grant, which opens it).
  const outboundFromSensitive = (g: Grant) =>
    open &&
    sensitive.includes(g.from) &&
    providers(g.to, apps).some(
      (p) =>
        !sensitive.includes(p.manifest.id) &&
        !file.grants.some((x) => reaches(p.manifest.id, g.from)(x) || (x.approved === 'owner' && reaches(g.from, p.manifest.id)(x))),
    );
  const needsOwner = (g: Grant) => (sensitiveTarget(g.to, apps, sensitive) && !alreadyOpen(g)) || outboundFromSensitive(g);
  for (const g of file.pending as Grant[]) {
    const k = `${g.from}\0${g.to}`;
    if (
      !ids.has(g.from) ||
      !providers(g.to, apps).length ||
      !(declared.has(k) || asked.has(k) || (open && g.reason === 'open')) ||
      file.denied.some((d) => sameAppGrant(d, g)) ||
      file.grants.some((x) => sameAppGrant(x, g))
    )
      continue;
    if (needsOwner(g)) keep.push(g);
    else if (g.reason === 'open')
      continue; // open mode connects it anyway: nothing to approve
    else {
      file.grants.push({
        kind: 'app',
        from: g.from,
        to: g.to,
        reason: g.reason,
        approved: g.reason === 'request' ? 'request' : 'install',
        created_at: now,
      });
      addNotice(file, apps, g.reason === 'request' ? 'requested' : 'added', g.from, g.to, now);
    }
  }
  file.pending = keep;
  // The owner's Disconnect / Deny is about the app it reaches, however it is named: a request by slot can't bring
  // back a provider the owner disconnected by id (or the other way round).
  const deniedTo = (from: string) =>
    new Set(file.denied.filter((d) => appGrant(d) && d.from === from).flatMap((d) => providers(d.to, apps).map((p) => p.manifest.id)));
  const blocked = (g: Grant) => providers(g.to, apps).some((p) => deniedTo(g.from).has(p.manifest.id));
  const consider = (g: Grant, approved: 'install' | 'request', quiet: boolean) => {
    if (!providers(g.to, apps).some((p) => p.manifest.id !== g.from)) return;
    if ([...file.grants.filter((x) => x.approved !== 'open'), ...file.denied, ...(file.pending as Grant[])].some((old) => sameAppGrant(old, g)))
      return;
    if (blocked(g)) return;
    if (needsOwner(g)) {
      (file.pending as Grant[]).push({ kind: 'app', from: g.from, to: g.to, reason: g.reason, created_at: now });
      addNotice(file, apps, 'pending', g.from, g.to, now);
      return;
    }
    file.grants.push({ kind: 'app', from: g.from, to: g.to, reason: g.reason, approved, created_at: now });
    if (!quiet) addNotice(file, apps, approved === 'request' ? 'requested' : 'added', g.from, g.to, now);
  };
  for (const a of apps) for (const g of targets(a, apps)) consider(g, 'install', !seededBefore.has(a.manifest.id));
  for (const [from, tos] of requests) {
    if (!ids.has(from)) continue;
    for (const to of tos) {
      const g: Grant = { kind: 'app', from, to: requestTarget(to, ids), reason: 'request' };
      consider(g, 'request', false);
    }
  }
  // Open mode: a newly installed app would reach every app, so its connection to each sensitive app is new and waits
  // for the owner, unless a decision about that pair already exists.
  if (open)
    for (const a of apps) {
      const from = a.manifest.id;
      if (seededBefore.has(from)) continue;
      for (const s of sensitive) {
        if (s === from || !ids.has(s)) continue;
        const rows = [...file.grants, ...file.denied, ...(file.pending as Grant[])];
        if (rows.some((g) => appGrant(g) && g.from === from && providers(g.to, apps).some((p) => p.manifest.id === s))) continue;
        (file.pending as Grant[]).push({ kind: 'app', from, to: s, reason: 'open', created_at: now });
        addNotice(file, apps, 'pending', from, s, now);
      }
    }
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
    if ((svc.volumes ?? []).some((v: any) => target(v) === '/wizard-requests' && source(v) !== '${APP_DATA_DIR}/wizard-requests'))
      throw new Error('Reserved connection requests mount /wizard-requests');
    svc.volumes = [
      ...(svc.volumes ?? []).filter((v: any) => target(v) !== '/wizard' && target(v) !== '/wizard-requests'),
      '${APP_DATA_DIR}/wizard:/wizard:ro',
      // The app's only way to ask for a new connection at runtime: it writes connections.json here (see
      // applyConnectionPolicy). The folder is the app's own, so the App Manager knows who asked.
      '${APP_DATA_DIR}/wizard-requests:/wizard-requests',
    ];
    const env = { WIZARD_GRANTS_FILE: '/wizard/grants.json', WIZARD_CONNECTION_REQUESTS: '/wizard-requests/connections.json' };
    if (Array.isArray(svc.environment)) {
      svc.environment = [
        ...svc.environment.filter((e: string) => !Object.keys(env).some((k) => e.startsWith(`${k}=`))),
        ...Object.entries(env).map(([k, v]) => `${k}=${v}`),
      ];
    } else svc.environment = { ...svc.environment, ...env };
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
        if ((ID.test(to) || /^slot:[a-z0-9][a-z0-9-]{1,40}$/.test(to)) && ![...file.grants, ...file.denied].some((x) => sameAppGrant(x, g)))
          file.grants.push(g);
      }
      adopted.add(app.manifest.id);
    }
  }
  const requests = new Map<string, string[]>();
  for (const app of apps) {
    // The app's own requests folder (mounted read-write at /wizard-requests): where it may ask for a connection.
    await confined(app.data, REQUESTS_DIR, true);
    await fs.chmod(path.join(app.data, REQUESTS_DIR), 0o777);
    const asked = await readRequests(app);
    if (asked.length) requests.set(app.manifest.id, asked);
  }
  applyConnectionPolicy(apps, file, requests, new Set(file.seeded ?? []));
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
  // Open-mode grants only keep a network path open; they mint no app-to-app key.
  for (const g of file.grants.filter((x) => appGrant(x) && x.approved !== 'open')) {
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
