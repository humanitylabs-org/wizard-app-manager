// QA-only trusted recipe review. No optional configuration is an enrollment input.
import fs from 'node:fs/promises';
import { isDeepStrictEqual } from 'node:util';
import * as yaml from 'yaml';
import { type } from 'arktype';
import { DockerComposeBuilder } from '../packages/backend/src/modules/docker/builders/compose.builder';
import { validateSeal } from '../packages/backend/src/modules/docker/mandatory-seal';
import { dynamicComposeSchemaYaml } from '../packages/common/src/schemas';
import { castAppUrn } from '../packages/backend/src/common/helpers/app-helpers';

const [mode, subnet] = process.argv.slice(2);
const urn = castAppUrn('agent-synthetic:migrated');
const compiled = JSON.parse(await fs.readFile('/app/compiled.json', 'utf8'));
validateSeal(compiled, urn);
const recipe = JSON.parse(await fs.readFile('/app/recipe.json', 'utf8'));
const parsed = dynamicComposeSchemaYaml(recipe);
if (parsed instanceof type.errors) throw new Error('synthetic recipe invalid');
if (mode === 'schema') {
  const missing = structuredClone(recipe); delete missing['x-runtipi'];
  // Selector, not a permissive raw YAML parser, requires root metadata.
  if (!recipe['x-runtipi'] || recipe['x-runtipi'].schema_version !== 2 || 'x-runtipi' in missing) throw new Error('fixture selector invalid');
} else if (mode === 'review' && subnet && /^[0-9.]+\/[0-9]+$/.test(subnet)) {
  const form = {skipRun:true, skipPull:true, skipEnv:false, openPort:false, forcePull:false,
    exposed:false, exposedLocal:false, enableAuth:false, isVisibleOnGuestDashboard:false};
  const expected = yaml.parse(new DockerComposeBuilder().getDockerCompose(parsed, form, urn, subnet, 'amd64'));
  const actual = yaml.parse(await fs.readFile('/data/apps/migrated/agent-synthetic/docker-compose.generated.yml', 'utf8'));
  if (!isDeepStrictEqual(actual, expected)) throw new Error('installed generation differs from reviewed source builder');
  await fs.writeFile('/data/reviewed-catalogue.json', JSON.stringify(expected), {flag:'wx', mode:0o600});
} else throw new Error('invalid review mode');
console.log(JSON.stringify({reviewed:true, mode}));
