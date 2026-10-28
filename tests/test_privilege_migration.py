"""Slice 6: filesystem copy, finite metadata, interruption, rollback and ordering."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from wam import privilege_migration as m


@pytest.fixture
def migration(tmp_path, monkeypatch):
    control = tmp_path/'control'; control.mkdir(mode=0o700)
    base = tmp_path/'system'
    old = tmp_path/'hermes'; old.mkdir()
    (old/'.wizard-os').mkdir(); (old/'runtipi').mkdir()
    agent = SimpleNamespace(pw_name='hermes',pw_dir=str(old),pw_uid=os.getuid(),pw_gid=os.getgid())
    monkeypatch.setattr(m,'BASE',base); monkeypatch.setattr(m,'CONTROL',control)
    monkeypatch.setattr(m,'SYSTEMD',tmp_path/'systemd')
    monkeypatch.setattr(m.pwd,'getpwnam',lambda name: agent if name=='hermes' else SimpleNamespace(pw_uid=os.getuid(),pw_gid=os.getgid(),pw_dir=str(base)))
    obj=m.Migration('hermes',str(old/'.hermes'),'candidate')
    obj.doc.update(groups=['users','docker'],units={},containers=[],running_apps=[],operator='hermes',serve={})
    return obj


@pytest.mark.parametrize('interruption',m.STEPS)
def test_resume_after_each_step(migration,monkeypatch,interruption):
    calls=[]; failed=[False]
    for step in m.STEPS:
        def action(step=step):
            calls.append(step)
            if step==interruption and not failed[0]:
                failed[0]=True
                raise InterruptedError('power loss')
        monkeypatch.setattr(migration,'snapshot_step' if step=='snapshot' else step,action)
    with pytest.raises(InterruptedError): migration.migrate()
    journal=json.loads(migration.path.read_text())
    assert journal['current']==interruption and interruption not in journal['done']
    # Reload the journal, not a lucky in-memory object.
    restored=m.Migration('hermes',migration.doc['hermes_home'],'candidate')
    migration.doc=restored.doc
    migration.migrate()
    assert migration.doc['done']==list(m.STEPS)
    assert calls.count(interruption)==2
    for step in m.STEPS:
        if step != interruption: assert calls.count(step)==1
    before=list(calls); migration.migrate(); assert calls==before


def test_doctor_failure_keeps_docker_and_never_grants(migration,monkeypatch):
    calls=[]
    migration.doc['done']=list(m.STEPS[:m.STEPS.index('doctor')])
    monkeypatch.setattr(migration,'system',lambda *a,**k: (_ for _ in ()).throw(ValueError('doctor failed')))
    for step in ('grant','remove_docker','complete'):
        monkeypatch.setattr(migration,step,lambda step=step:calls.append(step))
    with pytest.raises(ValueError,match='doctor failed'): migration.migrate()
    assert calls==[]
    assert not migration.doc.get('doctor_passed')


def test_docker_removal_is_last_and_revokes_live_process_groups(migration,monkeypatch):
    commands=[]
    monkeypatch.setattr(m,'run',lambda argv,**k:commands.append(argv) or SimpleNamespace(stdout='hermes docker',returncode=0))
    with pytest.raises(ValueError,match='Doctor'): migration.remove_docker()
    assert commands==[]
    migration.doc['doctor_passed']=True; migration.remove_docker()
    assert commands[1][:3]==['gpasswd','-d','hermes']
    assert commands[2][:2]==['loginctl','terminate-user']
    assert m.STEPS.index('doctor') < m.STEPS.index('grant') < m.STEPS.index('remove_docker') < m.STEPS.index('complete')


def test_snapshot_is_opaque_copy_and_survives_partial_retry(migration,monkeypatch):
    marker=migration.old/'runtipi/app-data/wizardapps/recorder/data/private.bin'
    marker.parent.mkdir(parents=True); marker.write_bytes(b'\x00private\xff')
    (migration.old/'.wizard-os/agents/private/home').mkdir(parents=True)
    private=migration.old/'.wizard-os/agents/private/home/state.db'; private.write_bytes(b'never parse')
    original=Path.read_text
    def sealed(path,*a,**kw):
        assert '/app-data/' not in str(path) and '/agents/' not in str(path)
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',sealed)
    # Avoid host root integration targets in this filesystem fixture.
    realcopy=m.copy
    def copy(source,target):
        if str(source).startswith(str(migration.old)): realcopy(source,target)
    monkeypatch.setattr(m,'copy',copy)
    migration.snapshot_step(); migration.snapshot_step()
    assert (migration.snapshot/'runtipi/app-data/wizardapps/recorder/data/private.bin').read_bytes()==b'\x00private\xff'
    assert (migration.snapshot/'state/agents/private/home/state.db').read_bytes()==b'never parse'


def test_wiring_changes_only_manager_metadata_not_private_bytes(migration,monkeypatch):
    rt=m.BASE/'runtipi'; state=m.BASE/'.wizard-os'
    rt.mkdir(parents=True); state.mkdir()
    (state/'alongside.json').write_text(json.dumps({'agent':{'hermes_home':migration.doc['hermes_home']}}))
    override=rt/'user-config/wizardapps/agent-private/docker-compose.yml'
    override.parent.mkdir(parents=True)
    override.write_text(f"services:\n  engine:\n    volumes:\n      - {migration.old}/.wizard-os/agents/private/home:/opt/data\n      - {migration.old}/.claude:/root/.claude\n")
    data=rt/'app-data/wizardapps/recorder/data/media'; data.mkdir(parents=True)
    private=data/'content'; private.write_text(str(migration.old/'runtipi'))
    monkeypatch.setattr(migration,'system',lambda code: None)
    migration.wiring()
    assert str(m.BASE/'.wizard-os/agents/private/home') in override.read_text()
    assert str(migration.old/'.claude') in override.read_text()
    assert private.read_text()==str(migration.old/'runtipi')
    assert (state/'api-token').stat().st_mode & 0o777==0o600
    assert (state/'api-token').read_text() != (state/'agent-token').read_text()


def test_rollback_before_snapshot_restarts_exact_original_units(migration,monkeypatch):
    calls=[]
    migration.doc['done']=['inventory','quiesce']
    migration.doc['units']={'wizard-app-manager-api.service':{'enabled':True,'active':True}}
    monkeypatch.setattr(migration,'user',lambda *a,**k:calls.append(a))
    migration.rollback(); assert migration.doc['state']=='rolled_back'
    assert calls==[('systemctl','--user','enable','wizard-app-manager-api.service'),('systemctl','--user','start','wizard-app-manager-api.service')]
    migration.rollback(); assert len(calls)==2


def test_operator_reassign_requires_named_prior_owner_and_preserves_serve():
    source=(Path(__file__).parents[1]/'scripts/prepare-named-user.sh').read_text()
    assert '--reassign-from' in source
    assert '[ "$operator" != "$reassign_from" ]' in source
    assert 'before_serve=' in source
    assert 'tailscale serve reset' not in source


def test_policy_migration_never_opens_app_data_or_private_agent_metadata(tmp_path,monkeypatch):
    from wam import host_policy,managed_overrides
    c=SimpleNamespace(home=tmp_path/'.wizard-os',runtipi_root=tmp_path/'runtipi',migration_metadata_only=True)
    c.home.mkdir(); c.runtipi_root.mkdir()
    recipe=c.runtipi_root/'apps/wizardapps/agent-private/docker-compose.yml'
    recipe.parent.mkdir(parents=True); recipe.write_text('services:\n  engine:\n    image: example\n    volumes: [/opt/existing:/data]\n')
    original=Path.read_text
    def sealed(path,*a,**k):
        assert '/app-data/' not in str(path) and '/agents/' not in str(path)
        return original(path,*a,**k)
    monkeypatch.setattr(Path,'read_text',sealed)
    monkeypatch.setattr(managed_overrides,'agent_mounts',lambda *a:(_ for _ in ()).throw(AssertionError('private registry read')))
    host_policy.migrate(c)
    approvals=json.loads((c.home/'host-approvals.json').read_text()) if (c.home/'host-approvals.json').exists() else host_policy._read(c.home)
    assert approvals['risk_set_migration']
    assert any(x['source']=='grandfathered at upgrade' for x in approvals['approvals'].values())


def test_timer_keeps_owner_schedule_but_never_promotes_an_arbitrary_target():
    text=m.timer_template('[Timer]\nOnCalendar=*-*-* 03:41:00\nRandomizedDelaySec=2m\nPersistent=true\n')
    assert '03:41:00' in text and 'RandomizedDelaySec=2m' in text
    with pytest.raises(ValueError,match='owner mapping'):
        m.timer_template('[Timer]\nOnCalendar=daily\nUnit=unrelated-root.service\n')


def test_relocate_never_remaps_container_data_owners(migration,monkeypatch):
    for suffix in ('runtipi/app-data/wizardapps/files/data','runtipi/data/db','runtipi/media','state/agents/private/home'):
        p=migration.snapshot/suffix; p.mkdir(parents=True); (p/'opaque').write_bytes(b'unchanged')
    (m.CONTROL/'source').mkdir(); (m.CONTROL/'source/known.py').write_text('print(1)')
    calls=[]
    monkeypatch.setattr(m.os,'chown',lambda path,*a,**kw:calls.append(str(path)))
    migration.relocate()
    for path in calls:
        assert '/app-data/' not in path and '/agents/' not in path and '/media/' not in path and '/data/db' not in path
    assert (m.BASE/'runtipi/app-data/wizardapps/files/data/opaque').read_bytes()==b'unchanged'
    assert (m.BASE/'.wizard-os/agents/private/home/opaque').read_bytes()==b'unchanged'
    assert str(m.BASE/'wizard-app-manager/known.py') in calls


def test_full_snapshot_rollback_restores_data_tokens_units_and_groups_after_interruption(migration,monkeypatch,tmp_path):
    migration.doc['done']=list(m.STEPS); migration.doc['root_paths']=[]
    migration.doc['state']='migrated'
    root=migration.snapshot
    for directory in ('runtipi/app-data/wizardapps/calendar/data','state','user-units','hermes-plugins'):
        (root/directory).mkdir(parents=True)
    (root/'runtipi/app-data/wizardapps/calendar/data/canary').write_bytes(b'original data')
    (root/'state/api-token').write_bytes(b'original token')
    (root/'user-units/wizard-app-manager-api.service').write_bytes(b'original unit')
    (root/'hermes-config.yaml').write_bytes(b'original config')
    (root/'hermes-.env').write_bytes(b'original key')
    (root/'wam').write_bytes(b'original client')
    migration.tree.mkdir(parents=True)
    commands=[]; original_run=m.run
    def run(argv,**kw):
        if argv[0]=='cp': return original_run(argv,**kw)
        commands.append(argv)
        return SimpleNamespace(stdout='{}' if argv[:3]==['tailscale','serve','status'] else '',returncode=0)
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m,'HELPER',str(tmp_path/'no-helper'))
    monkeypatch.setattr(migration,'user',lambda *a,**kw:SimpleNamespace(returncode=3,stdout=''))
    realcopy=m.copy; interrupted=[False]
    def copy(source,target):
        if source==root/'state' and not interrupted[0]:
            interrupted[0]=True
            raise InterruptedError('interrupted after first restored tree')
        return realcopy(source,target)
    monkeypatch.setattr(m,'copy',copy)
    with pytest.raises(InterruptedError): migration.rollback()
    assert json.loads(migration.path.read_text())['rollback_current']=='restore original data and user units'
    assert migration.doc['state']=='rolling_back'
    migration.rollback()
    assert migration.doc['state']=='rolled_back'
    assert (migration.old/'runtipi/app-data/wizardapps/calendar/data/canary').read_bytes()==b'original data'
    assert (migration.old/'.wizard-os/api-token').read_bytes()==b'original token'
    assert (migration.old/'.config/systemd/user/wizard-app-manager-api.service').read_bytes()==b'original unit'
    assert (Path(migration.doc['hermes_home'])/'config.yaml').read_bytes()==b'original config'
    assert ['tailscale','set','--operator=hermes'] in commands
    assert any(c[0]=='usermod' and 'docker' in c[2] for c in commands)
    count=len(commands); migration.rollback(); assert len(commands)==count


def test_doctor_marks_success_only_after_all_rows_and_links(migration,monkeypatch):
    calls=[]
    monkeypatch.setattr(migration,'system',lambda code:calls.append(code))
    monkeypatch.setattr(migration,'check_links',lambda:calls.append('links'))
    migration.doctor()
    assert 'all(ok for _,ok,_ in rows)' in calls[0]
    assert calls[-1]=='links' and migration.doc['doctor_passed']


def test_service_preserves_extra_agent_registry_at_agent_uid_without_host_symlinks():
    from wam.privsep import service_override
    st={'agent':{'uid':1000,'gid':1000,'hermes_home':'/home/hermes/.hermes','origin':'http://127.0.0.1:8642'},
        'shell':{'port':8080},'migration':{'legacy_home':'/home/hermes'}}
    text=service_override(st,Path('/var/lib/wizard-os/.wizard-os'))
    assert '/var/lib/wizard-os/.wizard-os/agents:/state/agents' in text
    assert '/var/lib/wizard-os/.wizard-os/agents:/home/hermes/.wizard-os/agents' in text
    assert 'user: "1000:1000"' in text
    assert '/var/run/docker.sock' not in text


def test_auxiliary_units_are_regenerated_protected_and_have_real_newlines(migration,monkeypatch):
    p=migration.snapshot/'user-units/wizard-troubleshooter.service'
    p.parent.mkdir(parents=True); p.write_text('never adopt this ExecStart')
    migration.doc['units']={p.name:{'active':True,'enabled':True}}
    def system(code,**kwargs):
        compile(code,'protected-auxiliary-generator','exec')
        assert "env['CHATGPT_AUTH_FILE']=''" in code
        return SimpleNamespace(stdout=json.dumps({p.name:'[Service]\nExecStart=node protected\n[Install]\nWantedBy=default.target\n'}))
    monkeypatch.setattr(migration,'system',system)
    result=migration.auxiliary_units()[p.name]
    assert '[Service]\nUser=wizard-system\nGroup=wizard-system\n' in result
    assert 'WantedBy=multi-user.target' in result


def test_fixed_system_units_preserve_schedule_not_user_execstart(migration,monkeypatch):
    u=migration.snapshot/'user-units'; u.mkdir(parents=True)
    (u/'wizard-backup.service').write_text('[Service]\nExecStart=malicious-owner-command\n')
    (u/'wizard-backup.timer').write_text('[Timer]\nOnCalendar=*-*-* 02:17:00\nPersistent=true\n')
    migration.doc['units']={name:{'enabled':True,'active':False} for name in ('wizard-backup.service','wizard-backup.timer')}
    monkeypatch.setattr(migration,'user',lambda *a,**k:None)
    commands=[]; monkeypatch.setattr(m,'run',lambda a,**k:commands.append(a))
    migration.units()
    text=(m.SYSTEMD/'wizard-backup.service').read_text()
    assert 'User=wizard-system' in text and '-m wam backup run --scheduled' in text
    assert 'malicious-owner-command' not in text
    assert '02:17:00' in (m.SYSTEMD/'wizard-backup.timer').read_text()
    assert not any('gpasswd' in a for a in commands)


def test_prepare_uses_protected_source_and_opaque_main_agent_handoff(migration,monkeypatch):
    state=migration.snapshot/'state'; state.mkdir(parents=True)
    (state/'linked.json').write_bytes(b'opaque existing link record')
    commands=[]; realrun=m.run
    def run(a,**kw):
        if a[0]=='cp': return realrun(a,**kw)
        commands.append(a)
    monkeypatch.setattr(m,'run',run)
    migration.prepare()
    assert (migration.old/'.wizard-os/linked.json').read_bytes()==b'opaque existing link record'
    assert (m.CONTROL/'agent-handoff-ready').is_file()
    assert commands[0][0:2]==['/usr/bin/python3','-I']
    assert commands[0][2]==migration.tree/'scripts/prepare-alongside.py'
    assert commands[0][-1]=='--migration'
    # A resume must not overwrite the handoff after wizard link completed.
    (migration.old/'.wizard-os/linked.json').write_bytes(b'completed')
    migration.prepare()
    assert (migration.old/'.wizard-os/linked.json').read_bytes()==b'completed'


def test_migration_grant_is_not_renewed_by_resume_or_rollback_marker(migration,monkeypatch):
    calls=[]
    monkeypatch.setattr(m,'run',lambda argv,**kw:calls.append((argv,kw)))
    monkeypatch.setattr(migration,'system',lambda *a,**k:pytest.fail('root migration must not elevate through developer.helper'))
    # Only this known root ledger is outside the test filesystem.
    original=Path.exists
    monkeypatch.setattr(Path,'exists',lambda p:False if str(p)=='/var/lib/wizard-os-server-access/migration-used' else original(p))
    migration.grant(); migration.grant()
    assert calls == [([m.HELPER, 'grant'], {'input': json.dumps({'seconds': 604800, 'migration': True}), 'capture_output': True})]
    assert (m.CONTROL/'migration-used').is_file()


def test_start_requires_successful_lifecycle_and_preserved_links(migration,monkeypatch):
    calls=[]
    monkeypatch.setattr(migration,'system',lambda code:calls.append(code))
    monkeypatch.setattr(migration,'check_links',lambda:calls.append('links'))
    migration.start()
    assert 'regenerate' in calls[0]
    assert 'assert not r["failed"]' in calls[1] and calls[-1]=='links'


def test_inventory_is_metadata_only_and_captures_original_authority(migration,monkeypatch):
    (migration.old/'.wizard-os/alongside.json').write_text('{"mode":"alongside"}')
    monkeypatch.setattr(m.os,'getgrouplist',lambda *a:[1000,123])
    monkeypatch.setattr(m.grp,'getgrgid',lambda gid:SimpleNamespace(gr_name='docker' if gid==123 else 'hermes'))
    monkeypatch.setattr(migration,'user',lambda *a,**k:SimpleNamespace(returncode=1,stdout=''))
    commands=[]
    def run(argv,**kw):
        commands.append(argv)
        if argv[:3]==['tailscale','debug','prefs']: stdout='{"OperatorUser":"hermes"}'
        elif argv[:3]==['tailscale','serve','status']: stdout='{}'
        elif '{{.Label "runtipi.appurn"}}' in argv: stdout='wizard-calendar:wizardapps\n'
        else: stdout=''
        return SimpleNamespace(returncode=0,stdout=stdout)
    monkeypatch.setattr(m,'run',run)
    migration.inventory()
    assert migration.doc['groups']==['hermes','docker']
    assert migration.doc['operator']=='hermes'
    assert migration.doc['running_apps']==['wizard-calendar:wizardapps']
    assert not any('inspect' in c for c in commands)
    assert not migration.snapshot.exists()
    assert commands[0] == ['/usr/bin/python3', '-I', m.CONTROL/'source/scripts/check-migration-authority.py', 'hermes']


def test_inventory_policy_refusal_prevents_quiesce_or_snapshot(migration,monkeypatch):
    (migration.old/'.wizard-os/alongside.json').write_text('{"mode":"alongside"}')
    monkeypatch.setattr(m.os,'getgrouplist',lambda *a:[1000])
    monkeypatch.setattr(m.grp,'getgrgid',lambda gid:SimpleNamespace(gr_name='hermes'))
    def refuse(argv, **kw):
        raise m.subprocess.CalledProcessError(2, argv)
    monkeypatch.setattr(m,'run',refuse)
    monkeypatch.setattr(migration,'quiesce',lambda:pytest.fail('authority must be checked first'))
    with pytest.raises(m.subprocess.CalledProcessError): migration.migrate()
    assert not migration.snapshot.exists() and not m.BASE.exists()
    assert migration.doc['done'] == []


def test_failed_root_grant_never_consumes_control_marker(migration,monkeypatch):
    original=Path.exists
    monkeypatch.setattr(Path,'exists',lambda p:False if str(p)=='/var/lib/wizard-os-server-access/migration-used' else original(p))
    def refuse(argv, **kw):
        raise m.subprocess.CalledProcessError(2, argv)
    monkeypatch.setattr(m,'run',refuse)
    with pytest.raises(m.subprocess.CalledProcessError): migration.grant()
    assert not (m.CONTROL/'migration-used').exists()


def test_refuses_nonroot_owner_command(monkeypatch):
    monkeypatch.setattr(os,'getuid',lambda:1000)
    with pytest.raises(ValueError,match='owner'): m.main(['rollback'])
