/**
 * Wizard app-to-app grants: pure logic (no Docker, no files). Spec: wizard-os spec/PERMISSIONS.md.
 *
 * A grant lets a consumer app call a provider app (or any app filling a slot). With WIZARD_APP_ISOLATION=1 each
 * Wizard app's main services live on their own network `wizard-app-<id>`, and a granted consumer's containers are
 * connected to the provider's network. Off by default: nothing here runs and compose files are unchanged.
 */
import * as yaml from 'yaml';

export const WIZARD_STORE = 'wizardapps';
export const GRANTS_SCHEMA = 1;

export const isWizardIsolationEnabled = (env: NodeJS.ProcessEnv = process.env) => env.WIZARD_APP_ISOLATION === '1';
export const wizardAppNetworkName = (appName: string) => `wizard-app-${appName}`;
export const isWizardStore = (appStoreId: string) => appStoreId === WIZARD_STORE;

export type WizardAppInfo = {
  id: string;
  type?: string;
  category?: string;
  provides: string[];
  uses: string[];
  hasMcp: boolean;
};

export type Grant = {
  from: string;
  /** an app id, or `slot:<slot>` for every installed app that provides the slot */
  to: string;
  reason: 'uses' | 'agent' | 'owner';
  approved: 'default' | 'owner';
  created_at: number;
};

export type GrantsFile = {
  schema: number;
  updated_at: number;
  /** apps whose default grants were already proposed (so a revoked default is not re-added) */
  seeded: string[];
  grants: Grant[];
  denied: { from: string; to: string }[];
};

export const emptyGrantsFile = (now: number): GrantsFile => ({ schema: GRANTS_SCHEMA, updated_at: now, seeded: [], grants: [], denied: [] });

const strList = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []);

export const parseGrantsFile = (text: string | null, now: number): GrantsFile => {
  if (!text) return emptyGrantsFile(now);
  const raw = JSON.parse(text) as Partial<GrantsFile>;
  return {
    schema: GRANTS_SCHEMA,
    updated_at: Number(raw.updated_at) || now,
    seeded: strList(raw.seeded),
    grants: (Array.isArray(raw.grants) ? raw.grants : []).filter((g) => g && typeof g.from === 'string' && typeof g.to === 'string'),
    denied: (Array.isArray(raw.denied) ? raw.denied : []).filter((d) => d && typeof d.from === 'string' && typeof d.to === 'string'),
  };
};

/** Read the fields grants need from a wizard.md (front matter only). */
export const parseWizardMd = (text: string): WizardAppInfo | null => {
  const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
  if (!m?.[1]) return null;
  const d = (yaml.parse(m[1]) ?? {}) as Record<string, unknown>;
  if (typeof d.id !== 'string') return null;
  return {
    id: d.id,
    type: typeof d.type === 'string' ? d.type : undefined,
    category: typeof d.category === 'string' ? d.category : undefined,
    provides: strList(d.provides),
    uses: strList(d.uses),
    hasMcp: !!d.mcp && typeof d.mcp === 'object',
  };
};

export const isAgentApp = (a: WizardAppInfo) => a.category === 'agent' || (!a.category && a.type === 'agent');

/** Provider app ids a grant target points at. `slot:x` matches apps providing x, or an app whose id is x. */
export const resolveTarget = (to: string, apps: WizardAppInfo[]): string[] => {
  if (to.startsWith('slot:')) {
    const slot = to.slice(5);
    return apps.filter((a) => a.provides.includes(slot) || a.id === slot).map((a) => a.id);
  }
  return apps.some((a) => a.id === to) ? [to] : [];
};

/**
 * Default grants for one app, given everything installed: agent→app first (an agent gets every app with an MCP;
 * a new app with an MCP is granted to every agent), then the app's own `uses:` (an installed app id, else a slot).
 */
export const defaultGrantsFor = (app: WizardAppInfo, apps: WizardAppInfo[]): Pick<Grant, 'from' | 'to' | 'reason'>[] => {
  const out: Pick<Grant, 'from' | 'to' | 'reason'>[] = [];
  const others = apps.filter((a) => a.id !== app.id);
  if (isAgentApp(app)) for (const p of others) if (p.hasMcp) out.push({ from: app.id, to: p.id, reason: 'agent' });
  if (app.hasMcp) for (const agent of others) if (isAgentApp(agent)) out.push({ from: agent.id, to: app.id, reason: 'agent' });
  for (const u of app.uses) {
    if (u === app.id) continue;
    out.push({ from: app.id, to: others.some((a) => a.id === u) ? u : `slot:${u}`, reason: 'uses' });
  }
  return out;
};

/** Add default grants for installed apps not seeded yet. Never re-adds a denied pair or duplicates a grant. */
export const seedDefaults = (file: GrantsFile, apps: WizardAppInfo[], now: number): { file: GrantsFile; changed: boolean } => {
  const fresh = apps.filter((a) => !file.seeded.includes(a.id));
  if (!fresh.length) return { file, changed: false };
  const key = (g: { from: string; to: string }) => `${g.from}\u0000${g.to}`;
  const have = new Set(file.grants.map(key));
  const denied = new Set(file.denied.map(key));
  const grants = [...file.grants];
  for (const app of fresh) {
    for (const g of defaultGrantsFor(app, apps)) {
      if (have.has(key(g)) || denied.has(key(g))) continue;
      have.add(key(g));
      grants.push({ ...g, approved: 'default', created_at: now });
    }
  }
  return { file: { ...file, updated_at: now, grants, seeded: [...file.seeded, ...fresh.map((a) => a.id)].sort() }, changed: true };
};

/** provider id -> consumer ids allowed to reach it */
export const allowedConsumers = (grants: Grant[], apps: WizardAppInfo[]): Map<string, Set<string>> => {
  const ids = new Set(apps.map((a) => a.id));
  const out = new Map<string, Set<string>>();
  for (const g of grants) {
    if (!ids.has(g.from)) continue;
    for (const p of resolveTarget(g.to, apps)) {
      if (p === g.from) continue;
      if (!out.has(p)) out.set(p, new Set());
      out.get(p)?.add(g.from);
    }
  }
  return out;
};

export type ContainerView = { id: string; appUrn?: string; networks: string[] };
export type NetworkChange = { network: string; containerId: string; consumer: string };

const appOfUrn = (urn?: string) => {
  if (!urn) return null;
  const [name, store] = urn.split(':');
  return name && store === WIZARD_STORE ? name : null;
};

/**
 * What to connect/disconnect so each provider network holds the provider plus its granted consumers.
 * Only a consumer's "front" containers are connected: those on the consumer's own `wizard-app-<id>` network
 * (its main and add_to_main_network services), never its private databases. Only Runtipi-managed containers
 * are ever disconnected; providers whose network doesn't exist yet (`existingNetworks`) are skipped.
 */
export const planNetworkChanges = (
  apps: WizardAppInfo[],
  grants: Grant[],
  containers: ContainerView[],
  existingNetworks: Set<string>,
): { connect: NetworkChange[]; disconnect: NetworkChange[] } => {
  const allowed = allowedConsumers(grants, apps);
  const connect: NetworkChange[] = [];
  const disconnect: NetworkChange[] = [];
  for (const provider of apps) {
    const network = wizardAppNetworkName(provider.id);
    if (!existingNetworks.has(network)) continue;
    const ok = allowed.get(provider.id) ?? new Set<string>();
    for (const c of containers) {
      const consumer = appOfUrn(c.appUrn);
      const onIt = c.networks.includes(network);
      if (consumer === provider.id) continue;
      const wanted = !!consumer && ok.has(consumer) && c.networks.includes(wizardAppNetworkName(consumer));
      if (wanted && !onIt) connect.push({ network, containerId: c.id, consumer });
      if (!wanted && onIt && c.appUrn) disconnect.push({ network, containerId: c.id, consumer: consumer ?? c.appUrn });
    }
  }
  return { connect, disconnect };
};
