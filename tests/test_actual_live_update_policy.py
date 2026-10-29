"""Actual-live authority adaptation: refuse before queue, never auto-approve."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from wam import cli, app_updates as au, update_policy as policy, source_integrity as si


def cfg(tmp_path):
    queue = []
    class Rt:
        def app(self, urn): return {'app': {'status': 'stopped', 'version': 1}}
        def update(self, *a, **kw): queue.append(a)
        def wait_status(self, *a): return 'stopped'
    c = SimpleNamespace(home=tmp_path/'home', runtipi_root=tmp_path/'rt', runtipi=lambda: Rt())
    return c, queue


def recipe(c, text):
    p = c.runtipi_root/'repos/wizardapps/apps/demo/docker-compose.yml'
    p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)
    return p


@pytest.mark.parametrize('text', [
    'services: {demo: {image: x, privileged: true}}',
    'services: {demo: {image: x, network_mode: host}}',
    'services: {demo: {image: x, volumes: ["/var/run/docker.sock:/socket"]}}',
    'services: {demo: {image: x, volumes: ["/home/owner:/private:ro"]}}',
    'services: {demo: {image: x, cap_add: [SYS_ADMIN]}}',
    'services: {demo: {image: x, mystery_host_power: true}}',
    'services: {demo: {image: x}}\nx-runtipi: {overrides: [{services: {demo: {privileged: true}}}]}',
])
def test_protected_recipe_never_queues_or_writes_approval(tmp_path, text):
    c, queue = cfg(tmp_path); p = recipe(c, text); before = p.read_bytes()
    with pytest.raises(SystemExit, match='owner review required'):
        cli.app_update(c, 'demo')
    assert not queue and p.read_bytes() == before and not c.home.exists()


def test_missing_recipe_unknown_not_permission(tmp_path):
    c, queue = cfg(tmp_path)
    with pytest.raises(SystemExit, match='no prepared recipe'):
        cli.app_update(c, 'demo')
    assert queue == []


def test_override_not_approval_marker(tmp_path):
    c, queue = cfg(tmp_path); recipe(c, 'services: {demo: {image: x}}')
    p=c.runtipi_root/'user-config/wizardapps/demo/docker-compose.yml';p.parent.mkdir(parents=True)
    p.write_text('x-wizard-networks: true\nservices: {demo: {volumes: ["/etc:/host:ro"]}}')
    with pytest.raises(SystemExit, match='owner review required'): cli.app_update(c, 'demo')
    assert queue == []


def test_owner_source_edit_preserved_before_queue(tmp_path):
    c, queue = cfg(tmp_path); recipe(c, 'services: {demo: {image: x}}')
    from wam import packaging
    installed = c.runtipi_root/'apps/wizardapps/demo'; installed.mkdir(parents=True)
    (installed/'wizard.md').write_text('---\nruntime: {package: source}\n---\n')
    commit='a'*40; (installed/'.wizard-commit').write_text(commit)
    source = packaging.source_dir(packaging.app_data_root(c.runtipi_root),'wizardapps','demo',commit)
    source.mkdir(parents=True); (source/'code').write_text('upstream');si.record(source)
    (source/'code').write_text('OWNER-EDIT'); before=si.inventory(source)
    with pytest.raises(SystemExit, match='owner-source'): cli.app_update(c, 'demo')
    assert queue == [] and si.inventory(source) == before


def test_plain_recipe_queues_once_in_manual_without_enabling(tmp_path, monkeypatch):
    c, queue = cfg(tmp_path); recipe(c, 'services: {demo: {image: x}}')
    au.set_mode('ask', c.home, tmp_path/'units', systemctl=False)
    saved=(c.home/au.STATE).read_bytes()
    monkeypatch.setattr(cli.time, 'sleep', lambda *a: None)
    assert cli.app_update(c, 'demo')['status'] == 'stopped'
    assert queue == [('demo:wizardapps',)] and (c.home/au.STATE).read_bytes() == saved
    assert not list((tmp_path/'units').glob('*.timer'))


def test_raw_update_cannot_bypass_source_and_recipe_boundary(tmp_path):
    with pytest.raises(ValueError, match='not safe'):
        cli.runtipi_cli(tmp_path, ['app','update','--all'])
