"""Failure regressions from the independent small-group release review."""
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam.backup import appdata, restore
from wam.backup.box import Agent


@pytest.mark.parametrize('phase', ['before-swap', 'new-publication', 'killed-after-rename', 'killed-then-undo'])
def test_kept_agent_tree_survives_error_after_staging_before_swap(tmp_path, phase, monkeypatch):
    live = tmp_path / 'app-data/wizardapps/wizard-agent-service'
    kept = live / 'data/wizard-os/agents/hermes/local-identity'
    kept.parent.mkdir(parents=True)
    kept.write_text('destination-only fixture')
    token = live / 'data/wizard-os/mcp-token'
    token.write_text('synthetic per-machine token')
    tools = tmp_path / 'bin'
    tools.mkdir()
    mv = tools / 'mv'
    actual_mv = shutil.which('mv')
    assert actual_mv
    if phase == 'before-swap':
        failure = '[ "$1" != "$FAIL_RENAME" ] || exit 91\n'
    elif phase == 'new-publication':
        failure = 'case "$1" in *.wizard-new/app-data) exit 91;; esac\n'
    else:
        failure = 'case "$1" in *.wizard-new/app-data) kill -9 "$PPID"; exit 91;; esac\n'
    mv.write_text('#!/bin/sh\n' + failure + 'exec ' + actual_mv + ' "$@"\n')
    mv.chmod(0o755)
    incoming = io.BytesIO()
    with tarfile.open(fileobj=incoming, mode='w') as t:
        ti = tarfile.TarInfo('app-data/data/incoming')
        ti.size = 3
        t.addfile(ti, io.BytesIO(b'new'))
    env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'], RT=str(tmp_path),
               STORE='wizardapps', APP='wizard-agent-service', KEEP='data/wizard-os/agents\ndata/wizard-os/mcp-token',
               DATA_ONLY='0', VOLS='', V=str(tmp_path / 'volumes'), FAIL_RENAME=str(live))
    r = subprocess.run(['sh', '-c', appdata.RESTORE_SH], input=incoming.getvalue(), env=env, capture_output=True)
    assert r.returncode != 0
    if phase in ('killed-after-rename', 'killed-then-undo'):
        assert (live.with_name(live.name + '.wizard-old') / kept.relative_to(live)).read_text() == 'destination-only fixture'
        env['PATH'] = os.environ['PATH']
        if phase == 'killed-then-undo':
            box = SimpleNamespace(files_root=lambda: tmp_path / 'files', runtipi_root=tmp_path)
            ad = appdata.AppData(box)
            box.appdata = ad
            def helper(mounts, values, script, **kw):
                values = dict(values, RT=str(tmp_path))
                rep = subprocess.run(['sh', '-c', script], env=dict(os.environ, **values), capture_output=True)
                if rep.returncode:
                    raise appdata.AppDataError(rep.stderr.decode())
            monkeypatch.setattr(ad, '_helper', helper)
            j = {'stamp': 'fixture', 'agents': {}, 'move_journal_version': 1,
                 'steps': [{'kind': 'app_snapshot', 'urn': 'wizard-agent-service:wizardapps'}]}
            monkeypatch.setattr(restore, 'last_restore', lambda b: (tmp_path, j))
            class ReachedNestedTrees(Exception):
                pass
            def nested_trees(*args):
                # Actual Undo must reconcile the app BEFORE nested home paths
                # are recreated. Without the preflight both originals are absent.
                assert token.read_text() == 'synthetic per-machine token'
                assert kept.read_text() == 'destination-only fixture'
                (kept.parent / 'restored-owner-entry').write_text('owner Undo fixture')
                raise ReachedNestedTrees()
            monkeypatch.setattr(restore, '_undo_journaled_trees', nested_trees)
            with pytest.raises(ReachedNestedTrees):
                restore._undo(box)
        r = subprocess.run(['sh', '-c', appdata.RESTORE_SH], input=incoming.getvalue(), env=env, capture_output=True)
        assert r.returncode == 0, r.stderr
    assert kept.read_text() == 'destination-only fixture', 'cleanup must never delete live kept agents'
    assert token.read_text() == 'synthetic per-machine token'


def test_app_undo_archive_fsynced_before_publish_and_parent_after(tmp_path, monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as t:
        ti = tarfile.TarInfo('app-data/item')
        ti.size = 3
        t.addfile(ti, io.BytesIO(b'old'))
    stream.seek(0)
    proc = SimpleNamespace(stdout=stream, wait=lambda: 0, kill=lambda: None)
    monkeypatch.setattr(appdata.subprocess, 'Popen', lambda *a, **kw: proc)
    box = SimpleNamespace(child_env={})
    ad = appdata.AppData(box)
    monkeypatch.setattr(ad, 'image', lambda: 'unused-fixture-image')
    events = []
    real_sync, real_replace = os.fsync, os.replace
    def sync(fd):
        events.append('dir-sync' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file-sync')
        real_sync(fd)
    def replace(src, dst):
        events.append('publish')
        real_replace(src, dst)
    monkeypatch.setattr(appdata.os, 'fsync', sync)
    monkeypatch.setattr(appdata.os, 'replace', replace)
    ad._tar({'app-data': tmp_path}, [], [], tmp_path / 'undo.tar.gz', 'fixture')
    assert events == ['file-sync', 'publish', 'dir-sync']


@pytest.mark.parametrize('runtime', ['host', 'container'])
def test_stop_agent_failure_refuses_import_before_mutation(tmp_path, runtime):
    a = Agent('hermes', True, tmp_path / 'hh', runtime, 'hermes-agent:wizardapps')
    box = SimpleNamespace(runner=SimpleNamespace(gateway=lambda *a: False),
                          rt=SimpleNamespace(stop=lambda *a: None), wait_status=lambda *a, **k: 'running')
    with pytest.raises(restore.RestoreError, match='did not stop'):
        restore._stop_agent(box, a, {a.urn: {'status': 'running'}})


def test_undo_stop_failure_never_reaches_tree_moves(tmp_path, monkeypatch):
    a = tmp_path / 'hermes'
    j = {'stamp': 'fixture', 'agents': {'hermes': {'home': str(a), 'runtime': 'host'}}, 'steps': []}
    box = SimpleNamespace(files_root=lambda: tmp_path / 'files', runner=SimpleNamespace(gateway=lambda *a: False))
    monkeypatch.setattr(restore, 'last_restore', lambda b: (tmp_path, j))
    monkeypatch.setattr(restore, '_undo_journaled_trees', lambda *a: pytest.fail('mutated after stop failure'))
    j['move_journal_version'] = 1
    with pytest.raises(restore.RestoreError, match='did not stop'):
        restore._undo(box)
