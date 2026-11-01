#!/usr/bin/env python3
"""Forum lease only. Synthetic state, final WAM documents, exact scoped cleanup."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import traceback

CASES = []
PROJECTS = []
NETWORKS = []


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write(p, value):
    Path(p).write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')


def run(argv, timeout=180, ok=True, cwd=None, env=None):
    p = subprocess.run(list(map(str, argv)), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=timeout, cwd=cwd, env=env)
    if ok and p.returncode:
        # Synthetic-only output is retained, not echoed to the chat/model.
        (OUT / ('failure-' + str(len(CASES)) + '.log')).write_bytes(p.stdout + p.stderr)
        raise RuntimeError('operation-failed:' + str(p.returncode))
    return p


def case(name, fn):
    row = {'id': name, 'status': 'running'}
    CASES.append(row)
    write(OUT / 'cases.json', CASES)
    try:
        row.update(status='passed', result=fn())
    except Exception as exc:
        row.update(status='failed', error_type=type(exc).__name__,
                   sites=[{'file': Path(x.filename).name, 'line': x.lineno} for x in traceback.extract_tb(exc.__traceback__)[-5:]])
        raise
    finally:
        write(OUT / 'cases.json', CASES)


def docker(*args, **kw):
    return run(['docker', '--context', 'default', *args], **kw)


def integrity():
    assert sha(HERE / 'manifest.json') == ARGS.manifest_sha256
    for f, row in MANIFEST['files'].items():
        p = HERE / f
        assert p.is_file() and not p.is_symlink() and sha(p) == row['sha256']
    assert sha(OLD / 'manifest.json') == MANIFEST['old_manifest_sha256']
    assert sha(OLD / 'run-forum.py') == MANIFEST['old_runner_sha256']
    for f, row in json.loads((OLD / 'manifest.json').read_text())['files'].items():
        assert sha(OLD / f) == row['sha256']
    for app in ('chatgpt', 'claude'):
        assert sha(HERE / 'sources' / app / 'prepare_private_data.py') == sha(OLD / 'sources' / app / 'prepare_private_data.py')
    return {'manifest': ARGS.manifest_sha256, 'runner': sha(Path(__file__)), 'unchanged_40_runner': sha(OLD / 'run-forum.py')}


def prior40():
    receipt = OLD / 'evidence/acceptance.json'
    if not receipt.exists():
        assert not (OLD / 'runtime').exists() and not (OLD / 'evidence').exists(), 'old stage interrupted: resume explicitly, never duplicate'
        run(['/usr/bin/python3', OLD / 'run-forum.py', '--manifest-sha256', MANIFEST['old_manifest_sha256']], timeout=1500)
    d = json.loads(receipt.read_text())
    assert d['ok'] and d['actual'] == d['expected'] == d['passed'] == 40
    assert d['failed'] == 0 and d['manifest_sha256'] == MANIFEST['old_manifest_sha256'] and d['runner_sha256'] == MANIFEST['old_runner_sha256']
    rows = json.loads((OLD / 'evidence/cases.json').read_text())
    assert len(rows) == len({r['id'] for r in rows}) == 40 and all(r['status'] == 'passed' for r in rows)
    return {'receipt': str(receipt), 'sha256': sha(receipt), 'passed': 40, 'not_final_render_boundary_proof': True}


def tooling():
    # Frozen candidate workspace; no lock rewrite or serving checkout reuse.
    run(['bun', 'install', '--frozen-lockfile'], timeout=900, cwd=WAM)
    run(['bun', 'run', 'build'], timeout=180, cwd=WAM / 'packages/common')
    # Only module resolution is shared with the exact base, never its source.
    for loc in [BASE / 'node_modules', BASE / 'packages/backend/node_modules', BASE / 'packages/common/node_modules']:
        donor = WAM / loc.relative_to(BASE)
        if donor.exists() and not loc.exists():
            loc.symlink_to(donor, target_is_directory=True)
    return {'bun': run(['bun', '--version']).stdout.decode().strip(), 'node': run(['node', '--version']).stdout.decode().strip()}


def source_contracts():
    p = run(['node', '--test', 'tests/credential-grants-optout.test.mjs'], cwd=WAM)
    (OUT / 'node-injector.log').write_bytes(p.stdout + p.stderr)
    assert b'# tests 24\n' in p.stdout and b'# pass 24\n' in p.stdout
    for app in ('chatgpt', 'claude'):
        p = run(['/usr/bin/python3', '-m', 'unittest', '-q', 'test_private_initialization'], cwd=HERE / 'sources' / app)
        (OUT / (app + '-source.log')).write_bytes(p.stdout + p.stderr)
        assert b'Ran 15 tests' in p.stderr and b'OK' in p.stderr
    output = OUT / 'vitest-optout.json'
    run(['bun', 'x', '--no-install', 'vitest', 'run', '--maxWorkers=1', '--reporter=json', '--outputFile=' + str(output),
         'src/modules/docker/__tests__/wizard-grants-optout.test.ts'], cwd=WAM / 'packages/backend')
    d = json.loads(output.read_text()); assert d['success'] and d['numTotalTests'] == d['numPassedTests'] == 28
    return {'injector_passed': 24, 'new_recipe_passed': 30, 'focused_backend_passed': 28, 'receipt': str(output)}


def continuity():
    # Select only normal grants injection + minting + automatic request continuity,
    # not the previously accepted 108-case lifecycle/security phase.
    output = OUT / 'vitest-continuity.json'
    run(['bun', 'x', '--no-install', 'vitest', 'run', '--maxWorkers=1', '--reporter=json', '--outputFile=' + str(output),
         'src/modules/docker/__tests__/wizard-app-grants.test.ts', 'src/modules/docker/__tests__/wizard-connection-policy.test.ts',
         '-t', 'seeds uses with unique|mounts dir read-only|approves a runtime request automatically'], cwd=WAM / 'packages/backend')
    d = json.loads(output.read_text()); assert d['success'] and d['numPassedTests'] == 3
    return {'selected_continuity_passed': 3, 'not_rerun': 'unchanged 108-case prior phase'}


def manager(scenario, phase):
    bun = shutil.which('bun'); assert bun and str(Path(bun).resolve()).startswith('/'), 'pin executable'
    env = ['PATH=' + os.environ['PATH'], 'HOME=' + str(HERE / 'root-home'), 'TMPDIR=' + str(HERE / 'tmp'),
           'WIZARD_CREDENTIAL_SYNTHETIC_ROOT=' + str(HERE), 'WIZARD_OS_HOME=' + str(HERE / 'runtime' / scenario / 'wizard-os'),
           'RUNTIPI_DOCKER_NETWORK=' + SCOPE + '-shared', 'DO_NOT_TRACK=1', 'SENTRY_DSN=']
    run(['sudo', '-n', '/usr/bin/env', '-i', *env, bun, '--tsconfig', WAM / 'packages/backend/tsconfig.json',
         WAM / 'packages/backend/qa/credential-init-render.ts', HERE, scenario, phase], timeout=180, cwd=WAM)
    return json.loads((OUT / (scenario + '-' + phase + '-manager.json')).read_text())


def configuration(meta, scenario, phase, app):
    a = meta['apps'][app]; project = SCOPE + '-' + scenario + '-' + app
    argv = ['docker', '--context', 'default', 'compose', '--project-name', project, '--env-file', a['envFile'], '-f', a['prepared'], 'config', '--format', 'json']
    p = run(['sudo', '-n', '/usr/bin/env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin', 'HOME=' + str(HERE / 'root-home'), *argv])
    cfg = json.loads(p.stdout); resolved = OUT / (scenario + '-' + phase + '-' + app + '.config.json'); write(resolved, cfg)
    services = cfg['services']; name = 'wizard-' + app
    for suffix in ('-data-init', '-data-validate'):
        s = services[name + suffix]
        assert len(s.get('volumes', [])) == 1
        mount = s['volumes'][0]
        assert mount['source'] == a['target'] and mount['target'] == ('/data' if app == 'chatgpt' else '/claude')
        assert bool(mount.get('read_only')) == suffix.endswith('validate')
        assert s['network_mode'] == 'none' and not s.get('networks')
        assert s['cap_drop'] == ['ALL'] and s.get('cap_add', []) == (['CHOWN'] if suffix.endswith('init') else [])
        assert s['user'] == ('0:0' if suffix.endswith('init') else '1000:1000') and s['read_only']
        assert 'WIZARD_GRANTS_FILE' not in s.get('environment', {}) and 'WIZARD_CONNECTION_REQUESTS' not in s.get('environment', {})
        assert not s.get('env_file') and not s.get('volumes_from') and not s.get('privileged', False)
    for n, s in services.items():
        if n.endswith(('-data-init', '-data-validate')): continue
        assert s['environment']['WIZARD_GRANTS_FILE'] == '/wizard/grants.json'
        assert s['environment']['WIZARD_CONNECTION_REQUESTS'] == '/wizard-requests/connections.json'
        assert {v['target'] for v in s['volumes']} >= {'/wizard', '/wizard-requests'}
    # This exact complete resolved document, not a rebuilt/sanitized subset, is launched.
    return resolved, project, a


def compose(file, project, *args, **kw):
    return docker('compose', '--project-name', project, '-f', file, *args, **kw)


def fixture(target, scenario):
    code = '''import pathlib,os
p=pathlib.Path('/case');p.mkdir(exist_ok=True)
kind=KIND
if kind=='populated-root':os.chown(p,0,0);os.chmod(p,0o700)
else:os.chown(p,1000 if kind!='wrong-owner' else 1101,1000 if kind!='wrong-owner' else 2202);os.chmod(p,0o700)
f=p/'known-secret.json'
if kind=='symlink':f.symlink_to('missing')
elif kind=='fifo':os.mkfifo(f,0o600);os.chown(f,1000,1000)
else:
 f.write_bytes(SENTINEL);os.chmod(f,0o600);os.chown(f,1000,1000)
 if kind=='hardlink':os.link(f,p/'second.json')
'''.replace('KIND', repr(scenario)).replace('SENTINEL', repr(LEGACY.SENTINEL))
    # Explicit fixture authority is separate from, and never added to, product helpers.
    LEGACY.run_python(LEGACY.OFFICIAL, code, [LEGACY.bind(target, '/case')], caps=['CHOWN', 'FOWNER', 'DAC_OVERRIDE'])


def snapshot(target):
    code = '''import pathlib,os,stat,json,hashlib
p=pathlib.Path('/case');rows={}
for f in [p,*p.iterdir()]:
 s=f.lstat();r={'uid':s.st_uid,'gid':s.st_gid,'mode':stat.S_IMODE(s.st_mode),'type':stat.S_IFMT(s.st_mode),'nlink':s.st_nlink}
 if stat.S_ISREG(s.st_mode):r['sha256']=hashlib.sha256(f.read_bytes()).hexdigest()
 rows[str(f.relative_to(p))]=r
print(json.dumps(rows,sort_keys=True))
'''
    return json.loads(LEGACY.run_python(LEGACY.OFFICIAL, code, [LEGACY.bind(target, '/case', readonly=True)], caps=['DAC_OVERRIDE']).stdout)


def launch(config, project, app, expected):
    name = 'wizard-' + app
    PROJECTS.append((str(config), project))
    proc = compose(config, project, 'up', '-d', name + '-data-validate', ok=False)
    if expected: assert proc.returncode == 0
    ids = compose(config, project, 'ps', '-a', '-q').stdout.decode().split()
    inventories = []
    for cid in ids:
        full = json.loads(docker('inspect', cid).stdout)[0]
        if full['State']['Running']:
            docker('wait', cid, timeout=60)
            full = json.loads(docker('inspect', cid).stdout)[0]
        # Only synthetic official-image one-shots may exist; no normal engine/front.
        service = full['Config']['Labels']['com.docker.compose.service']
        assert service in (name + '-data-init', name + '-data-validate')
        row = {'service': service, 'image': full['Image'], 'Config': full['Config'], 'HostConfig': full['HostConfig'],
               'Mounts': full['Mounts'], 'State': full['State'], 'NetworkSettings': full['NetworkSettings']}
        inventories.append(row)
        assert len(full['Mounts']) == 1
        declared = json.loads(Path(config).read_text())['services'][service]
        mount = full['Mounts'][0]
        assert mount['Source'] == declared['volumes'][0]['source'] and mount['Destination'] == declared['volumes'][0]['target']
        assert mount['RW'] == service.endswith('init')
        image = json.loads(docker('image', 'inspect', LEGACY.OFFICIAL).stdout)[0]
        assert full['Image'] == image['Id'] and full['Config']['User'] == declared['user']
        assert sorted(full['Config']['Env']) == sorted(image['Config']['Env'])
        assert not full['HostConfig']['Privileged']
        assert full['HostConfig']['NetworkMode'] == 'none' and full['HostConfig']['CapDrop'] == ['ALL']
        assert (full['HostConfig']['CapAdd'] or []) == (['CHOWN'] if service.endswith('init') else [])
        env = full['Config']['Env']
        assert not any(e.startswith(('WIZARD_GRANTS_FILE=', 'WIZARD_CONNECTION_REQUESTS=')) for e in env)
        assert full['HostConfig']['ReadonlyRootfs'] and any('no-new-privileges' in x for x in full['HostConfig']['SecurityOpt'])
        assert set(full['NetworkSettings']['Networks']) <= {'none'}
    write(OUT / (project + '-daemon.json'), inventories)
    assert inventories
    if expected:
        assert len(inventories) == 2 and all(x['State']['ExitCode'] == 0 for x in inventories)
    else:
        assert any(x['State']['ExitCode'] != 0 for x in inventories)
    return {'complete_daemon_inventory': str(OUT / (project + '-daemon.json')), 'expected_success': expected}


def scenario_case(scenario):
    meta = manager(scenario, 'install')
    results = {}
    for app in ('chatgpt', 'claude'):
        cfg, project, a = configuration(meta, scenario, 'install', app)
        target = Path(a['target'])
        # Docker creates ChatGPT's precise missing bind. Claude's legacy-empty
        # storage branch may already create it as root: both are real WAM output.
        before = None
        if scenario != 'fresh':
            run(['sudo', '-n', 'mkdir', '-p', target]); fixture(target, scenario)
            before = snapshot(target)
            manager(scenario, 'restart')  # actual lifecycle must preserve existing bytes/metadata
            assert snapshot(target) == before
        results[app] = launch(cfg, project, app, scenario in ('fresh', 'existing'))
        if scenario == 'fresh':
            fixture(target, 'existing'); before = snapshot(target)
            for phase in ('restart', 'update'):
                compose(cfg, project, 'down', '--remove-orphans')
                phase_meta = manager(scenario, phase)
                cfg, project, _ = configuration(phase_meta, scenario, phase, app)
                assert snapshot(target) == before
                launch(cfg, project, app, True)
                assert snapshot(target) == before
        elif scenario == 'existing': assert snapshot(target) == before
        compose(cfg, project, 'down', '--remove-orphans')
    return results


def cleanup():
    errors = []
    for config, project in dict.fromkeys(PROJECTS):
        if compose(config, project, 'down', '--remove-orphans', ok=False).returncode: errors.append(project)
    # Reused helpers track exact names and label-check each cleanup target.
    LEGACY.cleanup()
    for name in NETWORKS:
        label = docker('network', 'inspect', '--format', '{{index .Labels "wizard.init-exposure"}}', name).stdout.decode().strip()
        assert label == SCOPE
        if docker('network', 'rm', name, ok=False).returncode: errors.append(name)
    # Remove only the generated synthetic state, after all exact projects are down.
    if not errors and (HERE / 'runtime').is_dir():
        assert (HERE / 'runtime').resolve() == HERE / 'runtime'
        run(['sudo', '-n', 'rm', '-rf', '--', HERE / 'runtime'])
    assert not errors
    return {'owned_projects_down': True, 'owned_networks_removed': True, 'runtime_removed': True, 'global_cleanup': False}


def main():
    global HERE, OUT, OLD, WAM, BASE, MANIFEST, ARGS, SCOPE, LEGACY
    parser = argparse.ArgumentParser(); parser.add_argument('--stage', required=True); parser.add_argument('--manifest-sha256', required=True)
    ARGS = parser.parse_args(); HERE = Path(ARGS.stage).resolve(); SCOPE = HERE.name
    assert str(HERE).startswith('/srv/work/credential-init-exposure-') and platform.machine() == 'x86_64'
    os.fstat(int(os.environ['WIZARD_HEAVY_LOCK_FD']))
    assert sha(HERE / 'manifest.json') == ARGS.manifest_sha256
    MANIFEST = json.loads((HERE / 'manifest.json').read_text()); OLD = Path(MANIFEST['old_stage']); WAM = HERE / 'wam'; BASE = HERE / 'base-wam'
    assert str(OLD).startswith('/srv/work/credential-init-compat-') and OLD.resolve() == OLD
    integrity()  # Verify old helper code before importing/executing it.
    (HERE / 'execution.claim').mkdir(); OUT = HERE / 'evidence'; OUT.mkdir(mode=0o755)
    for d in ['tmp', 'runtime', 'root-home']: (HERE / d).mkdir(mode=0o755)
    assert shutil.disk_usage(HERE).free >= MANIFEST['reserve_bytes'] + MANIFEST['additional_peak_bytes']
    spec = importlib.util.spec_from_file_location('unchanged_fixture_helpers', OLD / 'run-forum.py')
    assert spec and spec.loader
    LEGACY = importlib.util.module_from_spec(spec); spec.loader.exec_module(LEGACY)
    setattr(LEGACY, 'SCOPE', SCOPE); setattr(LEGACY, 'OUT', OUT); setattr(LEGACY, 'RUNTIME', HERE / 'runtime')
    try:
        case('integrity-before', integrity)
        case('prior-40-byte-identical', prior40)
        case('frozen-tooling', tooling)
        case('schema-converter-builder-failclosed', source_contracts)
        case('engine-grants-runtime-request-continuity', continuity)
        run(['sudo', '-n', 'true'])
        assert docker('volume', 'inspect', SCOPE + '-absent-legacy', ok=False).returncode != 0
        found = docker('image', 'inspect', LEGACY.OFFICIAL, ok=False)
        assert found.returncode == 0, 'old40 must have loaded exact official image'
        NETWORKS.append(SCOPE + '-shared')
        docker('network', 'create', '--internal', '--label', 'wizard.init-exposure=' + SCOPE, NETWORKS[0])
        for scenario in ['fresh', 'existing', 'wrong-owner', 'populated-root', 'symlink', 'hardlink', 'fifo']:
            case('final-wam/' + scenario, lambda scenario=scenario: scenario_case(scenario))
    finally:
        try:
            case('owned-cleanup', cleanup)
        except Exception:
            pass  # case records the failure; still write the red acceptance receipt.
        try:
            case('integrity-after', integrity)
        except Exception:
            pass
        expected = MANIFEST['expected_case_ids']; ids = [x['id'] for x in CASES]
        passed = sum(x['status'] == 'passed' for x in CASES)
        report = {'ok': len(ids) == len(set(ids)) and set(ids) == set(expected) and passed == len(expected),
                  'expected': len(expected), 'actual': len(ids), 'passed': passed, 'missing': sorted(set(expected)-set(ids)),
                  'manifest_sha256': ARGS.manifest_sha256, 'runner_sha256': sha(Path(__file__)),
                  'old40_separately_hash_pinned': True, 'live_changes': False, 'release_acceptance': False,
                  'residual': ['PR382/404 approvals and draft DO-NOT-MERGE', 'Miguel acceptance', 'live/provider/official refresh canaries not authorized']}
        write(OUT / 'acceptance.json', report)
    assert report['ok']


if __name__ == '__main__': main()
