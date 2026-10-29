"""Synthetic lifecycle admission regressions; no engine or personal data."""
import json
from types import SimpleNamespace

import pytest
from wam import cli, owner, network, final_host_tokens as fht


@pytest.mark.parametrize('surface', ['browser', 'cli'])
@pytest.mark.parametrize('failure', [False, True])
def test_deny_stops_after_revocation_without_reading_recipe_risks(tmp_path, monkeypatch, surface, failure):
    root, home = tmp_path / 'runtipi', tmp_path / 'home'
    root.mkdir(); home.mkdir()
    monkeypatch.setenv('RUNTIPI_ROOT', str(root))
    monkeypatch.setenv('WIZARD_OS_HOME', str(home))
    monkeypatch.setattr(cli.release_pins, 'load', lambda *a, **k: None)
    c = SimpleNamespace(home=home, runtipi_root=root, runtipi=lambda: object())
    fht.record(c, 'demo', found=['web|privileged'])
    network.set_host_access_allowed(home, 'demo', True)
    monkeypatch.setattr(cli.Config, 'runtipi', lambda self: object())
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'demo'})
    monkeypatch.setattr(cli, 'host_access_risks', lambda *a, **k: pytest.fail('Deny must not inspect recipe risks'))
    calls = []

    def stop(config, action, app):
        assert json.loads(fht.path_for(c).read_text())['apps'] == {}
        assert network.host_access_allowed(home) == set()
        calls.append((action, app))
        return {'error': 'synthetic daemon failure'} if failure else {'success': True}

    monkeypatch.setattr(cli, 'app_lifecycle', stop)
    monkeypatch.setattr(network, 'reconcile', lambda *a, **k: calls.append('reconcile') or {'errors': []})
    if surface == 'browser':
        if failure:
            with pytest.raises(RuntimeError, match='stopping the app failed'):
                owner.execute(c, '/api/network', {'action': 'deny', 'app': 'demo'})
        else:
            owner.execute(c, '/api/network', {'action': 'deny', 'app': 'demo'})
    else:
        result = cli.main(['network', 'deny', 'demo'])
        assert result != 0 if failure else result == 0
    assert calls == [('stop', 'demo')] + ([] if failure else ['reconcile'])
    assert json.loads(fht.path_for(c).read_text())['apps'] == {}
    assert network.host_access_allowed(home) == set()


def test_bookkeeping_failure_does_not_suppress_safe_stop(tmp_path, monkeypatch):
    from wam import host_policy
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'runtipi', runtipi=lambda: object())
    fht.record(c, 'demo', found=['web|privileged'])
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'demo'})
    calls = []
    def stop(*args):
        assert json.loads(fht.path_for(c).read_text())['apps'] == {}
        calls.append('stop')
        return {'success': True}
    monkeypatch.setattr(cli, 'app_lifecycle', stop)
    monkeypatch.setattr(network, 'set_host_access_allowed', lambda *a: (_ for _ in ()).throw(ValueError('bookkeeping failed')))
    with pytest.raises(ValueError, match='bookkeeping failed'):
        host_policy.revoke_and_stop(c, 'demo')
    assert calls == ['stop']
    assert json.loads(fht.path_for(c).read_text())['apps'] == {}
