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


# Wire schemas are closed at EVERY Go-struct boundary. Map keys (labels,
# network names, tmpfs paths) are deliberately not case-folded. Python bool is
# an int subclass; exact types are mandatory before semantic comparisons.
def shape(value, fields):
    require(type(value) is dict and set(value) <= set(fields))
    for key, item in value.items():
        spec = fields[key]
        if isinstance(spec, dict):
            if item is not None: shape(item, spec)
        else:
            require(item is None or type(item) is spec)


def fields(kind, names):
    return dict.fromkeys(names.split(), kind)


RESTART = {'Name': str, 'MaximumRetryCount': int}
LOG = {'Type': str, 'Config': dict}
BIND = dict(Propagation=str, **fields(bool, 'NonRecursive CreateMountpoint ReadOnlyNonRecursive ReadOnlyForceRecursive'))
MOUNT = dict(Type=str, Source=str, Target=str, ReadOnly=bool, Consistency=str, BindOptions=BIND)
HEALTH = dict(Test=list, **fields(int, 'Interval Timeout StartPeriod StartInterval Retries'))
ENDPOINT = dict(**fields(str, 'MacAddress NetworkID EndpointID Gateway IPAddress IPv6Gateway GlobalIPv6Address'),
                **fields(int, 'GwPriority IPPrefixLen GlobalIPv6PrefixLen'),
                IPAMConfig=dict, Links=list, Aliases=list, DriverOpts=dict, DNSNames=list)
IPAM = dict(Driver=str, Config=list, Options=dict)
NETWORK = dict(**fields(str, 'Name Driver Scope'),
               **fields(bool, 'CheckDuplicate Internal Attachable Ingress EnableIPv6 EnableIPv4 ConfigOnly'),
               IPAM=IPAM, ConfigFrom=dict, Options=dict, Labels=dict)
HOST = dict(**fields(str, 'ContainerIDFile NetworkMode VolumeDriver CgroupnsMode IpcMode Cgroup PidMode UTSMode UsernsMode Runtime Isolation CgroupParent CpusetCpus CpusetMems'),
            **fields(bool, 'AutoRemove Privileged PublishAllPorts ReadonlyRootfs OomKillDisable Init'),
            **fields(int, 'OomScoreAdj ShmSize CpuShares Memory NanoCpus BlkioWeight CpuPeriod CpuQuota CpuRealtimePeriod CpuRealtimeRuntime MemoryReservation MemorySwap MemorySwappiness PidsLimit CpuCount CpuPercent IOMaximumIOps IOMaximumBandwidth'),
            **fields(list, 'Binds VolumesFrom ConsoleSize CapAdd CapDrop Dns DnsOptions DnsSearch ExtraHosts GroupAdd Links SecurityOpt BlkioWeightDevice BlkioDeviceReadBps BlkioDeviceWriteBps BlkioDeviceReadIOps BlkioDeviceWriteIOps Devices DeviceCgroupRules DeviceRequests Ulimits MaskedPaths ReadonlyPaths Mounts'),
            **fields(dict, 'PortBindings Sysctls Tmpfs'), LogConfig=LOG, RestartPolicy=RESTART)
CONTAINER = dict(**fields(str, 'Hostname Domainname User Image WorkingDir StopSignal'),
                 **fields(bool, 'AttachStdin AttachStdout AttachStderr Tty OpenStdin StdinOnce NetworkDisabled'),
                 **fields(list, 'Env Cmd Entrypoint OnBuild'), **fields(dict, 'Volumes Labels ExposedPorts'),
                 StopTimeout=int, Healthcheck=HEALTH, HostConfig=HOST,
                 NetworkingConfig={'EndpointsConfig': dict})


def strings(value):
    require(value is None or (type(value) is list and all(type(v) is str for v in value)))


def string_map(value):
    require(value is None or (type(value) is dict and all(type(v) is str for v in value.values())))


def endpoint_shape(endpoint):
    shape(endpoint, ENDPOINT)
    strings(endpoint.get('Aliases'))
    # Only producer zero values; no supplied identity/address/routing state.
    for key, kind in ENDPOINT.items():
        if key == 'Aliases': continue
        value = endpoint.get(key)
        zero = {str: '', int: 0, list: [], dict: {}}[kind]
        require(value is None or (type(value) is kind and value == zero))


def canonical_filters(raw, allowed):
    """Closed Args schema; emit ONLY the SDK's map-of-true-sets encoding.

    Moby FromJSON tries the whole map-of-bools, then the whole legacy map-of-
    arrays. Mixing these formats fails BOTH decoders. Reject ambiguous input
    (including false set entries), rather than silently changing its meaning.
    """
    value = decode(raw)
    require(type(value) is dict and set(value) <= allowed and len(value) <= 8)
    kinds = {type(v) for v in value.values()}
    require(not kinds or kinds in ({dict}, {list}))
    result = {}
    for key, values in value.items():
        require(type(values) in (dict, list) and 0 < len(values) <= 64)
        if type(values) is dict:
            require(all(type(v) is bool and v is True for v in values.values()))
        require(all(type(v) is str and 0 < len(v) <= 1024
                    and not re.search(r'[\x00-\x1f\x7f]', v) for v in values))
        require(len(set(values)) == len(values))
        result[key] = dict.fromkeys(sorted(values), True)
    return result


def intersect_filters(filters, authority, key):
    """Full owned IDs only upstream; caller ID prefixes can ONLY narrow them."""
    owned = sorted(authority)
    if key in filters:
        requested = filters[key]
        if key == 'id':
            require(all(re.fullmatch(r'[0-9a-f]{1,64}', v) for v in requested))
            owned = [v for v in owned if any(v.startswith(prefix) for prefix in requested)]
        else:
            # This task admits immutable image references only, no tag/glob
            # expansion or caller reference replacing the ownership constraint.
            require(all(v.startswith('sha256:') and ID.fullmatch(v[7:]) for v in requested))
            owned = [v for v in owned if v in requested]
    filters[key] = dict.fromkeys(owned or ['0'*64 if key == 'id' else 'sha256:'+'0'*64], True)
    return filters


def network_error_class(payload):
    if len(payload) > 8192:
        return 'engine_error_withheld'
    try:
        if decode(payload) == {'message': 'invalid filter'}:
            return 'invalid_filter'
    except (Denied, ValueError, TypeError):
        pass
    return 'engine_error_withheld'


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
            shape(mount, MOUNT)
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
        shape(body, CONTAINER)
        string_map(body.get('Labels'))
        for key in ('Env', 'Cmd', 'Entrypoint'):
            strings(body.get(key))
        # SDK v28.5.0 Config.OnBuild has no omitempty; Compose leaves it nil.
        # Never delegate image-build instructions, even a well-typed string list.
        require(body.get('OnBuild') is None or body['OnBuild'] == [])
        if body.get('Healthcheck') is not None:
            strings(body['Healthcheck'].get('Test'))
            for key in HEALTH.keys() - {'Test'}:
                value = body['Healthcheck'].get(key)
                require(value is None or 0 <= value <= 2**63-1)
        require(not self.activated or len(self.containers) < 5)
        require(set(query) <= {'name'} and query.get('name') in self.cfg['container_names'])
        require(query['name'] not in self.containers.values())
        require(len(self.containers) < 5 and self.activated)
        allowed = {'Hostname', 'Domainname', 'User', 'AttachStdin', 'AttachStdout', 'AttachStderr', 'Tty', 'OpenStdin',
                   'StdinOnce', 'Env', 'Cmd', 'Image', 'Volumes', 'WorkingDir', 'Entrypoint', 'NetworkDisabled',
                   'Labels', 'StopSignal', 'StopTimeout', 'Healthcheck', 'HostConfig', 'NetworkingConfig', 'ExposedPorts', 'OnBuild'}
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
        require(0 <= host.get('ShmSize', 0) <= 64*MIB)
        console = host.get('ConsoleSize')
        require(console is None or (type(console) is list and len(console) == 2 and all(type(v) is int and v == 0 for v in console)))
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
        require(type(networking.get('EndpointsConfig', {})) is dict)
        for network, endpoint in networking.get('EndpointsConfig', {}).items():
            self.lookup('networks', network)
            endpoint_shape(endpoint)

    def authorize(self, method, target, data=b''):
        path, query = self.path(target)
        require(method in ('GET', 'HEAD', 'POST', 'DELETE'))
        require(len(data) <= MAX_BODY)
        if method == 'POST' and path == '/containers/prune':
            require(len(data) <= 4096)  # closed SDK selector, not a general body
            body = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object,
                              parse_constant=lambda _: require(False)) if data else {}
        else:
            body = decode(data) if data else {}
        require(isinstance(body, dict))
        op = None
        if method in ('GET', 'HEAD') and path in ('/_ping', '/version'):
            require(not query and not data); op = 'plain'
        elif method == 'GET' and path == '/info':
            require(not query and not data); op = 'info'
        elif method == 'GET' and path in ('/containers/json', '/networks', '/images/json'):
            allowed_query = {'/networks': {'filters'}, '/containers/json': {'all', 'filters', 'size'},
                             '/images/json': {'all', 'filters', 'digests', 'shared-size'}}[path]
            require(not data and set(query) <= allowed_query)
            require(all(v in ('0', '1', 'false', 'true') for k, v in query.items() if k != 'filters'))
            key = 'containers' if path == '/containers/json' else 'networks' if path == '/networks' else 'images'
            allowed = {'containers': {'id', 'name', 'label', 'status'},
                       'networks': {'id', 'name', 'label'},
                       'images': {'reference', 'label', 'dangling'}}[key]
            filters = canonical_filters(query.get('filters', '{}'), allowed)
            table = self.containers if key == 'containers' else self.networks if key == 'networks' else self.cfg['images']
            filters = intersect_filters(filters, table, 'id' if key != 'images' else 'reference')
            query['filters'] = json.dumps(filters, sort_keys=True, separators=(',', ':'))
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
        elif method == 'POST' and path == '/containers/prune':
            # Daemon-wide mutation: NEVER forward, even with the exact app label.
            # Only that closed synthetic selector may enter the typed ledger.
            # docker-modem duplicates opts in the query AND a JSON POST body.
            # Accept only equivalent, closed selectors; this is still a refusal.
            require(set(query) == {'filters'})
            urn = self.cfg.get('app_urn')
            require(type(urn) is str and bool(urn))
            filters = canonical_filters(query['filters'], {'label'})
            require(filters == {'label': {'runtipi.appurn='+urn: True}})
            if data:
                require(set(body) == {'filters'})
                require(canonical_filters(json.dumps(body['filters'], allow_nan=False), {'label'}) == filters)
            return 'prune-refused', path, b'', filters
        elif method == 'POST' and path == '/containers/create':
            self.create_container(body, query); op = 'create-container'
        elif method == 'POST' and path == '/networks/create':
            shape(body, NETWORK)
            string_map(body.get('Labels'))
            string_map(body.get('Options'))
            require(self.activated and not query and len(self.networks) < 6)
            require(set(body) <= {'Name', 'CheckDuplicate', 'Driver', 'Internal', 'Attachable', 'Ingress', 'IPAM', 'EnableIPv6', 'Options', 'Labels', 'EnableIPv4', 'Scope', 'ConfigOnly', 'ConfigFrom'})
            # These unconditionally serialized SDK fields are not authority:
            # admit ONLY the exact unused producer values, not local/swarm
            # scope, config-only networks or any source network reference.
            require('Scope' not in body or body['Scope'] == '')
            require('ConfigOnly' not in body or body['ConfigOnly'] is False)
            require(body.get('ConfigFrom') is None)
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
                if (method == 'GET' and kind == 'networks' and action is None
                        and token in self.cfg['network_names'] and token not in self.networks.values()):
                    # Deliberate local absence, NEVER a daemon lookup. This is
                    # typed separately from refusal, with exact ledger accounting.
                    require(self.activated and not query and not data and not ID.fullmatch(token))
                    return 'discovery-miss', path, data, body
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
                    shape(body, {'Container': str, 'EndpointConfig': ENDPOINT, 'Force': bool})
                    require(not query and set(body) <= {'Container', 'EndpointConfig', 'Force'})
                    body['Container'] = self.lookup('containers', body.get('Container'))
                    endpoint = body.get('EndpointConfig') or {}
                    endpoint_shape(endpoint)
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
                if op == 'prune-refused':
                    p.audit('deny', reason='prune_refused', route='/containers/prune', filters=body)
                    self.reply(403, b'{"message":"task authority refused"}')
                    return
                if op == 'discovery-miss':
                    p.audit('discovery-miss', kind='network', name=target.split('/')[-1], status=404)
                    self.reply(404, b'{"message":"task network not created"}')
                    return
                if op in ('create-container', 'create-network'):
                    p.audit('creation-intent', kind='container' if op == 'create-container' else 'network',
                            name=query['name'] if op == 'create-container' else body['Name'])
                # Only closed, synthetic network discovery selectors; no
                # generic queries, bodies, headers or daemon error strings.
                if op == 'list-networks':
                    f = decode(query['filters'])
                    names = f.get('name', {})
                    ids = f.get('id', {})
                    if (set(f) <= {'id', 'name'} and set(names) <= set(p.cfg.get('network_names', []))
                            and set(ids) <= set(p.networks) | {'0'*64}):
                        p.audit('synthetic-network-query', filters=f)
                p.audit('forward', method=self.command, route=target.split('?', 1)[0], kind=op)
                upstream = UnixHTTP(self.server.raw_socket, timeout=600 if op == 'events' else 15)
                upstream.request(self.command, target, body=data or None,
                                 headers={'Content-Type': 'application/json', 'Connection': 'close'})
                upstream_socket = upstream.sock
                response = upstream.getresponse()
                if op != 'events':
                    payload = response.read(MAX_RESPONSE+1)
                    require(len(payload) <= MAX_RESPONSE)
                    if response.status < 300 and payload and ('json' in response.getheader('Content-Type', '') or op.startswith(('create-', 'list-')) or op in ('info', 'owned-network')):
                        value = decode(payload)
                        if op == 'list-networks' and not p.networks:
                            # The canary must prove the ENGINE returned [],
                            # not merely the guard's post-filtered empty list.
                            p.audit('zero-authority-network-result', upstream_empty=value == [])
                        payload = json.dumps(p.response(op, value, body, query)).encode()
                    if response.status < 300 and op.startswith('delete-'):
                        table = p.containers if op == 'delete-containers' else p.networks
                        table.pop(target.split('/')[2].split('?')[0], None)
                    details = {}
                    if op == 'list-networks' and response.status >= 400:
                        details['error_class'] = network_error_class(payload)
                        details['error_bytes'] = len(payload)
                    p.audit('response', kind=op, status=response.status, **details)
            if op == 'events':
                require(response.status == 200)
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Connection', 'close'); self.end_headers()
                end = time.monotonic()+600; total = 0
                while time.monotonic() < end:
                    # http.client decodes chunked upstream framing. Idle reads
                    # retain the finite stream lifetime, not the ordinary 15s
                    # request timeout; quiet lifecycle phases must not lose EOF.
                    upstream_socket.settimeout(max(.1, end-time.monotonic()))
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
