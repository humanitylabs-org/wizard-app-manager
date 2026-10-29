#!/usr/bin/env bun
/** Offline trusted administrator only. No automatic learning or HTTP endpoint.
 * Run under the same UID as the manager against its private dataDir. Input is
 * reviewed private_seal.apply compiler JSON, never optional user-config YAML.
 * Existing enrollment is immutable; deletion/revision needs separate approval.
 */
import fs from 'node:fs/promises';
import { MandatorySeal } from '../modules/docker/mandatory-seal';
const [dataDir, urn, input] = process.argv.slice(2);
if (!dataDir || !urn || !input || process.argv.length !== 5) {
  throw new Error('usage: bun packages/backend/src/cli/enroll-mandatory-seal.ts DATA_DIR APP_URN REVIEWED_COMPILED_JSON');
}
const record = await new MandatorySeal(dataDir).enroll(urn, JSON.parse(await fs.readFile(input, 'utf8')));
// No key/config/paths emitted; this hash binds the approved launch definition.
console.log(JSON.stringify({ urn: record.urn, policy: record.policy, sha256: record.sha256 }));
