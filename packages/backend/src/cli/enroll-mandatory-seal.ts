#!/usr/bin/env bun
/** Trusted administrator only, manager UID. Both inputs are reviewed compiler
 * output/input, never captured optional config. Same exclusion as daemon launch.
 */
import fs from 'node:fs/promises';
import { MandatorySeal } from '../modules/docker/mandatory-seal';
const [mode, dataDir, urn, input, catalogueInput, expected] = process.argv.slice(2);
if (!dataDir || !urn || !input || !catalogueInput ||
    !['enroll', 'stage'].includes(mode ?? '') ||
    (mode === 'enroll' ? process.argv.length !== 7 : process.argv.length !== 8 || !expected)) {
  throw new Error('usage: enroll-mandatory-seal.ts enroll|stage DATA_DIR APP_URN REVIEWED_COMPILED_JSON REVIEWED_GENERATED_JSON [EXPECTED_CURRENT_SHA256 for stage]');
}
const seal = new MandatorySeal(dataDir);
const compose = JSON.parse(await fs.readFile(input, 'utf8'));
const catalogue = JSON.parse(await fs.readFile(catalogueInput, 'utf8'));
const record = mode === 'stage'
  ? await seal.stage(urn, expected!, compose, catalogue)
  : await seal.enroll(urn, compose, catalogue);
console.log(JSON.stringify({ urn: record.urn, policy: record.policy, sha256: record.sha256, previous_sha256: record.previous?.sha256 }));
