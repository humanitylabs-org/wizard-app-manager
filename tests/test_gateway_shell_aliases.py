import copy
from types import SimpleNamespace
from wam import links


def config():
    return {'TCP':{str(p):{'HTTPS':True} for p in [8444,8796,8806,7000,7001]},'Web':{
        'box.ts.net:8444':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}},
        'box.ts.net:8796':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}},
        'box.ts.net:8806':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8806'},'/wizard-os-home/store-install':{'Proxy':'http://127.0.0.1:7791'}}},
        'box.ts.net:7000':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:7000'}}},
        'box.ts.net:7001':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}},
        'other.ts.net:8444':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}}},'AllowFunnel':{'box.ts.net:7001':True}}


def test_only_owner_private_shell_mounts():
    assert links.gateway_shell_ports(config(),'box.ts.net',8796)==[8444,8796,8806]


def test_install_routes_reach_aliases_and_preserve_conflicts(tmp_path,monkeypatch):
    cfg=config();cfg['Web']['box.ts.net:8444']['Handlers']['/sample']={'Proxy':'http://127.0.0.1:9999'}
    monkeypatch.setattr(links,'tailnet',lambda:{'state':'Running','host':'box.ts.net'})
    monkeypatch.setattr(links,'serve_status',lambda:copy.deepcopy(cfg))
    monkeypatch.setattr(links,'gateway_paths',lambda home:{'sample':{'path':'/sample/','strip':True}})
    calls=[]
    def run(args,*a):
        calls.append(args)
        port=next(s.split('=',1)[1] for s in args if s.startswith('--https='))
        path=next((s.split('=',1)[1].rstrip('/') for s in args if s.startswith('--set-path=')),'/')
        cfg['Web'].setdefault('box.ts.net:'+port,{'Handlers':{}})['Handlers'][path]={'Proxy':args[-1]}
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(links,'_run',run)
    reg={'apps':[{'id':'shell','name':'Shell','category':'interface','status':'running','view':{'url':'http://127.0.0.1:8796/'}},
                 {'id':'sample','name':'Sample','status':'running','view':{'url':'http://127.0.0.1:9000/'}}]}
    result=links.publish(reg,tmp_path,{'shell'})
    assert 'sample@8444' in result['errors']
    assert cfg['Web']['box.ts.net:8444']['Handlers']['/sample']['Proxy']=='http://127.0.0.1:9999'
    for port in [8796,8806]:assert cfg['Web'][f'box.ts.net:{port}']['Handlers']['/sample']['Proxy']=='http://127.0.0.1:9000'
    assert not any('--https=7000' in a for a in calls)
    assert next(x for x in result['links'] if x['id']=='sample')['open']=='/sample/'
