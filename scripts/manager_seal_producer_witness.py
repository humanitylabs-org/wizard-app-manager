#!/usr/bin/env python3
"""Pinned Compose producer -> real guard -> scripted counting Unix peer.

Readiness/protocol witness ONLY, never installed/Docker proof. The peer has no
Docker client, host daemon path, subprocess escape or fallback. Export only
synthetic fixture requests. Production forwarding/cleanup authority unchanged.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import socketserver
import tempfile
import threading

import manager_seal_forward_guard as guard
import manager_seal_installed_path as rig

MAX_REQUESTS = 192
MAX_TRACE = 4*guard.MIB
MAX_OUTPUT = 64*1024  # each producer stream; overflow is failure, never pass


class SyntheticPolicy(guard.Policy):
    """Instrumentation ONLY here: never installed on the real Docker guard."""
    def __init__(self, config, persist, incoming, trace_file):
        super().__init__(config, persist)
        self.incoming = incoming; self.incoming_bytes = 0
        self.trace_file = trace_file
        self.context = threading.local(); self.trace_error = None

    def authorize(self, method, target, data=b''):
        # Called under the unchanged handler's lock, BEFORE any policy decision.
        # All inputs belong to the empty-HOME, synthetic-only pinned producer.
        row = dict(sequence=self.sequence+1, method=method, target=target,
                   http_version=getattr(self.context, 'http_version', None),
                   headers=getattr(self.context, 'headers', []),
                   body_base64=base64.b64encode(data).decode(),
                   body_bytes=len(data), body_sha256=hashlib.sha256(data).hexdigest())
        try: row['body_utf8'] = data.decode('utf-8')
        except UnicodeDecodeError: pass  # base64 always retains exact bytes
        encoded = json.dumps(row, separators=(',', ':'))+'\n'
        if len(self.incoming) >= MAX_REQUESTS or self.incoming_bytes+len(encoded.encode()) > MAX_TRACE:
            self.trace_error = 'incoming_trace_cap'
            raise guard.Denied('synthetic trace cap')
        self.incoming.append(row); self.incoming_bytes += len(encoded.encode())
        try:
            self.trace_file.write(encoded); self.trace_file.flush(); os.fsync(self.trace_file.fileno())
        except OSError:
            self.trace_error = 'incoming_trace_write'
            raise guard.Denied('synthetic trace write failure')
        try:
            result = super().authorize(method, target, data)
            row['authorization'] = result[0]
            return result
        except (guard.Denied, ValueError, KeyError, TypeError) as exc:
            row['authorization'] = 'refused'
            frames = []; tb = exc.__traceback__
            while tb:
                frames.append({'file': Path(tb.tb_frame.f_code.co_filename).name,
                               'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
                tb = tb.tb_next
            row['refusal_frames'] = frames[-8:]
            raise


class SyntheticHandler(guard.Handler):
    def handle_one(self):
        self.server.policy.context.headers = list(self.headers.raw_items())
        self.server.policy.context.http_version = self.request_version
        try: super().handle_one()
        finally: self.server.policy.context.headers = []


def run_bounded(command, env, cwd, timeout=60):
    """Drain both pipes with hard retained bounds; kill/reap on cap/timeout."""
    process = subprocess.Popen(command, env=env, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    streams = {}; overflow = threading.Event()
    def drain(name, pipe):
        data = bytearray(); observed = 0
        while chunk := pipe.read(4096):
            observed += len(chunk); data.extend(chunk[:max(0, MAX_OUTPUT-len(data))])
            if observed > MAX_OUTPUT:
                overflow.set(); process.kill()
        pipe.close(); streams[name] = dict(data=bytes(data), observed_bytes=observed)
    threads = [threading.Thread(target=drain, args=(name, pipe), daemon=True)
               for name, pipe in [('stdout', process.stdout), ('stderr', process.stderr)]]
    for thread in threads: thread.start()
    timed_out = False
    try: process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True; process.kill(); process.wait(timeout=5)
    for thread in threads: thread.join(timeout=5)
    assert not any(thread.is_alive() for thread in threads), 'producer pipe drain incomplete'
    return dict(exit_code=process.returncode, timed_out=timed_out, output_overflow=overflow.is_set(), streams=streams)


class PeerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def run_request(self):
        peer = self.server.peer
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= guard.MAX_BODY: raise AssertionError('protocol body cap')
        data = self.rfile.read(size)
        with peer.lock:
            if len(peer.calls) >= MAX_REQUESTS:
                peer.trace_error = 'upstream_request_cap'; raise AssertionError('protocol request cap')
            path, query = peer.path(self.path)
            body = guard.decode(data) if data else {}
            row = {'method': self.command, 'target': self.path,
                   'body_utf8': data.decode(), 'body_sha256': hashlib.sha256(data).hexdigest()}
            size = len(json.dumps(row).encode())
            if peer.trace_bytes+size > MAX_TRACE:
                peer.trace_error = 'upstream_trace_cap'; raise AssertionError('protocol trace cap')
            peer.trace_bytes += size; peer.calls.append(row)
            try: status, response = peer.respond(self.command, path, query, body)
            except (AssertionError, KeyError, TypeError, ValueError) as exc:
                row['peer_error'] = type(exc).__name__
                status, response = 500, {'message': 'unsupported synthetic peer protocol; inspect upstream_trace'}
        payload = response if isinstance(response, bytes) else json.dumps(response).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload))); self.end_headers()
        if self.command != 'HEAD': self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = do_DELETE = run_request


class Peer:
    """Closed synthetic Engine protocol; unsupported operations always fail."""
    def __init__(self, root):
        self.calls = []; self.containers = {}; self.networks = {}; self.lock = threading.Lock(); self.trace_bytes = 0
        self.trace_error = None
        self.path = guard.Policy({}).path
        self.server = socketserver.ThreadingUnixStreamServer(str(root/'peer.sock'), PeerHandler); self.server.peer = self
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    def respond(self, method, path, query, body):
        if method in ('HEAD', 'GET') and path == '/_ping': return 200, b'OK'
        if method == 'GET' and path == '/version': return 200, {'ApiVersion': '1.51', 'MinAPIVersion': '1.24', 'Version': '28.5.0', 'Os': 'linux', 'Arch': 'amd64'}
        if method == 'GET' and path == '/info': return 200, {'OSType': 'linux', 'Architecture': 'x86_64', 'Swarm': {'LocalNodeState': 'inactive'}}
        if method == 'GET' and path.startswith('/images/') and path.endswith('/json'):
            return 200, {'Id': rig.PYTHON, 'RepoTags': [rig.PYTHON], 'Os': 'linux', 'Architecture': 'amd64',
                         'Config': {'Volumes': {}, 'Labels': {}, 'Env': [], 'Cmd': ['sh']}, 'RootFS': {'Type': 'layers', 'Layers': []}}
        if method == 'GET' and path == '/containers/json':
            # create --no-start begins with no observed containers. No lifecycle
            # state is synthesized for acceptance; these are protocol replies.
            return 200, []
        if method == 'GET' and path == '/networks': return 200, list(self.networks.values())
        if method == 'POST' and path == '/networks/create':
            ident = hashlib.sha256(('network:'+body['Name']).encode()).hexdigest()
            self.networks[ident] = {'Id': ident, **body, 'Containers': {}, 'Scope': 'local'}
            return 201, {'Id': ident, 'Warning': ''}
        if method == 'POST' and path == '/containers/create':
            ident = hashlib.sha256(('container:'+query['name']).encode()).hexdigest()
            self.containers[ident] = {'Id': ident, 'Name': '/'+query['name'], 'Config': {k: v for k, v in body.items() if k not in ('HostConfig', 'NetworkingConfig')},
                'HostConfig': body['HostConfig'], 'NetworkSettings': {'Networks': body.get('NetworkingConfig', {}).get('EndpointsConfig', {})},
                'State': {'Status': 'created', 'Running': False}, 'Mounts': []}
            return 201, {'Id': ident, 'Warnings': []}
        match = re.fullmatch('/networks/([a-f0-9]{64})', path)
        if method == 'GET' and match and match[1] in self.networks: return 200, self.networks[match[1]]
        match = re.fullmatch('/containers/([a-f0-9]{64})/json', path)
        if method == 'GET' and match and match[1] in self.containers: return 200, self.containers[match[1]]
        raise AssertionError('unsupported scripted protocol request')

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


def witness(compose, output):
    assert rig.sha(compose) == rig.COMPOSE_SHA, 'pinned Compose digest mismatch'
    # Short owned socket paths; never mount or contact the actual engine.
    with tempfile.TemporaryDirectory(prefix='msp-', dir=os.environ.get('TMPDIR')) as tmp:
        root = Path(tmp)
        assert len(os.fsencode(root/'guard.sock')) < 104
        (root/'home').mkdir(); (root/'data').mkdir()
        (root/'data/key').write_text('protocol-fixture-only')
        (root/'data/home').mkdir()
        project = rig.PROJECT; owner = 'msp-owner'
        recipe, _ = rig.fixture(str(root/'data'), owner, 1000, 1000)
        file = root/'compose.json'; file.write_text(json.dumps(recipe))
        names = [s['container_name'] for s in recipe['services'].values()]
        networks = [project+'_'+n for n in recipe['networks']]
        cfg = dict(owner=owner, project=project, containers={}, networks={},
                   container_names=names, network_names=networks, images=[rig.PYTHON], services=rig.SERVICES,
                   user='1000:1000', uplink=project+'_'+rig.MAIN+'-edge-uplink',
                   binds={str(root/'data/key'): {'target': '/run/provider-key/key', 'mode': 'ro'},
                          str(root/'data/home'): {'target': '/home/synthetic', 'mode': 'rw'}})
        peer = Peer(root); rows = []; incoming = []
        trace_path = output.with_name('producer-incoming.jsonl')
        trace_file = trace_path.open('x', encoding='utf-8'); trace_path.chmod(0o600)
        policy = SyntheticPolicy(cfg, rows.append, incoming, trace_file); policy.activated = True
        server = guard.Server(str(root/'guard.sock'), str(root/'peer.sock'), policy)
        server.RequestHandlerClass = SyntheticHandler
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        document = {'protocol_pass': False, 'installed_proof': False,
                    'compose_sha256': rig.COMPOSE_SHA, 'fixture': recipe,
                    'fixture_sha256': rig.sha(root/'compose.json'),
                    'incoming_trace': incoming, 'upstream_trace': peer.calls, 'guard_ledger': rows}
        try:
            command = [str(compose), '--ansi', 'never', '--progress', 'quiet', '--parallel', '1', '-p', project, '-f', str(file),
                       'create', '--no-build', '--pull', 'never']
            result = run_bounded(command, env={'PATH': '/usr/bin:/bin', 'HOME': str(root/'home'), 'TMPDIR': str(root),
                'DOCKER_HOST': 'unix://'+str(root/'guard.sock'), 'DOCKER_API_VERSION': '1.51', 'COMPOSE_DISABLE_ENV_FILE': 'true'},
                cwd=root)
            streams = {}
            for name, stream in result.pop('streams').items():
                data = stream['data']; path = output.with_name('producer-'+name+'.bin')
                path.write_bytes(data); path.chmod(0o600)
                streams[name] = {'file': path.name, 'retained_bytes': len(data),
                                 'observed_bytes': stream['observed_bytes'],
                                 'sha256': hashlib.sha256(data).hexdigest(),
                                 'utf8': data.decode(errors='replace')}
            document.update(producer_exit_code=result['exit_code'], producer=result, producer_streams=streams)
            assert not result['timed_out'], 'Compose protocol timeout; inspect synthetic evidence'
            assert not result['output_overflow'], 'Compose protocol output cap; inspect synthetic evidence'
            assert not policy.trace_error, 'Compose incoming trace cap; inspect synthetic evidence'
            assert not peer.trace_error and all('peer_error' not in row for row in peer.calls), 'unsupported peer protocol; inspect upstream_trace'
            assert result['exit_code'] == 0, 'Compose protocol create failed; inspect incoming_trace and producer-stderr.bin'
            assert len(peer.containers) == 5 and len(peer.networks) == 4
            accounting = rig.reconcile_guard_accounting(rows, 0, networks)
            assert accounting['network_discovery_misses'] == 4
            assert len(peer.calls) == sum(r['operation'] == 'forward' for r in rows)
            requests = [c for c in peer.calls if policy.path(c['target'])[0] == '/containers/create']
            assert len(requests) == 5
            operational = {'NetworkID', 'EndpointID', 'Gateway', 'IPAddress', 'IPPrefixLen', 'IPv6Gateway', 'GlobalIPv6Address', 'GlobalIPv6PrefixLen', 'DNSNames'}
            for request in requests:
                body = guard.decode(request['body_utf8'])
                for endpoint in body['NetworkingConfig']['EndpointsConfig'].values():
                    assert operational <= set(endpoint), 'missing actual SDK zero fields'
                    guard.endpoint_shape(endpoint)
            assert len(incoming) == len(peer.calls)+accounting['network_discovery_misses']
            assert all(row['authorization'] != 'refused' for row in incoming)
            document.update(protocol_pass=True, summary={'producer': 'Compose v2.40.0 / SDK v28.5.0',
                'container_requests': 5, 'network_requests': 4, 'incoming_requests': len(incoming),
                'upstream_requests': len(peer.calls), **accounting})
            return document['summary']
        except Exception as exc:
            document['protocol_pass'] = False
            document['failure_class'] = type(exc).__name__
            frames = []; tb = exc.__traceback__
            while tb:
                frames.append({'file': Path(tb.tb_frame.f_code.co_filename).name,
                               'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
                tb = tb.tb_next
            document['failure_frames'] = frames[-8:]
            raise
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2); peer.close()
            trace_file.close()
            document['incoming_trace_file'] = {'file': trace_path.name, 'bytes': trace_path.stat().st_size,
                'sha256': rig.sha(trace_path), 'complete': policy.trace_error is None}
            document['trace_error'] = policy.trace_error
            document['upstream_trace_error'] = peer.trace_error
            payload = json.dumps(document, indent=2)+'\n'
            # Incoming JSON is <=4 MiB; pretty JSON and the separate bounded
            # forwarded trace have a finite joint envelope too.
            assert len(payload.encode()) <= 24*guard.MIB, 'synthetic witness document cap'
            output.write_text(payload); output.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); print(json.dumps(witness(args.compose.resolve(), args.output.resolve()), sort_keys=True))


if __name__ == '__main__': main()
