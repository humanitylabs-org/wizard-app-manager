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


def test_guest_queries_catalog_first_and_skips_apps_main_cannot_install(guest, tmp_path, monkeypatch):
    selection = tmp_path / 'apps.json'
    monkeypatch.setattr(guest, 'SELECTION', selection)
    monkeypatch.setattr(guest.os, 'getuid', lambda: 0)
    calls = []
    class AfterInstalls(Exception): pass
    def agent(*args):
        calls.append(args)
        if args == ('wam', 'search', '--json'):
            return json.dumps([{'id': n} for n in ('wizard-calendar', 'wizard-notes', 'wizard-shopping', 'profiles')])
        if args == ('wam', 'install', 'wizard-notes'):
            raise AssertionError('wizard-notes: install failed')
        if args[:2] == ('wam', 'install'):
            return ''
        raise AfterInstalls
    monkeypatch.setattr(guest, 'agent', agent)
    def stop_mkdir(*a, **k): raise AfterInstalls
    monkeypatch.setattr(guest.Path, 'mkdir', stop_mkdir)
    with pytest.raises(AfterInstalls): guest.main(['legacy', 'a' * 40])
    assert calls[0] == ('wam', 'search', '--json')
    assert json.loads(selection.read_text()) == ['wizard-calendar', 'wizard-shopping', 'profiles']


def test_calendar_install_failure_is_never_skipped(guest, tmp_path, monkeypatch):
    monkeypatch.setattr(guest, 'SELECTION', tmp_path / 'apps.json')
    monkeypatch.setattr(guest.os, 'getuid', lambda: 0)
    def agent(*args):
        if args == ('wam', 'search', '--json'):
            return json.dumps([{'id': n} for n in ('wizard-calendar', 'wizard-notes', 'wizard-shopping')])
        raise AssertionError('install failed')
    monkeypatch.setattr(guest, 'agent', agent)
    with pytest.raises(AssertionError): guest.main(['legacy', 'a' * 40])


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


def test_select_apps_uses_unprefixed_public_index_ids(guest):
    catalog = [{'id': name} for name in ('wizard-calendar', 'profiles', 'wisdom', 'transcriber')]
    assert guest.select_apps(catalog) == ['wizard-calendar', 'profiles', 'wisdom']


def test_select_candidates_puts_calendar_first_and_keeps_fallbacks(guest):
    catalog = [{'id': n} for n in ('wisdom', 'wizard-notes', 'wizard-calendar', 'profiles', 'money')]
    assert guest.select_candidates(catalog) == ['wizard-calendar', 'wizard-notes', 'profiles', 'wisdom', 'money']


@pytest.mark.parametrize('verb,status', [('stop', 'stopped'), ('start', 'running'), ('restart', 'running')])
def test_app_lifecycle_never_dispatches_to_runtipi_global_controls(guest, monkeypatch, verb, status):
    calls = []
    result = {'app': 'wizard-calendar', 'status': status}
    def agent(*args):
        calls.append(args)
        return json.dumps(result)
    monkeypatch.setattr(guest, 'agent', agent)
    assert guest.app_lifecycle(verb, 'wizard-calendar') == result
    assert calls == [('wam', 'app', verb, 'wizard-calendar')]


@pytest.mark.parametrize('result', [
    {},
    {'app': 'wizard-calendar', 'status': 'stopped'},
    {'app': 'other-app', 'status': 'running'},
    {'app': 'wizard-calendar', 'status': 'running', 'error': 'start failed'},
])
def test_successful_process_without_expected_app_state_is_not_lifecycle_success(guest, monkeypatch, result):
    monkeypatch.setattr(guest, 'agent', lambda *args: json.dumps(result))
    with pytest.raises(AssertionError): guest.app_lifecycle('start', 'wizard-calendar')


def test_global_cli_text_is_not_app_lifecycle_success(guest, monkeypatch):
    monkeypatch.setattr(guest, 'agent', lambda *args: 'Runtipi started successfully')
    with pytest.raises(ValueError): guest.app_lifecycle('start', 'wizard-calendar')
