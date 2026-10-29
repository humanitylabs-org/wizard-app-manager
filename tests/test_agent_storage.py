"""Focused storage contract regressions. Kernel/VM claims live in the queued rig.

No pytest fixture here pretends a mock stopped flag is a production writer fence.
These tests are queued to forum, not run on the development/live host.
"""
import ast
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO=Path(__file__).resolve().parents[1]
PRODUCT=REPO/'scripts/protected-agent-storage.py'


def load():
    spec=importlib.util.spec_from_file_location('storage_under_test',PRODUCT)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_normal_process_cannot_mutate_even_when_root_or_stopped(monkeypatch,tmp_path):
    m=load()
    monkeypatch.setattr(m.os,'getuid',lambda:0);monkeypatch.setattr(m.os,'geteuid',lambda:0)
    monkeypatch.setattr(m.os,'getpid',lambda:1234)
    monkeypatch.setattr(m.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=1100,pw_gid=1100,pw_name=name,pw_dir='/home/hermes'))
    monkeypatch.setattr(m,'ANCHOR',tmp_path/'physical');monkeypatch.setattr(m,'CONTROL',tmp_path/'control')
    with pytest.raises(m.Refusal,match='maintenance-needed'):
        m.Transaction('hermes','/home/hermes/.hermes').run()
    assert not (tmp_path/'physical').exists() and not (tmp_path/'control').exists()


@pytest.mark.parametrize('logical',('/home/hermes/../.hermes','/home/hermes//.hermes','relative','/home/hermes/.hermes/'))
def test_noncanonical_source_token_not_repaired(logical):
    with pytest.raises(ValueError):load().clean_path(logical)


def test_snapshot_rollback_guard_precedes_all_mutation(monkeypatch,tmp_path):
    m=load();calls=[]
    monkeypatch.setattr(m,'require_fence',lambda:calls.append('fence'))
    def guard():calls.append('db-intent-guard');raise m.Refusal('db_intent-or-later')
    monkeypatch.setattr(m,'rollback_guard',guard)
    monkeypatch.setattr(m.pwd,'getpwnam',lambda name:SimpleNamespace(pw_uid=1100,pw_gid=1100,pw_name=name,pw_dir='/home/hermes'))
    monkeypatch.setattr(m,'ANCHOR',tmp_path/'physical');monkeypatch.setattr(m,'CONTROL',tmp_path/'control')
    with pytest.raises(m.Refusal,match='db_intent'):
        m.Transaction('hermes','/home/hermes/.hermes').run('rollback')
    assert calls==['fence','db-intent-guard'] and list(tmp_path.iterdir())==[]


@pytest.mark.parametrize('phase',('db_intent','db_rotated','queue_intent','queue_rotated','file_committed','restarted','verified','unknown'))
def test_credential_intent_is_forward_only(monkeypatch,phase):
    m=load();monkeypatch.setattr(m,'protected_json',lambda *a,**k:{'phase':phase})
    with pytest.raises(m.Refusal,match='forward-only'):m.rollback_guard()


@pytest.mark.parametrize('phase',('prepared','quiesced','aborted'))
def test_precredential_mutation_guard_allows_rollback(monkeypatch,phase):
    m=load();monkeypatch.setattr(m,'protected_json',lambda *a,**k:{'phase':phase})
    m.rollback_guard()


def test_installer_and_updater_preflight_precede_mutations():
    get=(REPO/'get.sh').read_text();install=(REPO/'install.sh').read_text()
    begin=get.index('if separated_manager; then',get.index('storage_preflight()'))
    assert get.index('storage_preflight check-pairing',begin)<get.index('MODE=alongside; export',begin)
    branch=get.index('storage_preflight check "$ALONG_USER" "$ALONG_HOME"')
    assert branch<get.index('    prepare_server',branch)<get.index('prepare-alongside.py',branch)
    assert install.index('check-pairing')<install.index('(cd "$HERE" && python3 -m wam.release_pins)')
    prep=(REPO/'scripts/prepare-alongside.py').read_text()
    assert prep.index("storage['require_mapping'](name, hh)")<prep.index("groups = {")<prep.index('BASE.mkdir')


def test_legacy_migration_storage_actions_are_one_primitive_not_a_new_migrator():
    text=(REPO/'wam/privilege_migration.py').read_text()
    body=text[text.index('def main('):]
    assert 'storage-relocate' in body and 'storage-rollback' in body
    assert body.index("storage['Transaction']")<body.index('CONTROL.mkdir')
    assert body.index("rollback_allowed(CONTROL/'credentials')")<body.index('CONTROL.mkdir')
    assert body.index("storage['require_mapping'](agent, logical)")<body.index('CONTROL.mkdir')
    assert 'NOT a storage writer fence' in text


def test_samefs_rename_crossfs_opaque_stage_and_no_cleanup_delete():
    text=PRODUCT.read_text();tree=ast.parse(text)
    assert "'method': 'rename' if st.st_dev == os.fstat(self.dest.fd).st_dev else 'copy'" in text
    assert 'rename_new(self.home, self.logical.name, self.dest, str(self.uid))' in text
    assert "self.transfer(self.control/self.doc['snapshot'], ANCHOR/name)" in text
    assert 'RENAME_NOREPLACE' not in text or 'renameat2' in text
    assert not any(isinstance(n,ast.Attribute) and n.attr in ('rmtree','chmod','chown','system') for n in ast.walk(tree))
    # Only fixed control files and the selected compatibility symlink are unlinked;
    # no recursive cleanup or home-ownership repair is a product recovery action.
    assert 'os.unlink(self.logical.name, dir_fd=self.home.fd)' in text
    assert 'os.fchmod(fd, mode)' in text


def test_runtime_inventory_binds_actual_power_cut_and_respawner_cases():
    guest=(REPO/'qa/privsep-storage/guest.py').read_text()
    host=(REPO/'qa/privsep-storage/run.py').read_text()
    assert 'Restart=always' in guest and 'removed_fence_permitted_forbidden_transaction' in guest
    assert 'real_payload_write_syscalls_observed' in guest
    assert "os.getpid() == 1" in guest and "os.getpid() != 1" in guest
    assert "signal.SIGKILL if power_cut" in host
    assert 'set(names)==set(self.doc[\'expected_cases\'])' in host
    assert "not tree.findall('.//skipped')" in host
    assert 'WIZARD_HEAVY_LOCK_FD' in host and '/srv/work/heavy/production/worker.lock' in host
    assert '217179869184' in host and '21474836480' in host
