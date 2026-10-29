import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { confined, type Compose } from './wizard-storage';

const POLICY = 'tinfoil-sealed-v1';
const LABEL = 'wizard.private-network-policy';
const SECTIONS = ['services', 'networks', 'volumes', 'configs', 'secrets'] as const;
const fail = () => new Error('Mandatory sealed launch policy invalid; refusing lifecycle mutation');
export const sealHash = (value: string) => createHash('sha256').update(value).digest('hex');
const canonical = (v: any): string => JSON.stringify(v, (_key, value) =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.keys(value).sort().map((k) => [k, value[k]])) : value);
const literal = (v: unknown) => JSON.stringify(v).replaceAll('$', '$$');

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
  // This mandatory profile is the already compiled provider-held-key topology.
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
}

export type SealRecord = { schema: 1; urn: string; policy: string; sha256: string; compose: Compose };

/** Directory existence is the permanent enrollment marker; a missing policy fails closed.
 * App folder replacement, uninstall and app backup/restore do not own this directory.
 */
export class MandatorySeal {
  constructor(readonly dataDir: string) {}
  private async enrollment(urn: string, create = false) {
    return confined(this.dataDir, `state/mandatory-seals/${sealHash(urn)}`, create);
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
      if (record.schema !== 1 || record.urn !== urn || record.policy !== POLICY || record.sha256 !== sealHash(canonical(record.compose))) throw fail();
      validateSeal(record.compose, urn);
      return record;
    } catch { throw fail(); }
  }
  /** Offline trusted administrator entry only. No catalogue/HTTP/user-config caller. */
  async enroll(urn: string, compose: Compose) {
    validateSeal(compose, urn);
    const existing = await this.read(urn);
    if (existing) {
      if (canonical(existing.compose) !== canonical(compose)) throw fail();
      return existing;
    }
    const dir = await this.enrollment(urn, true);
    const record: SealRecord = { schema: 1, urn, policy: POLICY, sha256: sealHash(canonical(compose)), compose };
    // Exclusive creation, never overwrite a missing/corrupt enrollment on retry.
    await fs.writeFile(path.join(dir, 'policy.json'), `${JSON.stringify(record)}\n`, { flag: 'wx', mode: 0o600 });
    return record;
  }
  /** Overrides entire service/network maps, not just arrays. No optional extra
   * service or shared/default network survives composition. Keep manager labels.
   */
  overlay(record: SealRecord) {
    const services = Object.fromEntries(Object.entries(record.compose.services).map(([name, service]) => [name, {
      ...service, labels: { ...service.labels, 'runtipi.managed': 'true', 'runtipi.appurn': record.urn },
    }]));
    return SECTIONS.map((key) => `${key}: !override ${literal(key === 'services' ? services : record.compose[key] ?? {})}`).join('\n') + '\n';
  }
  validateCatalogue(record: SealRecord, generated: Compose) {
    // A security overlay must not make an image/command-changing catalogue
    // update report success while silently continuing the old executable.
    if (canonical(Object.keys(generated.services ?? {}).sort()) !== canonical(Object.keys(record.compose.services).sort())) throw fail();
    for (const [name, approved] of Object.entries(record.compose.services)) {
      const requested = generated.services[name];
      for (const key of ['image', 'command', 'entrypoint'])
        if (canonical(requested[key] ?? null) !== canonical(approved[key] ?? null)) throw fail();
    }
  }
  validateEffective(actual: Compose, approved: Compose) {
    for (const key of SECTIONS)
      if (canonical(actual[key] ?? {}) !== canonical(approved[key] ?? {})) throw fail();
    // Nothing from the optional top-level document is carried into launch.
  }
  snapshot(effective: Compose) {
    return literal(Object.fromEntries(SECTIONS.filter((key) => effective[key]).map((key) => [key, effective[key]])));
  }
}
