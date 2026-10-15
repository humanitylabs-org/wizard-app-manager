import json
from pathlib import Path
from unittest.mock import patch
import pytest
from wam import cli, private

@pytest.mark.parametrize('value', ['owner/repo/extra', '../repo', 'https://evil.test/owner/repo', 'owner/repo ', ' owner/repo', 'owner/..', 42])
def test_invalid_alias_repository_fails_closed(tmp_path, monkeypatch, value):
    monkeypatch.setenv('WIZARD_OS_HOME', str(tmp_path))
    (tmp_path/'app-manager.json').write_text(json.dumps({'private_apps': {'my-app': value}}))
    with pytest.raises(SystemExit, match='owner/repo'):
        cli.Config()


def test_alias_manifest_identity_checked_before_any_side_effect(tmp_path):
    def fetch(repo, commit, src, tok):
        src.mkdir(parents=True)
        (src/'wizard.md').write_text('---\nid: other-app\n---\n')
    with patch.object(private,'head_commit',return_value='a'*40), patch.object(private,'fetch',side_effect=fetch), patch.object(private,'pull') as pull:
        with pytest.raises(private.PrivateError, match='does not match manifest'):
            private.prepare(tmp_path,'wizardapps','owner/repo',tok='fixture',expected_id='my-app')
    pull.assert_not_called()
    assert not (tmp_path/'repos').exists()


def test_explicit_catalog_pin_wins_over_alias(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_OS_HOME',str(tmp_path))
    (tmp_path/'app-manager.json').write_text(json.dumps({'private_apps': {'my-app':'owner/repo'}}))
    c=cli.Config()
    release={'id':'my-app','repo':'owner/pinned','commit':'a'*40}
    with patch('wam.test_catalog.resolve',return_value=release), patch.object(private,'prepare',return_value=release) as prepare:
        assert cli.resolve_any(c,'my-app') == release
        prepare.assert_called_once_with(c.runtipi_root,cli.STORE,'owner/pinned',release=release)


def test_alias_failure_never_falls_through_to_public(tmp_path, monkeypatch):
    monkeypatch.setenv('WIZARD_OS_HOME',str(tmp_path))
    (tmp_path/'app-manager.json').write_text(json.dumps({'private_apps': {'my-app':'https://github.com/owner/repo'}}))
    c=cli.Config()
    with patch('wam.test_catalog.resolve',return_value=None), patch.object(private,'prepare',side_effect=private.PrivateError('denied')) as prepare, patch.object(cli.index,'resolve') as index:
        with pytest.raises(SystemExit,match='denied'):
            cli.resolve_any(c,'my-app')
    prepare.assert_called_once_with(c.runtipi_root,cli.STORE,'owner/repo',expected_id='my-app')
    index.assert_not_called()
