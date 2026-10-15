"""Portable builder contracts; no public services or developer profile required."""
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from wam import builder, app_standard, wizardmd


def test_sentence_new_check_and_metadata(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Build me a habit tracker')
    doc = wizardmd.front_matter((root / 'wizard.md').read_text())
    assert doc['api']['spec'] == 'openapi.json'
    assert doc['view']['source']['path'] == 'web'
    assert doc['view']['source']['build'] == 'python3 app/build_view.py'
    assert doc['mcp']['path'] == '/mcp'
    assert (root / '.git').is_dir()
    assert builder.check_app(root, proposal=True)['ok']
    assert json.loads((root / '.builder.json').read_text())['request'] == 'Build me a habit tracker'
    with pytest.raises(ValueError):
        builder.new_app('habit-tracker', tmp_path, 'overwrite')


@pytest.mark.parametrize('bad', ['../escape', 'a/b', 'A_Name', 'x'])
def test_id_confinement(tmp_path, bad):
    with pytest.raises(ValueError):
        builder.new_app(bad, tmp_path, 'request')


def test_missing_api_is_proposal_warning_without_approved_drift(tmp_path):
    root = builder.new_app('sample-app', tmp_path, 'Hello')
    p = root / 'wizard.md'
    d = wizardmd.front_matter(p.read_text())
    del d['api']
    p.write_text('---\n' + yaml.safe_dump(d) + '---\n# Sample\n')
    assert builder.check_app(root)['ok']
    proposed = builder.check_app(root, proposal=True)
    assert proposed['ok'] and proposed['warnings']
    assert proposed['policy'] == 'PR24 proposed (opt-in)'


def test_prepare_publish_binds_payload_and_performs_no_network(tmp_path):
    root = builder.new_app('sample-app', tmp_path, 'Hello')
    with patch('subprocess.run', side_effect=AssertionError('public command')):
        plan = builder.prepare_publish(root, 'example-org/sample-app')
    assert plan['state'] == 'prepared_requires_human_approval'
    assert plan['github']['visibility'] == 'public'
    assert plan['github']['topics'] == ['wizard-app']
    assert plan['catalog']['body'] == {'repo': 'example-org/sample-app'}
    assert plan['signing']['state'] == 'not_signed'
    assert plan['source_sha256'] == builder.source_hash(root)
    assert plan['github']['repo'] == 'example-org/sample-app'
    assert plan['wizard_source'] == 'https://github.com/example-org/sample-app'


def test_local_install_requires_explicit_stage_and_preserves_metadata(tmp_path):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    with pytest.raises(ValueError, match='stage-only'):
        builder.install_local(root, tmp_path / 'state', stage_only=False)
    rec = builder.install_local(root, tmp_path / 'state', stage_only=True)
    assert rec['state'] == 'staged_not_running'
    assert rec['api']['spec'] == 'openapi.json'
    assert rec['view']['source']['dist'] == 'web'
    assert not (tmp_path / 'state' / 'apps.json').exists()
    assert rec['lifecycle_owner'] == 'App installation lane'


def test_skill_lives_outside_generated_refresh(tmp_path):
    os_home, hermes = tmp_path / 'os', tmp_path / 'hermes'
    rec = builder.install_skill(os_home, hermes)
    skill = Path(rec['skill'])
    assert skill.is_relative_to(os_home / 'system-skills')
    assert yaml.safe_load((hermes / 'config.yaml').read_text())['skills']['external_dirs'] == [str(os_home / 'system-skills')]
    (os_home / 'skills').mkdir()
    (os_home / 'skills' / 'generated').touch()
    shutil.rmtree(os_home / 'skills')
    assert skill.is_file()
    assert builder.install_skill(os_home, hermes)['state'] == 'installed'


def test_reject_symlink_and_secret_source(tmp_path):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    (root / '.env').write_text('PASSWORD=dummy')
    with pytest.raises(ValueError, match='secret|excluded'):
        builder.prepare_publish(root, 'example/sample-app')
    (root / '.env').unlink()
    (root / 'escape').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='symlink'):
        builder.install_local(root, tmp_path / 'state', stage_only=True)


def test_build_compose_copies_source_not_private_profile(tmp_path):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    rec = builder.install_local(root, tmp_path / 'state', stage_only=True, port=18799)
    compose = yaml.safe_load(Path(rec['compose']).read_text())
    service = compose['services']['sample-app']
    assert service['ports'] == ['127.0.0.1:18799:8799']
    assert service['read_only'] and service['cap_drop'] == ['ALL']
    assert not any('docker.sock' in str(v) for v in service['volumes'])
    assert builder.source_hash(Path(rec['source'])) == rec['source_sha256']


def test_proposal_override_metadata_validation(tmp_path):
    root = builder.new_app('sample-app', tmp_path, 'Hello')
    d = wizardmd.front_matter((root / 'wizard.md').read_text())
    d['backend'] = 'none'
    (root / 'wizard.md').write_text('---\n' + yaml.safe_dump(d) + '---\n# Sample\n')
    assert builder.check_app(root)['ok']
    assert not builder.check_app(root, proposal=True)['ok']


@pytest.fixture
def app_server(tmp_path):
    import os, socket, time, urllib.request
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    env = dict(os.environ, PORT=str(port), DATA_DIR=str(tmp_path/'data'), BIND='127.0.0.1')
    server = Path(builder.REPO)/'templates/hello-wizard/app/server.py'
    proc = subprocess.Popen([__import__('sys').executable,str(server)], env=env)
    origin = f'http://127.0.0.1:{port}'
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(origin+'/healthz',timeout=1);break
            except Exception:
                time.sleep(.05)
        assert proc.poll() is None
        yield origin, tmp_path/'data'
    finally:
        proc.terminate();proc.wait(timeout=5)


def test_real_api_mcp_roundtrip_and_failures(app_server):
    import urllib.request, urllib.error
    origin,data=app_server
    def call(path, body=None, **headers):
        req=urllib.request.Request(origin+path, None if body is None else json.dumps(body).encode(),
                                   {'Content-Type':'application/json',**headers})
        with urllib.request.urlopen(req,timeout=5) as response:
            return json.load(response)
    def rpc(method,params=None):
        return call('/mcp',{'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    assert rpc('initialize')['result']['protocolVersion']=='2025-06-18'
    init=call('/api/habits');assert init['habits']==[]
    created=rpc('tools/call',{'name':'habits_add','arguments':{'name':'Drink water'}})
    assert not created['result']['isError']
    habit=call('/api/habits')['habits'][0]
    assert habit['name']=='Drink water'
    call('/api/habits/'+str(habit['id'])+'/today', {'done':True})
    assert rpc('tools/call',{'name':'habits_list'})['result']['structuredContent']['habits'][0]['done']
    rpc('tools/call',{'name':'habits_set_done','arguments':{'id':habit['id'],'done':False}})
    assert not call('/api/habits')['habits'][0]['done']
    with pytest.raises(urllib.error.HTTPError) as e:
        call('/api/habits',{'name':'Drink water'})
    assert e.value.code==409
    with pytest.raises(urllib.error.HTTPError) as e:
        call('/api/habits',{'name':'Other'},Origin='https://foreign.invalid')
    assert e.value.code==403
    # DNS rebinding: an attacker's Host with its own matching Origin is still refused, reads too.
    port=origin.rsplit(':',1)[1]
    for headers in [{'Host':'attacker.example:'+port,'Origin':'http://attacker.example:'+port},{'Host':'attacker.example:'+port}]:
        with pytest.raises(urllib.error.HTTPError) as e:
            call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/list'},**headers)
        assert e.value.code==403
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(origin+'/api/habits',headers={'Host':'attacker.example:'+port}))
    assert e.value.code==403
    assert call('/api/habits',{'name':'Same origin'},Origin=origin)['habits']
    assert (data/'habits.sqlite3').is_file()
    head=urllib.request.Request(origin+'/',method='HEAD')
    assert urllib.request.urlopen(head).status==200
    assert rpc('tools/call',{'name':'missing'})['error']['code']==-32602
    bad=rpc('tools/call',{'name':'habits_add','arguments':{'name':''}})
    assert bad['result']['isError']
    # The owner's local day: accepted within a day of UTC, rejected when malformed or far away.
    import datetime as dt
    utc=dt.datetime.now(dt.timezone.utc).date()
    ahead=(utc+dt.timedelta(days=1)).isoformat()
    call('/api/habits/'+str(habit['id'])+'/today',{'done':True,'day':ahead})
    assert call('/api/habits?day='+ahead)['habits'][0]['done']
    assert not call('/api/habits?day='+utc.isoformat())['habits'][0]['done']
    for day in ['yesterday', (utc-dt.timedelta(days=5)).isoformat()]:
        with pytest.raises(urllib.error.HTTPError) as e:
            call('/api/habits?day='+day)
        assert e.value.code==400
    # Static view: allowlisted files only, no traversal.
    assert urllib.request.urlopen(origin+'/wizard-ui.css').status==200
    for path in ['/../app/server.py','/.hidden','/app/server.py']:
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(origin+path)


def test_generated_view_defaults_to_containing_skin(tmp_path):
    root=builder.new_app('skin-app',tmp_path,'Small app')
    html=(root/'web/index.html').read_text();kit=(root/'web/wizard-ui.css').read_text()
    assert html.index('boot.js') < html.index('wizard-ui.css') < html.index('styles.css')
    assert html.index('/wizard-os-home/core/app-client.js') < html.index('app.js')
    assert 'data-app="skin-app"' in html
    # Colours come only from appearance roles; a hosted app paints no canvas of its own.
    assert '--wz-text' in kit and '--wz-accent' in kit and '--wz-border' in kit
    assert 'body { font: 15px/1.4 var(--font); color: var(--ink); background: transparent;' in kit
    assert 'html[data-presentation="standalone"]:not(.wz-hosted) body { background: var(--canvas); }' in kit
    app=(root/'web/app.js').read_text()
    assert 'WizardApp.back.register' in app
    assert 'back.useHistory({ isRoot: () => true })' not in app
    assert 'history.pushState' not in app
    assert not (root/'web/theme-client.js').exists()
    assert json.loads((root/'appearance-provenance.json').read_text())['schema'] == 3
    assert 'prefers-color-scheme: light' in (root/'web/standalone.css').read_text()
    compose=yaml.safe_load((root/'runtipi/docker-compose.yml').read_text())
    assert compose['services']['skin-app']['command'][0]=='python'
    import base64,re
    blob=re.search(r"b64decode\('([^']+)'\)",compose['services']['skin-app']['command'][2]).group(1)
    shipped=set(json.loads(base64.b64decode(blob)))
    assert {'web/'+p.name for p in (root/'web').iterdir()} <= shipped
    env=compose['services']['skin-app']['environment']
    assert env['WIZARD_TRUSTED_HOSTS']=='${WIZARD_TRUSTED_HOSTS:-}'
    from wam.builder_assets import package_inline
    (root/'web'/'logo.png').write_bytes(b'\x89PNG')
    with pytest.raises(ValueError, match='text view files only'):
        package_inline(root)



def test_two_homes_never_share_docker_project_or_data(tmp_path):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    a = builder.install_local(root, tmp_path / 'home-a', stage_only=True, port=18801)
    b = builder.install_local(root, tmp_path / 'home-b', stage_only=True, port=18802)
    assert a['project'] != b['project'] and a['project'].startswith('wizard-dev-sample-app-')
    for rec in (a, b):
        compose = yaml.safe_load(Path(rec['compose']).read_text())
        assert compose['volumes'] == {'app-data': {}}   # Compose prefixes it with the unique project
        assert json.loads((Path(rec['compose']).parent / 'stage.json').read_text())['project'] == rec['project']


def test_receipt_hash_is_the_staged_snapshot_and_edits_during_staging_abort(tmp_path, monkeypatch):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    rec = builder.install_local(root, tmp_path / 'home', stage_only=True)
    assert builder.source_hash(Path(rec['source'])) == rec['source_sha256'] == builder.source_hash(root)
    real = builder.shutil.copyfile
    def edit_while_copying(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        if str(src).endswith('wizard.md'):
            Path(src).write_text(Path(src).read_text() + '\nedited mid-stage\n')
        return out
    monkeypatch.setattr(builder.shutil, 'copyfile', edit_while_copying)
    with pytest.raises(ValueError, match='changed while staging'):
        builder.install_local(root, tmp_path / 'home-2', stage_only=True)
    assert not (tmp_path / 'home-2' / 'staged-apps' / 'sample-app').exists()


@pytest.mark.parametrize('field,value', [('skill', '../outside/SKILL.md'), ('skill', '/etc/hostname')])
def test_manifest_references_must_stay_inside_the_app(tmp_path, field, value):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    (tmp_path / 'dev' / 'outside').mkdir(); (tmp_path / 'dev' / 'outside' / 'SKILL.md').write_text('---\nname: x\ndescription: y\n---\n')
    wm = root / 'wizard.md'; doc = wizardmd.front_matter(wm.read_text()); doc[field] = value
    wm.write_text('---\n' + yaml.safe_dump(doc) + '---\n# Sample\n')
    assert not builder.check_app(root)['ok']
    with pytest.raises(ValueError):
        builder.prepare_publish(root, 'example/sample-app')
    with pytest.raises(ValueError):
        builder.install_local(root, tmp_path / 'home', stage_only=True)


def test_manifest_reference_through_symlinked_folder_is_refused(tmp_path):
    root = builder.new_app('sample-app', tmp_path / 'dev', 'Hello')
    outside = tmp_path / 'elsewhere'; shutil.copytree(root / 'skill', outside)
    shutil.rmtree(root / 'skill'); (root / 'skill').symlink_to(outside, target_is_directory=True)
    rows = builder.check_app(root)['rules']
    assert any(r['rule'] == 'builder: skill stays inside the app' and not r['ok'] for r in rows)


def test_production_install_is_never_rerouted_to_the_builder():
    import inspect
    from wam import cli
    src = inspect.getsource(cli.main)
    # Only the builder's own verbs route to it; `wam install <x>` always means a production install,
    # whatever folders happen to exist in the current directory.
    assert '{"new", "check", "install-local", "update-local", "publish", "builder-skill", "package-inline"}' in src
    assert '"install-local", *argv' not in src and 'is_dir()' not in src
