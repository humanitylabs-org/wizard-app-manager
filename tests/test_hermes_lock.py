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
    assert pin['commit'] == '8db4cc2aa51b9b91cd9369631b2aa47b4dc48c1d'
    assert pin['repo'] == 'oscarhenrycollins/hermes-agent'
    assert release_pins.resolve('unrelated') is None


def test_host_target_refuses_an_arbitrary_snapshot_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(hermes_update.updates, 'installed', lambda _: '1.0.0')
    (tmp_path / '1.0.0.json').write_text(json.dumps({'hermes': {'tag': 'v0.22.5', 'commit': 'a' * 40}}))
    with pytest.raises(ValueError, match='shared exact lock'):
        hermes_update.target(tmp_path, tmp_path.as_uri())


@pytest.mark.parametrize('promotion', [None, '', 'stale-review-id', 'invalid'])
def test_release_rerun_skips_hermes_but_updates_unrelated_app(tmp_path, monkeypatch, promotion):
    rows = [{'app': a, 'repo': 'fixture/' + a, 'commit': 'b' * 40} for a in ('hermes-agent', 'files')]
    monkeypatch.setattr(cli.release_pins, 'load', lambda: {'default_apps': dict(enumerate(rows))})
    monkeypatch.setattr(cli, 'installed_ids', lambda _: {'hermes-agent', 'files'})
    if promotion is None:
        monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    else:
        monkeypatch.setenv('WIZARD_HERMES_PROMOTION', promotion)
    c = SimpleNamespace(runtipi_root=tmp_path)
    for row in rows:
        live = tmp_path / 'apps' / cli.STORE / row['app']; live.mkdir(parents=True)
        (live / '.wizard-commit').write_text('a' * 40)
        (live / '.wizard-source').write_text(json.dumps({'repo': row['repo']}))
    staged = tmp_path / 'repos' / cli.STORE / 'apps' / 'hermes-agent'
    staged.mkdir(parents=True)
    (staged / '.wizard-selection').write_text('original selection\n')
    (staged / '.wizard-commit').write_text('a' * 40)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    calls = []
    from test_installed_default_release_pins import package
    def resolve(_, app):
        calls.append(('resolve', app))
        if app == 'hermes-agent':
            (staged / '.wizard-selection').write_text('poisoned selection\n')
            (staged / '.wizard-commit').write_text('b' * 40)
        package(tmp_path, app, 'b' * 40, '1', store=True)
        return {}
    def update(_, app, **k):
        calls.append(('update', app))
        package(tmp_path, app, 'b' * 40, '1')
        return {'status': 'stopped'}
    monkeypatch.setattr(cli, 'resolve_any', resolve)
    monkeypatch.setattr(cli, 'app_update', update)
    rt = SimpleNamespace(app=lambda _: {'app': {'status': 'stopped', 'config': {'openPort': False, 'exposed': False, 'exposedLocal': False}}})
    assert cli.release_updates(c, rt, {}) == ['files']
    assert calls == [('resolve', 'files'), ('update', 'files')]
    assert {p.relative_to(tmp_path): p.read_bytes() for p in staged.rglob('*') if p.is_file()} == {
        p: data for p, data in before.items() if p.parts[:4] == ('repos', cli.STORE, 'apps', 'hermes-agent')}
    assert (tmp_path / 'apps' / cli.STORE / 'hermes-agent' / '.wizard-commit').read_text() == 'a' * 40


def test_release_rerun_authorizes_before_preparing_approved_hermes(tmp_path, monkeypatch):
    from wam import hermes_lock
    recipe = hermes_lock.load()['agent_recipe']
    row = {'app': 'hermes-agent', 'repo': recipe['repo'], 'commit': recipe['commit']}
    monkeypatch.setattr(cli.release_pins, 'load', lambda: {'default_apps': {'agent': row}})
    monkeypatch.setattr(cli, 'installed_ids', lambda _: {'hermes-agent'})
    monkeypatch.setenv('WIZARD_HERMES_PROMOTION', 'approved-test')
    live = tmp_path / 'apps' / cli.STORE / 'hermes-agent'
    live.mkdir(parents=True)
    (live / '.wizard-commit').write_text('a' * 40)
    (live / '.wizard-source').write_text(json.dumps({'repo': recipe['repo']}))
    calls = []
    monkeypatch.setattr(hermes_lock, 'authorize', lambda: calls.append('authorize') or 'approved-test')
    from test_installed_default_release_pins import package
    def prepare(*a):
        calls.append('resolve')
        package(tmp_path, 'hermes-agent', row['commit'], '1', store=True)
        return {}
    def update(*a, **k):
        calls.append('update')
        package(tmp_path, 'hermes-agent', row['commit'], '1')
        return {'status': 'stopped'}
    monkeypatch.setattr(cli, 'resolve_any', prepare)
    monkeypatch.setattr(cli, 'app_update', update)
    rt = SimpleNamespace(app=lambda _: {'app': {'status': 'stopped', 'config': {'openPort': False, 'exposed': False, 'exposedLocal': False}}})
    assert cli.release_updates(SimpleNamespace(runtipi_root=tmp_path), rt, {}) == ['hermes-agent']
    assert calls == ['authorize', 'resolve', 'update']


def test_flag_first_or_bulk_passthrough_cannot_bypass_pause(tmp_path, monkeypatch):
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    monkeypatch.setattr(cli.subprocess, 'call', lambda *a, **kw: pytest.fail('must not invoke runtipi-cli'))
    for args in (['app', 'update', '--no-backup', 'hermes-agent'], ['app', 'update', '--all']):
        with pytest.raises(ValueError, match='paused'):
            cli.runtipi_cli(tmp_path, args)


def test_runtime_client_forwards_only_explicit_approved_promotion(monkeypatch):
    from wam.runtipi import Runtipi
    from wam import hermes_lock
    client = object.__new__(Runtipi)
    calls = []
    monkeypatch.setattr(client, 'req', lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.delenv('WIZARD_HERMES_PROMOTION', raising=False)
    with pytest.raises(ValueError, match='paused'):
        client.update('hermes-agent:wizardapps')
    assert not calls
    client.update('files:wizardapps')
    assert calls[-1][0][2] == {'performBackup': True}
    monkeypatch.setattr(hermes_lock, 'authorize', lambda: 'fixture-approved')
    client.update('hermes-agent:wizardapps')
    assert calls[-1][0][2] == {'performBackup': True, 'hermesPromotion': 'fixture-approved'}


@pytest.mark.parametrize('event', ['push', 'pull_request'])
@pytest.mark.parametrize('policy_input', ['wam/hermes-lock.json', 'releases/hermes-lock.json', '.dockerignore'])
def test_backend_policy_inputs_trigger_image_rebuild(event, policy_input):
    import fnmatch
    import yaml
    root = Path(__file__).resolve().parents[1]
    # BaseLoader keeps GitHub's `on` key a string (not YAML 1.1 boolean True).
    workflow = yaml.load((root / '.github/workflows/image.yml').read_text(), Loader=yaml.BaseLoader)
    paths = workflow['on'][event]['paths']
    assert any(fnmatch.fnmatch(policy_input, pattern) for pattern in paths)


def test_future_release_image_inputs_cover_workflow_and_policy():
    import re
    root = Path(__file__).resolve().parents[1]
    text = (root / '.github/workflows/image.yml').read_text()
    # Same inline paths format consumed by wizard-os check_release.workflow_paths.
    required = {x.strip().strip('\"\'') for m in re.finditer(r'^\s*paths:\s*\[([^\]]*)\]', text, re.M)
                for x in m.group(1).split(',') if x.strip()}
    match = re.search(r'```json\n(.*?)\n```', (root / 'docs/hermes-update.md').read_text(), re.S)
    assert match, 'future release image_inputs must have a concrete policy-aware example'
    example = json.loads(match.group(1))
    assert required <= set(example)
    assert {'wam/hermes-lock.json', 'releases/hermes-lock.json', '.dockerignore'} <= required


def test_public_lock_and_packaged_lock_are_identical():
    root = Path(__file__).resolve().parents[1]
    assert (root / 'releases/hermes-lock.json').read_bytes() == (root / 'wam/hermes-lock.json').read_bytes()


def _pin_release(box, pin, recipe):
    path = box['rel'] / '9.9.9.json'
    release = json.loads(path.read_text())
    release['hermes'] = copy.deepcopy(pin)
    release['default_apps']['agent'] = {**release['default_apps']['agent'], **{k: recipe[k] for k in ('app', 'repo', 'commit')}}
    path.write_text(json.dumps(release))


def test_installer_takes_a_stable_release_on_the_replaced_pin(box):
    # wizard-app-manager#856: the promoted lock goes to For testers first, so For everyone's stable snapshot still pins
    # the replaced engine for 72h. A fresh install of it must not fail closed on the new shared lock.
    lock = json.loads((box['rel'] / 'hermes-lock.json').read_text())
    _pin_release(box, lock['previous']['hermes'], lock['previous']['agent_recipe'])
    result, _ = box['run']()
    assert 'shared exact' not in result.stderr, result.stderr
    assert (box['home'] / 'install-env').exists(), result.stdout + result.stderr


@pytest.mark.parametrize('mix', ['old-engine-new-recipe', 'new-engine-old-recipe'])
def test_installer_refuses_a_mix_of_replaced_and_locked_pins(box, mix):
    lock = json.loads((box['rel'] / 'hermes-lock.json').read_text())
    old, new = lock['previous'], lock
    pin, recipe = ((old['hermes'], new['agent_recipe']) if mix == 'old-engine-new-recipe'
                   else (new['hermes'], old['agent_recipe']))
    _pin_release(box, pin, recipe)
    result, _ = box['run']()
    assert result.returncode != 0 and 'shared exact' in result.stderr
    assert not (box['home'] / 'install-env').exists()


def test_release_pins_accept_the_replaced_pin_only_as_a_pair():
    from wam import hermes_lock
    lock = hermes_lock.load()
    old = lock['previous']
    rel = {'hermes': old['hermes'], 'default_apps': {'agent': old['agent_recipe']}}
    hermes_lock.check_release(rel)
    rel['default_apps']['agent'] = lock['agent_recipe']
    with pytest.raises(ValueError, match='frozen'):
        hermes_lock.check_release(rel)
    with pytest.raises(ValueError, match='shared exact lock'):
        hermes_lock.check_pin(old['hermes'])


def test_a_release_on_the_replaced_pin_installs_its_recipe(tmp_path, monkeypatch):
    from wam import hermes_lock
    lock = hermes_lock.load()
    old = lock['previous']
    pins = tmp_path / 'pins.json'
    release = {'images': {'wizard-app-manager': {'ref': 'ghcr.io/x/wam', 'digest': 'sha256:' + 'a' * 64},
                          'wizard-shell': {'ref': 'ghcr.io/x/shell', 'digest': 'sha256:' + 'b' * 64}},
               'hermes': old['hermes'], 'default_apps': {'agent': {**old['agent_recipe']}}}
    pins.write_text(json.dumps(release))
    monkeypatch.setenv('WIZARD_RELEASE_PINS', str(pins))
    assert release_pins.resolve('hermes-agent')['commit'] == old['agent_recipe']['commit']
    release['hermes'], release['default_apps']['agent'] = lock['hermes'], {**lock['agent_recipe']}
    pins.write_text(json.dumps(release))
    assert release_pins.resolve('hermes-agent')['commit'] == lock['agent_recipe']['commit']
