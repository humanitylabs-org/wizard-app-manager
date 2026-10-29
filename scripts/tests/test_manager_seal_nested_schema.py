"""Narrow real Unix forwarding regressions; no Docker daemon or binary."""
import copy
import json
import unittest
import test_manager_seal_forward_guard as protocol
from test_manager_seal_forward_guard import NET, OWN, OTHER, NEW
import manager_seal_forward_guard as g
import manager_seal_installed_path as rig


def producer_endpoint():
    # v2.40.0 createEndpointSettings + SDK v28.5.0 EndpointSettings.
    # This replay is NOT the binary witness; the latter runs prebootstrap.
    return dict(IPAMConfig={}, Links=None, Aliases=['qa-main', 'main'], MacAddress='',
                DriverOpts=None, GwPriority=0, NetworkID='', EndpointID='', Gateway='',
                IPAddress='', IPPrefixLen=0, IPv6Gateway='', GlobalIPv6Address='',
                GlobalIPv6PrefixLen=0, DNSNames=None)


class Nested(unittest.TestCase):
    setUp = protocol.Protocol.setUp
    tearDown = protocol.Protocol.tearDown
    request = protocol.Protocol.request
    denied = protocol.Protocol.denied
    container = protocol.Protocol.container

    def contexts(self):
        create = '/containers/create?name=qa-main'
        base = self.container()
        leaf = self.root/'bind'; leaf.mkdir(exist_ok=True)
        self.policy.cfg['binds'] = {str(leaf): {'target': '/synthetic', 'mode': 'ro'}}
        mount = dict(Type='bind', Source=str(leaf), Target='/synthetic', ReadOnly=True,
                     BindOptions={'CreateMountpoint': True})
        base['HostConfig']['Mounts'] = [mount]
        base['Healthcheck'] = {'Test': ['NONE']}
        base['NetworkingConfig']['EndpointsConfig'][NET] = producer_endpoint()
        network = dict(Name='qa-net', Driver='bridge', Internal=True, IPAM={},
                       Labels={'wizard.qa.manager-seal': 'qa'})
        connect = dict(Container=OWN, EndpointConfig=producer_endpoint(), Force=False)
        return [(create, base, (), g.CONTAINER),
                (create, base, ('HostConfig',), g.HOST),
                (create, base, ('HostConfig', 'RestartPolicy'), g.RESTART),
                (create, base, ('HostConfig', 'LogConfig'), g.LOG),
                (create, base, ('HostConfig', 'Mounts', 0), g.MOUNT),
                (create, base, ('HostConfig', 'Mounts', 0, 'BindOptions'), g.BIND),
                (create, base, ('Healthcheck',), g.HEALTH),
                (create, base, ('NetworkingConfig',), g.CONTAINER['NetworkingConfig']),
                (create, base, ('NetworkingConfig', 'EndpointsConfig', NET), g.ENDPOINT),
                ('/networks/create', network, (), g.NETWORK),
                ('/networks/create', network, ('IPAM',), g.IPAM),
                ('/networks/'+NET+'/connect', connect, (), {'Container': str, 'EndpointConfig': g.ENDPOINT, 'Force': bool}),
                ('/networks/'+NET+'/connect', connect, ('EndpointConfig',), g.ENDPOINT)]

    @staticmethod
    def at(body, path):
        for key in path: body = body[key]
        return body

    def test_case_aliases_and_unknown_at_every_admitted_struct(self):
        for route, base, path, schema in self.contexts():
            # Baseline MUST be admissible: no vacuous refusal from another field.
            self.policy.authorize('POST', route, json.dumps(base).encode())
            for key in [*schema, 'FutureField']:
                for alias in {key.lower(), key.upper(), key.swapcase()}:
                    if alias == key: continue
                    body = copy.deepcopy(base); node = self.at(body, path)
                    node[alias] = node.get(key, 'unexpected')
                    with self.subTest(route=route, path=path, alias=alias):
                        self.denied('POST', route, body)
            body = copy.deepcopy(base); self.at(body, path)['Unknown'] = None
            self.denied('POST', route, body)

    def test_wrong_types_including_bool_integer_confusion_every_struct_field(self):
        for route, base, path, schema in self.contexts():
            for key, spec in schema.items():
                wrong = 0 if spec is bool else True if spec is int else 1
                body = copy.deepcopy(base); self.at(body, path)[key] = wrong
                with self.subTest(route=route, path=path, key=key): self.denied('POST', route, body)
        for key in ('Memory', 'MemorySwap', 'PidsLimit', 'NanoCpus', 'ShmSize'):
            for value in (True, False, 1.0, '0', None):
                body = self.container(); body['HostConfig'][key] = value
                self.denied('POST', '/containers/create?name=qa-main', body)

    def test_restart_alias_original_repro_and_unknown_member(self):
        for restart in ({'Name': 'no', 'name': 'always'}, {'name': 'no', 'Name': 'always'},
                        {'Name': 'no', 'MaximumRetryCount': False}, {'Name': 'no', 'bogus': 0}):
            body = self.container(); body['HostConfig']['RestartPolicy'] = restart
            self.denied('POST', '/containers/create?name=qa-main', body)

    def test_disabled_nested_structs_reject_even_zero_aliases_and_unknowns(self):
        # Whole disallowed typed objects/collections are closed, including zeros.
        for field in ('IPv4Address', 'IPv6Address', 'LinkLocalIPs', 'Unknown'):
            for route in ('create', 'connect'):
                ep = producer_endpoint(); ep['IPAMConfig'] = {field: '', field.lower(): ''}
                if route == 'create':
                    body = self.container(); body['NetworkingConfig']['EndpointsConfig'][NET] = ep
                    self.denied('POST', '/containers/create?name=qa-main', body)
                else: self.denied('POST', '/networks/'+NET+'/connect', {'Container': OWN, 'EndpointConfig': ep})
        for field in ('Subnet', 'IPRange', 'Gateway', 'AuxiliaryAddresses', 'Unknown'):
            body = {'Name': 'qa-net', 'Driver': 'bridge', 'Internal': True,
                    'Labels': {'wizard.qa.manager-seal': 'qa'}, 'IPAM': {'Config': [{field: '', field.lower(): ''}]}}
            self.denied('POST', '/networks/create', body)
        for field in ('PathOnHost', 'PathInContainer', 'CgroupPermissions', 'Driver', 'Count', 'DeviceIDs', 'Capabilities', 'Options', 'Name', 'Soft', 'Hard', 'Path', 'Weight', 'Rate', 'Unknown'):
            for collection in ('Devices', 'DeviceRequests', 'Ulimits', 'BlkioWeightDevice', 'BlkioDeviceReadBps', 'BlkioDeviceWriteBps', 'BlkioDeviceReadIOps', 'BlkioDeviceWriteIOps'):
                body = self.container(); body['HostConfig'][collection] = [{field: 0, field.lower(): 0}]
                self.denied('POST', '/containers/create?name=qa-main', body)
        for field in ('HostIp', 'HostPort', 'Unknown'):
            body = self.container(); body['HostConfig']['PortBindings'] = {'80/tcp': [{field: '', field.lower(): ''}]}
            self.denied('POST', '/containers/create?name=qa-main', body)

    def test_pinned_ordinary_shape_positive_create_and_connect(self):
        ep = producer_endpoint(); body = self.container()
        body['NetworkingConfig']['EndpointsConfig'][NET] = ep
        self.raw.replies['/containers/create'] = {'Id': NEW}
        before = len(self.raw.calls)
        self.assertEqual(self.request('POST', '/containers/create?name=qa-main', body)[0], 201)
        self.assertEqual(len(self.raw.calls), before+1)
        self.assertEqual(json.loads(self.raw.calls[-1][2]), body)
        self.assertEqual(self.request('POST', '/networks/'+NET+'/connect', {'Container': OWN, 'EndpointConfig': ep})[0], 200)
        self.assertEqual(json.loads(self.raw.calls[-1][2])['EndpointConfig'], ep)

    def test_every_endpoint_nonzero_or_malformed_refused_on_both_routes(self):
        for key, kind in g.ENDPOINT.items():
            bad = {str: 'populated', int: 1, dict: {'name': 'override'}, list: ['populated']}[kind]
            values = [bad, True, False, 1.0, '0', {}, []]
            if key == 'Aliases': values = [1, {}, [True], [1], [None]]
            for value in values:
                # Empty typed values are legitimate zeros, not negatives.
                if key != 'Aliases' and type(value) is kind and value == {str: '', int: 0, dict: {}, list: []}[kind]: continue
                ep = producer_endpoint(); ep[key] = value
                body = self.container(); body['NetworkingConfig']['EndpointsConfig'][NET] = ep
                with self.subTest(key=key, value=value):
                    self.denied('POST', '/containers/create?name=qa-main', body)
                    self.denied('POST', '/networks/'+NET+'/connect', {'Container': OWN, 'EndpointConfig': ep})
        # Whole disallowed struct collections/maps cannot hide alias members.
        for key in ('Devices', 'DeviceRequests', 'Ulimits', 'BlkioWeightDevice', 'BlkioDeviceReadBps', 'BlkioDeviceWriteBps', 'BlkioDeviceReadIOps', 'BlkioDeviceWriteIOps'):
            body = self.container(); body['HostConfig'][key] = [{'name': '', 'Name': ''}]
            self.denied('POST', '/containers/create?name=qa-main', body)

    def test_discovery_exact_local_miss_then_real_registration_no_unknown_authority(self):
        before = len(self.raw.calls)
        self.assertEqual(self.request('GET', '/v1.51/networks/qa-net')[0], 404)
        self.assertEqual(len(self.raw.calls), before)
        self.assertEqual(self.rows[-1]['operation'], 'discovery-miss')
        for token in ('qa-ne', 'qa-net-other', 'QA-NET', OTHER, OTHER[:12]):
            self.denied('GET', '/networks/'+token)
        for method, suffix, body in [('GET', '?verbose=false', None), ('HEAD', '', None), ('GET', '', {}), ('POST', '/connect', {'Container': OWN}), ('DELETE', '', None)]:
            self.denied(method, '/networks/qa-net'+suffix, body)
        self.policy.activated = False; self.denied('GET', '/networks/qa-net'); self.policy.activated = True
        self.raw.replies['/networks/create'] = {'Id': NEW}
        self.assertEqual(self.request('POST', '/networks/create', {'Name': 'qa-net', 'Driver': 'bridge', 'Internal': True, 'Labels': {'wizard.qa.manager-seal': 'qa'}})[0], 201)
        self.assertEqual(self.request('GET', '/networks/qa-net')[0], 200)
        self.assertEqual(self.raw.calls[-1][1], '/networks/'+NEW)
        self.request('DELETE', '/networks/'+NEW)
        self.assertEqual(self.request('GET', '/networks/qa-net')[0], 404)


class Accounting(unittest.TestCase):
    def rows(self):
        return [{'operation': 'deny'}, {'operation': 'discovery-miss', 'kind': 'network', 'name': 'qa-net', 'status': 404},
                {'operation': 'creation-intent', 'kind': 'network', 'name': 'qa-net'},
                {'operation': 'created', 'kind': 'network', 'name': 'qa-net', 'id': NEW}]

    def test_exact_accounting_positive_recreate_and_all_unexpected_failures(self):
        rows = self.rows()
        self.assertEqual(rig.reconcile_guard_accounting(rows, 1, ['qa-net'])['network_discovery_misses'], 1)
        self.assertEqual(rig.reconcile_guard_accounting(rows+rows[1:], 1, ['qa-net'], expected_cycles=2)['network_discovery_misses'], 2)
        cases = [rows+rows[1:], rows+[{'operation': 'deny'}], rows[1:], rows[:1]+rows[2:], rows[:-1], rows[:2]+[rows[1]]+rows[2:], rows[:1], []]
        for field, value in [('name', 'unknown'), ('kind', 'container'), ('status', 403), ('status', True), ('status', '404')]:
            changed = copy.deepcopy(rows); changed[1][field] = value; cases.append(changed)
        for names, cycles in [(['qa-net', 'uncreated'], 1), (['qa-net'], True), (['qa-net'], 2)]:
            with self.assertRaises(AssertionError): rig.reconcile_guard_accounting(rows, 1, names, expected_cycles=cycles)
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(AssertionError): rig.reconcile_guard_accounting(case, 1, ['qa-net'])


if __name__ == '__main__': unittest.main()
