"""A Builder-bundle copy of wam must never run box commands with its own (possibly older) code.

2 Nov 2026: `wam install oscarhenrycollins/news` from a builder session ran ~/.wizard-os/builder/current/bin/wam, an
Oct 30 bundle without the owner-gateway fix in links.py, and rewrote Messenger's launcher to its raw port.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import builder_bundle as bb, bundle_delegate as bd  # noqa: E402


def _bundle(tmp_path):
    root = tmp_path / 'releases' / ('a' * 64)
    (root / 'wam').mkdir(parents=True)
    (root / 'bundle.json').write_text(json.dumps({'owner': bd.MARKER, 'schema': 1}))
    return root


def _box(tmp_path, body='#!/bin/sh\ncd /x && exec python3 -m wam "$@"\n'):
    cmd = tmp_path / 'bin' / 'wam'
    cmd.parent.mkdir(exist_ok=True)
    cmd.write_text(body)
    cmd.chmod(0o755)
    return cmd


def _run(argv, root, env):
    calls = []
    out = bd.delegate(argv, source=root, env=env, execv=lambda *a: calls.append(a))
    return out, calls


@pytest.mark.parametrize('cmd', ['install', 'links', 'apps', 'app', 'app-updates', 'updates', 'uninstall', 'doctor',
                                 'builder-bundle', 'builder-skill', 'views', 'alongside', 'network', 'grants',
                                 'bootstrap', 'search'])
def test_box_commands_from_the_bundle_run_the_box_wam(tmp_path, cmd):
    root, box = _bundle(tmp_path), _box(tmp_path)
    env = {'WAM_BOX_BIN': str(box)}
    with pytest.MonkeyPatch.context() as m:
        m.setenv('WAM_BOX_BIN', str(box))
        out, calls = _run([cmd, 'x', '--json'], root, env)
    assert out and calls == [(str(box), [str(box), cmd, 'x', '--json'])]
    assert env[bd.GUARD] == '1'


@pytest.mark.parametrize('cmd', sorted(bd.BUILDER_COMMANDS) + ['--help'])
def test_builder_commands_stay_in_the_bundle(tmp_path, monkeypatch, cmd):
    root, box = _bundle(tmp_path), _box(tmp_path)
    monkeypatch.setenv('WAM_BOX_BIN', str(box))
    assert _run([cmd, 'src'], root, {}) == (False, [])


def test_no_box_wam_means_the_bundle_runs_everything(tmp_path, monkeypatch):
    """A stock agent container has only the bundle (/opt/wizard-builder)."""
    monkeypatch.setenv('WAM_BOX_BIN', str(tmp_path / 'missing'))
    assert _run(['links'], _bundle(tmp_path), {}) == (False, [])


def test_never_loops_or_delegates_to_another_bundle(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    monkeypatch.setenv('WAM_BOX_BIN', str(_box(tmp_path, '#!/bin/sh\n# wizard-builder-bundle-v1\nexec x "$@"\n')))
    assert _run(['links'], root, {}) == (False, [])             # ~/.local/bin/wam is itself a bundle wrapper
    monkeypatch.setenv('WAM_BOX_BIN', str(_box(tmp_path)))
    assert _run(['links'], root, {bd.GUARD: '1'}) == (False, [])  # already delegated once


def test_a_checkout_wam_is_not_a_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv('WAM_BOX_BIN', str(_box(tmp_path)))
    assert _run(['links'], bd.SOURCE, {}) == (False, [])          # the box's own wam runs its own commands


def test_installed_bundle_launcher_delegates_end_to_end(tmp_path, monkeypatch):
    """The real installed launcher: `wam links --json` from the bundle reaches the box wam with the same arguments."""
    os_home = tmp_path / 'wizard-os'
    out = bb.install(os_home)
    seen = tmp_path / 'seen'
    box = _box(tmp_path, f'#!/bin/sh\necho "$@" > {seen}\necho "$WAM_FROM_BUILDER_BUNDLE" >> {seen}\n')
    env = {**os.environ, 'WAM_BOX_BIN': str(box), 'HOME': str(tmp_path)}
    env.pop(bd.GUARD, None)
    launcher = os_home / 'builder' / 'current' / 'bin' / 'wam'
    p = subprocess.run([str(launcher), 'links', '--json'], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert seen.read_text().split('\n')[:2] == ['links --json', '1']
    seen.unlink()
    p = subprocess.run([str(launcher), 'new', '--help'], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and not seen.exists()             # builder commands still run from the bundle
    assert out['state'] == 'installed'


def test_doctor_flags_a_bundle_older_than_this_app_manager(tmp_path, monkeypatch):
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home)
    assert bb.health(os_home)[0][1] is True
    stale = os_home / 'builder' / 'current' / 'wam' / 'links.py'
    stale.chmod(0o644)
    stale.write_text(stale.read_text() + '\n# older\n')
    manifest = json.loads((os_home / 'builder/current/bundle.json').read_text())
    files = bb._files(os_home / 'builder/current')
    import hashlib
    manifest['files'] = files
    manifest['sha256'] = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    (os_home / 'builder/current/bundle.json').write_text(json.dumps(manifest))
    row = bb.health(os_home)[0]
    assert row[1] is False and 'older than' in row[2] and 'wam builder-bundle install' in row[2]
    bb.install(os_home)                                         # the repair doctor --fix runs
    assert bb.health(os_home)[0][1] is True
