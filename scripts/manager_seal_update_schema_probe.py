#!/usr/bin/env python3
"""Offline, synthetic-only replay of shipped schema and installed-info logic.

Inputs are exact source-map entries, not a backend execution or an HTTP mock.
Run the generated JS with Node/Bun after installing only lock-pinned arktype in
an isolated proof directory. Never imports/boots the application.
"""
import ast
import hashlib
import json
from pathlib import Path
import re


def generate(repo, sources, output):
    repo, output = Path(repo), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    script = ast.parse((repo/'scripts/manager_seal_installed_path.py').read_text())
    assignments = [n for n in ast.walk(script) if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'config' for t in n.targets)
                   and isinstance(n.value, ast.Dict)]
    assert len(assignments) == 1
    expression = ast.Expression(assignments[0].value)
    config = eval(compile(expression, '<synthetic fixture literal>', 'eval'), {'MAIN': 'agent-synthetic'})
    config.update(version='2.0.0', tipi_version=2)
    raw = json.dumps(config, separators=(',', ':')).encode()
    (output/'config.json').write_bytes(raw)
    schema = sources['../../common/dist/schemas/app-info.js']
    urn = sources['../../common/dist/types/app-urn.js']
    assert set(re.findall(r"from ['\"]([^'\"]+)", urn)) <= {'arktype'}
    (output/'app-info.mjs').write_text(schema.replace('../types/app-urn.js', './app-urn.mjs'))
    (output/'app-urn.mjs').write_text(urn)
    source = sources['../src/modules/apps/app-files-manager.ts']
    begin = source.index('  public async getInstalledAppInfo(')
    end = source.index('\n  /**', begin)
    method = source[begin:end].replace('public async', 'async').replace('appUrn: AppUrn', 'appUrn')
    # Exact shipped method with a filesystem adapter pointed ONLY at this
    # synthetic fixture. Schema/availability/exception branches are untouched.
    probe = """import fs from 'node:fs/promises';
import {type} from 'arktype';
import {appInfoSchema} from './app-info.mjs';
import {createHash} from 'node:crypto';
const raw=await fs.readFile(new URL('./config.json',import.meta.url));
const config=JSON.parse(raw);const urn='agent-synthetic:migrated';
const parsed=appInfoSchema({...config,urn});
const errors=[];
class Reader {
 getAppPaths(){return {appInstalledDir:'/synthetic'}};
 filesystem={isFile:async p=>p==='/synthetic/config.json',readTextFile:async()=>raw.toString()};
 logger={error:(...args)=>errors.push(args.join(' '))};
METHOD
}
const installed=await new Reader().getInstalledAppInfo(urn);
const result={config_sha256:createHash('sha256').update(raw).digest('hex'),
 schema_valid:!(parsed instanceof type.errors),schema_error:parsed instanceof type.errors?parsed.summary:null,
 installed_info_version:installed?.tipi_version,installed_info_available:installed?.available,errors};
console.log(JSON.stringify(result,null,2));
if(!result.schema_valid||result.installed_info_version!==2||errors.length)process.exitCode=1;
""".replace('METHOD', method)
    # Only path is an otherwise built-in dependency in the actual method.
    (output/'probe.mjs').write_text("import path from 'node:path';\n"+probe)
    return {'config_sha256': hashlib.sha256(raw).hexdigest(), 'source_sha256': {
        name: hashlib.sha256(value.encode()).hexdigest() for name, value in sources.items()}}
