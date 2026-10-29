"""Owner code is held, never inferred as permission to replace, including explicit bulk updates."""
import json
from types import SimpleNamespace

import pytest

from wam import app_updates as au, packaging, source_integrity as si
from wam import cli, updates


def installed(tmp_path):
    root = tmp_path / 'runtipi'
    live = root / 'apps/wizardapps/demo'
    live.mkdir(parents=True)
    (live / 'wizard.md').write_text('---\nid: demo\nruntime:\n  package: source\n---\n')
    (live / '.wizard-commit').write_text('a' * 40)
    source = packaging.source_dir(packaging.app_data_root(root), 'wizardapps', 'demo', 'a' * 40)
    source.mkdir(parents=True)
    (source / 'code.py').write_text('upstream')
    return root, live, source


@pytest.mark.parametrize('mutation', ['change', 'add', 'delete', 'link', 'unknown'])
def test_changed_or_unrecorded_source_cannot_update(tmp_path, mutation):
    root, live, source = installed(tmp_path)
    si.record(source)
    assert si.update_block(root, 'wizardapps', 'demo') is None
    if mutation == 'change':
        (source / 'code.py').write_text('owner')
    elif mutation == 'add':
        (source / 'my.py').write_text('owner')
    elif mutation == 'delete':
        (source / 'code.py').unlink()
    elif mutation == 'link':
        (source / 'alias').symlink_to('code.py')
    else:
        si.record_path(source).unlink()
    assert si.update_block(root, 'wizardapps', 'demo') == 'owner-source'
    row = {'app': {'appName': 'demo', 'version': 1, 'status': 'running', 'appStoreSlug': 'wizardapps'},
           'metadata': {'latestVersion': 2}}
    assert au.candidates([row], store='wizardapps', runtipi_root=root, home=tmp_path / 'home',
                         release_managed=set())[0]['skip'] == 'owner-source'
    class NoMutation:
        def app(self, urn):
            return {"app": {"status": "running"}}
        def __getattr__(self, name):
            raise AssertionError(f'update touched Runtipi: {name}')
    with pytest.raises(SystemExit, match='nothing replaced'):
        cli.app_update(SimpleNamespace(runtipi_root=root, runtipi=lambda: NoMutation()), 'demo:wizardapps')
    packaging.prune(packaging.app_data_root(root), 'wizardapps', 'demo', 'different')
    assert source.is_dir()


def test_packaging_is_idempotent_but_never_replaces_owner_bytes(tmp_path):
    root, live, source = installed(tmp_path)
    upstream = tmp_path / 'checkout'
    upstream.mkdir()
    (upstream / 'code.py').write_text('upstream')
    c = {'services': {'demo': {'image': 'python'}}}
    assert packaging.prepare_package('source', c, upstream, source, 'demo', 'a' * 40)
    assert si.pristine(source)
    (source / 'code.py').write_text('owner')
    with pytest.raises(packaging.PackagingError, match='nothing replaced'):
        packaging.prepare_package('source', c, upstream, source, 'demo', 'a' * 40)
    assert (source / 'code.py').read_text() == 'owner'
    packaging.prune(packaging.app_data_root(root), 'wizardapps', 'demo', 'new')
    assert source.exists()


def test_source_inventory_format_and_link_are_portable(tmp_path):
    import hashlib
    source = tmp_path / 'src'
    source.mkdir()
    (source / 'code').write_text('upstream')
    (source / 'empty').mkdir()
    (source / 'alias').symlink_to('code')
    assert si.inventory(source) == {'code': {'sha256': hashlib.sha256(b'upstream').hexdigest()},
                                    'empty': {'directory': True}, 'alias': {'link': 'code'}}


def test_manual_choice_survives_reconciliation_and_no_default_is_consent(tmp_path, monkeypatch):
    home, units = tmp_path / 'home', tmp_path / 'units'
    monkeypatch.setattr(au, '_units_dir', lambda: units)
    au.set_mode('ask', home, units, systemctl=False)
    assert au.ensure_timer(home, units, systemctl=False)['app_timer'] is False
    assert au.mode(home) == 'ask' and not (units / f'{au.UNIT}.timer').exists()
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: SimpleNamespace(installed=lambda: []))
    assert au.status(c)['chosen'] is True
    assert au.apply_if_auto(c, is_busy=lambda: pytest.fail('Manual queried activity'))['started'] is False
    other = SimpleNamespace(home=tmp_path / 'fresh', runtipi_root=c.runtipi_root, runtipi=c.runtipi)
    assert au.status(other)['app_mode'] == 'auto' and au.status(other)['chosen'] is False


def test_queued_timer_rechecks_mode_but_manual_update_still_runs(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    units = tmp_path / 'units'
    au.set_mode('ask', home, units, systemctl=False)
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: pytest.fail('automatic mutated'))
    monkeypatch.setattr(updates, 'installing', lambda h: False)
    assert au.apply(c, automatic=True, notify=False)['started'] is False
    calls = []
    c.runtipi = lambda: SimpleNamespace(installed=lambda: calls.append('check') or [])
    assert au.apply(c, dry_run=True, notify=False)['dry_run'] is True
    assert calls == ['check'] and au.mode(home) == 'ask'


def test_manual_stops_next_app_in_already_running_batch(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    units = tmp_path / 'units'
    calls = []
    rows = [{'id': a, 'from': 1, 'to': 2, 'status': 'running', 'skip': None} for a in ['first', 'second']]
    c = SimpleNamespace(home=home, runtipi_root=tmp_path / 'rt', runtipi=lambda: SimpleNamespace(installed=lambda: []))
    monkeypatch.setattr(updates, 'installing', lambda h: False)
    monkeypatch.setattr(au, 'candidates', lambda *a, **kw: rows)
    monkeypatch.setattr(au, '_containers', lambda urn: ['running'])
    monkeypatch.setattr(au, 'baseline', lambda *a: True)
    monkeypatch.setattr(au, 'verify', lambda *a: None)
    monkeypatch.setattr(cli, 'STORE', 'wizardapps')
    def update(*a):
        calls.append(a[-1])
        au.set_mode('ask', home, units, systemctl=False)
        return {'version': 2, 'status': 'running'}
    monkeypatch.setattr(cli, 'app_update', update)
    result = au.apply(c, automatic=True, notify=False)
    assert calls == ['first'] and result['updated'] == ['first']
