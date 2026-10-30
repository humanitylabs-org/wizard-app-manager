#!/usr/bin/python3 -I
"""Finite synthetic guest assertions; not an init and never calls Transaction."""
import base64
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import sys
import time
import urllib.request
import http.client
import socket

ROOT = Path('/root/storage-production')
SOURCE = Path('/root/storage-proof/source')
REQUEST = Path('/etc/wizard-agent-storage/request.json')
BOOT = '/usr/local/lib/wizard-storage/maintenance-init'
CONTROLS = (Path('/var/lib/wizard-agent-homes'), Path('/var/lib/wizard-agent-storage'))
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/k9sAAAAASUVORK5CYII=')
SESSION = 'storage-production-synthetic'


def save(name, value):
    (ROOT/(name+'.json')).write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')


def run(*args, timeout=180):
    p = subprocess.run([str(v) for v in args], capture_output=True, timeout=timeout)
    assert p.returncode == 0, 'fixed guest command failed: '+str(args[0])
    return p.stdout.decode()


def agent(*args):
    a = pwd.getpwnam('hermes')
    return run('runuser', '-u', 'hermes', '--', 'env', '-i', 'HOME=/home/hermes',
        'HERMES_HOME=/home/hermes/.hermes', 'PATH=/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin',
        'XDG_RUNTIME_DIR=/run/user/'+str(a.pw_uid),
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(a.pw_uid)+'/bus', *args)


def fingerprint(path):
    # Only the fixed synthetic bytes named here; no agent configuration payloads.
    st = path.stat()
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'uid': st.st_uid, 'gid': st.st_gid, 'mode': st.st_mode & 0o7777}


def unit():
    raw = agent('systemctl', '--user', 'show', 'hermes-gateway.service',
                '-p', 'Environment', '-p', 'ExecStart', '-p', 'FragmentPath')
    value = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    # systemd appends runtime PIDs/timestamps to ExecStart's stable command.
    value['ExecStart'] = value['ExecStart'].split('; start_time=',1)[0]
    return value


def setup_request(case):
    assert all(not os.path.lexists(p) for p in CONTROLS)
    home = Path('/home/storageqa/.hermes')
    assert home.is_dir() and not home.is_symlink()
    REQUEST.parent.mkdir(mode=0o700, exist_ok=True)
    if os.path.lexists(REQUEST): REQUEST.unlink()
    assert not (REQUEST.parent/'completed.json').exists()
    request: dict = {'action':'relocate', 'agent':'storageqa', 'logical':str(home)}
    if case == 'cold-wrong-field': request['command'] = 'QA_PRIVATE_CANARY'
    elif case == 'cold-wrong-type': request['logical'] = {'QA_PRIVATE_CANARY': 1}
    elif case == 'cold-invalid-action': request['action'] = 'QA_PRIVATE_CANARY'
    raw = json.dumps(request).encode()
    if case == 'cold-invalid-json': raw = b'{QA_PRIVATE_CANARY'
    if case == 'cold-symlink-request':
        target = ROOT/'symlink-request.json'; target.write_bytes(raw); target.chmod(0o600)
        REQUEST.symlink_to(target)
    else:
        REQUEST.write_bytes(raw); REQUEST.chmod(0o666 if case == 'cold-writable-request' else 0o600)
    save('request-before', {'sha256':hashlib.sha256(raw).hexdigest(),
                           'synthetic':fingerprint(home/'sentinel')})
    if case == 'cold-foreign-writer':
        # Negative ONLY. This trusted QA init execs the EXACT real bootstrap,
        # keeping a late foreign userspace process alive. Direct positive boot
        # uses BOOT, never this wrapper; there is no product bypass switch.
        wrapper = ROOT/'foreign-init'
        wrapper.write_text('#!/usr/bin/python3 -I\nimport os,time,pwd\n'
            'if os.fork()==0:\n a=pwd.getpwnam("storageqa");os.setgid(a.pw_gid);os.setuid(a.pw_uid)\n'
            ' while True:\n  with open("/home/storageqa/.hermes/foreign-writer", "a") as f: f.write("live\\n");f.flush();os.fsync(f.fileno())\n  time.sleep(0.05)\n'
            'os.execv("'+BOOT+'", ["'+BOOT+'"])\n')
        wrapper.chmod(0o755)


def refusal_readback(case):
    assert all(not os.path.lexists(p) for p in CONTROLS)
    assert REQUEST.exists() and not (REQUEST.parent/'completed.json').exists()
    before = json.loads((ROOT/'request-before.json').read_text())
    assert hashlib.sha256(REQUEST.read_bytes()).hexdigest() == before['sha256']
    assert fingerprint(Path('/home/storageqa/.hermes/sentinel')) == before['synthetic']
    # The original stock gateway was deliberately disabled for cold maintenance.
    uid = pwd.getpwnam('hermes').pw_uid
    assert subprocess.run(['pgrep','-u',str(uid)],capture_output=True).returncode == 1


def online():
    before = {str(p):os.path.lexists(p) for p in CONTROLS}
    for argv in ([BOOT], ['/usr/bin/python3','-I',str(SOURCE/'scripts/protected-agent-storage.py'),
                          'relocate','storageqa','/home/storageqa/.hermes']):
        p = subprocess.run(argv, capture_output=True, timeout=20)
        assert p.returncode == 2 and b'QA_PRIVATE_CANARY' not in p.stdout+p.stderr
    assert before == {str(p):os.path.lexists(p) for p in CONTROLS}
    # Real nested PID1 bootstrap; hard timeout kills its entire process group.
    p = subprocess.Popen(['unshare','--pid','--fork','--mount-proc',BOOT],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        try:
            out, err = p.communicate(timeout=6)
            raise AssertionError('nested bootstrap did not remain maintenance')
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL); out, err = p.communicate(timeout=10)
        assert b'"state":"refused"' in out and b'"state":"admitted"' not in out
        assert b'QA_PRIVATE_CANARY' not in out+err
    finally:
        if p.poll() is None: os.killpg(p.pid,signal.SIGKILL); p.wait(timeout=10)
    assert before == {str(p):os.path.lexists(p) for p in CONTROLS}
    save('online', {'ordinary_bootstrap_refused':True, 'ordinary_transaction_refused':True,
                    'real_nested_pid1_refused':True, 'nested_reaped':True})


def seed_agent():
    # Invoked as hermes with the stock activated environment before relocation.
    assert os.geteuid() == pwd.getpwnam('hermes').pw_uid
    sys.path.insert(0, '/home/hermes/hermes-agent')
    from hermes_state import SessionDB
    home = Path(os.environ['HERMES_HOME'])
    (home/'cache/images').mkdir(parents=True, exist_ok=True)
    (home/'cache/images/storage-production.png').write_bytes(PNG)
    (home/'storage-production-sentinel').write_bytes(b'synthetic home bytes before cold migration\n')
    db = SessionDB()
    db.create_session(SESSION, 'api')
    db.append_message(SESSION, 'user', 'storage-production-history-sentinel')
    db.append_message(SESSION, 'assistant', 'MEDIA:/home/hermes/.hermes/cache/images/storage-production.png')
    db.append_message(SESSION, 'assistant', 'MEDIA:/hermes/cache/images/storage-production.png')
    db.close()


def snapshot_agent():
    save('original-agent', {'unit':unit(), 'sentinel':fingerprint(Path('/home/hermes/.hermes/storage-production-sentinel')),
        'image':fingerprint(Path('/home/hermes/.hermes/cache/images/storage-production.png')),
        'home_mode':Path('/home/hermes/.hermes').stat().st_mode & 0o7777})
    a = pwd.getpwnam('hermes')
    # Explicit owner-authorized stop, disable and linger removal for the fixture.
    agent('systemctl','--user','disable','--now','hermes-gateway.service')
    run('loginctl','disable-linger','hermes')
    run('systemctl','stop','user@'+str(a.pw_uid)+'.service')
    # After this helper returns, no gateway may run again until successful readback.


def valid_request():
    assert not os.path.lexists(REQUEST.parent/'completed.json')
    REQUEST.unlink(missing_ok=True)
    REQUEST.write_text(json.dumps({'action':'relocate','agent':'hermes','logical':'/home/hermes/.hermes'}))
    REQUEST.chmod(0o600)


def return_normal():
    original = json.loads((ROOT/'original-agent.json').read_text())
    home = Path('/home/hermes/.hermes')
    a = pwd.getpwnam('hermes')
    assert home.is_symlink() and os.readlink(home) == '/var/lib/wizard-agent-homes/'+str(a.pw_uid)
    assert not REQUEST.exists() and (REQUEST.parent/'completed.json').is_file()
    assert fingerprint(home/'storage-production-sentinel') == original['sentinel']
    assert fingerprint(home/'cache/images/storage-production.png') == original['image']
    assert home.stat().st_mode & 0o7777 == original['home_mode']
    assert (home.stat().st_uid, home.stat().st_gid) == (a.pw_uid, a.pw_gid)
    # Reenable ONLY after protected completion + fixed synthetic readback.
    run('loginctl','enable-linger','hermes')
    run('systemctl','start','user@'+str(a.pw_uid)+'.service')
    assert unit() == original['unit']
    agent('systemctl','--user','enable','--now','hermes-gateway.service')
    agent('systemctl','--user','restart','hermes-gateway.service')
    wait_health('http://127.0.0.1:8642/health')
    save('return-normal', {'same_unit_and_environment':True,'uid':a.pw_uid,'logical_home':str(home),
          'stock_gateway_restart_and_health':True,'synthetic_bytes_and_modes':True,'request_retired':True})


def wait_health(url, service=False):
    end = time.monotonic()+180
    while time.monotonic()<end:
        try:
            with urllib.request.urlopen(url,timeout=5) as response: value=json.load(response)
            if not service or value.get('status') == 'ok' and value.get('upstream_reachable'): return value
        except Exception: pass
        time.sleep(2)
    raise AssertionError('fixed health endpoint did not become useful')


def installed(backend, service_image):
    sys.path.insert(0, '/var/lib/wizard-os/wizard-app-manager')
    from wam import alongside, privsep, network
    state = Path('/var/lib/wizard-os/.wizard-os')
    st = alongside.load(state); a = pwd.getpwnam('hermes')
    origin = 'http://127.0.0.1:'+str(alongside.service_port(st))
    physical = '/var/lib/wizard-agent-homes/'+str(a.pw_uid)
    doc = network.load_yaml(privsep.service_override(st,state))
    generated = doc['services']['wizard-agent-service']
    assert generated['volumes'].tag == '!override'
    mounts = generated['volumes'].value
    assert physical+':/hermes' in mounts
    assert physical+':/home/hermes/.hermes' in mounts
    uid = pwd.getpwnam('wizard-system').pw_uid
    # Mandatory final helper from the same unchanged backend image, exact
    # production invocation/hardening. Old logical source is a negative control.
    fixed = ['docker','run','--rm','--restart=no','--stop-timeout=1','--network=none',
        '--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--user=0:0',
        '--entrypoint=/bin/busybox','--mount','type=bind,source=/,target=/host,readonly', backend,
        'sh','-c','/bin/busybox timeout -s KILL 12 "$@"; rc=$?; exit "$rc"',
        'wizard-bind-metadata-supervisor','/app/libexec/wizard-bind-metadata',str(uid)]
    rows=[]
    for path,ok in [(physical,True),('/home/hermes/.hermes',False)]:
        p=subprocess.run(fixed+[path],capture_output=True,timeout=20)
        assert (p.returncode==0)==ok
        if ok: assert p.stdout == b'safe\n'
        rows.append({'physical_source':ok,'accepted':p.returncode==0})
    manager=json.loads(run('docker','inspect','runtipi'))[0]
    assert manager['Image']==backend
    # Verify actual installed immutable main/helper bytes, no backend source overlay.
    for path,expected in [('/app/main.mjs','b824d438aa844d0f1920c0e29a04f8f82986a81bbf812b53b5b8399b9a62ebbe'),
             ('/app/libexec/wizard-bind-metadata','c9590812ad90f8afb61312fc8299bc357f6224b52e8df4a7b41be044cbeffc32')]:
        actual=run('docker','exec','runtipi','sha256sum',path).split()[0]
        assert actual==expected
        assert not any(path==m['Destination'] or path.startswith(m['Destination'].rstrip('/')+'/') for m in manager['Mounts'])
    ids=run('docker','ps','-q','--filter','label=com.docker.compose.service=wizard-agent-service').split()
    assert len(ids)==1
    cid=ids[0]; info=json.loads(run('docker','inspect',cid))[0]
    assert info['Image']==service_image
    expected=[r for r in json.loads((ROOT/'service-members.json').read_text())
              if r['path'].startswith('server/') and r['kind']=='file']
    assert expected
    for offset in range(0,len(expected),40):
        batch=expected[offset:offset+40]
        got=dict((line.split()[1],line.split()[0]) for line in
            run('docker','exec',cid,'sha256sum',*['/app/'+r['path'] for r in batch]).splitlines())
        assert all(got.get('/app/'+r['path'])==r['sha256'] for r in batch)
    assert info['Config']['User']==str(a.pw_uid)+':'+str(a.pw_gid)
    assert not info['HostConfig']['Privileged'] and info['HostConfig']['CapDrop']==['ALL']
    assert 'no-new-privileges:true' in info['HostConfig']['SecurityOpt']
    assert not any(m['Source'] in ('/', '/var/run/docker.sock') for m in info['Mounts'])
    for destination in ('/hermes','/home/hermes/.hermes'):
        assert any(m['Source']==physical and m['Destination']==destination for m in info['Mounts'])
    assert run('docker','exec',cid,'id','-u').strip()==str(a.pw_uid)
    groups=set(run('id','-Gn','hermes').split())
    assert not groups.intersection({'docker','sudo','wheel','admin'})
    for argv in (['sudo','-n','true'],['docker','info']):
        denied=subprocess.run(['runuser','-u','hermes','--','env','-i',
            'HOME=/home/hermes','PATH=/usr/bin:/bin',*argv],capture_output=True,timeout=20)
        assert denied.returncode!=0
    for parent in (Path('/var'),Path('/var/lib'),Path('/var/lib/wizard-agent-homes')):
        metadata=parent.stat();assert metadata.st_uid==0 and not metadata.st_mode & 0o022
    agent('python3','-c',"import os,errno\ntry: os.rename("+repr(physical)+","+repr(physical+'.agent-rename-test')+")\nexcept OSError as e: assert e.errno in (errno.EACCES,errno.EPERM)\nelse: raise AssertionError('agent renamed protected physical home')")
    assert agent('systemctl','--user','show','hermes-gateway.service','-p','MainPID','--value').strip()!='0'
    wait_health(origin+'/wizard/health',True)
    run('docker','exec',cid,'node','-e',
        "const fs=require('fs');if(process.getuid()!==Number(process.argv[1]))process.exit(1);"
        "const a='/home/hermes/.hermes/storage-production-rw';fs.writeFileSync(a,'agent-uid');"
        "if(fs.readFileSync('/hermes/storage-production-rw','utf8')!=='agent-uid')process.exit(2)",str(a.pw_uid))
    assert agent('cat','/home/hermes/.hermes/storage-production-rw')=='agent-uid'
    original=json.loads((ROOT/'original-agent.json').read_text())['unit']
    current=unit()
    assert current['FragmentPath']==original['FragmentPath'] and current['ExecStart']==original['ExecStart']
    import shlex
    old_env=dict(v.split('=',1) for v in shlex.split(original['Environment']))
    new_env=dict(v.split('=',1) for v in shlex.split(current['Environment']))
    assert all(new_env.get(k)==v for k,v in old_env.items())
    # Actual HTTP through source-bound Service -> unmodified stock gateway ->
    # synthetic history and both old absolute path forms -> actual artifact bytes.
    with urllib.request.urlopen(origin+'/wizard/hermes/api/sessions/'+SESSION+'/messages?agent_id=hermes',timeout=20) as response:
        raw=response.read().decode()
    assert 'storage-production-history-sentinel' in raw
    assert 'unavailable' not in raw.lower()
    ids=re.findall(r'/wizard/attachments/([0-9a-f]{32})',raw)
    assert len(ids)>=2, 'both logical and old container absolute media references must project'
    for ident in set(ids):
        with urllib.request.urlopen(origin+'/wizard/attachments/'+ident+'?agent_id=hermes',timeout=10) as response:
            assert response.read()==PNG
    save('installed', {'compiled_backend_image':backend,'source_bound_service_image':service_image,
        'actual_service_server_files_verified':len(expected),
        'actual_final_helper_cases':rows,'generated_and_actual_physical_binds':True,
        'agent_uid_read_write':True,'real_http_history_and_artifacts_both_absolute_paths':True})


def stale_mapping():
    # Only the finite public root-controlled mapping is changed; restore exactly
    # in finally. APIs/teardown/network writes must not even be constructed.
    sys.path.insert(0,'/var/lib/wizard-os/wizard-app-manager')
    from wam import cli, alongside
    from types import SimpleNamespace
    home=Path('/var/lib/wizard-os/.wizard-os'); a=pwd.getpwnam('hermes')
    mapping=Path('/var/lib/wizard-agent-homes/.mappings')/(str(a.pw_uid)+'.json')
    saved=mapping.read_bytes(); override=Path('/var/lib/wizard-os/runtipi/user-config/wizardapps/wizard-agent-service/docker-compose.yml')
    original=override.read_bytes(); state=(home/'alongside.json').read_bytes()
    def forbidden(): raise AssertionError('API constructed before stale mapping preflight')
    c=SimpleNamespace(home=home,runtipi=forbidden)
    results=[]
    try:
        for status in ('unavailable','stale'):
            doc=json.loads(saved)
            if status=='unavailable': doc['state']='unavailable'
            else: doc['identity'][1]+=1
            mapping.write_text(json.dumps(doc))
            for action in ('install','app_update','app_lifecycle'):
                try:
                    if action=='install': cli.install(c,'wizard-agent-service')
                    elif action=='app_update': cli.app_update(c,'wizard-agent-service')
                    else: cli.app_lifecycle(c,'restart','wizard-agent-service')
                except ValueError as exc: assert str(exc).startswith('maintenance-needed:')
                else: raise AssertionError('stale mapping accepted')
                results.append(status+'-'+action)
            assert override.read_bytes()==original and (home/'alongside.json').read_bytes()==state
    finally: mapping.write_bytes(saved)
    save('stale-mapping',{'refused_before_api_or_writes':results})


def replay_state(compare):
    a=pwd.getpwnam('hermes')
    paths=[REQUEST.parent/'completed.json',Path('/var/lib/wizard-agent-storage')/str(a.pw_uid)/'journal.json']
    current=[fingerprint(p) for p in paths]
    assert not REQUEST.exists()
    if compare: assert json.loads((ROOT/'retired-before.json').read_text())==current
    else: save('retired-before',current)


def daemon_request(method, path, *, socket_path='/var/run/docker.sock'):
    """Guest-only Docker API: structural HTTP status, never CLI wording."""
    connection = http.client.HTTPConnection('localhost', timeout=20)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    try:
        sock.connect(socket_path)
        connection.sock = sock
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
        assert len(raw) <= 4 * 1024 * 1024, 'bounded daemon response exceeded'
        return response.status, None if not raw else json.loads(raw)
    finally:
        connection.close()
        sock.close()


# Derived from this campaign's finite installs (appUrn.replace(':', '_')),
# NOT network names or a prefix/suffix ownership guess. Inventory supplies the
# exact full IDs; every destructive request and readback stays bound to those.
CLEANUP_PROJECTS = ('runtipi', 'wizard-agent-service_wizardapps', 'wizard-ux_wizardapps')


def cleanup_containers():
    # A fresh synthetic guest owns only the finite Wizard/Runtipi projects.
    # Never delete another project or turn daemon/permission failures into absence.
    report = {'attempted_exact_ids': [], 'removed_exact_ids': [], 'absent_exact_ids': [],
              'unowned_exact_ids': [], 'readback_absent': False, 'passed': False}
    first_error = None
    def inventory():
        status, rows = daemon_request('GET', '/containers/json?all=true')
        assert status == 200 and isinstance(rows, list), 'daemon inventory unavailable'
        assert all(isinstance(r, dict) and re.fullmatch('[0-9a-f]{64}', r.get('Id', '')) for r in rows)
        assert len({r['Id'] for r in rows}) == len(rows)
        return rows
    def identity():
        status, info = daemon_request('GET', '/info')
        assert status == 200 and isinstance(info, dict) and isinstance(info.get('ID'), str) and info['ID']
        return info['ID']
    try:
        engine = identity()
        rows = inventory()
        for row in rows:
            labels = row.get('Labels') or {}
            project = labels.get('com.docker.compose.project', '')
            if project in CLEANUP_PROJECTS:
                report['attempted_exact_ids'].append(row['Id'])
            else:
                report['unowned_exact_ids'].append(row['Id'])
        for cid in report['attempted_exact_ids']:
            try:
                status, _ = daemon_request('DELETE', '/containers/' + cid + '?force=true&v=true')
                assert status in (204, 404), 'exact owned container removal failed'
                if status == 204:
                    report['removed_exact_ids'].append(cid)
                status, missing = daemon_request('GET', '/containers/' + cid + '/json')
                assert status == 404 and isinstance(missing, dict) and isinstance(missing.get('message'), str)
                report['absent_exact_ids'].append(cid)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        remaining = inventory()
        assert identity() == engine, 'daemon changed during cleanup'
        report['readback_absent'] = (not remaining and not report['unowned_exact_ids'] and
            report['absent_exact_ids'] == report['attempted_exact_ids'] and first_error is None)
        assert report['readback_absent'], 'guest container inventory or exact absence proof is incomplete'
        if first_error is not None:
            raise first_error
        report['passed'] = True
    except Exception as exc:
        # Preserve bounded progress even after a partial install/removal failure.
        report['first_error_type'] = type(first_error or exc).__name__
        if first_error is not None:
            raise first_error from None
        raise
    finally:
        save('container-cleanup', report)


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='seed-agent': seed_agent()
    else:
        assert os.geteuid()==0 and Path('/run/wizard-storage-production-synthetic').is_file()
        if mode=='request': setup_request(sys.argv[2])
        elif mode=='refusal': refusal_readback(sys.argv[2])
        elif mode=='online': online()
        elif mode=='snapshot-agent': snapshot_agent()
        elif mode=='valid-request': valid_request()
        elif mode=='return-normal': return_normal()
        elif mode=='installed': installed(*sys.argv[2:])
        elif mode=='stale-mapping': stale_mapping()
        elif mode=='retired-before': replay_state(False)
        elif mode=='retired-after': replay_state(True)
        elif mode=='cleanup-containers': cleanup_containers()
        else: raise AssertionError('unknown finite phase')
