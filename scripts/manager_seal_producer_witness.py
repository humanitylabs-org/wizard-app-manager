#!/usr/bin/env python3
"""Pinned Compose producer -> real guard -> scripted counting Unix peer.

Readiness/protocol witness ONLY, never installed/Docker proof. The peer has no
Docker client, host daemon path, subprocess escape or fallback. Export only
synthetic fixture requests. Production forwarding/cleanup authority unchanged.
"""
import argparse
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


class PeerHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def run_request(self):
        peer = self.server.peer
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 <= size <= guard.MAX_BODY: raise AssertionError('protocol body cap')
        data = self.rfile.read(size)
        with peer.lock:
            assert len(peer.calls) < 192, 'protocol request cap'
            path, query = peer.path(self.path)
            body = guard.decode(data) if data else {}
            peer.calls.append({'method': self.command, 'target': self.path,
                               'body_utf8': data.decode(), 'body_sha256': hashlib.sha256(data).hexdigest()})
            status, response = peer.respond(self.command, path, query, body)
        payload = response if isinstance(response, bytes) else json.dumps(response).encode()
        self.send_response(status); self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload))); self.end_headers()
        if self.command != 'HEAD': self.wfile.write(payload)

    do_GET = do_HEAD = do_POST = do_DELETE = run_request


class Peer:
    """Closed synthetic Engine protocol; unsupported operations always fail."""
    def __init__(self, root):
        self.calls = []; self.containers = {}; self.networks = {}; self.lock = threading.Lock()
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
        peer = Peer(root); rows = []
        policy = guard.Policy(cfg, rows.append); policy.activated = True
        server = guard.Server(str(root/'guard.sock'), str(root/'peer.sock'), policy)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            command = [str(compose), '--ansi', 'never', '--progress', 'quiet', '--parallel', '1', '-p', project, '-f', str(file),
                       'create', '--no-build', '--pull', 'never']
            result = subprocess.run(command, env={'PATH': '/usr/bin:/bin', 'HOME': str(root/'home'), 'TMPDIR': str(root),
                'DOCKER_HOST': 'unix://'+str(root/'guard.sock'), 'DOCKER_API_VERSION': '1.51', 'COMPOSE_DISABLE_ENV_FILE': 'true'},
                cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            assert len(result.stdout) <= 64*1024, 'protocol output cap'
            # Preserve the real synthetic trace even on a producer/guard refusal;
            # a failure can never mint the positive protocol witness.
            output.write_text(json.dumps({'protocol_pass': False, 'installed_proof': False,
                'compose_sha256': rig.COMPOSE_SHA, 'producer_exit_code': result.returncode,
                'upstream_trace': peer.calls, 'guard_ledger': rows}, indent=2)+'\n')
            # Save the genuine producer output only inside this synthetic lane.
            assert result.returncode == 0, 'Compose protocol create failed: '+result.stdout.decode(errors='replace')
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
            document = {'protocol_pass': True, 'installed_proof': False, 'compose_sha256': rig.COMPOSE_SHA,
                'summary': {'producer': 'Compose v2.40.0 / SDK v28.5.0', 'container_requests': 5, 'network_requests': 4,
                            'upstream_requests': len(peer.calls), **accounting},
                'fixture': recipe, 'upstream_trace': peer.calls, 'guard_ledger': rows,
                'producer_exit_code': result.returncode, 'producer_output_sha256': hashlib.sha256(result.stdout).hexdigest()}
            output.write_text(json.dumps(document, indent=2)+'\n')
            assert output.stat().st_size < rig.MIB
            return document['summary']
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2); peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', type=Path, required=True); parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); print(json.dumps(witness(args.compose.resolve(), args.output.resolve()), sort_keys=True))


if __name__ == '__main__': main()
