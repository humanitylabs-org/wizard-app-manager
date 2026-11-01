"""Release upgrades reconcile installed recipes, not just prepared store metadata."""
import json
import shutil
from types import SimpleNamespace

from wam import cli, registry, release_pins


def package(root, app, commit, version, *, store=False, fields=()):
    path = root / ('repos' if store else 'apps') / cli.STORE
    if store:
        path /= 'apps'
    path /= app
    path.mkdir(parents=True, exist_ok=True)
    (path / '.wizard-commit').write_text(commit + '\n')
    (path / '.wizard-source').write_text(json.dumps({'repo': 'example/' + app}))
    (path / 'wizard.md').write_text(f'---\nid: {app}\nversion: {version}\n---\n')
    (path / 'config.json').write_text(json.dumps({'version': version, 'form_fields': list(fields)}))
    return path


def box(tmp_path, monkeypatch, *, rollback=False, required_type='password', saved=None):
    doc = {'default_apps': {app: {'app': app, 'repo': 'example/' + app,
                                 'commit': 'b' * 40, 'index_version': '0.2.6'}
                            for app in ('wizard-claude', 'files')}}
    monkeypatch.setattr(release_pins, 'load', lambda: doc)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: set(doc['default_apps']))
    monkeypatch.setattr(cli, 'shared_value', lambda c, var, uses: 'synthetic-shared-password' if required_type == 'password' else None)
    configs = {app: dict(saved or {'port': 12345, 'openPort': False, 'exposed': False,
                                    'exposedLocal': False, 'OWNER_FIELD': 'keep'}) for app in doc['default_apps']}
    patches, updated = [], []
    for app in doc['default_apps']:
        package(tmp_path, app, 'a' * 40, '0.1.2')
    def prepare(c, app):
        fields = [{'env_variable': 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD', 'type': required_type,
                   'required': True}] if app == 'wizard-claude' else []
        package(tmp_path, app, 'b' * 40, '0.2.6', store=True, fields=fields)
        return {'id': app, 'uses': ['hermes-agent']}
    def req(method, route, form, **kw):
        assert method == 'PATCH' and route.endswith('/update-config')
        app = route.split('/')[2].split(':')[0]
        configs[app] = form
        patches.append(app)
    rt = SimpleNamespace(app=lambda urn: {'app': {'status': 'running', 'config': configs[urn.split(':')[0]]}}, req=req)
    def update(c, app):
        updated.append(app)
        if app == 'wizard-claude':
            assert configs[app].get('HERMES_DASHBOARD_BASIC_AUTH_PASSWORD')
        if not rollback:
            shutil.copytree(tmp_path / 'repos' / cli.STORE / 'apps' / app,
                            tmp_path / 'apps' / cli.STORE / app, dirs_exist_ok=True)
        return {'status': 'running'}  # a rolled-back update looks healthy too
    monkeypatch.setattr(cli, 'resolve_any', prepare)
    monkeypatch.setattr(cli, 'app_update', update)
    c = SimpleNamespace(runtipi=lambda: rt, runtipi_root=tmp_path, home=tmp_path / 'home', secrets=tmp_path / 'home/secrets')
    return c, rt, configs, patches, updated


def test_upgrade_brings_every_installed_default_app_to_release_pin_including_claude(tmp_path, monkeypatch):
    c, rt, configs, patches, updated = box(tmp_path, monkeypatch)
    failed = {}
    assert cli.release_updates(c, rt, failed) == ['wizard-claude', 'files']
    assert not failed
    assert patches == ['wizard-claude']
    assert configs['wizard-claude'] == {'port': 12345, 'openPort': False, 'exposed': False,
                                      'exposedLocal': False, 'OWNER_FIELD': 'keep',
                                      'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD': 'synthetic-shared-password'}
    # A second upgrade (including a clean current-stable install) is a no-op.
    assert cli.release_updates(c, rt, failed) == []
    assert updated == ['wizard-claude', 'files']


def test_running_rollback_is_a_failed_release_update_not_success(tmp_path, monkeypatch):
    c, rt, _, _, _ = box(tmp_path, monkeypatch, rollback=True)
    failed = {}
    assert cli.release_updates(c, rt, failed) == []
    assert set(failed) == {'wizard-claude', 'files'}
    assert all('does not match the release pin' in error for error in failed.values())
    meta = registry.app_meta(tmp_path, cli.STORE, 'wizard-claude')
    assert meta['w']['version'] == '0.1.2' and meta['commit'] == 'a' * 40


def test_new_required_owner_input_fails_without_attempting_update(tmp_path, monkeypatch):
    c, rt, _, patches, updated = box(tmp_path, monkeypatch, required_type='text')
    failed = {}
    assert cli.release_updates(c, rt, failed) == ['files']
    assert 'new required settings need owner input' in failed['wizard-claude']
    assert patches == [] and updated == ['files']


def test_upgrade_preserves_existing_credentials_and_owner_settings(tmp_path, monkeypatch):
    saved = {'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD': 'existing-synthetic-password', 'port': 13456,
             'openPort': False, 'exposed': False, 'exposedLocal': False, 'OWNER_FIELD': 'unchanged'}
    c, rt, configs, patches, _ = box(tmp_path, monkeypatch, saved=saved)
    assert cli.release_updates(c, rt, {}) == ['wizard-claude', 'files']
    assert not patches
    assert configs['wizard-claude'] == saved


def test_form_reuses_saved_passwords_and_preserves_other_saved_secrets(tmp_path, monkeypatch):
    c, _, _, _, _ = box(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'shared_value', lambda *a: None)
    c.secrets.mkdir(parents=True)
    p = c.secrets / 'wizard-claude.json'
    p.write_text(json.dumps({'OLD_PASSWORD': 'synthetic-old', 'RETRY_PASSWORD': 'synthetic-retry'}))
    fields = [{'type': 'password', 'env_variable': key, 'required': True}
              for key in ('RETRY_PASSWORD', 'NEW_PASSWORD')]
    form, missing = cli.build_form(c, 'wizard-claude', {'form_fields': fields}, {})
    saved = json.loads(p.read_text())
    assert not missing and form['RETRY_PASSWORD'] == 'synthetic-retry'
    assert saved == {'OLD_PASSWORD': 'synthetic-old', 'RETRY_PASSWORD': 'synthetic-retry',
                     'NEW_PASSWORD': form['NEW_PASSWORD']}
    assert cli.build_form(c, 'wizard-claude', {'form_fields': fields}, {})[0] == form
    assert p.stat().st_mode & 0o777 == 0o600


def test_real_dashboard_shared_input_works_without_recipe_uses_edge(tmp_path, monkeypatch):
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path)
    p = tmp_path / 'app-data' / cli.STORE / 'hermes-agent' / 'app.env'
    p.parent.mkdir(parents=True)
    p.write_text('HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=synthetic-agent-password\n')
    assert cli.shared_value(c, 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD', []) == 'synthetic-agent-password'
    from wam import alongside, host_dashboard
    monkeypatch.setattr(alongside, 'load', lambda _: {'mode': 'alongside'})
    h = host_dashboard.env_path(c.home)
    h.parent.mkdir(parents=True)
    h.write_text('HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=synthetic-host-password\n')
    assert cli.shared_value(c, 'HERMES_DASHBOARD_BASIC_AUTH_PASSWORD', []) == 'synthetic-host-password'


def test_release_update_preserves_stopped_state_and_rejects_unfinished_update(tmp_path, monkeypatch):
    c, rt, configs, _, _ = box(tmp_path, monkeypatch)
    monkeypatch.setattr(rt, 'app', lambda urn: {'app': {'status': 'stopped', 'config': configs[urn.split(':')[0]]}})
    update = cli.app_update
    def stopped(c, app):
        update(c, app)
        return {'status': 'stopped'}
    monkeypatch.setattr(cli, 'app_update', stopped)
    assert cli.release_updates(c, rt, {}) == ['wizard-claude', 'files']
    for app in configs:
        package(tmp_path, app, 'a' * 40, '0.1.2')
    monkeypatch.setattr(cli, 'app_update', lambda c, app: {'status': 'updating'})
    failed = {}
    assert cli.release_updates(c, rt, failed) == []
    assert all('did not finish' in e for e in failed.values())


def test_failed_settings_change_is_reported_and_does_not_attempt_update(tmp_path, monkeypatch):
    c, rt, _, _, updated = box(tmp_path, monkeypatch)
    def refuse(*a, **kw):
        raise cli.RuntipiError('settings change failed')
    monkeypatch.setattr(rt, 'req', refuse)
    failed = {}
    assert cli.release_updates(c, rt, failed) == ['files']
    assert updated == ['files']
    assert 'settings change failed' in failed['wizard-claude']


def test_invalid_release_version_is_rejected_before_update(tmp_path, monkeypatch):
    c, rt, _, patches, updated = box(tmp_path, monkeypatch)
    doc = release_pins.load()
    doc['default_apps']['wizard-claude']['index_version'] = '0.2.99'
    failed = {}
    assert cli.release_updates(c, rt, failed) == ['files']
    assert updated == ['files'] and not patches
    assert 'prepared recipe version' in failed['wizard-claude']
