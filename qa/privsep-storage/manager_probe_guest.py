#!/usr/bin/env python3
"""Disposable QEMU guest only: two imports, no application/Compose/startup, no prune.

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
report = {'schema': 'privsep-manager-probe-guest-v1', 'commands': [], 'checks': [], 'first_error': None,
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
    out = run('create-probe', ['docker', 'create', '--no-healthcheck', '--network', 'none', '--label',
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


def cycle(name, original):
    archive = ROOT / 'backend.tar.gz'
    with archive.open('rb') as f: assert C.sha_file(f)['sha256'] == ARCHIVE_SHA
    # Same exact saved archive, never a pull or rebuild. Identify only its single loaded locator.
    text = run('load-' + name, ['docker', 'load', '-i', archive], timeout=300)[1].decode().strip()
    m = re.fullmatch(r'Loaded image ID: (sha256:[0-9a-f]{64})', text)
    if m: locator = m.group(1)
    else:
        tags = json.loads((ROOT / 'backend.json').read_text())['manifest'][0]['RepoTags']
        assert len(tags) == 1 and text == 'Loaded image: ' + tags[0]; locator = tags[0]
    run('tag-probe', ['docker', 'tag', locator, PINNED])
    doc = docker_json('image-inspect', ['image', 'inspect', PINNED])[0]
    assert doc['Id'] not in report['baseline_images']
    assert set(image_ids()) - set(report['baseline_images']) == {doc['Id']}
    report['owned_images'] = [doc['Id']]; save()
    image = C.image_facts(doc)
    # Fresh shell only: does not execute the app CMD, healthcheck, node or Compose.
    raw = run('run-shell', ['docker', 'run', '--rm', '--no-healthcheck', '--label', 'com.docker.compose.project=manager_probe', '--network', 'none', '--entrypoint', 'sh',
                            PINNED, '-c', SHELL])[1]
    methods = {'run': C.parse_shell(raw)}; mount = C.mount_facts(raw)
    raw = run('run-readonly-shell', ['docker', 'run', '--rm', '--read-only', '--no-healthcheck', '--label', 'com.docker.compose.project=manager_probe', '--network', 'none',
                                    '--entrypoint', 'sh', PINNED, '-c', SHELL])[1]
    methods['readonly'] = C.parse_shell(raw)
    cid = create(['--entrypoint', 'sh', PINNED, '-c', 'exit 0'])
    try:
        copied = {}
        for p in C.PATHS:
            target = ROOT / 'copied-file'
            assert not target.exists()
            rc, error = run('cp-fixed-file', ['docker', 'cp', cid + ':/' + p, target], required=False, stderr=True)
            if rc == 0:
                assert target.is_file() and not target.is_symlink()
                with target.open('rb') as f: copied[p] = C.sha_file(f)
                target.unlink()
            else:
                # A daemon/copy error must not be mislabeled as expected absence.
                assert C.cp_missing(error, p, cid) and methods['run'][p]['state'] == 'absent' and not target.exists()
                copied[p] = C.absent()
        methods['cp'] = copied
        exported = ROOT / 'container.tar'
        run('export-container', ['docker', 'export', '-o', exported, cid], timeout=180)
        with exported.open('rb') as f: methods['export'] = C.files_from_tar(f)
        exported.unlink()
    finally:
        remove(cid)
    saved = ROOT / 'roundtrip.tar'
    run('save-roundtrip', ['docker', 'save', '-o', saved, PINNED], timeout=180)
    result = {'name': name, 'image': image, 'methods': methods, 'saved': C.archive(saved),
              'mount': mount, 'cleanup_absent': not report['owned_ids']}
    saved.unlink()
    # Explicitly remove only the image this probe just loaded, no system/image prune.
    if name == 'first_load':
        run('remove-owned-image', ['docker', 'image', 'rm', '-f', doc['Id']], timeout=180)
        assert image_ids() == report['baseline_images']; report['owned_images'] = []; save()
    return result


def fresh(revision):
    assert re.fullmatch('[0-9a-f]{40}', revision)
    assert not Path('/var/lib/wizard-os').exists()
    assert not Path('/usr/bin/tailscale').exists() and not Path('/var/lib/tailscale').exists()
    assert run('fresh-container-inventory', ['docker', 'ps', '-aq'])[1] == b''
    report['baseline_images'] = image_ids()
    assert run('fresh-pinned-absent', ['docker', 'image', 'inspect', PINNED], required=False)[0] != 0
    report['checks'].append({'name': 'fresh-synthetic-inputs', 'passed': True}); save()


def probe():
    assert report['checks'] and 'probe' not in report['phases'][:-1]
    with (ROOT / 'backend.tar.gz').open('rb') as f: assert C.sha_file(f)['sha256'] == ARCHIVE_SHA
    original = C.archive(ROOT / 'backend.tar.gz')
    assert C.bundle(original['last_layer']['app/main.mjs'])
    info = docker_json('engine-info', ['info', '--format', '{{json .}}'])
    version = docker_json('engine-version', ['version', '--format', '{{json .}}'])
    filesystem = run('docker-filesystem', ['findmnt', '-n', '-o', 'FSTYPE', '-T', '/var/lib/docker'])[1].decode().strip()
    kernel = run('kernel-version', ['uname', '-r'])[1].decode().strip()
    engine = C.engine_facts(version, info, kernel, filesystem, shutil.disk_usage('/var/lib/docker').free)
    doc = {'schema': 'privsep-manager-probe-v1', 'original': original, 'engine': engine, 'cycles': [],
           'review_required': True, 'full_acceptance': False, 'reload_without_prune': True}
    for name in C.CYCLES:
        doc['cycles'].append(cycle(name, original))
        # Private partial progress is still metadata only; never accepted as a complete export.
        (ROOT / 'probe-progress.json').write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n')
    doc['verdict'] = C.classify(doc); C.validate_export(doc)
    p = ROOT / 'manager-probe-export.json'; p.write_text(json.dumps(doc, sort_keys=True, indent=2) + '\n'); p.chmod(0o600)
    report['verdict'] = doc['verdict']; save()


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
    for image in report['owned_images']:
        assert image not in report['baseline_images']; run('cleanup-owned-image', ['docker', 'image', 'rm', '-f', image], 180)
    assert image_ids() == report['baseline_images']
    report['owned_images'] = []
    report['exact_cleanup'] = {'negative_survived_owned_cleanup': True, 'negative_removed_by_full_id_absent': True,
                               'engine_id_stable': True, 'baseline_image_inventory_unchanged': True}
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
