"""Source-bound serializer replay. NOT execution of Compose, SDK, or Docker."""
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_forward_guard as g
import test_manager_seal_forward_guard as protocol
from test_manager_seal_forward_guard import NEW

ROOT = Path(__file__).parent/'fixtures/manager_seal_wire'


def struct(file, name):
    """Parse only these SHA-bound simple public Go struct declarations."""
    source = (ROOT/file).read_text()
    body = re.search(r'^type '+name+r' struct \{\n(.*?)^\}', source, re.M | re.S)[1]
    result = {}
    for line in body.splitlines():
        line = line.split('//', 1)[0].strip()
        if not line: continue
        parts = line.split()
        if len(parts) == 1:
            result['@'+parts[0]] = {'embedded': True}
            continue
        field, kind = parts[:2]
        tag = re.search(r'json:"([^"]*)"', line)
        tag = tag[1] if tag else ''
        wire = tag.split(',')[0] or field
        result[wire] = {'kind': kind, 'omit': 'omitempty' in tag}
    return result


def expand(file, name, embedded=None):
    result = {}
    for key, spec in struct(file, name).items():
        if key.startswith('@'):
            other_file, other_name = embedded[key[1:]]
            result.update(struct(other_file, other_name))
        else: result[key] = spec
    return result


class Source(unittest.TestCase):
    def test_source_digests_and_actual_dependency_versions(self):
        manifest = json.loads((ROOT/'public-sources.json').read_text())
        for file, item in manifest.items():
            data = (ROOT/file).read_bytes()
            self.assertEqual(len(data), item['bytes'])
            self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
        mod = (ROOT/'compose-go.mod').read_text()
        self.assertIn('github.com/docker/docker v28.5.0+incompatible', mod)
        self.assertIn('github.com/moby/docker-image-spec v1.3.1', mod)
        self.assertIn('json.NewEncoder(params).Encode(data)', (ROOT/'sdk-request.go').read_text())
        self.assertIn('cli.post(ctx, "/networks/create", nil, networkCreateRequest, nil)', (ROOT/'sdk-network-create.go').read_text())
        create = (ROOT/'compose-create.go').read_text()
        options = re.search(r'createOpts := network.CreateOptions\{(.*?)\n\t\}', create, re.S)[1]
        for field in ('Scope:', 'ConfigOnly:', 'ConfigFrom:'):
            self.assertNotIn(field, options)
        config = re.search(r'containerConfig := container.Config\{(.*?)\n\t\}', create, re.S)[1]
        self.assertNotIn('OnBuild:', config)

    def test_all_relevant_structs_reviewed_together_no_unconditional_omissions(self):
        config = expand('sdk-create-request.go', 'CreateRequest', {'*Config': ('sdk-config.go', 'Config')})
        host = expand('sdk-hostconfig.go', 'HostConfig', {'Resources': ('sdk-hostconfig.go', 'Resources')})
        network = expand('sdk-network.go', 'CreateRequest', {'CreateOptions': ('sdk-network.go', 'CreateOptions')})
        cases = [(config, g.CONTAINER, {'ArgsEscaped', 'MacAddress', 'Shell'}),
                 (host, g.HOST, {'Annotations', 'StorageOpt', 'KernelMemory', 'KernelMemoryTCP'}),
                 (network, g.NETWORK, set()),
                 (struct('sdk-endpoint.go', 'EndpointSettings'), g.ENDPOINT, set()),
                 (struct('sdk-ipam.go', 'IPAM'), g.IPAM, set()),
                 (struct('sdk-hostconfig.go', 'RestartPolicy'), g.RESTART, set()),
                 (struct('sdk-hostconfig.go', 'LogConfig'), g.LOG, set()),
                 (struct('sdk-network.go', 'NetworkingConfig'), g.CONTAINER['NetworkingConfig'], set()),
                 (struct('sdk-mount.go', 'Mount'), g.MOUNT, {'VolumeOptions', 'ImageOptions', 'TmpfsOptions', 'ClusterOptions'}),
                 (struct('sdk-mount.go', 'BindOptions'), g.BIND, set()),
                 (struct('healthcheck.go', 'HealthcheckConfig'), g.HEALTH, set())]
        for sdk, admitted, excluded in cases:
            self.assertEqual(set(sdk)-set(admitted), excluded)
            self.assertFalse(set(admitted)-set(sdk))
            for key in excluded:
                self.assertTrue(sdk[key]['omit'], key+' cannot be silently omitted')
        # Disabled nested collections/references remain wholly non-delegated.
        self.assertEqual(set(struct('sdk-network.go', 'ConfigReference')), {'Network'})
        self.assertEqual(set(struct('sdk-ipam.go', 'IPAMConfig')), {'Subnet', 'IPRange', 'Gateway', 'AuxiliaryAddresses'})
        self.assertEqual(set(struct('sdk-endpoint.go', 'EndpointIPAMConfig')), {'IPv4Address', 'IPv6Address', 'LinkLocalIPs'})
        self.assertEqual(set(struct('sdk-network.go', 'ConnectOptions')), {'Container', 'EndpointConfig'})
        self.assertEqual(set(struct('sdk-network.go', 'DisconnectOptions')), {'Container', 'Force'})

    def test_exact_replay_derived_from_pinned_field_order_and_zero_values(self):
        specs = expand('sdk-network.go', 'CreateRequest', {'CreateOptions': ('sdk-network.go', 'CreateOptions')})
        body = {}
        overrides = {'Name': 'qa-net', 'Driver': 'bridge', 'Internal': True,
                     'Labels': {'wizard.qa.manager-seal': 'qa'}}
        for key, spec in specs.items():
            if spec['omit']: continue  # all optional pointer flags are nil here
            kind = spec['kind']
            body[key] = overrides.get(key, False if kind == 'bool' else '' if kind == 'string' else None)
        # For these ASCII-only simple values Python's compact encoder equals
        # encoding/json's struct field order and final Encoder.Encode newline.
        replay = (json.dumps(body, separators=(',', ':'))+'\n').encode()
        self.assertEqual(replay, (ROOT/'network-create-source-replay.json').read_bytes())


class Replay(unittest.TestCase):
    setUp = protocol.Protocol.setUp
    tearDown = protocol.Protocol.tearDown
    request = protocol.Protocol.request
    denied = protocol.Protocol.denied
    container = protocol.Protocol.container

    def test_exact_source_serializer_network_bytes_forward_unchanged(self):
        data = (ROOT/'network-create-source-replay.json').read_bytes()
        self.raw.replies['/networks/create'] = {'Id': NEW}
        connection = g.UnixHTTP(str(self.root/'proxy.sock'))
        try:
            connection.request('POST', '/v1.51/networks/create', data)
            response = connection.getresponse(); response.read()
            self.assertEqual(response.status, 201)
        finally: connection.close()
        self.assertEqual(self.raw.calls[-1][2], data)
        self.assertEqual(set(json.loads(data)), {'Driver', 'Scope', 'IPAM', 'Internal', 'Attachable', 'Ingress', 'ConfigOnly', 'ConfigFrom', 'Options', 'Labels', 'Name'})

    def test_new_network_fields_only_exact_unused_values_and_never_references(self):
        base = json.loads((ROOT/'network-create-source-replay.json').read_bytes())
        self.policy.authorize('POST', '/networks/create', json.dumps(base).encode())
        values = {'Scope': ['local', 'swarm', ' ', None, 0, False, [], {}],
                  'ConfigOnly': [True, None, 0, 1, 0.0, 'false', [], {}],
                  'ConfigFrom': [{}, {'Network': ''}, {'Network': 'qa-net'}, {'network': 'qa-net'}, {'Network': '', 'Unknown': None}, '', False, 0, []]}
        for key, bad in values.items():
            for value in bad:
                body = copy.deepcopy(base); body[key] = value
                with self.subTest(field=key, value=value): self.denied('POST', '/networks/create', body)
        for key in ('Scope', 'ConfigOnly', 'ConfigFrom'):
            for alias in (key.lower(), key.upper(), key.swapcase()):
                for value in (None, False, '', {}, []):
                    body = copy.deepcopy(base); body[alias] = value
                    self.denied('POST', '/networks/create', body)

    def test_collective_container_ordinary_unconditional_fields(self):
        body = self.container()
        # Every unconditional field emitted by the pinned SDK is represented,
        # preserving the already-admitted fixture's nonzero security controls.
        config = struct('sdk-config.go', 'Config')
        host = expand('sdk-hostconfig.go', 'HostConfig', {'Resources': ('sdk-hostconfig.go', 'Resources')})
        def zero(kind):
            if kind.startswith(('*', '[]', 'map[')): return None
            if kind == '[2]uint': return [0, 0]
            if kind in ('LogConfig', 'RestartPolicy'): return {}
            if kind == 'bool': return False
            if kind in ('int', 'int64', 'uint16', 'uint64'): return 0
            return ''
        for specs, admitted, target in [(config, g.CONTAINER, body), (host, g.HOST, body['HostConfig'])]:
            for key, spec in specs.items():
                if not spec['omit']:
                    value = None if admitted[key] in (list, dict) and key != 'ConsoleSize' else zero(spec['kind'])
                    target.setdefault(key, value)
        self.raw.replies['/containers/create'] = {'Id': NEW}
        self.assertEqual(self.request('POST', '/containers/create?name=qa-main', body)[0], 201)
        self.assertIn('OnBuild', body)
        self.assertIsNone(body['OnBuild'])

    def test_onbuild_is_never_build_authority(self):
        for zero in (None, []):
            body = self.container(); body['OnBuild'] = zero
            self.policy.authorize('POST', '/containers/create?name=qa-main', json.dumps(body).encode())
        for value in (['RUN id'], [''], [None], {}, '', False, 0):
            body = self.container(); body['OnBuild'] = value
            self.denied('POST', '/containers/create?name=qa-main', body)
        for alias in ('onbuild', 'ONBUILD', 'oNbUILD'):
            body = self.container(); body[alias] = None
            self.denied('POST', '/containers/create?name=qa-main', body)


if __name__ == '__main__': unittest.main()
