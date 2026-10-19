"""Exact authorized Store selections, exercised without touching owner apps."""
import json
import threading
from types import SimpleNamespace
import pytest
from wam import store_selection as ss, store_install as si, cli, private, registry

PIN = 'a' * 40
ENTRY = {'id': 'demo', 'repo': 'org/demo', 'commit': PIN, 'uses': []}
BODY = {'source': 'owner-private', 'repo': 'org/demo', 'commit': PIN, 'operation': 'install'}

@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(ss.test_catalog, 'resolve', lambda app: dict(ENTRY) if app == 'demo' else None)
    monkeypatch.setattr(ss.index, 'resolve', lambda index, ref: {**ENTRY, 'install': {'commit': PIN}})
    monkeypatch.setattr(ss.index, 'search', lambda *args,**kwargs:[])
    return SimpleNamespace(home=tmp_path, runtipi_root=tmp_path, index='https://index.test', runtipi=lambda: None)

@pytest.mark.parametrize('field,value', [('source','foreign'),('repo','evil/demo'),('commit','b'*40),('commit','main'),('operation','delete')])
def test_rejects_unapproved_selections(c, field, value):
    with pytest.raises(ValueError): ss.resolve(c, 'demo', {**BODY, field:value})

@pytest.mark.parametrize('body', [{}, {**BODY,'url':'https://evil.test'}, {**BODY,'commit':None}, {**BODY,'source':'owner-private\n'}, {**BODY,'repo':'../demo'}])
def test_rejects_missing_extra_and_malformed_fields(c, body):
    with pytest.raises(ValueError): ss.resolve(c, 'demo', body)

def test_private_and_public_exact_pins(c):
    assert ss.resolve(c,'demo',BODY)['entry'] == ENTRY
    result=ss.resolve(c,'demo',{**BODY,'source':'standard'})
    assert result['commit']==PIN
    with pytest.raises(ValueError): ss.resolve(c,'wrong-id',BODY)

def test_public_selection_uses_repo_not_first_id(c, monkeypatch):
    calls=[]
    monkeypatch.setattr(ss.index,'resolve',lambda index,ref: calls.append(ref) or {**ENTRY,'install':{'commit':PIN}})
    ss.resolve(c,'demo',{**BODY,'source':'standard'})
    assert calls==['org/demo']


def test_legacy_id_only_is_public_unambiguous_and_pinned(c,monkeypatch):
    monkeypatch.setattr(ss.index,'search',lambda *args,**kwargs:[ENTRY])
    result=ss.resolve(c,'demo',{})
    assert result['source']=='standard' and result['commit']==PIN and result['operation']=='install'
    monkeypatch.setattr(ss.index,'search',lambda *args,**kwargs:[ENTRY,{**ENTRY,'repo':'foreign/demo'}])
    with pytest.raises(ValueError,match='ambiguous'):ss.resolve(c,'demo',{})

def test_alpha_requires_subscription_and_exact_pin(c, monkeypatch):
    home=c.home/'alpha'; home.mkdir(); monkeypatch.setenv('WIZARD_ALPHA_HOME',str(home))
    (home/'config.json').write_text(json.dumps({'sourceId':'alpha-test'}))
    (home/'snapshot.json').write_text(json.dumps({'source':{'id':'alpha-test'},'apps':[ENTRY]}))
    assert ss.resolve(c,'demo',{**BODY,'source':'alpha-test'})['commit']==PIN
    with pytest.raises(ValueError): ss.resolve(c,'demo',{**BODY,'source':'alpha-test','commit':'b'*40})

def test_jobs_join_only_the_exact_selection(c, monkeypatch):
    si._jobs.clear(); gate=threading.Event(); calls=[]
    monkeypatch.setattr(si,'_links',lambda c:None)
    def install(c,a): calls.append(a); gate.wait(5); return {'status':'running'}
    s=ss.resolve(c,'demo',BODY)
    try:
        code,job=si.start(c,'demo',install,set,selection=s)
        assert code==202 and job['selection']==BODY
        assert si.start(c,'demo',install,set,selection=s)[1] is job
        assert si.start(c,'demo',install,set,selection={**s,'commit':'b'*40})[0]==409
        assert calls==['demo']
    finally: gate.set()

@pytest.mark.parametrize('operation,have', [('install',{'demo'}),('update',set()),('switch',set())])
def test_presence_mismatch_fails_before_prepare(c, monkeypatch, operation, have):
    monkeypatch.setattr(cli,'installed_ids',lambda rt:have)
    monkeypatch.setattr(private,'prepare',lambda *a,**k: pytest.fail('must not prepare'))
    monkeypatch.setattr(registry,'app_meta',lambda *a: {'repo':'org/demo'})
    with pytest.raises(ValueError): ss.execute(c,'demo',ss.resolve(c,'demo',{**BODY,'operation':operation}))

def test_repository_conflict_fails_before_prepare(c,monkeypatch):
    monkeypatch.setattr(cli,'installed_ids',lambda rt:{'demo'})
    installed=c.runtipi_root/'apps/wizardapps/demo';installed.mkdir(parents=True)
    (installed/'.wizard-source').write_text(json.dumps({'repo':'foreign/demo'}))
    monkeypatch.setattr(registry,'app_meta',lambda *a:{'repo':'foreign/demo'})
    monkeypatch.setattr(private,'prepare',lambda *a,**k:pytest.fail('must not prepare'))
    with pytest.raises(ValueError,match='repository conflict'): ss.execute(c,'demo',ss.resolve(c,'demo',{**BODY,'operation':'switch'}))

def test_served_origins_only_exact_owner_routes(monkeypatch):
    def run(*a,**k):
        return SimpleNamespace(stdout=json.dumps({'Web':{
            'box.ts.net:8444': {'Handlers':{si.PREFIX:{'Proxy':'http://127.0.0.1:7791'}}},
            'box.ts.net:8806': {'Handlers':{si.PREFIX:{'Proxy':'http://127.0.0.1:7791'}}},
            'box.ts.net:7777': {'Handlers':{si.PREFIX:{'Proxy':'http://evil.test'}}},
            'other.ts.net:8444': {'Handlers':{si.PREFIX:{'Proxy':'http://127.0.0.1:7791'}}}}}))
    monkeypatch.setattr(si.subprocess,'run',run)
    assert si.served_origins('box.ts.net')=={'https://box.ts.net:8444','https://box.ts.net:8806'}


@pytest.mark.parametrize('source', ['standard','owner-private'])
@pytest.mark.parametrize('operation', ['install','update','switch'])
def test_selected_lifecycle_uses_prepared_record_and_native_backup_update(c,monkeypatch,source,operation):
    import shutil
    calls=[]
    monkeypatch.setattr(cli,'installed_ids',lambda rt:set() if operation=='install' else {'demo'})
    if operation!='install':
        installed=c.runtipi_root/'apps/wizardapps/demo';installed.mkdir(parents=True)
        (installed/'.wizard-source').write_text(json.dumps({'repo':'org/demo'}))
    monkeypatch.setattr(registry,'app_meta',lambda *args:{'repo':'org/demo','commit':'b'*40})
    def prepare(root,store,repo,**kwargs):
        calls.append(('prepare',repo,kwargs))
        folder=root/'repos'/store/'apps'/'demo';folder.mkdir(parents=True)
        (folder/'.wizard-source').write_text(json.dumps({'repo':repo}))
        (folder/'.wizard-commit').write_text(PIN)
        return dict(ENTRY)
    def finish(c,app,**kwargs):
        calls.append(('lifecycle',app,kwargs))
        src=c.runtipi_root/'repos'/'wizardapps'/'apps'/app
        dst=c.runtipi_root/'apps'/'wizardapps'/app
        shutil.copytree(src,dst,dirs_exist_ok=True)
        return {'status':'running'}
    monkeypatch.setattr(private,'prepare',prepare)
    monkeypatch.setattr(cli,'install',finish)
    monkeypatch.setattr(cli,'app_update',finish)
    monkeypatch.setattr(cli,'write_registry',lambda c:None)
    result=ss.execute(c,'demo',ss.resolve(c,'demo',{**BODY,'source':source,'operation':operation}))
    assert result['status']=='running'
    assert calls[0][1]=='org/demo'
    if source=='owner-private': assert calls[0][2]['release']==ENTRY
    else: assert calls[0][2]['source_pin']['commit']==PIN
    if operation=='install': assert calls[1][2]['_record']==ENTRY
    else: assert calls[1][2]=={} # native default backup=True, no uninstall or volume deletion
    assert json.loads((c.runtipi_root/'apps/wizardapps/demo/.wizard-selection').read_text())=={'repo':'org/demo','commit':PIN}


def test_http_selection_and_auth_fail_closed(c,monkeypatch):
    from http.server import ThreadingHTTPServer
    from urllib.request import Request,urlopen
    from urllib.error import HTTPError
    (c.home/'links.json').write_text(json.dumps({'host':'box.ts.net'}))
    monkeypatch.setattr(cli.alongside,'load',lambda home:{})
    monkeypatch.setattr(si,'peer_uid',lambda port:0)
    monkeypatch.setattr(si,'owner_login',lambda:'owner@test')
    monkeypatch.setattr(si,'served_origins',lambda host:{'https://box.ts.net:8444'})
    monkeypatch.setattr(ss,'execute',lambda *args:pytest.fail('invalid requests must never execute'))
    cli.StoreHandler.cfg=c
    server=ThreadingHTTPServer(('127.0.0.1',0),cli.StoreHandler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    headers={'Tailscale-User-Login':'owner@test','Origin':'https://box.ts.net:8444','X-Wizard-Store':'1','Content-Type':'application/json'}
    def post(body,extra=None):
        req=Request(f'http://127.0.0.1:{server.server_port}/demo',data=json.dumps(body).encode(),headers={**headers,**(extra or {})})
        try:
            with urlopen(req) as r:return r.status,json.load(r)
        except HTTPError as e:return e.code,json.load(e)
    try:
        assert post({})[0]==400
        assert post({**BODY,'commit':'b'*40})[0]==400
        assert post(BODY,{'Origin':'https://evil.test'})[0]==403
        assert post(BODY,{'Tailscale-User-Login':'guest@test'})[0]==403
        monkeypatch.setattr(si,'peer_uid',lambda port:1000)
        assert post(BODY)[0]==403
    finally:
        server.shutdown();server.server_close();thread.join()
