"""Focused QA identity/regression controls; no real Docker, VM or storage work."""
import ast
import copy
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace
import pytest

REPO = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO/'qa/privsep-storage'/filename)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


identity = load('service_identity_regressions','service_identity.py')
continuation = load('service_continuation_regressions','service_continue.py')


def saved_image(*, compressed=False, damaged=False, duplicate=False, filename='config.json'):
    layer = b'synthetic-layer-data\n'
    digest = 'sha256:'+hashlib.sha256(layer).hexdigest()
    config = {'os':'linux','architecture':'amd64','config':{'Labels':{'revision':'accepted'}},
              'rootfs':{'type':'layers','diff_ids':[digest]}}
    raw = json.dumps(config).encode()
    manifest = [{'Config':filename,'RepoTags':['accepted:backend'],'Layers':['layer.tar']}]
    members = [(filename,raw),('layer.tar',gzip.compress(layer) if compressed else layer),
               ('manifest.json',json.dumps(manifest).encode())]
    if damaged: members[1] = ('layer.tar',b'different-layer\n')
    if duplicate: members.append((filename,raw))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        for name,body in members:
            m=tarfile.TarInfo(name);m.size=len(body);archive.addfile(m,io.BytesIO(body))
    stream.seek(0)
    accepted={'config_sha256':hashlib.sha256(raw).hexdigest(),
        'platform':{'os':'linux','architecture':'amd64','variant':None},
        'labels':config['config']['Labels'],'rootfs':config['rootfs']}
    return stream,accepted


@pytest.mark.parametrize('kind',['legacy-name','oci-blob-name','gzip-layer'])
def test_saved_config_bytes_not_filename_or_store_handle_are_identity(kind):
    stream,accepted=saved_image(compressed=kind=='gzip-layer',
        filename='blobs/sha256/'+'0'*64 if kind=='oci-blob-name' else '0'*64+'.json')
    actual=identity.archive_identity(stream)
    identity.bind_archive(actual,accepted,tag='accepted:backend')
    assert actual['config_sha256']!='0'*64


@pytest.mark.parametrize('bad',['config','platform','labels','layers','tag'])
def test_tag_and_matching_revision_do_not_waive_exact_config_or_layer_binding(bad):
    stream,accepted=saved_image();actual=identity.archive_identity(stream)
    wrong=copy.deepcopy(accepted)
    if bad=='config':wrong['config_sha256']='0'*64
    elif bad=='platform':wrong['platform']['architecture']='arm64'
    elif bad=='labels':wrong['labels']['revision']='other'
    elif bad=='layers':wrong['rootfs']['diff_ids']=['sha256:'+'0'*64]
    else:actual['repo_tags']=['wrong:tag']
    with pytest.raises(AssertionError):identity.bind_archive(actual,wrong,tag='accepted:backend')


@pytest.mark.parametrize('bad',['damaged-layer','duplicate-config','path-escape','multiple-manifests'])
def test_archive_must_be_unambiguous_and_layers_must_match_config(bad):
    stream,_=saved_image(damaged=bad=='damaged-layer',duplicate=bad=='duplicate-config',
                         filename='../config.json' if bad=='path-escape' else 'config.json')
    if bad=='multiple-manifests':
        with tarfile.open(fileobj=stream,mode='r') as a:
            members=[(m.name,a.extractfile(m).read()) for m in a]
        data=json.loads(members[-1][1]);members[-1]=('manifest.json',json.dumps(data+data).encode())
        stream=io.BytesIO()
        with tarfile.open(fileobj=stream,mode='w') as a:
            for name,body in members:
                m=tarfile.TarInfo(name);m.size=len(body);a.addfile(m,io.BytesIO(body))
        stream.seek(0)
    with pytest.raises(AssertionError):identity.archive_identity(stream)


def inspected(accepted, handle):
    return {'Id':handle,'Os':'linux','Architecture':'amd64','RootFS':{
        'Type':'layers','Layers':accepted['rootfs']['diff_ids']},
        'Config':{'Labels':accepted['labels']},'Descriptor':{'digest':handle}}


@pytest.mark.parametrize('store',['classic','descriptor'])
def test_actual_engine_handle_remains_distinct_from_portable_config_digest(store,monkeypatch):
    stream,accepted=saved_image();config='sha256:'+accepted['config_sha256']
    handle=config if store=='classic' else 'sha256:'+'d'*64
    info=inspected(accepted,handle);commands=[]
    def output(argv,**kwargs):
        commands.append(argv);return json.dumps([info]).encode()
    monkeypatch.setattr(identity.subprocess,'check_output',output)
    class Export:
        stdout=stream
        def wait(self,**kwargs):return 0
        def poll(self):return 0
    monkeypatch.setattr(identity.subprocess,'Popen',lambda argv,**kwargs:Export())
    row=identity.attest_loaded(accepted,'accepted:backend')
    assert row['engine_image_id']==handle and row['config_digest']==config
    assert len(commands)==2


@pytest.mark.parametrize('bad',['same-label-wrong-config','retag-race','failed-export'])
def test_actual_attester_refuses_wrong_saved_content_or_changed_locator(bad,monkeypatch):
    stream,accepted=saved_image();info=inspected(accepted,'sha256:'+'d'*64)
    if bad=='same-label-wrong-config': accepted['config_sha256']='0'*64
    calls=[]
    def output(argv,**kwargs):
        row=copy.deepcopy(info);calls.append(argv)
        if bad=='retag-race' and len(calls)==2:row['Id']='sha256:'+'e'*64
        return json.dumps([row]).encode()
    monkeypatch.setattr(identity.subprocess,'check_output',output)
    class Export:
        stdout=stream
        def wait(self,**kwargs):return 1 if bad=='failed-export' else 0
        def poll(self):return 0
    monkeypatch.setattr(identity.subprocess,'Popen',lambda argv,**kwargs:Export())
    with pytest.raises(AssertionError):identity.attest_loaded(accepted,'accepted:backend')


@pytest.mark.parametrize('bad',['id','os','arch','variant','labels','layers'])
def test_inspect_identity_also_requires_exact_platform_labels_and_ordered_layers(bad):
    _,accepted=saved_image();info=inspected(accepted,'sha256:'+'d'*64)
    if bad=='id':info['Id']='not-an-id'
    elif bad=='os':info['Os']='windows'
    elif bad=='arch':info['Architecture']='arm64'
    elif bad=='variant':info['Variant']='v8'
    elif bad=='labels':info['Config']['Labels']={}
    else:info['RootFS']['Layers']=[]
    with pytest.raises(AssertionError):identity.bind_inspect(info,accepted)


def test_service_tail_uses_actual_accepted_consumer_without_storage_prefix(tmp_path):
    source=(REPO/'qa/privsep-storage/production_run.py').read_text()
    handle='sha256:'+'d'*64;service='sha256:'+'e'*64
    seen=[]
    class Probe:
        doc={'revision':continuation.PRIOR_REV,'service_revision':'e1ba0d0564f1e0e3da3540f12f884e67ae17fbdf'}
        receipt={};evidence=tmp_path
        def guest(self,label,script,*args):seen.append((label,script))
        def copy(self,path,remote,download):path.write_text(service if path.name=='service-image-id' else 'unit export fixture')
        def save(self):pass
        def collect_json(self,name):seen.append(('collect',name))
        def mark(self,name,fn):seen.append(('mark',name));fn()
    continuation.service_tail(source,handle)(Probe())
    assert [s for n,s in seen if n=='mark']==list(continuation.REMAINING_CASES)
    installed=next(s for n,s in seen if n=='installed-service')
    assert handle+' '+service in installed
    labels=[n for n,_ in seen]
    assert labels[0]=='package-exact-service' and 'normal-alongside-production-install' in labels
    assert not any(n.startswith(('request-','cold-','return-normal','docker-prerequisite','load-backend')) for n in labels)
    assert 'get.sh' in next(s for n,s in seen if n=='normal-alongside-production-install')


def test_service_only_execute_never_calls_committed_storage_or_full_python_work():
    source=(REPO/'qa/privsep-storage/service_continue.py').read_text()
    tree=ast.parse(source)
    cls=next(n for n in ast.walk(tree) if isinstance(n,ast.ClassDef) and n.name=='ServiceRunner')
    fn=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='execute_service')
    calls=[ast.unparse(n.func) for n in ast.walk(fn) if isinstance(n,ast.Call)]
    assert calls.count('self.start_normal')==1
    assert not set(calls).intersection({'self.cold','self.continue_production','self.focused_tests','Transaction'})
    body=ast.unparse(fn)
    assert body.index("'committed-state-before'")<body.index("'attest-loaded-backend'")<body.index('service_tail(')
    assert 'tests/test_storage_service_identity.py --junitxml=' in body
    assert "'committed-state-after'" in body


def test_state_observer_never_calls_transaction_or_changes_request():
    tree=ast.parse((REPO/'qa/privsep-storage/service_state.py').read_text())
    calls=[ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n,ast.Call)]
    assert not any('Transaction' in n or n.endswith("['main']") or n.endswith('unlink') for n in calls)
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='committed')
    body=ast.unparse(fn)
    assert "product['require_mapping']('hermes', str(logical))" in body
    assert "'committed'" in body and "'retired-before.json'" in body
    assert not any(isinstance(n,ast.Call) and ast.unparse(n.func).endswith(('write_text','write_bytes')) for n in ast.walk(fn))


def test_capacity_uses_remaining_original_peak_without_changing_reserve():
    assert continuation.RESERVE==217179869184
    assert continuation.REMAINING+2498531328==68719476736
    body=ast.unparse(ast.parse((REPO/'qa/privsep-storage/service_continue.py').read_text()))
    assert 'free >= RESERVE + pending' in body
    assert 'disk_binding(PRIOR' in body and "'original_committed_disks_preserved'" in body
