"""GET /api/system/devices on the host API and its Serve route beside the shell (wizard-app-manager#568).

Synthetic fixtures only (the same ones as wizard-ux qa-system-devices.mjs); no real tailnet or ssh is touched.
"""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import cli, devices, links, resources, store_install

MAC = """os=Darwin
cpus=10
memtotal=34359738368
memused=22548578304
cpuidle=40.5
uptime=388000
disk=/dev/disk3s1s1|239362496|12348620|9994252|/
disk=/dev/disk3s6|239362496|10486460|9994252|/System/Volumes/VM
disk=/dev/disk3s5|239362496|197865956|9994252|/System/Volumes/Data
disk=/dev/disk5s1|3219456|3098224|107784|/private/var/run/com.apple.security.cryptexd/mnt/x
disk=/dev/disk7s1|1953276972|1309598260|643385900|/Volumes/Media Drive"""
LINUX = """os=Linux
cpus=8
memtotal=33000000000
memused=14000000000
cpuidle=80
uptime=100
disk=/dev/sda1|405196772|257038268|148142120|/
disk=/dev/sda15|106858|6182|100676|/boot/efi
disk=/dev/loop3|65536|65536|0|/snap/core/1
disk=tmpfs|1000000000|0|1000000000|/dev/shm
disk=/dev/sdb1|1048576000|524288000|524288000|/srv/data"""


def test_mac_probe_data_volume_is_the_internal_disk():
    r = devices.parse_probe(MAC)
    assert (r['platform'], r['cpu'], r['memory']) == ('macos', 60, 66)
    assert [d['label'] for d in r['disks']] == ['Internal disk', 'Media Drive']
    assert (r['disks'][0]['pct'], r['disks'][0]['freeGB']) == (96, 10)
    assert r['disks'][1]['mount'] == '/Volumes/Media Drive'


def test_linux_probe_keeps_root_and_real_data_disks_only():
    r = devices.parse_probe(LINUX)
    assert [d['label'] for d in r['disks']] == ['System disk', 'data']
    assert (r['disks'][0]['pct'], r['disks'][0]['usedGB'], r['disks'][0]['sizeGB']) == (63, 245, 386)


def test_unknown_platform_is_refused():
    with pytest.raises(ValueError):
        devices.parse_probe('os=Plan9')


def test_registry_drops_unsafe_hosts_bad_ids_and_duplicates():
    r = devices.clean_registry({'devices': [
        {'id': 'ok', 'connect': {'type': 'ssh', 'host': 'box'}}, {'id': 'ok', 'connect': {'type': 'ssh', 'host': 'box2'}},
        {'id': 'x', 'connect': {'type': 'ssh', 'host': '-oProxyCommand=evil'}},
        {'id': 'Bad Id', 'connect': {'type': 'ssh', 'host': 'h'}}, {'id': 'y', 'connect': {'type': 'http', 'host': 'h'}},
        {'id': 'this', 'connect': {'type': 'ssh', 'host': 'h'}}]})
    assert [d['id'] for d in r['devices']] == ['ok']


def test_saved_wizards_keep_url_kind_role_org_for_the_switcher():
    """wizard-ux#1186: the switcher needs url/kind/role/org; a credentialed or non-web address is dropped."""
    r = devices.clean_registry({'managed': [
        {'id': 'acme', 'name': 'Acme', 'url': 'https://acme.example.ts.net:8796', 'kind': 'company',
         'role': 'technical-admin', 'org': 'Acme Ltd', 'peopleCount': 5, 'people': ['Ann']},
        {'id': 'evil', 'name': 'Evil', 'url': 'https://u:p@evil.example/', 'kind': 'root', 'role': 'god'},
        {'id': 'js', 'name': 'Js', 'url': 'javascript:alert(1)'}]})
    acme, evil, js = r['managed']
    assert acme == {'id': 'acme', 'name': 'Acme', 'url': 'https://acme.example.ts.net:8796/', 'kind': 'company',
                    'role': 'technical-admin', 'org': 'Acme Ltd', 'people': ['Ann'], 'peopleCount': 5}
    assert evil == {'id': 'evil', 'name': 'Evil'} and js == {'id': 'js', 'name': 'Js'}


def _reader(tmp_path, reg, **kw):
    p = tmp_path / 'devices.json'
    p.write_text(json.dumps(reg))
    return devices.Reader(p, **kw)


def test_reader_this_box_first_registered_matched_rest_listed(tmp_path):
    tail = {'self': {'name': 'alpha', 'dns': 'alpha', 'os': 'linux', 'online': True}, 'peers': [
        {'name': 'bravo', 'dns': 'bravo', 'os': 'macOS', 'online': True, 'lastSeen': None},
        {'name': 'charlie', 'dns': 'charlie', 'os': 'linux', 'online': False, 'lastSeen': '2026-10-13T00:00:00Z'},
        {'name': 'gw', 'dns': 'gw', 'os': 'linux', 'online': True, 'lastSeen': None},
        {'name': 'printer', 'dns': 'printer', 'os': 'linux', 'online': True, 'lastSeen': None},
        {'name': 'Phone', 'dns': 'phone', 'os': 'android', 'online': True, 'lastSeen': None}]}

    def ssh(host, shell=None):
        if host == 'charlie':
            raise RuntimeError('timed out')
        return devices.parse_probe(MAC)
    read = _reader(tmp_path, {'this': {'tailnet': ['gw']}, 'hide': ['printer'], 'devices': [
        {'id': 'bravo', 'name': 'Bravo', 'connect': {'type': 'ssh', 'host': 'bravo'}},
        {'id': 'charlie', 'name': 'Charlie', 'connect': {'type': 'ssh', 'host': 'charlie'}}],
        'managed': [{'id': 'acme', 'name': 'Acme', 'url': 'https://acme.example/', 'peer': 'bravo'}]},
        probe_local_impl=lambda: devices.parse_probe(LINUX), probe_ssh_impl=ssh, tailnet_impl=lambda: tail)
    j = read()
    assert [(d['id'], d['status']) for d in j['devices']] == [('this', 'online'), ('bravo', 'online'), ('charlie', 'offline')]
    assert j['devices'][0]['name'] == 'Alpha' and j['devices'][2]['lastSeen'] == '2026-10-13T00:00:00Z'
    assert [p['name'] for p in j['network']['peers']] == ['Phone'] and j['network']['peers'][0]['group'] == 'human'
    assert j['managed'] == [{'id': 'acme', 'name': 'Acme', 'url': 'https://acme.example/', 'peer': 'bravo',
                             'peerName': 'bravo', 'peerOnline': True}]
    assert '100.' not in json.dumps(j)


def test_default_install_without_registry_gives_only_this_box(tmp_path):
    read = devices.Reader(tmp_path / 'missing.json', probe_local_impl=lambda: devices.parse_probe(LINUX),
                          tailnet_impl=lambda: None)
    j = read()
    assert len(j['devices']) == 1 and j['devices'][0]['status'] == 'online'
    assert j['network'] == {'available': False, 'peers': []} and 'managed' not in j


def test_readings_are_cached_so_polling_devices_never_stack_probes(tmp_path):
    calls, clock = [], [1000.0]

    def local():
        calls.append(1)
        return devices.parse_probe(LINUX)
    read = devices.Reader(tmp_path / 'missing.json', probe_local_impl=local, tailnet_impl=lambda: None,
                          now=lambda: clock[0])
    read(); read()
    assert len(calls) == 1
    clock[0] += 30000
    read()
    assert len(calls) == 2


def _serve(home, reader=None):
    class Handler(cli.Handler):
        cfg = SimpleNamespace(home=home)
        token = 'synthetic-api-token'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, 'http://127.0.0.1:' + str(server.server_port) + devices.PATH


def test_devices_route_identity_required_read_only_generic_failure(tmp_path, monkeypatch):
    calls = []

    def read():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError('synthetic secret detail')
        return {'devices': [], 'managed': [{'id': 'acme', 'name': 'Acme', 'url': 'https://acme.example/'}]}
    monkeypatch.setattr(devices, 'reader_for', lambda home: read)
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'Owner@example.com')
    server, thread, url = _serve(tmp_path)
    who = {'Tailscale-User-Login': 'owner@example.com'}
    try:
        for method, headers, status in [('GET', {}, 403), ('GET', {**who, 'Sec-Fetch-Site': 'cross-site'}, 403),
                                        ('GET', {'Tailscale-User-Login': 'guest@example.com'}, 403),
                                        ('POST', who, 401)]:
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers))
            assert e.value.code == status
        assert calls == []
        with urllib.request.urlopen(urllib.request.Request(url, headers=who)) as r:
            assert r.headers['Cache-Control'] == 'no-store'
            assert json.load(r)['managed'][0]['url'] == 'https://acme.example/'
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers=who))
        assert e.value.code == 503 and json.load(e.value) == {'error': 'devices_unavailable'}
        monkeypatch.setattr(store_install, 'owner_login', lambda: '')   # owner unknown: refused, never assumed
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(url, headers=who))
        assert e.value.code == 403 and json.load(e.value) == {'error': 'owner_only'}
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_devices_route_reads_the_box_registry(tmp_path, monkeypatch):
    """End to end on the real reader: the registry under the App Manager's home reaches every device."""
    (tmp_path / 'devices.json').write_text(json.dumps({'managed': [{'id': 'two', 'name': 'Second Wizard',
                                                                    'url': 'https://two.example/', 'kind': 'remote'}]}))
    monkeypatch.delenv('WIZARD_DEVICES_FILE', raising=False)
    monkeypatch.setattr(devices, 'probe_local', lambda: devices.parse_probe(LINUX))
    monkeypatch.setattr(devices, 'tailnet_status', lambda: None)
    monkeypatch.setattr(devices, '_readers', {})
    monkeypatch.setattr(store_install, 'owner_login', lambda: 'o@example.com')
    server, thread, url = _serve(tmp_path)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'Tailscale-User-Login': 'o@example.com'})) as r:
            j = json.load(r)
        assert j['managed'] == [{'id': 'two', 'name': 'Second Wizard', 'url': 'https://two.example/', 'kind': 'remote'}]
        assert j['devices'][0]['id'] == 'this'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_devices_route_is_published_exactly_beside_the_shell(monkeypatch):
    config = {'TCP': {'8796': {'HTTPS': True}}, 'Web': {'test.ts.net:8796': {'Handlers': {
        '/': {'Proxy': 'http://127.0.0.1:8796'}, resources.PATH: {'Proxy': 'http://127.0.0.1:7790/api/system/load'}}}}}
    monkeypatch.setattr(links, 'serve_status', lambda: config)
    calls = []

    def run(args, timeout):
        calls.append(args)
        config['Web']['test.ts.net:8796']['Handlers'][args[-2].split('=', 1)[1]] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(links, '_run', run)
    assert resources.publish('test.ts.net', 8796, 7790, path=resources.DEVICES_PATH)['state'] == 'ready'
    assert calls == [['tailscale', 'serve', '--bg', '--https=8796', '--set-path=/api/system/devices',
                      'http://127.0.0.1:7790/api/system/devices']]
    resources.publish('test.ts.net', 8796, 7790, path=resources.DEVICES_PATH)
    assert len(calls) == 1   # already published: nothing rewritten
    config['Web']['test.ts.net:8796']['Handlers'][resources.DEVICES_PATH] = {'Proxy': 'http://127.0.0.1:9582'}
    with pytest.raises(ValueError, match='left untouched'):   # the owner's own sidecar route is never replaced
        resources.publish('test.ts.net', 8796, 7790, path=resources.DEVICES_PATH)
    assert len(calls) == 1
    with pytest.raises(ValueError, match='not a published'):
        resources.publish('test.ts.net', 8796, 7790, path='/api/system')


def test_links_publish_both_resource_routes(monkeypatch, tmp_path):
    """`wam links` publishes /api/system/devices next to /api/system/load (before #568 nothing published it)."""
    host = 'box.ts.net'
    cfg = {'TCP': {'8796': {'HTTPS': True}}, 'Web': {f'{host}:8796': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8796'}}}}}
    monkeypatch.delenv('WAM_PRIVATE_CATALOG', raising=False)
    monkeypatch.setattr(links, 'tailnet', lambda: {'state': 'Running', 'host': host})
    monkeypatch.setattr(links, 'serve_status', lambda: json.loads(json.dumps(cfg)))

    def run(args, *a):
        port = next(x.split('=', 1)[1] for x in args if x.startswith('--https='))
        path = next((x.split('=', 1)[1] for x in args if x.startswith('--set-path=')), '/')
        cfg['Web'].setdefault(f'{host}:{port}', {'Handlers': {}})['Handlers'][path] = {'Proxy': args[-1]}
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(links, '_run', run)
    reg = {'apps': [{'id': 'wizard-ux', 'name': 'Wizard Shell', 'category': 'interface', 'status': 'running',
                     'provides': ['shell'], 'view': {'url': 'http://127.0.0.1:8796/wizard-os-home/'}}]}
    res = links.publish(reg, tmp_path, {'wizard-ux'})
    h = cfg['Web'][f'{host}:8796']['Handlers']
    assert h['/api/system/load'] == {'Proxy': 'http://127.0.0.1:7790/api/system/load'}
    assert h['/api/system/devices'] == {'Proxy': 'http://127.0.0.1:7790/api/system/devices'}
    assert h['/'] == {'Proxy': 'http://127.0.0.1:8796'}   # the shell's own route is untouched
    assert res['devices']['state'] == 'ready' and 'devices' not in res['errors']
    # An owner's route on that path stays theirs; the rest of the publish still succeeds.
    cfg['Web'][f'{host}:8796']['Handlers']['/api/system/devices'] = {'Proxy': 'http://127.0.0.1:9582'}
    res = links.publish(reg, tmp_path, {'wizard-ux'})
    assert h['/api/system/devices'] == {'Proxy': 'http://127.0.0.1:9582'}
    assert 'left untouched' in res['errors']['devices'] and res['resources']['state'] == 'ready'
