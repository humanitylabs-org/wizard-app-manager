import json
from test_release_pins import box, SHELL_DIGEST, OTHER, STUB_INSTALL, git


def split_release(box):
    path=box['rel']/'9.9.9.json'
    rel=json.loads(path.read_text())
    rel['images']['wizard-ux']=rel['images'].pop('wizard-shell')
    rel['images']['wizard-ux']['ref']='ghcr.io/humanitylabs-org/wizard-ux:latest'
    rel['images']['wizard-agent-service']={'ref':'ghcr.io/humanitylabs-org/wizard-agent-service:latest','digest':OTHER}
    rel['default_apps']['shell']['app']='wizard-ux'
    rel['default_apps']['agent-service']={'app':'wizard-agent-service','index_version':'0.2.0'}
    path.write_text(json.dumps(rel))
    docker=box['home'].parent/'bin/docker'
    text=docker.read_text().replace('wizard-shell','wizard-ux')
    text=text.replace('echo "runtipi";', 'echo "wizard-agent-service_wizardapps-1"; echo "runtipi";')
    text=text.replace('runtipi) echo img-runtipi', 'wizard-agent-service*) echo img-service ;; runtipi) echo img-runtipi')
    text=text.replace('img-shell) echo', 'img-service) echo \'["ghcr.io/humanitylabs-org/wizard-agent-service@'+OTHER+'"]\' ;;\n      img-shell) echo')
    docker.write_text(text)
    apps=json.loads(json.dumps(box['apps']))
    apps['apps'][0]['id']='wizard-ux'
    apps['apps'].append({'id':'wizard-agent-service','version':'0.2.0'})
    return apps


def test_dirty_checkout_is_rejected_before_install(box):
    initial, _ = box['run']()
    assert initial.returncode == 0
    (box['home']/'wam'/'install.sh').write_text('#!/bin/bash\ntouch "$HOME/dirty-was-run"\n')
    p,rec=box['run']()
    assert p.returncode==1 and 'local changes' in p.stderr
    assert not (box['home']/'dirty-was-run').exists()


def test_split_images_validate_and_read_back_both(box):
    apps=split_release(box)
    p,rec=box['run'](APPS_JSON=json.dumps(apps),WIZARD_MODE='alongside')
    assert p.returncode==0,p.stdout+p.stderr
    assert rec['status']=='pinned'
    assert {'image wizard-ux','image wizard-agent-service'} <= {c['name'] for c in rec['checks']}


def test_partial_split_manifest_rejected_before_install(box):
    split_release(box)
    path=box['rel']/'9.9.9.json'; rel=json.loads(path.read_text())
    del rel['images']['wizard-agent-service']; path.write_text(json.dumps(rel))
    p,rec=box['run']()
    assert p.returncode==2 and not (box['home']/'install-env').exists()


def test_similarly_named_container_is_not_accepted(box):
    apps=split_release(box)
    docker=box['home'].parent/'bin/docker'
    docker.write_text(docker.read_text().replace('echo "wizard-agent-service_wizardapps-1"','echo "other-wizard-agent-service_wizardapps-1"'))
    p,rec=box['run'](APPS_JSON=json.dumps(apps),WIZARD_MODE='alongside')
    assert p.returncode==1 and rec['status']=='mismatch'
    assert not next(c for c in rec['checks'] if c['name']=='image wizard-agent-service')['ok']


def test_failed_install_does_not_queue_updates(box):
    apps=split_release(box)
    p,rec=box['run'](APPS_JSON=json.dumps(apps),WIZARD_MODE='alongside',SHELL_DIGEST=OTHER,INSTALL_RC='5')
    assert p.returncode==5 and rec['status']=='install-failed'
    assert 'updating wizard-ux' not in p.stdout


def test_split_pins_exported_to_installer(box):
    split_release(box)
    source=box['src']/'install.sh'
    source.write_text(STUB_INSTALL.replace('exit "${INSTALL_RC:-0}"', 'printf "%s\\n%s\\n" "$WIZARD_UX_IMAGE" "$WIZARD_AGENT_SERVICE_IMAGE" > "$HOME/split-env"\nexit "${INSTALL_RC:-0}"'))
    git('add','.',cwd=box['src']); git('-c','user.name=t','-c','user.email=t@t','commit','-qm','split-env',cwd=box['src'])
    path=box['rel']/'9.9.9.json'; rel=json.loads(path.read_text())
    rel['components']['wizard-app-manager']['commit']=git('rev-parse','HEAD',cwd=box['src']); path.write_text(json.dumps(rel))
    p,_=box['run']()
    assert (box['home']/'split-env').read_text().splitlines()==['ghcr.io/humanitylabs-org/wizard-ux@'+SHELL_DIGEST,'ghcr.io/humanitylabs-org/wizard-agent-service@'+OTHER]
