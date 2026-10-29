#!/usr/bin/env python3
"""Task-only real Docker forwarding boundary. Not a Docker implementation.

Only the controller starts this process with the raw socket. The manager sees
only the listener. Authority is exact returned IDs, never a caller-supplied
label. Every unrecognized request fails before opening an upstream connection.
"""
import argparse
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import threading
import time
import urllib.parse

MIB = 2**20
MAX_BODY = MIB
MAX_RESPONSE = 16*MIB
ID = re.compile(r'[0-9a-f]{64}\Z')


class Denied(Exception):
    pass


def require(condition):
    if not condition:
        raise Denied('policy refusal')


def unique_object(pairs):
    obj = {}
    for k, v in pairs:
        require(k not in obj)
        obj[k] = v
    return obj


def decode(data):
    return json.loads(data, object_pairs_hook=unique_object)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=15):
        super().__init__('localhost', timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Policy:
    def __init__(self, config, persist=lambda row: None):
        self.cfg = config
        self.persist = persist
        self.lock = threading.RLock()
        self.containers = dict(config.get('containers', {}))
        self.networks = dict(config.get('networks', {}))
        self.history = set(self.containers) | set(self.networks)
        self.sequence = 0
        self.audit_bytes = 0
        self.event_rows = []
        self.activated = False
        for value in self.history:
            require(ID.fullmatch(value))

    def audit(self, operation, **fields):
        with self.lock:
            self.sequence += 1
            row = dict(sequence=self.sequence, operation=operation, **fields)
            size = len(json.dumps(row)) + 1
            require(self.audit_bytes + size <= 16*MIB)
            self.audit_bytes += size
            self.persist(row)

    def lookup(self, kind, token):
        table = self.containers if kind == 'containers' else self.networks
        if token in table:
            return token
        ids = [i for i, name in table.items() if token == name]
        require(len(ids) == 1)
        return ids[0]

    def path(self, target):
        require(len(target) <= 16384 and not target.startswith(('http:', 'https:', '//')))
        parsed = urllib.parse.urlsplit(target)
        require(not parsed.fragment and not parsed.netloc and not parsed.scheme)
        path = urllib.parse.unquote(parsed.path, errors='strict')
        require(not re.search(r'%|\\|[\x00-\x20\x7f]|(?:^|/)\.\.?(/|$)', path))
        require(not re.search(r'%2[fF]|%5[cC]', parsed.path))
        path = re.sub(r'^/v1\.[0-9]{1,3}(?=/)', '', path)
        require(re.fullmatch(r'/[A-Za-z0-9_./:@-]*', path))
        pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        query = unique_object(pairs)
        return path, query

    def own_binds(self, host):
        require(not host.get('VolumesFrom'))
        allowed = self.cfg.get('binds', {})
        bindings = []
        for bind in host.get('Binds') or []:
            require(isinstance(bind, str))
            parts = bind.split(':')
            require(len(parts) == 3 and parts[2] in ('rw', 'ro'))
            bindings.append(parts)
        for mount in host.get('Mounts') or []:
            require(set(mount) <= {'Type', 'Source', 'Target', 'ReadOnly', 'Consistency', 'BindOptions'})
            require(mount.get('Type') == 'bind' and mount.get('ReadOnly') in (None, True, False))
            require(mount.get('Consistency') in (None, '', 'consistent'))
            options = mount.get('BindOptions') or {}
            require(set(options) <= {'Propagation', 'NonRecursive', 'CreateMountpoint', 'ReadOnlyNonRecursive', 'ReadOnlyForceRecursive'})
            require(options.get('Propagation') in (None, '', 'rprivate', 'private'))
            require(not any(options.get(k) for k in ('NonRecursive', 'ReadOnlyNonRecursive', 'ReadOnlyForceRecursive')))
            require(options.get('CreateMountpoint') in (None, False, True))
            bindings.append([mount.get('Source'), mount.get('Target'), 'ro' if mount.get('ReadOnly') else 'rw'])
        require(len(bindings) <= 2 and len({b[1] for b in bindings}) == len(bindings))
        for source, destination, mode in bindings:
            require(source in allowed and allowed[source] == {'target': destination, 'mode': mode})
            # The guard mounts this task's tmpfs at its SAME daemon path.
            # Parents are trusted/non-writable; bind source leaves are sealed
            # readonly in the manager. Reject a symlink at every component.
            p = Path(source)
            require(str(p) == source and p.is_absolute())
            for part in [p, *p.parents]:
                require(not part.is_symlink())
            require(p.exists())

    def create_container(self, body, query):
        require(not self.activated or len(self.containers) < 5)
        require(set(query) <= {'name'} and query.get('name') in self.cfg['container_names'])
        require(query['name'] not in self.containers.values())
        require(len(self.containers) < 5 and self.activated)
        allowed = {'Hostname', 'Domainname', 'User', 'AttachStdin', 'AttachStdout', 'AttachStderr', 'Tty', 'OpenStdin',
                   'StdinOnce', 'Env', 'Cmd', 'Image', 'Volumes', 'WorkingDir', 'Entrypoint', 'NetworkDisabled',
                   'Labels', 'StopSignal', 'StopTimeout', 'Healthcheck', 'HostConfig', 'NetworkingConfig', 'ExposedPorts'}
        require(set(body) <= allowed)
        require(body.get('Image') in self.cfg['images'])
        require(body.get('User') == self.cfg['user'])
        require(not body.get('Volumes') and not body.get('ExposedPorts') and not body.get('OpenStdin'))
        labels = body.get('Labels') or {}
        require(labels.get('com.docker.compose.project') == self.cfg['project'])
        require(labels.get('com.docker.compose.service') in self.cfg['services'])
        require(labels.get('wizard.qa.manager-seal') == self.cfg['owner'])
        host = body.get('HostConfig', {})
        harmless = {'Binds', 'ContainerIDFile', 'LogConfig', 'NetworkMode', 'PortBindings', 'RestartPolicy',
                    'AutoRemove', 'VolumeDriver', 'VolumesFrom', 'ConsoleSize', 'CapAdd', 'CapDrop', 'CgroupnsMode',
                    'Dns', 'DnsOptions', 'DnsSearch', 'ExtraHosts', 'GroupAdd', 'IpcMode', 'Cgroup', 'Links',
                    'OomScoreAdj', 'PidMode', 'Privileged', 'PublishAllPorts', 'ReadonlyRootfs', 'SecurityOpt',
                    'UTSMode', 'UsernsMode', 'ShmSize', 'Sysctls', 'Runtime', 'Isolation', 'CpuShares', 'Memory',
                    'NanoCpus', 'CgroupParent', 'BlkioWeight', 'BlkioWeightDevice', 'BlkioDeviceReadBps',
                    'BlkioDeviceWriteBps', 'BlkioDeviceReadIOps', 'BlkioDeviceWriteIOps', 'CpuPeriod', 'CpuQuota',
                    'CpuRealtimePeriod', 'CpuRealtimeRuntime', 'CpusetCpus', 'CpusetMems', 'Devices',
                    'DeviceCgroupRules', 'DeviceRequests', 'MemoryReservation', 'MemorySwap', 'MemorySwappiness',
                    'OomKillDisable', 'PidsLimit', 'Ulimits', 'CpuCount', 'CpuPercent', 'IOMaximumIOps',
                    'IOMaximumBandwidth', 'MaskedPaths', 'ReadonlyPaths', 'Init', 'Tmpfs', 'Mounts'}
        require(set(host) <= harmless)
        for key in harmless - {'Binds', 'Mounts', 'LogConfig', 'NetworkMode', 'RestartPolicy', 'ConsoleSize', 'CapDrop',
                              'ReadonlyRootfs', 'SecurityOpt', 'ShmSize', 'Memory', 'MemorySwap', 'PidsLimit',
                              'NanoCpus', 'Tmpfs', 'Init', 'Ulimits'}:
            require(host.get(key) in (None, False, '', 0, [], {}))
        require(host.get('ReadonlyRootfs') is True)
        require(host.get('CapDrop') == ['ALL'])
        require(host.get('SecurityOpt') in (['no-new-privileges:true'], ['no-new-privileges']))
        require(0 < host.get('Memory', 0) <= 128*MIB)
        require(host.get('MemorySwap') == host['Memory'])
        require(0 < host.get('PidsLimit', 0) <= 32)
        require(0 < host.get('NanoCpus', 0) <= 500000000)
        require(host.get('ShmSize', 0) <= 64*MIB)
        log = host.get('LogConfig') or {}
        require(set(log) <= {'Type', 'Config'} and log.get('Type') == 'none' and log.get('Config') in (None, {}))
        require((host.get('RestartPolicy') or {}).get('Name') in ('no', ''))
        require(not (host.get('RestartPolicy') or {}).get('MaximumRetryCount'))
        require(not host.get('Tmpfs') or host['Tmpfs'] == {'/tmp': 'rw,noexec,nosuid,nodev,size=4194304,mode=1777'})
        require(not host.get('Ulimits'))
        self.own_binds(host)
        self.lookup('networks', host.get('NetworkMode'))
        networking = body.get('NetworkingConfig') or {}
        require(set(networking) <= {'EndpointsConfig'})
        for network, endpoint in networking.get('EndpointsConfig', {}).items():
            self.lookup('networks', network)
            require(set(endpoint) <= {'Aliases', 'IPAMConfig', 'Links', 'MacAddress', 'DriverOpts', 'GwPriority'})
            require(not any(endpoint.get(k) for k in ('IPAMConfig', 'Links', 'MacAddress', 'DriverOpts')))

    def authorize(self, method, target, data=b''):
        path, query = self.path(target)
        require(method in ('GET', 'HEAD', 'POST', 'DELETE'))
        require(len(data) <= MAX_BODY)
        body = decode(data) if data else {}
        require(isinstance(body, dict))
        op = None
        if method in ('GET', 'HEAD') and path in ('/_ping', '/version'):
            require(not query and not data); op = 'plain'
        elif method == 'GET' and path == '/info':
            require(not query and not data); op = 'info'
        elif method == 'GET' and path in ('/containers/json', '/networks', '/images/json'):
            require(not data and set(query) <= {'all', 'filters', 'size', 'digests', 'shared-size'})
            filters = decode(query.get('filters', '{}'))
            require(isinstance(filters, dict) and set(filters) <= {'id', 'name', 'label', 'status', 'dangling', 'reference'})
            key = 'containers' if path == '/containers/json' else 'networks' if path == '/networks' else 'images'
            table = self.containers if key == 'containers' else self.networks if key == 'networks' else self.cfg['images']
            # Keep caller filters; add an immutable authority intersection.
            owned = list(table) or ['0'*64]
            filters['id' if key != 'images' else 'reference'] = owned
            query['filters'] = json.dumps(filters, separators=(',', ':'))
            op = 'list-' + key
        elif method == 'GET' and path == '/events':
            require(not data and set(query) <= {'since', 'until', 'filters'})
            if 'until' in query:
                require(re.fullmatch(r'[0-9.]+', query['until']))
            if 'since' in query:
                require(re.fullmatch(r'[0-9.]+', query['since']))
            if 'since' in query:
                require(float(query['since']) >= self.cfg.get('event_floor', 0))
            # Network events do NOT reliably carry network/Compose labels.
            # Type-filter upstream, then exact-ID-filter every real event before
            # forwarding/persisting; never log unknown actor metadata.
            query['filters'] = json.dumps({'type': ['container', 'network']})
            op = 'events'
        elif method == 'POST' and path == '/containers/create':
            self.create_container(body, query); op = 'create-container'
        elif method == 'POST' and path == '/networks/create':
            require(self.activated and not query and len(self.networks) < 6)
            require(set(body) <= {'Name', 'CheckDuplicate', 'Driver', 'Internal', 'Attachable', 'Ingress', 'IPAM', 'EnableIPv6', 'Options', 'Labels', 'EnableIPv4'})
            require(body.get('Name') in self.cfg['network_names'] and body['Name'] not in self.networks.values())
            require(body.get('Driver') in ('', 'bridge') and not body.get('EnableIPv6') and not body.get('Attachable') and not body.get('Ingress'))
            ipam = body.get('IPAM') or {}
            require(set(ipam) <= {'Driver', 'Config', 'Options'} and ipam.get('Driver') in (None, '', 'default')
                    and ipam.get('Config') in (None, []) and ipam.get('Options') in (None, {}))
            require(body.get('Internal') is True or body['Name'] == self.cfg.get('uplink'))
            require((body.get('Options') or {}) in ({}, {'com.docker.network.bridge.inhibit_ipv4': 'true'}))
            require((body.get('Labels') or {}).get('wizard.qa.manager-seal') == self.cfg['owner'])
            op = 'create-network'
        else:
            match = re.fullmatch(r'/(containers|networks)/([^/]+)(?:/(json|start|stop|kill|wait|logs|top|connect|disconnect))?', path)
            if match:
                kind, token, action = match.groups()
                owned = self.lookup(kind, token)
                # Rewrite names to the exact owned full ID: never prefix/name TOCTOU.
                path = '/'+kind+'/'+owned+('/'+action if action else '')
                if method == 'GET' and action in ('json', 'logs', 'top') and kind == 'containers':
                    require(set(query) <= {'stdout', 'stderr', 'timestamps', 'tail', 'follow', 'since', 'until', 'details'})
                    require(query.get('follow', 'false') in ('false', '0'))
                    op = 'owned-read'
                elif method == 'GET' and kind == 'networks' and action is None:
                    require(not query); op = 'owned-network'
                elif method == 'DELETE' and action is None:
                    require(set(query) <= {'force', 'v', 'link'}); op = 'delete-'+kind
                elif method == 'POST' and kind == 'containers' and action in ('start', 'stop', 'kill', 'wait'):
                    require(set(query) <= {'t', 'signal', 'condition'} and not data)
                    require(action != 'start' or self.activated)
                    op = 'owned-mutate'
                elif method == 'POST' and kind == 'networks' and action in ('connect', 'disconnect'):
                    require(not query and set(body) <= {'Container', 'EndpointConfig', 'Force'})
                    body['Container'] = self.lookup('containers', body.get('Container'))
                    endpoint = body.get('EndpointConfig') or {}
                    require(set(endpoint) <= {'Aliases', 'IPAMConfig', 'Links', 'DriverOpts', 'GwPriority'})
                    require(not any(endpoint.get(k) for k in ('IPAMConfig', 'Links', 'DriverOpts')))
                    require(not body.get('Force')); data = json.dumps(body).encode(); op = 'owned-mutate'
            elif method == 'GET' and path.startswith('/images/') and path.endswith('/json'):
                image = path[len('/images/'):-len('/json')]
                require(image in self.cfg['images'] and not query); op = 'owned-image'
        require(op is not None)
        require(op in ('create-container', 'create-network', 'owned-mutate') or not data)
        return op, path + ('?'+urllib.parse.urlencode(query) if query else ''), data, body

    def response(self, op, value, body, query):
        if op.startswith('create-'):
            ident = value.get('Id') or value.get('ID')
            require(isinstance(ident, str) and ID.fullmatch(ident))
            table = self.containers if op == 'create-container' else self.networks
            name = query['name'] if op == 'create-container' else body['Name']
            require(ident not in self.history)
            # Persist ownership before sending a successful create to the caller.
            self.audit('created', kind='container' if op == 'create-container' else 'network', id=ident, name=name)
            table[ident] = name; self.history.add(ident)
        if op.startswith('list-'):
            require(isinstance(value, list))
            table = self.containers if op == 'list-containers' else self.networks if op == 'list-networks' else self.cfg['images']
            return [v for v in value if (v.get('Id') or v.get('ID')) in table]
        if op == 'info':
            result = {key: value[key] for key in ('OSType', 'Architecture') if key in value}
            if 'LocalNodeState' in (value.get('Swarm') or {}):
                result['Swarm'] = {'LocalNodeState': value['Swarm']['LocalNodeState']}
            return result
        if op == 'owned-network':
            value['Containers'] = {k: v for k, v in (value.get('Containers') or {}).items() if k in self.containers}
        return value

    def event(self, row):
        with self.lock:
            ident = (row.get('Actor') or {}).get('ID')
            require(row.get('Type') in ('container', 'network'))
            if ident not in self.history:
                return None
            attrs = (row.get('Actor') or {}).get('Attributes') or {}
            if attrs.get('container') and attrs['container'] not in self.history:
                return None
            self.audit('event', type=row['Type'], action=row.get('Action'), id=ident,
                       container=attrs.get('container'), timeNano=row.get('timeNano'))
            return row


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, listener, raw_socket, policy):
        self.raw_socket = raw_socket; self.policy = policy
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(listener, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close(); return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass

    def setup(self):
        super().setup(); self.connection.settimeout(20)

    def do_HEAD(self): self.handle_one()
    def do_GET(self): self.handle_one()
    def do_POST(self): self.handle_one()
    def do_DELETE(self): self.handle_one()
    def do_PUT(self): self.handle_one()
    def do_PATCH(self): self.handle_one()
    def do_OPTIONS(self): self.handle_one()

    def reply(self, status, payload, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close'); self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)
        self.close_connection = True

    def handle_one(self):
        upstream = None
        try:
            names = [k.lower() for k in self.headers.keys()]
            require(len(names) == len(set(names)))
            require('transfer-encoding' not in names and 'upgrade' not in names and 'expect' not in names)
            require(self.headers.get('Content-Length', '0').isdigit())
            length = int(self.headers.get('Content-Length', '0'))
            require(length <= MAX_BODY)
            data = self.rfile.read(length)
            require(len(data) == length)
            p = self.server.policy
            # Serialize ordinary requests through registration; event decoding
            # takes the same lock, avoiding create-response identity races.
            with p.lock:
                op, target, data, body = p.authorize(self.command, self.path, data)
                _, query = p.path(target)
                if op in ('create-container', 'create-network'):
                    p.audit('creation-intent', kind='container' if op == 'create-container' else 'network',
                            name=query['name'] if op == 'create-container' else body['Name'])
                p.audit('forward', method=self.command, route=target.split('?', 1)[0], kind=op)
                upstream = UnixHTTP(self.server.raw_socket)
                upstream.request(self.command, target, body=data or None,
                                 headers={'Content-Type': 'application/json', 'Connection': 'close'})
                response = upstream.getresponse()
                if op != 'events':
                    payload = response.read(MAX_RESPONSE+1)
                    require(len(payload) <= MAX_RESPONSE)
                    if response.status < 300 and payload and ('json' in response.getheader('Content-Type', '') or op.startswith(('create-', 'list-')) or op in ('info', 'owned-network')):
                        payload = json.dumps(p.response(op, decode(payload), body, query)).encode()
                    if response.status < 300 and op.startswith('delete-'):
                        table = p.containers if op == 'delete-containers' else p.networks
                        table.pop(target.split('/')[2].split('?')[0], None)
                    p.audit('response', kind=op, status=response.status)
            if op == 'events':
                require(response.status == 200)
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Connection', 'close'); self.end_headers()
                end = time.monotonic()+600; total = 0
                while time.monotonic() < end:
                    line = response.readline(65537)
                    if not line: break
                    require(len(line) <= 65536 and line.endswith(b'\n'))
                    total += len(line); require(total <= 16*MIB)
                    row = p.event(decode(line))
                    if row is not None:
                        self.wfile.write(json.dumps(row).encode()+b'\n'); self.wfile.flush()
                self.close_connection = True
            else:
                self.reply(response.status, payload, response.getheader('Content-Type', 'application/json'))
        except (Denied, ValueError, KeyError, TypeError) as exc:
            try:
                tb = exc.__traceback__; line = None
                while tb:
                    if tb.tb_frame.f_code.co_filename == __file__ and tb.tb_frame.f_code.co_name != 'require': line = tb.tb_lineno
                    tb = tb.tb_next
                self.server.policy.audit('deny', rule_line=line, reason='rule_refused')
                self.reply(403, b'{"message":"task authority refused"}')
            except (OSError, Denied):
                self.close_connection = True
        except (OSError, http.client.HTTPException):
            self.close_connection = True
        finally:
            if upstream: upstream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--raw-socket', default='/var/run/docker.sock')
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--activate-file', type=Path, required=True)
    args = parser.parse_args()
    cfg = decode(args.config.read_bytes())
    with args.ledger.open('x', buffering=1) as ledger:
        policy = Policy(cfg, lambda row: ledger.write(json.dumps(row, separators=(',', ':'))+'\n'))
        server = Server(args.socket, args.raw_socket, policy)
        os.chmod(args.socket, 0o666)  # parent directory/mounts are task-private
        def activate():
            while not args.activate_file.exists(): time.sleep(.05)
            with policy.lock:
                require(args.activate_file.read_text() == cfg['activation'])
                policy.audit('activated'); policy.activated = True
        threading.Thread(target=activate, daemon=True).start()
        server.serve_forever(poll_interval=.1)


if __name__ == '__main__':
    main()
