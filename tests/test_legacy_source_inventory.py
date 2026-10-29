"""Failing-first: trust upstream at the INSTALLED commit, never the next recipe or local cache."""
import json
import shutil
from types import SimpleNamespace

import pytest

from wam import cli, packaging, private, release_pins, source_integrity as si
from test_owner_app_updates import installed


def legacy(tmp_path, monkeypatch):
    root, live, source = installed(tmp_path)
    (live / '.wizard-source').write_text(json.dumps({'repo': 'example/demo'}))
    upstream = tmp_path / 'trusted'
    shutil.copytree(source, upstream)
    calls = []
    def fetch(repo, commit, dest, tok):
        calls.append((repo, commit))
        assert repo == 'example/demo' and commit == 'a' * 40
        shutil.copytree(upstream, dest, dirs_exist_ok=True)
    monkeypatch.setattr(private, 'fetch', fetch)
    monkeypatch.setattr(private, 'token', lambda: '')
    return root, live, source, upstream, calls


def test_pristine_legacy_gets_exact_installed_baseline_not_next_recipe(tmp_path, monkeypatch):
    root, live, source, upstream, calls = legacy(tmp_path, monkeypatch)
    recipe = root / 'repos/wizardapps/apps/demo'
    recipe.mkdir(parents=True)
    (recipe / '.wizard-commit').write_text('b' * 40)
    # Poisoning the reused local checkout must not establish trust either.
    cache = root / 'repos/.wizardapps-src' / ('demo@' + 'a' * 12)
    shutil.copytree(source, cache)
    (cache / 'code.py').write_text('not trusted')
    assert si.update_block(root, 'wizardapps', 'demo', verify_legacy=True) is None
    assert calls == [('example/demo', 'a' * 40)]
    assert si.pristine(source)
    assert si.update_block(root, 'wizardapps', 'demo', verify_legacy=True) is None
    assert len(calls) == 1


@pytest.mark.parametrize('mutation', ['modify', 'add', 'delete', 'symlink'])
def test_legacy_changes_never_get_blessed(tmp_path, monkeypatch, mutation):
    root, live, source, upstream, calls = legacy(tmp_path, monkeypatch)
    if mutation == 'modify': (source / 'code.py').write_text('owner')
    if mutation == 'add': (source / 'mine').write_text('owner')
    if mutation == 'delete': (source / 'code.py').unlink()
    if mutation == 'symlink':
        (source / 'code.py').unlink()
        (source / 'code.py').symlink_to('../outside')
    before = si.inventory(source)
    assert si.update_block(root, 'wizardapps', 'demo', verify_legacy=True) == 'owner-source'
    assert not si.record_path(source).exists()
    assert si.inventory(source) == before
    assert calls == [('example/demo', 'a' * 40)]


def test_missing_upstream_is_cannot_confirm_not_owner_changed(tmp_path, monkeypatch):
    root, live, source, _, _ = legacy(tmp_path, monkeypatch)
    def unavailable(*args): raise private.PrivateError('offline')
    monkeypatch.setattr(private, 'fetch', unavailable)
    assert si.update_block(root, 'wizardapps', 'demo', verify_legacy=True) == 'source-unverified'
    assert not si.record_path(source).exists()


def test_malformed_installed_yaml_is_unverified_not_a_global_status_failure(tmp_path, monkeypatch):
    root, live, source, _, calls = legacy(tmp_path, monkeypatch)
    (live / 'wizard.md').write_text('---\nruntime: [\n---\n')
    assert si.update_block(root, 'wizardapps', 'demo', verify_legacy=True) == 'source-unverified'
    assert calls == []
    assert not si.record_path(source).exists()


@pytest.mark.parametrize('same_commit', [False, True])
@pytest.mark.parametrize('modified', [False, True])
def test_installer_rerun_verifies_legacy_before_prepare_and_reports_hold_separately(tmp_path, monkeypatch, same_commit, modified):
    root, live, source, _, calls = legacy(tmp_path, monkeypatch)
    if modified: (source / 'code.py').write_text('owner')
    target = 'a' * 40 if same_commit else 'b' * 40
    monkeypatch.setattr(release_pins, 'load', lambda: {'default_apps': {'demo': {'app': 'demo', 'repo': 'example/demo', 'commit': target}}})
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {'demo'})
    monkeypatch.setattr(cli.registry, 'app_meta', lambda *args: {})
    prepared = []
    def prepare(c, app):
        assert si.pristine(source), 'verification precedes recipe/config mutation'
        prepared.append(app)
        raise RuntimeError('stop after guarded preparation')
    monkeypatch.setattr(cli, 'resolve_any', prepare)
    failed, held = {}, {}
    c = SimpleNamespace(runtipi_root=root)
    assert cli.release_updates(c, None, failed, held=held) == []
    assert calls == [('example/demo', 'a' * 40)]
    assert held == ({'demo': 'owner-source'} if modified else {})
    assert prepared == ([] if modified or same_commit else ['demo'])
    assert bool(failed) == (not modified and not same_commit)


@pytest.mark.parametrize('args', [['app', 'update', '--no-backup', 'demo'], ['app', 'update', '--all']])
def test_update_passthroughs_never_skip_source_guard(tmp_path, monkeypatch, args):
    monkeypatch.setattr(cli.subprocess, 'call', lambda *a, **kw: pytest.fail('unsafe passthrough'))
    with pytest.raises(ValueError, match='wam app update <app>'):
        cli.runtipi_cli(tmp_path, args)
