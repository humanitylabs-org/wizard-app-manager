"""Source-bound QA-only continuation; never replay accepted candidate suites."""
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile

from manager_seal_bundle import SAVED_BUNDLE, validate_bundle, failure_record

SOURCE = '3116f500f4d7df959a9f4d586dec4f9a9b7564b5'
PINS = {
    'SOURCE.json': '0feee5f3c55834e9164f4e45d7ea5564790040f95ac0ef9bfb2fbc465e2c23b3',
    'docker-receipt.json': '76bee4c997f2c176838c0e90a348b9fb2b1250005dca045e0db97d9639c3e6cf',
    'docker-artifacts.tar.gz': '168a63a43777a34a1bf3b5a3fa6b5daef6f52339828f9a761a3a36024754de44',
    # Backend wire log, NOT the separately retained heavy-worker spool log.
    'docker.log': 'd3a0ac71040441040291e2bf3dc6f6c5c3b2e5bdd3f38422c39c465ec58e9cbd',
}
QA_DELTA = {'scripts/manager-seal-docker.py', 'scripts/tests/test_manager_seal_docker.py',
            'scripts/manager_seal_bundle.py', 'scripts/manager_seal_continuation.py',
            'scripts/tests/test_manager_seal_bundle.py', 'docs/mandatory-manager-privacy.md'}
DIST = 'source/packages/backend/dist'
TRACE = 'candidate-trace/agent-synthetic_seal3116f500f4d7-events.json'


def verify_saved(root, manifest, runner):
    saved = root/'saved'
    for name, expected in PINS.items():
        p = saved/name
        assert stat.S_ISREG(p.lstat().st_mode) and p.stat().st_nlink == 1, 'saved evidence must be regular single-link file'
        assert p.stat().st_size <= 128*runner.MIB and runner.sha(p) == expected, 'saved evidence hash drift'
    previous = json.loads((saved/'SOURCE.json').read_text())
    changes = source_closure(previous, manifest)
    receipt = json.loads((saved/'docker-receipt.json').read_text())
    assert receipt['source'] == SOURCE and receipt['manifest_sha256'] == PINS['SOURCE.json']
    assert receipt['runner_sha256'] == previous['files']['scripts/manager-seal-docker.py']
    assert receipt['artifacts']['sha256'] == PINS['docker-artifacts.tar.gz']
    assert not receipt['source_handler_real_docker_pass'] and not receipt['packaged_api_pass']
    assert receipt['leaked_containers'] == [] and receipt['leaked_networks'] == []
    assert receipt['cleanup_volume_absent'] and receipt['reserve_preserved']
    assert not receipt['container_state']['OOMKilled'] and receipt['container_state']['ExitCode'] == 1
    with (saved/'docker.log').open('rb') as f:
        f.seek(max(0, f.seek(0, 2)-4096)); tail = f.read()
    assert b'line 318, in inside' in tail and b"<= {'main.js', 'main.js.map'}" in tail, 'saved failure is not inventory assertion'
    result = receipt['result']
    assert result['source'] == SOURCE and result['manifest_sha256'] == PINS['SOURCE.json']
    assert result['candidate_counts'] == {'numFailedTests': 0, 'numPassedTests': 19, 'numPendingTests': 0, 'numTotalTests': 19, 'success': True}
    assert result['adjacent_counts'] == {'numFailedTests': 0, 'numPassedTests': 60, 'numPendingTests': 0, 'numTotalTests': 60, 'success': True}
    assert len(result['candidate_checkpoints']) == len(runner.REQUIRED_CHECKS) and set(result['candidate_checkpoints']) == runner.REQUIRED_CHECKS
    assert result['rollback_prior_negative'] == {'source': runner.ROLLBACK_PRIOR_SOURCE, 'service_sha256': runner.ROLLBACK_PRIOR_SERVICE_SHA, 'specific_failure': True}
    assert 'baseline-negative' not in result['steps'] and 'candidate-bundle-node-syntax' not in result['steps']
    for label in ['candidate-real-docker', 'adjacent-lifecycle-regressions', 'changed-fixture-types',
                  'literal-dollar-regression', 'remediation-regression', 'candidate-backend-bundle']:
        assert result['steps'][label]['exit_code'] == 0, 'inherited gate did not pass'
    assert result['steps']['remediation-prior-negative']['exit_code'] == 1
    assert result['steps']['dependencies']['exit_code'] == 0 and result['steps']['dependencies']['comparison_complete']
    return {'source': SOURCE, 'pins': PINS, 'changed_qa_paths': sorted(changes),
            'unchanged_source_paths': len(set(previous['files'])-changes),
            'candidate_counts': result['candidate_counts'], 'candidate_checkpoints': result['candidate_checkpoints'],
            'adjacent_counts': result['adjacent_counts'], 'rollback_prior_negative': result['rollback_prior_negative'],
            'classification': 'inherited exact-source passed phases from failed job; no fresh candidate/full-suite claim'}


def source_closure(previous, manifest):
    assert previous['head'] == SOURCE and previous['baseline_tar_sha256'] == manifest['baseline_tar_sha256'], 'saved source identity'
    changes = {p for p in set(previous['files']) | set(manifest['files']) if previous['files'].get(p) != manifest['files'].get(p)}
    assert changes <= QA_DELTA, 'production or candidate fixture dependency changed; full proof cannot be inherited'
    return changes


def extract_saved(root, destination, runner):
    """Allow only pinned artifact members, no links/traversal or broad extraction."""
    receipt = json.loads((root/'saved/docker-receipt.json').read_text())
    steps = receipt['result']['steps']
    logs = {'logs/'+label+'.log' for label in steps if label != 'dependencies'}
    logs |= {'logs/'+name for name in receipt['dependencies']}
    files = logs | {'inside-result.json', 'candidate.json', 'adjacent.json', TRACE} | {DIST+'/'+n for n in SAVED_BUNDLE}
    directories = {'logs', 'candidate-trace', DIST}
    seen = set(); total = 0
    with tarfile.open(root/'saved/docker-artifacts.tar.gz') as tf:
        for m in tf:
            parts = PurePosixPath(m.name).parts
            assert parts and not m.name.startswith('/') and '..' not in parts and str(PurePosixPath(m.name)) == m.name, 'saved archive path'
            assert m.name not in seen, 'duplicate saved member'
            seen.add(m.name); total += m.size
            assert len(seen) <= 100 and total <= 64*runner.MIB and m.size <= 32*runner.MIB, 'saved archive budget'
            assert (m.isdir() and m.name in directories) or (m.isfile() and m.name in files), 'unexpected saved archive member/type'
            dst = destination/m.name
            if m.isdir(): dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                assert src is not None, 'saved archive regular payload missing'
                with src, dst.open('xb') as out:
                    shutil.copyfileobj(src, out)
                assert dst.stat().st_size == m.size, 'saved extraction length'
    assert seen == files | directories, 'saved archive incomplete'
    assert json.loads((destination/'inside-result.json').read_text()) == receipt['result'], 'inside/outer evidence disagreement'
    for label, metadata in steps.items():
        if label == 'dependencies': continue
        p = destination/'logs'/str(label+'.log')
        assert p.stat().st_size == metadata['bytes'] and runner.sha(p) == metadata['sha256'], 'saved step log hash'
    for name, metadata in receipt['dependencies'].items():
        p = destination/'logs'/name
        assert p.stat().st_size == metadata['bytes'] and runner.sha(p) == metadata['sha256'], 'saved dependency evidence hash'
    for filename, key in [('candidate.json', 'candidate_counts'), ('adjacent.json', 'adjacent_counts')]:
        assert runner.report_counts(json.loads((destination/filename).read_text())) == receipt['result'][key], 'saved machine report counts'
    trace = json.loads((destination/TRACE).read_text())
    assert not trace.get('error') and trace['checks'] == receipt['result']['candidate_checkpoints'], 'saved checkpoint trace'
    runner.rollback_negative((destination/'logs/remediation-prior-negative.log').read_text())
    return destination/DIST


def inside(args, runner):
    root = Path('/source'); fixture = Path(args.fixture_root); checkout = Path('/work/source')
    Path('/work/logs').mkdir(exist_ok=True)
    result = {'source': args.source, 'manifest_sha256': args.manifest_sha256, 'steps': {},
              'phase': 'continuation-source-verification', 'source_handler_real_docker_pass': False,
              'packaged_api_pass': False, 'image_built': False, 'full_docker_acceptance': False, 'RUNTIPI_ADMITTED': False,
              'classification': 'composite source-handler fixture only; installed lifecycle/API/image/provider remain unfinished'}
    def step(label, argv, cwd=checkout, timeout=300, allow_failure=False):
        result['phase'] = label
        print('STEP '+label, flush=True)
        log = Path('/work/logs', label+'.log')
        with log.open('wb') as f:
            p = runner.subprocess.run(list(map(str, argv)), cwd=cwd, stdout=f, stderr=runner.subprocess.STDOUT, timeout=timeout)
        assert log.stat().st_size <= 32*runner.MIB, 'step log cap'
        result['steps'][label] = {'exit_code': p.returncode, 'bytes': log.stat().st_size, 'sha256': runner.sha(log)}
        if p.returncode and not allow_failure: raise RuntimeError('continuation step failed')
        return p.returncode
    try:
        manifest = runner.early.verify_source(root, args.source, args.manifest_sha256)
        result['inherited_candidate'] = verify_saved(root, manifest, runner)
        result['limits'] = runner.inside_limits(fixture)
        assert runner.sha('/tooling/node') == runner.early.NODE_SHA and runner.sha('/tooling/docker') == runner.DOCKER_SHA
        result['phase'] = 'saved-artifact-validation'
        dist = extract_saved(root, Path('/work/saved-artifacts'), runner)
        result['saved_bundle'] = validate_bundle(dist, root, saved=True)
        # Read-only syntax validation of the ORIGINAL saved backend bytes.
        step('saved-bundle-node-syntax', ['/tooling/node', '--check', dist/'main.js'], cwd=dist)
        checkout = runner.bootstrap(root, manifest, step)
        # Reproduce ONLY the two native filenames from their saved bytes using
        # the same zip-SHA-pinned Bun. No candidate backend build or test replay.
        probe = Path('/work/native-name-probe'); probe.mkdir()
        for name, original in [('cpufeatures-e8x7s2f1.node', 'cpufeatures.node'), ('sshcrypto-a3khcfhj.node', 'sshcrypto.node')]:
            shutil.copy2(dist/name, probe/original)
        (probe/'entry.js').write_text("export const cpu = require('./cpufeatures.node'); export const ssh = require('./sshcrypto.node');\n")
        (probe/'probe.mjs').write_text("const r=await Bun.build({entrypoints:['./entry.js'],outdir:'./out',target:'node',format:'esm',minify:true});\n"
            "if(!r.success)throw new Error('native filename probe failed');\n"
            "console.log(JSON.stringify(r.outputs.map(o=>({path:o.path.split('/').pop(),kind:o.kind,loader:o.loader,hash:o.hash,size:o.size})).sort((a,b)=>a.path.localeCompare(b.path))));\n")
        step('native-bundler-filename-probe', ['bun', 'probe.mjs'], cwd=probe)
        outputs = json.loads(Path('/work/logs/native-bundler-filename-probe.log').read_text())
        native_outputs = {o['path']: o for o in outputs if o['path'] in SAVED_BUNDLE and o['path'].endswith('.node')}
        assert set(native_outputs) == set(SAVED_BUNDLE)-{'main.js', 'main.js.map'}, 'pinned Bun did not reproduce exact native names'
        assert len(outputs) == 3 and {o['path'] for o in outputs} == {'entry.js', *native_outputs}, 'unexpected native probe output'
        for name in native_outputs:
            assert runner.sha(probe/'out'/name) == SAVED_BUNDLE[name][1], 'pinned Bun changed native bytes'
        result['native_bundler_outputs'] = outputs
        # Dependencies were RAM-only and destroyed by the failed job. This is
        # prerequisite reconstruction, NOT a replay of any accepted test phase.
        deps = {}; result['steps']['dependencies'] = deps
        runner.dependency_step(checkout, Path('/work/logs'), manifest['files']['bun.lock'], deps)
        assert deps['exit_code'] == 0 and deps['log_complete'] and deps['comparison_complete'], 'continuation dependency prerequisite'
        assert runner.sha(checkout/'bun.lock') == manifest['files']['bun.lock'], 'frozen lock drift'
        result['phase'] = 'saved-native-package-provenance'
        result['saved_bundle'] = validate_bundle(dist, root, saved=True, installed=checkout)
        step('continuation-focused-harness', ['python3', '-m', 'unittest', 'test_manager_seal_bundle', 'test_manager_seal_docker', '-v'], cwd=checkout/'scripts/tests')
        step('common-build', ['bun', 'run', 'build'], cwd=checkout/'packages/common')
        result['daemon_bind_preflight'] = runner.daemon_bind_preflight(fixture, args.owner)
        result['network_driver_preflight'] = runner.network_driver_preflight(args.owner)
        result['phase'] = 'baseline-source-closure'
        baseline = Path('/work/baseline'); baseline.mkdir()
        with tarfile.open(root/'baseline.tar') as tf:
            members = tf.getmembers(); total = 0; seen = set()
            for m in members:
                assert m.isfile() or m.isdir(), 'baseline unexpected link/type'
                assert not Path(m.name).is_absolute() and '..' not in Path(m.name).parts and m.name not in seen, 'baseline path/duplicate'
                seen.add(m.name); total += m.size
            assert total <= 64*runner.MIB and len(members) < 10000, 'baseline extraction budget'
            tf.extractall(baseline, filter='data')
        permitted = set(runner.PRODUCT_DELTA+['packages/backend/src/modules/docker/__tests__/wizard-restart-incident.test.ts'])
        for p in baseline.rglob('*'):
            if p.is_file():
                rel = str(p.relative_to(baseline))
                assert rel in permitted or runner.sha(p) == manifest['files'].get(rel), 'baseline shared closure drift'
        for rel in runner.PRODUCT_DELTA: shutil.copy2(baseline/rel, checkout/rel)
        os.environ.update(RUN_MANAGER_SEAL_DOCKER='1', MANAGER_SEAL_BASELINE_NEGATIVE='1', TMPDIR=str(fixture),
            MANAGER_SEAL_TEST_IMAGE=runner.PYTHON_IMAGE, MANAGER_SEAL_TEST_IMAGE_V2=runner.early.IMAGE,
            MANAGER_SEAL_TRACE_DIR='/work/baseline-trace', MANAGER_SEAL_QA_OWNER=args.owner,
            MANAGER_SEAL_PROJECT_SUFFIX=args.source[:12])
        vitest = runner.early.workspace_executable(checkout, 'backend', 'vitest')
        code = step('baseline-negative', [vitest, 'run', runner.TARGET, '-t', 'real Docker', '--maxWorkers=1', '--reporter=json', '--outputFile=/work/baseline.json'], cwd=checkout/'packages/backend', allow_failure=True)
        result['phase'] = 'baseline-specific-failure-validation'
        report = json.loads(Path('/work/baseline.json').read_text())
        result['baseline_counts'] = runner.report_counts(report)
        assert code != 0, 'original baseline unexpectedly passed'
        runner.baseline_failure(report)
        assert report['numFailedTests'] == 1 and report['numPassedTests'] == 1 and report['numPendingTests'] == 17 and report['numTotalTests'] == 19, 'original baseline active/filter inventory'
        result['baseline_bypass_reproduced'] = True
        for rel in runner.PRODUCT_DELTA: shutil.copy2(root/rel, checkout/rel)
        assert all(runner.sha(checkout/rel) == manifest['files'][rel] for rel in runner.PRODUCT_DELTA), 'candidate source restoration'
        # Candidate suite, 22 checkpoints, adjacent, types, dollar, remediation
        # and rollback negatives are inherited; NEVER call their test commands.
        result['candidate_backend_bundle_sha256'] = result['saved_bundle']['files']['main.js']['sha256']
        result['source_handler_real_docker_pass'] = True
        result['phase'] = 'complete-source-handler-composite'
    except Exception as exc:
        result['error'] = failure_record(exc, result['phase'])
        raise
    finally:
        result['tmpfs_used_bytes'] = (os.statvfs('/work').f_blocks-os.statvfs('/work').f_bfree)*os.statvfs('/work').f_frsize
        Path('/work/inside-result.json').write_text(json.dumps(result, indent=2)+'\n')
        runner.export_bundle([Path('/work/inside-result.json'), Path('/work/logs'), Path('/work/baseline.json'), Path('/work/baseline-trace'), Path('/work/native-name-probe')])
        print(runner.RESULT+json.dumps(result, sort_keys=True), flush=True)
