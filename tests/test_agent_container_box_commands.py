"""Inside the stock agent's container, box commands stop with one plain line (wizard-app-manager#589).

10 Nov 2026, fresh WizardOS install: the agent ran `wam` from the builder bundle in its own container, where there is no
box wam, no Docker socket and no Runtipi. `wam app-updates --apply wizardos-tracker` crashed on a missing
runtipi/.env, `wam doctor` failed its App Manager rows, and `wam builder-bundle fresh` wrote an override under the
container's own paths and claimed `restart_required: true`, so the agent told the owner a restart would fix it.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import builder_bundle as bb, bundle_delegate as bd, cli  # noqa: E402

BOX = ['app-updates', 'install', 'uninstall', 'updates', 'links', 'apps', 'views', 'app', 'grants', 'network',
       'alongside', 'bootstrap', 'builder-skill', 'agent-link', 'maintain']


def _bundle(tmp_path):
    root = tmp_path / 'releases' / ('a' * 64)
    (root / 'wam').mkdir(parents=True)
    (root / 'bundle.json').write_text(json.dumps({'owner': bd.MARKER, 'schema': 1}))
    return root


@pytest.fixture
def container(tmp_path, monkeypatch):
    """No box wam, no Runtipi .env, HOME inside the container."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('WAM_BOX_BIN', str(tmp_path / 'no-box-wam'))
    monkeypatch.delenv('RUNTIPI_ROOT', raising=False)
    monkeypatch.delenv('WIZARD_OS_HOME', raising=False)
    monkeypatch.delenv(bd.GUARD, raising=False)
    return _bundle(tmp_path)


def _stop(argv, root):
    said = []
    return bd.container_stop(argv, source=root, out=said.append), said


@pytest.mark.parametrize('cmd', BOX)
def test_box_commands_stop_in_the_agent_container(container, cmd):
    assert _stop([cmd, 'x'], container) == (True, [bd.BOX_ONLY])


@pytest.mark.parametrize('action', ['fresh', 'install', 'restore-local-copy'])
def test_builder_bundle_box_actions_stop(container, action):
    assert _stop(['builder-bundle', action], container) == (True, [bd.BOX_ONLY])


@pytest.mark.parametrize('argv', [[c, 'src'] for c in sorted(bd.BUILDER_COMMANDS)]
                         + [['search', 'x'], ['doctor'], ['--help'], [], ['builder-bundle', 'doctor'],
                            ['builder-bundle', 'rollback', '--revision', 'r']])
def test_builder_commands_doctor_and_search_keep_working(container, argv):
    assert _stop(argv, container) == (False, [])


def test_a_box_with_runtipi_is_not_the_container(container, tmp_path, monkeypatch):
    rt = tmp_path / 'home' / 'runtipi'
    rt.mkdir()
    (rt / '.env').write_text('INTERNAL_IP=127.0.0.1\n')
    assert _stop(['app-updates', '--apply', 'x'], container) == (False, [])
    other = tmp_path / 'elsewhere'
    other.mkdir()
    (other / '.env').write_text('')
    (rt / '.env').unlink()
    monkeypatch.setenv('RUNTIPI_ROOT', str(other))
    assert _stop(['install', 'x'], container) == (False, [])


def test_the_configured_runtipi_root_counts(container, tmp_path, monkeypatch):
    osh = tmp_path / 'osh'
    osh.mkdir()
    other = tmp_path / 'rt'
    other.mkdir()
    (other / '.env').write_text('')
    (osh / 'app-manager.json').write_text(json.dumps({'runtipi_root': str(other)}))
    monkeypatch.setenv('WIZARD_OS_HOME', str(osh))
    assert _stop(['links'], container) == (False, [])


def test_a_checkout_wam_never_stops(container):
    assert _stop(['app-updates', '--apply', 'x'], bd.SOURCE) == (False, [])


def test_fresh_refuses_in_the_container_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, 'in_agent_container', lambda *a, **k: True)
    c = SimpleNamespace(home=tmp_path / 'osh', runtipi_root=tmp_path / 'runtipi')
    with pytest.raises(ValueError, match="This runs on the box"):
        bb.fresh(c)
    assert not (tmp_path / 'osh').exists() and not (tmp_path / 'runtipi').exists()


def test_doctor_in_the_container_reports_box_rows_not_applicable(tmp_path, monkeypatch):
    monkeypatch.setattr(bd, 'in_agent_container', lambda *a, **k: True)
    monkeypatch.setattr(bb, 'health', lambda *a: [('Wizard builder bundle and host tooling', True, 'ok')])
    monkeypatch.setattr(bb, 'runtime_health', lambda c: pytest.fail('probes the box Docker from the container'))
    monkeypatch.setattr(cli.index, 'search', lambda *a: [1, 2])
    c = SimpleNamespace(home=tmp_path, runtipi_root=tmp_path / 'runtipi', index='idx',
                        runtipi=lambda: pytest.fail('calls the App Manager API from the container'))
    rows = cli.doctor(c, fix=True)
    by = {n: (ok, d) for n, ok, d in rows}
    for label in ('Wizard builder agent terminal reachability', 'App Manager API reachable'):
        assert by[label] == (None, bd.NOT_APPLICABLE)
    assert by['index reachable'][0] is True
    assert not [r for r in rows if r[1] is False]


def test_installed_launcher_in_a_container_end_to_end(tmp_path):
    """The real installed bundle launcher with no box wam and no Runtipi: box commands stop, nothing is written."""
    os_home = tmp_path / 'wizard-os'
    bb.install(os_home)
    home = tmp_path / 'home'
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ('RUNTIPI_ROOT', bd.GUARD, 'PYTHONPATH')}
    env.update(HOME=str(home), WAM_BOX_BIN=str(tmp_path / 'no-box-wam'), WIZARD_OS_HOME=str(os_home),
               WAM_INDEX='http://127.0.0.1:9')
    launcher = os_home / 'builder' / 'current' / 'bin' / 'wam'

    def run(*argv):
        return subprocess.run([str(launcher), *argv], env=env, cwd=tmp_path, capture_output=True, text=True,
                              timeout=90)

    before = sorted(p for p in tmp_path.rglob('*') if 'builder' not in p.relative_to(tmp_path).parts[:2])
    for argv in (['app-updates', '--apply', 'wizardos-tracker'], ['builder-bundle', 'fresh'], ['install', 'x'],
                 ['links', '--json']):
        p = run(*argv)
        assert p.returncode == 1 and p.stderr.strip() == bd.BOX_ONLY and p.stdout == '', (argv, p.stdout, p.stderr)
    assert not (os_home / 'builder' / 'restart-required').exists()
    assert not (home / 'runtipi').exists()
    after = sorted(p for p in tmp_path.rglob('*') if 'builder' not in p.relative_to(tmp_path).parts[:2])
    assert after == before
    p = run('doctor')
    assert 'N/A   App Manager API reachable  ' + bd.NOT_APPLICABLE in p.stdout, p.stdout + p.stderr
    assert 'N/A   Wizard builder agent terminal reachability' in p.stdout
    assert 'FAIL  App Manager API reachable' not in p.stdout
    assert not (os_home / 'api-token').exists()
    p = run('new', '--help')
    assert p.returncode == 0, p.stderr
