"""Synthetic seams on the exact deployed host baseline; no owner state or Docker."""
import json
from io import BytesIO
from types import SimpleNamespace
import pytest
from wam import app_updates as au, cli, updates
from test_app_updates import row


def test_existing_recipe_approval_refusal_is_needs_owner_not_rollback(tmp_path, monkeypatch):
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'rt',
                        runtipi=lambda: SimpleNamespace(installed=lambda: [row('demo', 1, 2)]))
    monkeypatch.setattr(updates, 'installing', lambda h: False)
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    monkeypatch.setattr(au, '_containers', lambda *a: [])
    monkeypatch.setattr(au, 'baseline', lambda *a: None)
    def refusal(*a, **kw):
        raise cli.host_policy.NeedsOwnerApproval('demo', 'a' * 64, ['host access'])
    monkeypatch.setattr(cli, 'app_update', refusal)
    monkeypatch.setattr(au, 'rollback', lambda *a: pytest.fail('refused approval tried rollback'))
    monkeypatch.setattr(au, 'verify', lambda *a: pytest.fail('refused approval reached verification'))
    monkeypatch.setattr(cli, 'write_registry', lambda *a: {})
    result = au.apply(c, notify=False)
    assert [r['id'] for r in result['needs_owner']] == ['demo']
    assert result['failed'] == result['rolled_back'] == result['updated'] == []


def test_settings_manual_post_and_real_get_use_same_synthetic_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    cfg = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt',
                          runtipi=lambda: SimpleNamespace(installed=lambda: []))
    monkeypatch.setattr(au, '_release_managed', lambda: set())
    monkeypatch.setattr(updates, 'status', lambda: {'mode': 'ask', 'update_available': False})
    original = au.ensure_timer
    monkeypatch.setattr(au, 'ensure_timer', lambda h, *a, **kw: original(h, tmp_path / 'units', False))
    handler = cli.StoreHandler.__new__(cli.StoreHandler)
    handler.cfg = cfg
    payload = json.dumps({'app_mode': 'ask'}).encode()
    handler.rfile = BytesIO(payload)
    handler.headers = {'Content-Length': str(len(payload))}
    sent = []
    handler._json = lambda code, body: sent.append((code, body))
    handler._updates('POST')
    handler._updates('GET')
    assert [r[0] for r in sent] == [200, 200]
    assert all(r[1]['apps']['app_mode'] == 'ask' and r[1]['apps']['chosen'] for r in sent)
    assert json.loads((home / au.STATE).read_text())['mode'] == 'ask'
    assert not list((tmp_path / 'units').glob('*.timer'))
