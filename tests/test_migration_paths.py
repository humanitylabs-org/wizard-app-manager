"""Relocation must preserve access without grandfathering newly added mounts."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from wam import migration_paths as p, host_policy


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_all_management_metadata_moves_but_private_payload_is_opaque(tmp_path, monkeypatch):
    rt, state = tmp_path/'system/runtipi', tmp_path/'system/.wizard-os'
    old = '/home/legacy/runtipi'
    files = [rt/'.env',rt/'state/settings.json',rt/'apps/wizardapps/demo/docker-compose.yml',
        rt/'apps/wizardapps/demo/docker-compose.generated.yml',rt/'repos/wizardapps/apps/demo/docker-compose.yml',
        rt/'apps/wizardapps/demo/.wizard-source',rt/'user-config/wizardapps/demo/docker-compose.yml',
        rt/'user-config/wizardapps/demo/app.env',state/'apps.json',state/'builder/current/bundle.json',
        rt/'app-data/wizard-os/apps.json']
    for f in files: put(f, old+'/app-data/demo\n')
    private=put(rt/'app-data/wizardapps/demo/data/private',old)
    source=put(rt/'app-data/.wizard-source/wizardapps/demo@1234/private',old)
    original=Path.read_text
    def sealed(path,*a,**kw):
        assert path not in (private,source), 'migration parsed opaque payload'
        return original(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',sealed)
    assert len(p.rewrite(rt,state,[old],[str(rt)]))==len(files)
    for f in files: assert f.read_text()==str(rt)+'/app-data/demo\n'
    assert original(private)==old and original(source)==old
    p.assert_clean(rt,state,[old])
    files[0].write_text(old+'\n')
    with pytest.raises(ValueError,match='Legacy management'):p.assert_clean(rt,state,[old])


def test_directory_boundaries_and_hermes_home_do_not_move():
    old='/home/legacy/runtipi'
    text=old+'/a '+old+'-other/a /home/legacy/.hermes/cache/audio '+old+':/data'
    assert p.replace(text,[old],['/system/runtipi'])=='/system/runtipi/a '+old+'-other/a /home/legacy/.hermes/cache/audio /system/runtipi:/data'


def test_approved_risks_rekey_before_recover_and_new_access_still_needs_approval(tmp_path):
    old='/home/legacy/runtipi'; rt=tmp_path/'system/runtipi'; home=tmp_path/'system/.wizard-os'
    recipe=put(rt/'apps/wizardapps/demo/docker-compose.yml',f'services:\n  demo:\n    image: example\n    volumes: ["{old}/external:/external", "/home/legacy/.hermes/cache/audio:/audio:ro"]\n')
    c=SimpleNamespace(home=home,runtipi_root=rt,migration_metadata_only=True)
    home.mkdir(); host_policy.migrate(c)
    before=host_policy._read(home)
    p.rewrite(rt,home,[old],[str(rt)])
    after=host_policy._read(home)
    row=next(iter(after['approvals'].values()))
    assert any(str(rt)+'/external' in x for x in row['risks'])
    assert any('/home/legacy/.hermes/cache/audio' in x for x in row['risks'])
    host_policy.require_app(c,'demo',where='installed')
    recipe.write_text(recipe.read_text().replace('image: example','image: example\n    privileged: true'))
    with pytest.raises(host_policy.NeedsOwnerApproval): host_policy.require_app(c,'demo',where='installed')
    assert host_policy._read(home)['approvals']==after['approvals']
    assert before['risk_set_migration']==after['risk_set_migration']


def test_regeneration_uses_runtipi_and_preserves_full_saved_form():
    calls=[]
    form={'port':9876,'exposed':True,'domain':'example.test','CUSTOM_SETTING':'unchanged'}
    rt=SimpleNamespace(installed=lambda:[{'info':{'urn':'demo:wizardapps'}}],
        app=lambda urn:{'app':{'config':form,'enableAuth':True}},
        req=lambda *a,**kw:calls.append((a,kw)))
    assert p.regenerate(rt)==['demo:wizardapps']
    assert calls==[(('PATCH','/app-lifecycle/demo:wizardapps/update-config',{**form,'enableAuth':True}),{'timeout':120})]
    assert 'enableAuth' not in form


def test_protected_service_wiring_retains_provenance_without_opening_hermes(tmp_path, monkeypatch):
    from wam import alongside, managed_overrides, privsep, network
    root=tmp_path/'runtipi'; home=tmp_path/'state'; home.mkdir(); root.mkdir()
    st={'mode':'alongside','agent':{'uid':1000,'gid':1000,'hermes_home':'/home/legacy/.hermes','origin':'http://127.0.0.1:8642'},
        'shell':{'port':8080},'migration':{'legacy_home':'/home/legacy'},'migration_metadata_only':True}
    alongside.save(home,st); (home/'privsep.json').write_text('{}')
    c=SimpleNamespace(home=home,runtipi_root=root,migration_metadata_only=True)
    doc=network.load_yaml(privsep.service_override(st,home))
    assert managed_overrides.normalize(c,'wizard-agent-service','wizardapps',doc) is None
    doc['services']['wizard-agent-service']['privileged']=True
    assert managed_overrides.normalize(c,'wizard-agent-service','wizardapps',doc) is not None


def test_metadata_symlink_escape_is_refused(tmp_path):
    rt=tmp_path/'runtipi'; home=tmp_path/'state';rt.mkdir();home.mkdir()
    outside=put(tmp_path/'outside','/old/runtipi')
    (rt/'.env').symlink_to(outside)
    with pytest.raises(ValueError,match='symlink'):p.rewrite(rt,home,['/old/runtipi'],[str(rt)])
