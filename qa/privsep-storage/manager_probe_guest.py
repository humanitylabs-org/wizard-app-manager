#!/usr/bin/env python3
"""Disposable QEMU guest only: as-found versus fresh import, no app startup or prune.

Only fixed metadata/hash/size projections leave the guest. Raw tool output and copied
bundle/save/export bytes remain private and are never printed or downloaded.
"""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import traceback

ROOT = Path('/root/storage-manager-probe')
TREE = Path('/opt/storage-manager-probe-source')
PINNED = 'privsep-wam:595523fb'
ARCHIVE_SHA = 'a953c4ce3590bc1448bacb0b5b259334a99a608fb0377af425daf87c888d49c0'
report = {'schema': 'privsep-manager-probe-guest-r3', 'commands': [], 'checks': [], 'first_error': None,
          'cleanup_errors': [], 'phases': [], 'owned_ids': [], 'owned_images': [], 'baseline_images': []}


def module(name, path):
    s = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m


HERE = Path(__file__).resolve().parent
C = module('probe_collector', HERE / 'collector.py' if (HERE / 'collector.py').exists() else HERE / 'manager_probe_collector.py')


def save():
    p = ROOT / 'native.json'; p.write_text(json.dumps(report, sort_keys=True, indent=2) + '\n'); p.chmod(0o600)


def run(label, argv, timeout=120, required=True, stderr=False):
    p = subprocess.Popen([str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True, env={**os.environ, 'LANG': 'C'})
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        try: os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        p.communicate(timeout=15)
        report['commands'].append({'operation': label, 'exit': None, 'timeout': True}); save(); raise
    report['commands'].append({'operation': label, 'exit': p.returncode,
                              'stdout_bytes': len(out), 'stderr_bytes': len(err),
                              'stdout_sha256': hashlib.sha256(out).hexdigest(), 'stderr_sha256': hashlib.sha256(err).hexdigest()})
    save(); assert len(out) <= 1048576
    if required: assert p.returncode == 0, 'fixed probe operation failed'
    return p.returncode, err if stderr else out


def docker_json(label, args):
    return json.loads(run(label, ['docker', *args])[1])


def image_ids():
    raw = run('image-inventory', ['docker', 'image', 'ls', '--no-trunc', '-aq'])[1].decode().splitlines()
    assert all(re.fullmatch('sha256:[0-9a-f]{64}', v) for v in raw)
    return sorted(set(raw))


def create(args, project='manager_probe'):
    assert project in ('manager_probe', 'notes_wizardapps')
    out = run('create-probe', ['docker', 'create', '--pull=never', '--no-healthcheck', '--network', 'none', '--label',
                              'com.docker.compose.project=' + project, *args])[1].decode().strip()
    assert re.fullmatch('[0-9a-f]{64}', out)
    report['owned_ids'].append(out); save(); return out


def remove(cid):
    assert cid in report['owned_ids']
    run('remove-exact-container', ['docker', 'rm', '-f', '-v', cid])
    rc, _ = run('readback-container-absent', ['docker', 'inspect', cid], required=False)
    assert rc != 0
    # A failed inspect alone is NOT absence: require a successful full inventory.
    raw = run('container-inventory', ['docker', 'ps', '-aq', '--no-trunc'])[1].decode().splitlines()
    assert all(re.fullmatch('[0-9a-f]{64}', x) for x in raw) and cid not in raw
    report['owned_ids'].remove(cid); save()


SHELL = '''ls -l /app
cat /proc/self/mountinfo
for p in app/main.mjs app/main.js; do
 if [ -f /$p ]; then
  n=$(stat -c %s /$p) || exit 20
  h=$(sha256sum /$p) || exit 21
  printf 'PROBE %s %s %s\\n' "$p" "$n" "${h%% *}"
 elif [ ! -e /$p ]; then printf 'PROBE %s ABSENT\\n' "$p"
 else exit 22; fi
done
'''


def bound_backend(original=None):
    backend = json.loads((ROOT / 'backend.json').read_text())
    assert backend['archive_sha256'] == ARCHIVE_SHA
    assert backend['rootfs']['diff_ids'][-1] == C.LAST_DIFF_ID
    if original is not None:
        if not C.is_unknown(original['config_sha256']):
            assert original['config_sha256'] == backend['config_sha256']
        if not C.is_unknown(original['diff_ids']):
            assert original['diff_ids'] == backend['rootfs']['diff_ids']
    return backend


def remove_bound_image():
    # Only the observed, archive-bound baseline ID is eligible, inside the disposable guest.
    assert report['baseline_id'] == C.BOUND_ID and C.BOUND_ID in report['baseline_images']
    for cid in list(report['owned_ids']): remove(cid)
    assert run('pre-eviction-container-inventory', ['docker', 'ps', '-aq', '--no-trunc'])[1] == b''
    assert image_ids() == report['baseline_images']
    pinned = docker_json('pre-eviction-pinned-id', ['image', 'inspect', '--format', '{{json .Id}}', PINNED])
    assert pinned == C.BOUND_ID
    run('remove-pinned-tag', ['docker', 'image', 'rm', '--no-prune', PINNED], timeout=180)
    # The tag could have been the only reference: require a successful inventory either way.
    remaining = image_ids()
    if C.BOUND_ID in remaining:
        run('remove-exact-baseline-image', ['docker', 'image', 'rm', '--no-prune', '-f', C.BOUND_ID], timeout=180)
    expected = sorted(set(report['baseline_images']) - {C.BOUND_ID})
    assert image_ids() == expected
    assert run('readback-baseline-absent', ['docker', 'image', 'inspect', C.BOUND_ID], required=False)[0] != 0
    report['baseline_removed_before_load'] = True; save()


def load_fresh(original):
    assert report.get('baseline_removed_before_load') is True
    assert C.BOUND_ID not in image_ids()
    archive = ROOT / 'backend.tar.gz'
    with archive.open('rb') as f: assert C.sha_file(f)['sha256'] == ARCHIVE_SHA
    backend = bound_backend(original)
    # Docker may print progress/warnings or a tag instead of an ID. The text is
    # not an identity proof: read back the exact bound ID and successful inventory.
    run('load-remove_and_fresh_load', ['docker', 'load', '-i', archive], timeout=300)
    loaded_id = docker_json('fresh-loaded-id-only', ['image', 'inspect', '--format', '{{json .Id}}', C.BOUND_ID])
    assert loaded_id == report['baseline_id'] == C.BOUND_ID
    print(loaded_id, flush=True)
    report['fresh_loaded_id'] = loaded_id
    report['fresh_id_equals_baseline'] = loaded_id == report['baseline_id']
    report['owned_images'] = [loaded_id]; save()
    run('tag-fresh-probe', ['docker', 'tag', loaded_id, PINNED])
    assert image_ids() == report['baseline_images']


PENDING_FACTS = {}


def checkpoint(doc):
    # Metadata only, atomic replacement; native.json remains downloadable even
    # when a subsequent command or optional facts operation fails.
    p = ROOT / 'probe-progress.json'; tmp = ROOT / 'probe-progress.tmp'
    tmp.write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n'); tmp.chmod(0o600); tmp.replace(p)
    report['measurement_checkpoint'] = doc; save()


def cycle(name, original):
    assert name in C.CYCLES
    doc = docker_json('image-inspect', ['image', 'inspect', PINNED])[0]
    assert doc['Id'] == report['baseline_id'] == C.BOUND_ID
    assert image_ids() == report['baseline_images']
    result = {'name': name, 'image': None, 'methods': {},
              'saved': None, 'mount': C.unparsed(None), 'cleanup_absent': False}
    def measured():
        report.setdefault('measurements', {})[name] = result
        save()
    # Fresh shell only: does not execute the app CMD, healthcheck, node or Compose.
    raw = run('run-shell', ['docker', 'run', '--rm', '--pull=never', '--no-healthcheck', '--label', 'com.docker.compose.project=manager_probe', '--network', 'none', '--entrypoint', 'sh',
                            PINNED, '-c', SHELL])[1]
    methods = result['methods']; methods['run'] = C.parse_shell(raw)
    PENDING_FACTS[name] = (doc, raw); measured()
    raw = run('run-readonly-shell', ['docker', 'run', '--rm', '--pull=never', '--read-only', '--no-healthcheck', '--label', 'com.docker.compose.project=manager_probe', '--network', 'none',
                                    '--entrypoint', 'sh', PINNED, '-c', SHELL])[1]
    methods['readonly'] = C.parse_shell(raw); measured()
    cid = create(['--entrypoint', 'sh', PINNED, '-c', 'exit 0'])
    try:
        copied = {}
        for p in C.PATHS:
            target = ROOT / 'copied-file'
            assert not target.exists() and not target.is_symlink()
            rc, error = run('cp-fixed-file', ['docker', 'cp', cid + ':/' + p, target], required=False, stderr=True)
            if rc == 0 and target.is_file() and not target.is_symlink():
                try:
                    with target.open('rb') as f: copied[p] = C.sha_file(f)
                except (OSError, ValueError): copied[p] = C.unknown_file(None)
            elif rc != 0 and C.cp_missing(error, p, cid) and not target.exists():
                copied[p] = C.absent()
            else:
                # Not absence: an unsupported member or failed cp is explicitly
                # unknown and cannot earn a causal verdict. Never follow a link.
                copied[p] = C.unknown_file(error)
            if target.is_symlink() or target.is_file(): target.unlink()
            elif target.is_dir(): shutil.rmtree(target)
        methods['cp'] = copied; measured()
        exported = ROOT / 'container.tar'
        run('export-container', ['docker', 'export', '-o', exported, cid], timeout=180)
        try:
            with exported.open('rb') as f: methods['export'] = C.files_from_tar(f)
        except OSError:
            methods['export'] = {p: C.unknown_file(None) for p in C.PATHS}
        measured(); exported.unlink(missing_ok=True)
    finally:
        remove(cid)
    saved = ROOT / 'roundtrip.tar'
    run('save-roundtrip', ['docker', 'save', '-o', saved, PINNED], timeout=180)
    result['saved'] = C.archive(saved); result['cleanup_absent'] = not report['owned_ids']
    measured(); saved.unlink(missing_ok=True)
    return result


def fresh(revision):
    assert re.fullmatch('[0-9a-f]{40}', revision)
    assert not Path('/var/lib/wizard-os').exists()
    assert not Path('/usr/bin/tailscale').exists() and not Path('/var/lib/tailscale').exists()
    assert run('fresh-container-inventory', ['docker', 'ps', '-aq'])[1] == b''
    report['baseline_images'] = image_ids()
    bound_backend()
    assert C.BOUND_ID in report['baseline_images']
    baseline = docker_json('baseline-id-only', ['image', 'inspect', '--format', '{{json .Id}}', C.BOUND_ID])
    assert baseline == C.BOUND_ID
    report['baseline_id'] = baseline; save()
    rc, _ = run('as-found-pinned-status', ['docker', 'image', 'inspect', PINNED], required=False)
    if rc == 0:
        assert docker_json('as-found-pinned-id', ['image', 'inspect', '--format', '{{json .Id}}', PINNED]) == baseline
    else:
        run('tag-as-found-probe', ['docker', 'tag', baseline, PINNED])
    assert image_ids() == report['baseline_images']
    report['checks'].append({'name': 'fresh-synthetic-inputs', 'passed': True}); save()


def optional_fact(label, argv, json_value=False):
    try:
        rc, raw = run(label, argv, timeout=15, required=False)
        if rc != 0: return C.unparsed(raw)
        try: return json.loads(raw) if json_value else raw.decode('ascii').strip()
        except (ValueError, UnicodeError): return C.unparsed(raw)
    except Exception as e:
        # Timeouts, missing executables and decode failures cannot discard data.
        return C.unparsed(type(e).__name__)


def engine_facts():
    info = optional_fact('engine-info', ['docker', 'info', '--format', '{{json .}}'], True)
    version = optional_fact('engine-version', ['docker', 'version', '--format', '{{json .}}'], True)
    filesystem = optional_fact('docker-filesystem', ['findmnt', '-n', '-o', 'FSTYPE', '-T', '/var/lib/docker'])
    kernel = optional_fact('kernel-version', ['uname', '-r'])
    try: free = shutil.disk_usage('/var/lib/docker').free
    except OSError: free = None
    return C.engine_facts(version, info, kernel, filesystem, free)


def probe():
    assert report['checks'] and 'probe' not in report['phases'][:-1]
    with (ROOT / 'backend.tar.gz').open('rb') as f: assert C.sha_file(f)['sha256'] == ARCHIVE_SHA
    original = C.archive(ROOT / 'backend.tar.gz')
    bound_backend(original)
    doc = {'schema': 'privsep-manager-probe-r3', 'original': original,
           'engine': None, 'cycles': [],
           'review_required': True, 'full_acceptance': False, 'reload_without_prune': True}
    for name in C.CYCLES:
        if name == 'remove_and_fresh_load':
            assert [x['name'] for x in doc['cycles']] == ['as_found']
            remove_bound_image()
            load_fresh(original)
        doc['cycles'].append(cycle(name, original))
        checkpoint(doc)
    # Both sets of five measurements and exact eviction/load proofs are durable
    # before parsing auxiliary mount/image metadata or running engine commands.
    report['measurements_complete_before_facts'] = True; checkpoint(doc)
    for cy in doc['cycles']:
        raw_image, raw_mount = PENDING_FACTS.pop(cy['name'])
        cy['image'] = C.image_facts(raw_image); cy['mount'] = C.mount_facts(raw_mount)
    doc['engine'] = engine_facts()
    for key in ('baseline_id', 'fresh_loaded_id', 'fresh_id_equals_baseline', 'baseline_removed_before_load'):
        doc[key] = report[key]
    doc['verdict'] = C.classify(doc); C.validate_export(doc)
    p = ROOT / 'manager-probe-export.json'; p.write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n'); p.chmod(0o600)
    report['verdict'] = doc['verdict']; checkpoint(doc)



def cleanup():
    for cid in list(report['owned_ids']): remove(cid)
    # Reuse the original exact-absence/foreign-container negative-control machinery.
    m = module('probe_cleanup', TREE / 'qa/privsep-storage/production_guest.py'); m.ROOT = ROOT
    m.CLEANUP_PROJECTS = ('manager_probe',)
    engine_status, engine_before = m.daemon_request('GET', '/info'); assert engine_status == 200
    cid = create(['--entrypoint', 'sh', PINNED, '-c', 'exit 0'], project='notes_wizardapps')
    status, before = m.daemon_request('GET', '/containers/' + cid + '/json'); assert status == 200
    try: m.cleanup_containers()
    except AssertionError: pass
    else: raise AssertionError('unowned-negative-not-refused')
    first = json.loads((ROOT / 'container-cleanup.json').read_text())
    (ROOT / 'cleanup-negative.json').write_text(json.dumps(first, sort_keys=True, indent=2) + '\n')
    assert first['unowned_exact_ids'] == [cid] and not first['passed']
    status, after = m.daemon_request('GET', '/containers/' + cid + '/json'); assert status == 200 and after == before
    remove(cid); m.cleanup_containers()
    final = json.loads((ROOT / 'container-cleanup.json').read_text())
    status, engine_after = m.daemon_request('GET', '/info')
    assert final['passed'] and final['readback_absent'] and status == 200 and engine_after['ID'] == engine_before['ID']
    # The freshly loaded ID equals the original baseline; retain it in this disposable
    # guest so the final inventory is equal. Original host/base disks are never written.
    assert all(image == C.BOUND_ID for image in report['owned_images'])
    assert image_ids() == report['baseline_images']
    report['owned_images'] = []
    report['exact_cleanup'] = {'negative_survived_owned_cleanup': True, 'negative_removed_by_full_id_absent': True,
                               'engine_id_stable': True, 'baseline_image_ids_restored': True}
    save()


def main():
    global report
    assert os.geteuid() == 0 and Path('/run/wizard-storage-production-synthetic').is_file()
    assert sys.argv[1] in ('fresh', 'probe', 'cleanup')
    if (ROOT / 'native.json').exists(): report = json.loads((ROOT / 'native.json').read_text())
    fd = os.open(ROOT / 'native.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    report['phases'].append(sys.argv[1]); save()
    try:
        if sys.argv[1] == 'fresh': fresh(sys.argv[2])
        elif sys.argv[1] == 'probe': probe()
        else: cleanup()
    except BaseException as e:
        error = {'type': type(e).__name__, 'phase': sys.argv[1], 'frames': [
            {'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno} for f in traceback.extract_tb(e.__traceback__)]}
        if report['first_error'] is None: report['first_error'] = error
        if sys.argv[1] == 'cleanup': report['cleanup_errors'].append(error)
        save(); return 1
    finally:
        save(); os.close(fd)
    return 0


if __name__ == '__main__':
    sys.exit(main())
