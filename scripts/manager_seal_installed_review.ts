// QA-only trusted recipe review. No optional configuration is an enrollment input.
import fs from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { isDeepStrictEqual } from 'node:util';
import * as yaml from 'yaml';
import { type } from 'arktype';
import { DockerComposeBuilder } from '../packages/backend/src/modules/docker/builders/compose.builder';
import { MandatorySeal, validateSeal } from '../packages/backend/src/modules/docker/mandatory-seal';
import { appInfoSchema, dynamicComposeSchemaYaml } from '../packages/common/src/schemas';
import { castAppUrn } from '../packages/backend/src/common/helpers/app-helpers';

const [mode, subnet] = process.argv.slice(2);
const urn = castAppUrn('agent-synthetic:migrated');
const compiled = JSON.parse(await fs.readFile('/app/compiled.json', 'utf8'));
validateSeal(compiled, urn);
const recipe = JSON.parse(await fs.readFile('/app/recipe.json', 'utf8'));
const parsed = dynamicComposeSchemaYaml(recipe);
if (parsed instanceof type.errors) throw new Error('synthetic recipe invalid');
const revisionCommand = 'import time; time.sleep(1800) # trusted revision 2';
const seal = new MandatorySeal('/data');
const git = (...args: string[]) => execFileSync('git', args, {encoding:'utf8',timeout:10000,maxBuffer:65536}).trim();
const digest = (raw: string | Buffer) => createHash('sha256').update(raw).digest('hex');
async function configEvidence(path: string) {
  const stat = await fs.lstat(path);
  if (!stat.isFile() || stat.size > 65536) throw new Error('synthetic config file bound');
  const raw = await fs.readFile(path); const config = JSON.parse(raw.toString());
  const parsed = appInfoSchema({...config,urn});
  return {path,sha256:digest(raw),version:config.version,tipi_version:config.tipi_version,
    schema_valid:!(parsed instanceof type.errors),available:parsed instanceof type.errors?null:parsed.available,
    schema_error:parsed instanceof type.errors?parsed.summary.slice(0,4096):null};
}
if (mode === 'diagnostic') {
  // Fixed synthetic paths only. No optional config, env, credentials or general file reads.
  const result: any = {read_only:true,urn};
  for (const [label,path] of Object.entries({installed:'/data/apps/migrated/agent-synthetic/config.json',
    repo:'/data/repos/migrated/apps/agent-synthetic/config.json',reviewed:'/data/reviewed-update/config.json'})) {
    try { result[label] = await configEvidence(path); } catch { result[label] = {read_error:true}; }
  }
  try {
    const path='/data/apps/migrated/agent-synthetic/docker-compose.yml';
    const info=await fs.lstat(path); if(!info.isFile()||info.size>65536)throw new Error('compose bound');
    const raw=await fs.readFile(path,'utf8'); const doc=yaml.parse(raw);
    result.installed_compose={sha256:digest(raw),commands:Object.fromEntries(Object.entries(doc.services).map(([k,v]:any)=>[k,v.command]))};
  } catch { result.installed_compose={read_error:true}; }
  for (const [label,dir] of Object.entries({repo:'/data/repos/migrated',upstream:'/data/reviewed-update/served-store/store.git'})) {
    try { result[label+'_head']=git('-C',dir,'rev-parse','HEAD'); } catch { result[label+'_head']=null; }
  }
  const raw=JSON.stringify(result); if(Buffer.byteLength(raw)>16384)throw new Error('diagnostic bound');
  console.log(raw);
} else if (['candidate', 'publish', 'readback'].includes(mode)) {
  const current = await seal.read(urn);
  if (!current) throw new Error('missing synthetic enrollment');
  if (mode === 'readback') {
    const config = JSON.parse(await fs.readFile('/data/apps/migrated/agent-synthetic/config.json', 'utf8'));
    const installed = yaml.parse(await fs.readFile('/data/apps/migrated/agent-synthetic/docker-compose.yml', 'utf8'));
    const generated = yaml.parse(await fs.readFile('/data/apps/migrated/agent-synthetic/docker-compose.generated.yml', 'utf8'));
    const candidate = JSON.parse(await fs.readFile('/data/reviewed-update/catalogue.json', 'utf8'));
    if (!isDeepStrictEqual(generated, candidate)) throw new Error('updated generated catalogue differs from review');
    if (Object.values(installed.services).some((s: any) => s.command?.[0] !== revisionCommand)) throw new Error('wrong installed revision');
    console.log(JSON.stringify({sha256:current.sha256, previous_sha256:current.previous?.sha256,
      installed_version:config.tipi_version, installed_config_sha256:(await configEvidence('/data/apps/migrated/agent-synthetic/config.json')).sha256,
      revision_command:revisionCommand}));
  } else if (mode === 'candidate' && subnet && /^[0-9.]+\/[0-9]+$/.test(subnet)) {
    if (current.previous) throw new Error('anchor already in use');
    const next = structuredClone(recipe); const doc = structuredClone(compiled);
    for (const service of Object.values(next.services) as any[]) service.command = [revisionCommand];
    for (const service of Object.values(doc.services) as any[]) service.command = [revisionCommand];
    const checked = dynamicComposeSchemaYaml(next);
    if (checked instanceof type.errors) throw new Error('candidate schema invalid');
    validateSeal(doc, urn);
    const form = {skipRun:true, skipPull:true, skipEnv:false, openPort:false, forcePull:false,
      exposed:false, exposedLocal:false, enableAuth:false, isVisibleOnGuestDashboard:false};
    const catalogue = yaml.parse(new DockerComposeBuilder().getDockerCompose(checked, form, urn, subnet, 'amd64'));
    // Immutable QA catalogue metadata, never optional config or installed output.
    const config = JSON.parse(await fs.readFile('/app/store/apps/agent-synthetic/config.json', 'utf8'));
    config.version = '2.0.0'; config.tipi_version = 2;
    await fs.mkdir('/data/reviewed-update', {mode:0o700});
    for (const [name, value] of Object.entries({compiled:doc, catalogue, recipe:next, config}))
      await fs.writeFile(`/data/reviewed-update/${name}.json`, JSON.stringify(value), {flag:'wx',mode:0o600});
    console.log(JSON.stringify({reviewed:true, mode, expected_sha256:current.sha256}));
  } else if (mode === 'publish') {
    if (!current.previous) throw new Error('candidate was not staged');
    const compiledNext = JSON.parse(await fs.readFile('/data/reviewed-update/compiled.json', 'utf8'));
    const catalogueNext = JSON.parse(await fs.readFile('/data/reviewed-update/catalogue.json', 'utf8'));
    if (!isDeepStrictEqual(current.compose, compiledNext) || !isDeepStrictEqual(current.catalogue, catalogueNext)) throw new Error('stage readback mismatch');
    const next = JSON.parse(await fs.readFile('/data/reviewed-update/recipe.json', 'utf8'));
    const config = JSON.parse(await fs.readFile('/data/reviewed-update/config.json', 'utf8'));
    const candidateConfig = appInfoSchema({...config,urn});
    if (candidateConfig instanceof type.errors || !candidateConfig.available) throw new Error('candidate config invalid/unavailable');
    // Publish a committed synthetic upstream, not dirty files in its disposable
    // consumer clone. Product */15 refresh force-checks out origin/main.
    const work='/data/reviewed-update/git-work';
    git('clone','--no-hardlinks','/app/store.git',work);
    await fs.writeFile(`${work}/apps/agent-synthetic/docker-compose.yml`, yaml.stringify(next));
    await fs.writeFile(`${work}/apps/agent-synthetic/config.json`, JSON.stringify(config));
    git('-C',work,'add','apps/agent-synthetic/config.json','apps/agent-synthetic/docker-compose.yml');
    git('-C',work,'-c','user.name=QA','-c','user.email=qa@example.invalid','commit','-m','Reviewed synthetic revision 2');
    const commit=git('-C',work,'rev-parse','HEAD');
    await fs.mkdir('/data/reviewed-update/served-store.next',{mode:0o700});
    git('clone','--bare','--no-hardlinks',work,'/data/reviewed-update/served-store.next/store.git');
    await fs.rename('/data/reviewed-update/served-store.next','/data/reviewed-update/served-store');
    // Exercise the same real HTTP upstream the product refresh consumes.
    const repo='/data/repos/migrated';
    git('-C',repo,'fetch','origin','main');
    if(git('-C',repo,'rev-parse','FETCH_HEAD')!==commit)throw new Error('upstream commit mismatch');
    git('-C',repo,'reset','--hard',commit);
    const publication=await configEvidence(`${repo}/apps/agent-synthetic/config.json`);
    if(!publication.schema_valid||!publication.available||publication.tipi_version!==2||publication.version!=='2.0.0')throw new Error('published version/schema mismatch');
    // Read exact publication back; the manager copies these reviewed store bytes.
    if (!isDeepStrictEqual(yaml.parse(await fs.readFile('/data/repos/migrated/apps/agent-synthetic/docker-compose.yml','utf8')),next) ||
      !isDeepStrictEqual(JSON.parse(await fs.readFile('/data/repos/migrated/apps/agent-synthetic/config.json','utf8')),config))
      throw new Error('publication readback mismatch');
    console.log(JSON.stringify({reviewed:true,mode,sha256:current.sha256,previous_sha256:current.previous.sha256,
      upstream_commit:commit,publication}));
  } else throw new Error('invalid trusted candidate mode');
} else if (mode === 'schema') {
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
if (mode === 'schema' || mode === 'review') console.log(JSON.stringify({reviewed:true, mode}));
