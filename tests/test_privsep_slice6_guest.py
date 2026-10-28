"""Slice 6 fixtures must be selected from the guest index and survive phases."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def guest():
    path = Path(__file__).parents[1] / 'qa/privsep-slice6/guest.py'
    spec = importlib.util.spec_from_file_location('slice6guest', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guest_selects_three_observed_apps_without_assuming_quest(guest):
    catalog = [{'id': name} for name in ('wizard-calendar', 'wizard-notes', 'wizard-shopping', 'unrelated')]
    assert guest.select_apps(catalog) == ['wizard-calendar', 'wizard-shopping', 'wizard-notes']


def test_guest_selects_available_alternatives(guest):
    catalog = [{'id': name} for name in ('wizard-stories', 'wizard-calendar', 'wizard-reference')]
    assert set(guest.select_apps(catalog)) == {row['id'] for row in catalog}


@pytest.mark.parametrize('ids', [('wizard-calendar', 'wizard-shopping'), ('wizard-notes', 'wizard-shopping', 'wizard-stories')])
def test_missing_data_fixture_or_three_apps_cannot_pass(guest, ids):
    with pytest.raises(AssertionError):
        guest.select_apps([{'id': name} for name in ids])


def test_guest_queries_catalog_before_first_install_and_persists_selection(guest, tmp_path, monkeypatch):
    selection = tmp_path / 'apps.json'
    monkeypatch.setattr(guest, 'SELECTION', selection)
    monkeypatch.setattr(guest.os, 'getuid', lambda: 0)
    calls = []
    class FirstInstall(Exception): pass
    def agent(*args):
        calls.append(args)
        if args == ('wam', 'search', '--json'):
            return json.dumps([{'id': name} for name in ('wizard-calendar', 'wizard-notes', 'wizard-shopping')])
        assert args == ('wam', 'install', 'wizard-calendar')
        assert json.loads(selection.read_text()) == ['wizard-calendar', 'wizard-shopping', 'wizard-notes']
        raise FirstInstall
    monkeypatch.setattr(guest, 'agent', agent)
    with pytest.raises(FirstInstall): guest.main(['legacy', 'a' * 40])
    assert calls == [('wam', 'search', '--json'), ('wam', 'install', 'wizard-calendar')]


def test_later_phase_loads_exact_selected_apps_without_new_catalog_lookup(guest, tmp_path, monkeypatch):
    selection = tmp_path / 'apps.json'
    apps = ['wizard-calendar', 'wizard-reference', 'wizard-stories']
    selection.write_text(json.dumps(apps))
    monkeypatch.setattr(guest, 'SELECTION', selection)
    monkeypatch.setattr(guest.os, 'getuid', lambda: 0)
    monkeypatch.setattr(guest, 'agent', lambda *a: pytest.fail('checkpoint must not query the catalog'))
    class Checkpoint(Exception): pass
    def hashes(root):
        assert guest.APPS == apps
        raise Checkpoint
    monkeypatch.setattr(guest, 'hashes', hashes)
    with pytest.raises(Checkpoint): guest.main(['checkpoint', 'a' * 40])


@pytest.mark.parametrize('read_only,content', [(True, 'grandfathered host access\n'), (False, 'grandfathered host access\n'), (True, 'wrong')])
def test_live_host_mount_and_canary_are_required(guest, monkeypatch, read_only, content):
    def run(argv):
        if argv[1] == 'ps': return 'synthetic-cid\n'
        if argv[1] == 'inspect':
            return json.dumps([{'Source': '/srv/slice6-host', 'Destination': '/preexisting', 'RW': not read_only}])
        return content
    monkeypatch.setattr(guest, 'run', run)
    if read_only and content == 'grandfathered host access\n': guest.host_canary()
    else:
        with pytest.raises(AssertionError): guest.host_canary()
