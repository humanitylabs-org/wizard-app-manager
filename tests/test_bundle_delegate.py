"""A Builder-bundle copy of wam must never run box commands with its own (possibly older) code.

2 Nov 2026: `wam install oscarhenrycollins/news` from a builder session ran ~/.wizard-os/builder/current/bin/wam, an
Oct 30 bundle without the owner-gateway fix in links.py, and rewrote Messenger's launcher to its raw port.
"""
import json
import os
import subprocess
import sys
import venv
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


@pytest.mark.parametrize('entries, expected', [
    (None, None),                          # Missing remains unset.
    ([], []),                              # Preserve the explicitly empty spelling.
    (['root'], None),                       # Only injected root: unset, not ''.
    (['root', 'root', 'alias', 'relative'], None),
    (['root', '', 'other', 'alias', '', 'relative', 'other', 'child', ''],
     ['', 'other', '', 'other', 'child', '']),
    (['other', '', 'child', 'other', ''], ['other', '', 'child', 'other', '']),
    (['root', ''], ['']),                   # Explicit empty survives root removal.
])
def test_delegate_removes_only_bundle_pythonpath(tmp_path, monkeypatch, entries, expected):
    root, box = _bundle(tmp_path), _box(tmp_path)
    alias = tmp_path / 'current'
    alias.symlink_to(root, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WAM_BOX_BIN', str(box))
    values = {'root': str(root), 'alias': str(alias), 'relative': os.path.relpath(root, tmp_path),
              'other': 'unrelated/../owner-path', 'child': str(root / 'wam'), '': ''}

    def path_value(items):
        return os.pathsep.join(values[item] for item in items)

    env = {'OWNER_SETTING': 'preserved'}
    if entries is not None:
        env['PYTHONPATH'] = path_value(entries)
    want = {'OWNER_SETTING': 'preserved', bd.GUARD: '1'}
    if expected is not None:
        want['PYTHONPATH'] = path_value(expected)
    original_sys_path = sys.path[:]
    original_cwd = Path.cwd()

    def execv(cmd, argv):
        assert (cmd, argv) == (str(box), [str(box), 'links', '--json'])
        assert env == want  # Cleaned before exec, including when env is an injected mapping.

    assert bd.delegate(['links', '--json'], source=root, env=env, execv=execv)
    assert env == want
    assert sys.path == original_sys_path and Path.cwd() == original_cwd


def test_delegate_cleans_real_environment_before_exec(tmp_path, monkeypatch):
    root, box = _bundle(tmp_path), _box(tmp_path)
    monkeypatch.setenv('WAM_BOX_BIN', str(box))
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(root), '', 'owner-path']))
    # Record ownership even when initially absent: delegate writes os.environ directly.
    monkeypatch.setenv(bd.GUARD, 'test-owned')
    monkeypatch.delenv(bd.GUARD, raising=False)

    def execv(cmd, argv):
        assert os.environ['PYTHONPATH'] == os.pathsep + 'owner-path'
        assert os.environ[bd.GUARD] == '1'

    assert bd.delegate(['links'], source=root, execv=execv)


@pytest.mark.parametrize('reason', ['builder', 'guard', 'missing-box', 'checkout'])
def test_no_delegation_leaves_environment_untouched(tmp_path, monkeypatch, reason):
    root, box = _bundle(tmp_path), _box(tmp_path)
    monkeypatch.setenv('WAM_BOX_BIN', str(box if reason != 'missing-box' else tmp_path / 'missing'))
    env = {'PYTHONPATH': os.pathsep.join([str(root), '', 'owner-path'])}
    if reason == 'guard':
        env[bd.GUARD] = '1'
    before = env.copy()
    assert _run(['new' if reason == 'builder' else 'links'],
                bd.SOURCE if reason == 'checkout' else root, env) == (False, [])
    assert env == before


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


def test_installed_bundle_launcher_imports_host_console_script(tmp_path):
    """An actual Python console entry point must import its installed code, not the frozen bundle."""
    host = tmp_path / 'host'
    venv.EnvBuilder(with_pip=False).create(host)
    python = host / 'bin' / 'python'
    probe = subprocess.run(
        [str(python), '-I', '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'],
        capture_output=True, text=True, check=True, timeout=20,
    )
    package = Path(probe.stdout.strip()) / 'wam'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / 'cli.py').write_text(
        'import json, os, sys\n'
        'def main():\n'
        '    print(json.dumps({"marker": "HOST", "argv": sys.argv[1:],\n'
        '                      "guard": os.environ.get("WAM_FROM_BUILDER_BUNDLE")}))\n'
        '    return 0\n'
    )
    box = host / 'bin' / 'wam'
    box.write_text(
        f'#!{python}\n'
        # Report import provenance before importing cli: even a dependency-free host venv gives a
        # precise stale-bundle failure, rather than an incidental missing dependency traceback.
        'import importlib.util, json, sys\n'
        'print(json.dumps({"cli": importlib.util.find_spec("wam.cli").origin}), flush=True)\n'
        'from wam.cli import main\n'
        'if __name__ == "__main__":\n'
        '    sys.exit(main())\n'
    )
    box.chmod(0o755)
    env = {**os.environ, 'WAM_BOX_BIN': str(box), 'HOME': str(tmp_path), 'PYTHONNOUSERSITE': '1'}
    env.pop(bd.GUARD, None)
    env.pop('PYTHONPATH', None)
    argv = ['links', '--help', 'argument with spaces', '']
    # Prove the synthetic installation works independently, without pip or real dependencies.
    direct = subprocess.run([str(box), *argv], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout.splitlines()[-1]) == {'marker': 'HOST', 'argv': argv, 'guard': None}

    os_home = tmp_path / 'wizard-os'
    assert bb.install(os_home)['state'] == 'installed'
    launcher = os_home / 'builder' / 'current' / 'bin' / 'wam'
    p = subprocess.run([str(launcher), *argv], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=20)
    provenance = json.loads(p.stdout.splitlines()[0])
    assert Path(provenance['cli']) == package / 'cli.py', 'delegated console script imported the stale bundle'
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout.splitlines()[-1]) == {'marker': 'HOST', 'argv': argv, 'guard': '1'}


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
