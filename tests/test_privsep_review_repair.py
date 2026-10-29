"""Focused security repair regressions; synthetic files only, no Docker/live data."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from wam import final_host_tokens as fht, host_policy, control_paths


def config(tmp_path):
    home = tmp_path / 'state'
    home.mkdir()
    return SimpleNamespace(home=home, runtipi_root=tmp_path / 'runtipi')


def test_unsupported_features_cannot_be_minted_and_clear_old_authority(tmp_path):
    c = config(tmp_path)
    fht.record(c, 'demo', found=['web|privileged'])
    fht.record(c, 'demo', found=['unsupported:web:future_feature', 'web|key:future_feature'])
    assert json.loads(fht.path_for(c).read_text())['apps'] == {}


def test_refused_recheck_clears_previously_accepted_authority(tmp_path):
    c = config(tmp_path)
    fht.record(c, 'demo', found=['web|privileged'])
    with patch.object(host_policy, '_require_app', side_effect=ValueError('changed recipe')):
        with pytest.raises(ValueError, match='changed recipe'):
            host_policy.require_app(c, 'demo')
    assert json.loads(fht.path_for(c).read_text())['apps'] == {}


def test_deny_clears_consumed_authority_even_if_network_write_fails(tmp_path):
    c = config(tmp_path)
    fht.record(c, 'demo', found=['web|privileged'])
    state = {'approvals': {'demo': {'sha256': 'synthetic'}}, 'pending': {}}
    with patch.object(host_policy, '_read', return_value=state), patch.object(host_policy, '_write'), \
         patch('wam.network.set_host_access_allowed', side_effect=ValueError('synthetic failure')):
        with pytest.raises(ValueError, match='synthetic failure'):
            host_policy.revoke(c, 'demo')
    assert 'demo' not in state['approvals']
    assert json.loads(fht.path_for(c).read_text())['apps'] == {}


def test_value_tokens_are_complete_not_presence_bits():
    assert fht.value_digest({'b': 2, 'a': 1}) == fht.value_digest({'a': 1, 'b': 2})
    assert fht.value_digest(['/dev/example-a']) != fht.value_digest(['/dev/example-b'])
    assert fht.value_digest({'example': '1'}) != fht.value_digest({'example': '2'})


def test_control_seal_changes_only_exact_control_anchors(tmp_path):
    rt = tmp_path / 'runtipi'
    (rt / 'apps' / 'syntheticstore' / 'demo').mkdir(parents=True)
    app = rt / 'app-data' / 'syntheticstore' / 'demo'
    payload = app / 'data'
    payload.mkdir(parents=True)
    secret = payload / 'app-secret'
    secret.write_text('synthetic-app-secret')
    payload.chmod(0o777)
    secret.chmod(0o640)
    before = (payload.stat().st_uid, payload.stat().st_gid, secret.stat().st_uid, secret.stat().st_gid, secret.stat().st_mode)
    # Spy chown rather than requiring root or impersonating a real account.
    with patch.object(control_paths.os, 'chown') as chown:
        control_paths.seal_app_controls(rt, 54321, 54321)
    assert [call.args[0] for call in chown.call_args_list] == [rt / 'app-data', app.parent, app]
    assert (app.stat().st_mode & 0o777) == 0o755
    assert (payload.stat().st_mode & 0o777) == 0o777
    assert before == (payload.stat().st_uid, payload.stat().st_gid, secret.stat().st_uid, secret.stat().st_gid, secret.stat().st_mode)
    assert secret.read_text() == 'synthetic-app-secret'


def test_control_seal_refuses_symlink_without_touching_payload(tmp_path):
    rt = tmp_path / 'runtipi'
    (rt / 'apps' / 'syntheticstore' / 'demo').mkdir(parents=True)
    (rt / 'app-data').mkdir()
    other = tmp_path / 'payload'
    other.mkdir()
    (rt / 'app-data' / 'syntheticstore').symlink_to(other, target_is_directory=True)
    with patch.object(control_paths.os, 'chown') as chown:
        with pytest.raises(ValueError, match='unsafe app control anchor'):
            control_paths.seal_app_controls(rt, 54321, 54321)
    assert all(call.args[0] != other for call in chown.call_args_list)
