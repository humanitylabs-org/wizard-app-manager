"""No real runtime or service is touched by these policy regressions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import cli, hermes_update, release_pins
from test_release_pins import box  # isolated installer sandbox fixture


def test_installer_refuses_hermes_recipe_drift_before_checkout(box):
    path = box['rel'] / '9.9.9.json'
    release = json.loads(path.read_text())
    release['default_apps']['agent']['commit'] = 'a' * 40
    path.write_text(json.dumps(release))
    result, _ = box['run']()
    assert result.returncode != 0 and 'shared exact Hermes lock' in result.stderr
    assert not (box['home'] / 'install-env').exists()


def test_installer_missing_lock_fails_closed(box):
    (box['rel'] / 'hermes-lock.json').unlink()
    result, _ = box['run']()
    assert result.returncode != 0 and 'Could not read the shared Hermes lock' in result.stderr
    assert not (box['home'] / 'install-env').exists()


def test_host_update_is_paused_before_lock_or_runtime_access(tmp_path, monkeypatch):
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    monkeypatch.setattr(hermes_update, '_lock', lambda *a: pytest.fail('must not touch a runtime lock'))
    out = hermes_update.update(tmp_path)
    assert out['state'] == 'refused' and 'paused' in out['reason']
    assert not list(tmp_path.iterdir())


def test_app_update_is_paused_before_runtime_access(monkeypatch):
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    c = SimpleNamespace(runtipi=lambda: pytest.fail('must not contact runtime'))
    for app in ('hermes-agent', 'hermes-agent:wizardapps', 'hermes-agent:otherstore'):
        with pytest.raises(SystemExit, match='paused'):
            cli.app_update(c, app)


def test_unpinned_fresh_install_resolves_exact_recipe(monkeypatch):
    monkeypatch.delenv('WIZARD_RELEASE_PINS', raising=False)
    pin = release_pins.resolve('hermes-agent')
    assert pin['commit'] == 'e8ae558f26bf4b981e0cad60e387386a1ad7b869'
    assert pin['repo'] == 'oscarhenrycollins/hermes-agent'
    assert release_pins.resolve('unrelated') is None


def test_host_target_refuses_an_arbitrary_snapshot_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_update.updates, 'installed', lambda _: '1.0.0')
    (tmp_path / '1.0.0.json').write_text(json.dumps({'hermes': {'tag': 'v0.22.5', 'commit': 'a' * 40}}))
    with pytest.raises(ValueError, match='shared exact lock'):
        hermes_update.target(tmp_path, tmp_path.as_uri())


def test_release_rerun_skips_hermes_but_updates_unrelated_app(tmp_path, monkeypatch):
    rows = [{'app': a, 'repo': 'fixture/' + a, 'commit': 'b' * 40} for a in ('hermes-agent', 'files')]
    monkeypatch.setattr(cli.release_pins, 'load', lambda: {'default_apps': dict(enumerate(rows))})
    monkeypatch.setattr(cli, 'installed_ids', lambda _: {'hermes-agent', 'files'})
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    c = SimpleNamespace(runtipi_root=tmp_path)
    for row in rows:
        live = tmp_path / 'apps' / cli.STORE / row['app']; live.mkdir(parents=True)
        (live / '.wizard-commit').write_text('a' * 40)
        (live / '.wizard-source').write_text(json.dumps({'repo': row['repo']}))
    calls = []
    monkeypatch.setattr(cli, 'resolve_any', lambda _, app: calls.append(('resolve', app)))
    monkeypatch.setattr(cli, 'app_update', lambda _, app: calls.append(('update', app)) or {})
    assert cli.release_updates(c, object(), {}) == ['files']
    assert calls == [('resolve', 'files'), ('update', 'files')]


def test_flag_first_or_bulk_passthrough_cannot_bypass_pause(tmp_path, monkeypatch):
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    monkeypatch.setattr(cli.subprocess, 'call', lambda *a, **kw: pytest.fail('must not invoke runtipi-cli'))
    for args in (['app', 'update', '--no-backup', 'hermes-agent'], ['app', 'update', '--all']):
        with pytest.raises(ValueError, match='paused'):
            cli.runtipi_cli(tmp_path, args)


def test_public_lock_and_packaged_lock_are_identical():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'releases/hermes-lock.json').read_bytes() == (root / 'wam/hermes-lock.json').read_bytes()
