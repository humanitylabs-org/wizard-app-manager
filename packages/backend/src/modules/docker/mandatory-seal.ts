import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { confined, containerPath, rewriteVolumes, targetOf, type Compose } from './wizard-storage';

const POLICY = 'tinfoil-sealed-v1';
const LABEL = 'wizard.private-network-policy';
const SECTIONS = ['services', 'networks', 'volumes', 'configs', 'secrets'] as const;
const RUNTIME = ['image', 'command', 'entrypoint'] as const;
const fail = () => new Error('Mandatory sealed launch policy invalid; refusing lifecycle mutation');
export const sealHash = (value: string) => createHash('sha256').update(value).digest('hex');
const canonical = (v: any): string => JSON.stringify(v, (_key, value) =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map((k) => [k, value[k]])) : value);
const literal = (v: unknown) => JSON.stringify(v).replaceAll('$', () => '$$');

/** Trusted compiler output only. Never infer enrollment from catalogue/user-config. */
export function validateSeal(doc: Compose, urn: string) {
  const main = urn.split(':')[0]!;
  if (!/^[a-z0-9][a-z0-9-]*:[a-z0-9][a-z0-9-]*$/.test(urn)) throw fail();
  if (doc?.services?.[main]?.labels?.[LABEL] !== POLICY) throw fail();
  const expected: Record<string, string[]> = {
    [main]: [`${main}-seal`],
    [`${main}-edge`]: [`${main}-seal`, `${main}-provider`, `${main}-edge-uplink`],
    [`${main}-tinfoil`]: [`${main}-provider`, `${main}-keyed`],
    [`${main}-search`]: [`${main}-provider`, `${main}-keyed`],
    [`${main}-key`]: [`${main}-seal`, `${main}-keyed`],
    [`${main}-gateway`]: [`${main}-seal`],
  };
  for (const name of [main, `${main}-edge`, `${main}-tinfoil`, `${main}-search`, `${main}-key`])
    if (!doc.services[name]) throw fail();
  for (const [name, service] of Object.entries(doc.services)) {
    if (!expected[name] || service.network_mode || service.privileged || service.pid === 'host' || service.ipc === 'host' || service.devices?.length) throw fail();
    if (canonical(Object.keys(service.networks ?? {}).sort()) !== canonical(expected[name].sort())) throw fail();
    if (service.restart !== 'no') throw fail();
    if (name !== `${main}-edge` && service.ports?.length) throw fail();
    if (name === `${main}-edge` && (service.ports ?? []).some((p: any) => typeof p !== 'string' || !p.startsWith('127.0.0.1:'))) throw fail();
  }
  const client = doc.services[main];
  if (!client.cap_drop?.includes('ALL') || !client.security_opt?.includes('no-new-privileges:true') ||
      (client.cap_add ?? []).some((c: string) => !['CHOWN', 'SETUID', 'SETGID', 'DAC_OVERRIDE'].includes(c))) throw fail();
  if (canonical(Object.keys(doc.networks ?? {}).sort()) !== canonical([`${main}-seal`, `${main}-provider`, `${main}-keyed`, `${main}-edge-uplink`].sort())) throw fail();
  for (const [name, network] of Object.entries(doc.networks) as [string, any][]) {
    if (network.external || network.name || network.driver) throw fail();
    if (name !== `${main}-edge-uplink`) {
      if (network.internal !== true || network.labels?.[LABEL] !== POLICY ||
          canonical(network.driver_opts ?? {}) !== canonical({ 'com.docker.network.bridge.inhibit_ipv4': 'true' })) throw fail();
    } else if (network.internal !== false || network.driver_opts) throw fail();
  }
  if ((doc['x-wizard-share-mounts'] ?? []).some((p: unknown) => !containerPath(p))) throw fail();
}

type Definition = { compose: Compose; catalogue: Compose; sha256: string };
export type SealRecord = Definition & { schema: 2; urn: string; policy: string; previous?: Definition };
const definition = (compose: Compose, catalogue: Compose): Definition => ({ compose, catalogue, sha256: sealHash(canonical({ compose, catalogue })) });
const validatePair = (compose: Compose, catalogue: Compose, urn: string) => {
  validateSeal(compose, urn);
  if (canonical(Object.keys(compose.services).sort()) !== canonical(Object.keys(catalogue.services).sort())) throw fail();
  for (const [name, svc] of Object.entries(compose.services)) for (const key of RUNTIME)
    if (canonical(svc[key] ?? null) !== canonical(catalogue.services[name][key] ?? null)) throw fail();
};
// A revision may change executable fields, never networking, key paths, mounts,
// environment (including public placeholder), capabilities, or socket topology.
const envelope = (doc: Compose) => {
  const copy = structuredClone(doc);
  for (const svc of Object.values(copy.services)) for (const key of RUNTIME) delete svc[key];
  return canonical(copy);
};

/** Manager-owned state outside app backup/restore. Existing directory + missing
 * policy is corrupt enrollment, not an ordinary app. No HTTP enrollment route.
 */
export class MandatorySeal {
  constructor(readonly dataDir: string) {}
  private async enrollment(urn: string, create = false) {
    return confined(this.dataDir, `state/mandatory-seals/${sealHash(urn)}`, create);
  }
  /** Atomic mkdir exclusion is shared by daemon instances and the trusted CLI.
   * Contention fails closed rather than waiting on a stale captured decision.
   * Crash leaves a fail-closed fence; only a stopped-manager administrator may
   * remove it after verifying no enrollment/launch owner remains. Never auto-steal.
   */
  async exclusive<T>(urn: string, apply: () => Promise<T>): Promise<T> {
    const parent = await confined(this.dataDir, 'state/mandatory-seal-locks', true);
    const lock = path.join(parent, sealHash(urn));
    await fs.mkdir(lock, { mode: 0o700 });
    try { return await apply(); }
    finally { await fs.rmdir(lock); }
  }
  async exists(urn: string): Promise<boolean> {
    // Remediation needs only sticky presence, never a valid/decodable policy.
    try { await fs.lstat(await this.enrollment(urn)); return true; }
    catch (e: any) { if (e.code === 'ENOENT') return false; throw fail(); }
  }
  async read(urn: string): Promise<SealRecord | null> {
    const dir = await this.enrollment(urn);
    const st = await fs.lstat(dir).catch((e) => { if (e.code === 'ENOENT') return null; throw fail(); });
    if (!st) return null;
    try {
      if (st.uid !== process.getuid?.() || st.mode & 0o022) throw fail();
      const file = path.join(dir, 'policy.json');
      const s = await fs.lstat(file);
      if (!s.isFile() || s.isSymbolicLink() || s.uid !== process.getuid?.() || s.mode & 0o077) throw fail();
      const record: SealRecord = JSON.parse(await fs.readFile(file, 'utf8'));
      if (record.schema !== 2 || record.urn !== urn || record.policy !== POLICY) throw fail();
      for (const d of [record, ...(record.previous ? [record.previous] : [])]) {
        if (d.sha256 !== definition(d.compose, d.catalogue).sha256) throw fail();
        validatePair(d.compose, d.catalogue, urn);
      }
      if (record.previous && (envelope(record.compose) !== envelope(record.previous.compose) || envelope(record.catalogue) !== envelope(record.previous.catalogue))) throw fail();
      return record;
    } catch { throw fail(); }
  }
  async enroll(urn: string, compose: Compose, catalogue: Compose = compose) {
    return this.exclusive(urn, async () => {
      validatePair(compose, catalogue, urn);
      const proposed = definition(compose, catalogue);
      const existing = await this.read(urn);
      if (existing) {
        if (existing.sha256 !== proposed.sha256) throw fail();
        return existing;
      }
      const dir = await this.enrollment(urn, true);
      const record: SealRecord = { schema: 2, urn, policy: POLICY, ...proposed };
      await fs.writeFile(path.join(dir, 'policy.json'), `${JSON.stringify(record)}\n`, { flag: 'wx', mode: 0o600 });
      return record;
    });
  }
  /** Trusted reviewed pair, CAS-bound to current revision. Never evict a retained
   * rollback anchor: staging is not evidence that the candidate was installed.
   * A second distinct revision requires a separately implemented trusted installed
   * transition under this fence; none is inferred from mutable generated files.
   */
  async stage(urn: string, expected: string, compose: Compose, catalogue: Compose) {
    return this.exclusive(urn, async () => {
      const prior = await this.read(urn);
      if (!prior || prior.sha256 !== expected) throw fail();
      validatePair(compose, catalogue, urn);
      if (envelope(compose) !== envelope(prior.compose) || envelope(catalogue) !== envelope(prior.catalogue)) throw fail();
      const proposed = definition(compose, catalogue);
      if (proposed.sha256 === prior.sha256) return prior; // byte-preserving no-op, including previous
      // No authoritative installed-state transition exists yet. Refuse rather
      // than treating latest staged as installed and evicting a live v1 anchor.
      if (prior.previous) throw fail();
      const record: SealRecord = { schema: 2, urn, policy: POLICY, ...proposed,
        previous: definition(prior.compose, prior.catalogue) };
      const dir = await this.enrollment(urn);
      const tmp = path.join(dir, 'policy.json.next');
      await fs.writeFile(tmp, `${JSON.stringify(record)}\n`, { flag: 'wx', mode: 0o600 });
      await fs.rename(tmp, path.join(dir, 'policy.json'));
      return record;
    });
  }
  /** Compare the entire requested catalogue, not only its image. Storage rewrites
   * and current declared share targets are the sole normalization exceptions.
   * Unsupported environment/mount/other requested changes refuse truthfully.
   */
  select(record: SealRecord, generated: Compose, managedTargets: string[]) {
    // New declared grants may extend authorization, but may not replace or hide
    // a required home/key/static bind. Previously compiled managed targets are
    // explicitly dynamic; all other approved bind destinations stay protected.
    const dynamic: string[] = record.compose['x-wizard-share-mounts'] ?? [];
    for (const svc of Object.values(record.compose.services)) for (const mount of svc.volumes ?? []) {
      const target = targetOf(mount);
      if (target && !dynamic.includes(target) && managedTargets.some((m) => m === target || m.startsWith(`${target}/`) || target.startsWith(`${m}/`))) throw fail();
    }
    const normalize = (input: Compose, approved: Compose) => {
      const copy = rewriteVolumes(input, record.urn.split(':')[0]!);
      const targets = new Set([...managedTargets, ...(approved['x-wizard-share-mounts'] ?? [])]);
      for (const svc of Object.values(copy.services)) svc.volumes = (svc.volumes ?? []).filter((v: any) => !targets.has(targetOf(v)));
      for (const key of ['x-wizard-volumes', 'x-wizard-share-mounts']) delete copy[key];
      return canonical(copy);
    };
    for (const d of [record, ...(record.previous ? [record.previous] : [])])
      if (normalize(generated, d.compose) === normalize(d.catalogue, d.compose)) return structuredClone(d.compose);
    throw fail();
  }
  overlay(urn: string, compose: Compose) {
    validateSeal(compose, urn);
    const services = Object.fromEntries(Object.entries(compose.services).map(([name, service]) => [name, {
      ...service, labels: { ...service.labels, 'runtipi.managed': 'true', 'runtipi.appurn': urn },
    }]));
    return SECTIONS.map((key) => `${key}: !override ${literal(key === 'services' ? services : compose[key] ?? {})}`).join('\n') + '\n';
  }
  validateEffective(actual: Compose, approved: Compose) {
    for (const key of SECTIONS)
      if (canonical(actual[key] ?? {}) !== canonical(approved[key] ?? {})) throw fail();
  }
  snapshot(effective: Compose) {
    // Compose config (including --format json) already escapes every dollar for
    // reinput. Unlike overlay's trusted raw values, this is rendered Compose,
    // not the in-memory resolved model. Escaping again changes runtime bytes.
    return JSON.stringify(Object.fromEntries(SECTIONS.filter((key) => effective[key]).map((key) => [key, effective[key]])));
  }
}
