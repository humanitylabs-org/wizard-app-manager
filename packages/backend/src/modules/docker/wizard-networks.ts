/** App networks (wam/network.py, docs/APP_NETWORKS.md), enforced where every Wizard app starts.
 *
 * wam builds the per-connection networks and writes each isolated app's user-config override. Apps are also
 * installed, updated and restarted without wam: the App Store views and Runtipi's own UI call this API directly.
 * So before every `compose up` of a Wizard app, whoever asked for it:
 *  - with isolation on, a service is never put on Runtipi's shared network unless it is published through the
 *    reverse proxy (Traefik needs it there) or the owner set its networks by hand in user-config. wam's watcher
 *    (`wam serve`) then adds the links and gateway network the app declares;
 *  - a compose that attaches networks the app does not own (an existing/external network, a fixed Docker name
 *    outside the app's project, a reserved wizard-* network, a non-bridge driver, network_mode host/bridge/container:)
 *    is refused unless the owner allowed that app (`wam network allow <id>`, `wam install <id> --allow-host-access`);
 *  - entries in wam's managed override block for services the recipe no longer has (an update renamed or removed
 *    one) are dropped, so Compose doesn't refuse the update.
 * wam mirrors its switch and the owner's allowances to user-config/wizard-app-networks.json. A missing or unreadable
 * file means isolation on, nothing allowed.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import * as yaml from 'yaml';

export const POLICY_FILE = 'wizard-app-networks.json';
export const WIZARD_STORE = 'wizardapps';
const MARK = 'x-wizard-networks';
const SHARED = 'tipi_main_network';
const OURS = /^(?:wizard-(?:app|link|gw)-[a-z0-9-]+|wizard-core)$/;
const ID = /^[a-z0-9][a-z0-9-]{0,62}$/;
const PRIVATE_PROVIDER = '# WAM private-provider internal bootstrap';

export type NetworkPolicy = { enabled: boolean; allow: string[] };
type Doc = { services?: Record<string, any>; networks?: Record<string, any>; [k: string]: unknown };

const missing = (e: NodeJS.ErrnoException) => {
  if (e.code === 'ENOENT') return null;
  throw e;
};

export async function readPolicy(dataDir: string): Promise<NetworkPolicy> {
  const raw = await fs.readFile(path.join(dataDir, 'user-config', POLICY_FILE), 'utf8').catch(missing);
  let doc: any = null;
  try {
    doc = raw ? JSON.parse(raw) : null;
  } catch {
    doc = null; // unreadable: fail closed
  }
  const allow = Array.isArray(doc?.allow_host_access) ? doc.allow_host_access : [];
  return { enabled: doc?.enabled !== false, allow: allow.filter((x: unknown) => typeof x === 'string' && ID.test(x)) };
}

/** Networks a compose attaches that the app does not own. Same rule as wam/network.py network_escapes(). */
export function networkEscapes(doc: Doc, app: string, store: string = WIZARD_STORE): string[] {
  const out: string[] = [];
  const own = `${app}_${store}_`;
  for (const [key, raw] of Object.entries(doc.networks ?? {})) {
    if (key === SHARED) continue; // Runtipi's own entry, written by the compose builder
    const d = raw && typeof raw === 'object' ? (raw as Record<string, any>) : {};
    const ext = d.external;
    const name = String(d.name ?? (ext && typeof ext === 'object' ? (ext.name ?? '') : ''));
    if (OURS.test(name) || OURS.test(key)) out.push(`network ${key}: the reserved Wizard network ${name || key}`);
    else if (ext) out.push(`network ${key}: the existing network ${name || key}, which this app does not own`);
    else if (name && !name.startsWith(own)) out.push(`network ${key}: the fixed Docker name ${name}, outside this app`);
    if (d.driver && String(d.driver) !== 'bridge') out.push(`network ${key}: driver ${String(d.driver)}`);
  }
  for (const [name, s] of Object.entries(doc.services ?? {})) {
    const mode = String(s?.network_mode ?? '').toLowerCase();
    if (mode === 'host' || mode === 'bridge' || mode === 'default' || mode.startsWith('container:')) out.push(`${name}: network_mode ${mode}`);
  }
  return out;
}

const exposed = (s: any) => {
  const labels = s?.labels;
  const v = Array.isArray(labels)
    ? labels
        .map(String)
        .find((l) => l.startsWith('traefik.enable='))
        ?.split('=')[1]
    : labels && typeof labels === 'object'
      ? labels['traefik.enable']
      : undefined;
  return String(v ?? '').toLowerCase() === 'true';
};

/** Services whose networks the owner set by hand in user-config (wam leaves those apps on their own settings). */
export function handSet(override: string | null): Set<string> | 'all' {
  if (!override) return new Set();
  if (override.startsWith(PRIVATE_PROVIDER)) return 'all';
  let doc: any;
  try {
    doc = yaml.parseDocument(override).toJS() ?? {};
  } catch {
    return 'all'; // unparseable: Compose fails on it anyway; don't guess
  }
  const mine = new Set<string>(Array.isArray(doc?.[MARK]?.services) ? doc[MARK].services : []);
  const out = new Set<string>();
  for (const [name, s] of Object.entries((doc?.services ?? {}) as Record<string, any>))
    if (!mine.has(name) && s && typeof s === 'object' && ('networks' in s || 'network_mode' in s)) out.add(name);
  return out;
}

/** The generated compose as it may start. Throws when it attaches networks the app does not own (not allowed). */
/** Host access in a compose: privileged mode, the container runtime socket. Same rule as wam/network.py host_access()
 * (the generated compose's own app-data binds are not host access, so this is narrower than the index checker). */
export function hostRisks(doc: Doc): string[] {
  const out: string[] = [];
  for (const [name, s] of Object.entries(doc.services ?? {})) {
    if (!s || typeof s !== 'object') continue;
    if (s.privileged === true || String(s.privileged).toLowerCase() === 'true') out.push(`${name}: privileged`);
    for (const v of Array.isArray(s.volumes) ? s.volumes : []) {
      const src = typeof v === 'string' ? v.split(':')[0] : String(v?.source ?? '');
      if (/docker\.sock|containerd\.sock|podman\.sock/.test(src ?? '')) out.push(`${name}: mounts the container runtime socket ${src}`);
    }
  }
  return out;
}

export function isolate(doc: Doc, app: string, policy: NetworkPolicy, override: string | null, store = WIZARD_STORE): boolean {
  if (!policy.enabled || policy.allow.includes(app)) return false; // off, or the owner's call for this app
  const escapes = [...networkEscapes(doc, app, store), ...hostRisks(doc)];
  if (escapes.length)
    throw new Error(
      `${app} refused: it asks for host access or networks it does not own, which would reach other apps (${escapes.join('; ')}). ` +
        `If you trust it completely: wam network allow ${app}`,
    );
  const hand = handSet(override);
  if (hand === 'all') return false;
  let changed = false;
  for (const [name, s] of Object.entries(doc.services ?? {})) {
    if (!s || typeof s !== 'object' || exposed(s) || hand.has(name)) continue;
    const nets = s.networks;
    if (Array.isArray(nets) && nets.includes(SHARED)) {
      s.networks = nets.filter((n: string) => n !== SHARED);
      changed = true;
    } else if (nets && typeof nets === 'object' && SHARED in nets) {
      delete nets[SHARED];
      changed = true;
    } else continue;
    if (!Object.keys(s.networks).length) delete s.networks; // Compose's own per-app default network
  }
  return changed;
}

/** Drop wam's managed override entries for services the compose no longer has. Returns the dropped names. */
export async function pruneStaleOverride(file: string, services: string[]): Promise<string[]> {
  const text = await fs.readFile(file, 'utf8').catch(missing);
  if (!text?.includes(MARK)) return [];
  const doc = yaml.parseDocument(text); // keeps comments and the !override / !reset tags
  const listed = doc.getIn([MARK, 'services']);
  const names: string[] = yaml.isSeq(listed) ? (listed.toJSON() as string[]) : [];
  const stale = names.filter((n) => !services.includes(n));
  if (!stale.length) return [];
  for (const n of stale) doc.deleteIn(['services', n]);
  doc.setIn([MARK, 'services'], doc.createNode(names.filter((n) => !stale.includes(n))));
  const svcs = doc.get('services');
  if (yaml.isMap(svcs) && !svcs.items.length) doc.delete('services');
  const st = await fs.stat(file);
  await fs.writeFile(`${file}.tmp-networks`, doc.toString(), { mode: st.mode & 0o777 });
  await fs.rename(`${file}.tmp-networks`, file);
  return stale;
}

/** Apply the network policy to one app's generated compose right before `compose up`. */
export async function enforceAppNetworks(urn: string, generated: string, dataDir: string): Promise<string> {
  const [app, store] = urn.split(':');
  if (store !== WIZARD_STORE || !app || !ID.test(app)) return generated;
  const doc = (yaml.parse(generated) ?? {}) as Doc;
  const overrideFile = path.join(dataDir, 'user-config', store, app, 'docker-compose.yml');
  await pruneStaleOverride(overrideFile, Object.keys(doc.services ?? {}));
  const override = await fs.readFile(overrideFile, 'utf8').catch(missing);
  return isolate(doc, app, await readPolicy(dataDir), override, store) ? yaml.stringify(doc) : generated;
}
