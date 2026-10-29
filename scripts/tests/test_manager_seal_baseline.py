"""Synthetic adversarial unit fixtures; NOT recorded Docker execution."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager_seal_baseline import baseline_failure, FIXTURE, SUITE, TITLE, ROUNDTRIP, COUNTS, ROLES


class BaselineClassifier(unittest.TestCase):
    def setUp(self):
        self.source = (Path(__file__).resolve().parents[2]/FIXTURE).read_bytes()
        self.project = 'agent-synthetic_seal0123456789ab'
        self.ids = [str(i)*64 for i in range(1, 6)]
        order = ['agent-synthetic-search', 'agent-synthetic-tinfoil', 'agent-synthetic', 'agent-synthetic-key', 'agent-synthetic-edge']
        self.trace = {'readiness': 'sentinel-create', 'checks': [], 'expectedContainers': self.ids[:], 'expectedNetworkIds': [], 'events': []}
        attrs = {'wizard.qa.manager-seal.fixture': self.project.replace('_', ':', 1), 'wizard.qa.manager-seal': 'manager-seal-docker-0123456789ab'}
        def create(cid, labels):
            return {'Type': 'container', 'Action': 'create', 'Actor': {'ID': cid, 'Attributes': labels}}
        self.trace['events'].append(create('a'*64, dict(attrs)))
        networks = {n: format(i, '064x') for i, n in enumerate(['seal', 'provider', 'keyed', 'edge-uplink', 'public_app', 'public_main'], 100)}
        for cid, role in zip(self.ids, order):
            self.trace['events'].append(create(cid, dict(attrs, **{'com.docker.compose.project': self.project, 'com.docker.compose.service': role})))
            names = ['agent-synthetic-'+s for s in ROLES[role]]+['public_app']+(['public_main'] if role == 'agent-synthetic' else [])
            inventory = {}
            for name in names:
                suffix = name.removeprefix('agent-synthetic-')
                full = self.project+'_'+name; nid = networks[suffix]; inventory[full] = nid
                self.trace['events'].append({'Type': 'network', 'Action': 'connect', 'Actor': {'ID': nid, 'Attributes': {'container': cid, 'name': full, 'type': 'bridge'}}})
            self.trace['expectedNetworkIds'].append([cid, inventory])
        failed = {'status': 'failed', 'ancestorTitles': [SUITE], 'title': TITLE, 'fullName': SUITE+' '+TITLE, 'failureMessages': [
            'AssertionError: expected [ …(3) ] to deeply equal [ …(2) ]\n'
            f'    at inspect (/work/source/{FIXTURE}:579:59)\n'
            '    at processTicksAndRejections (node:internal/process/task_queues:103:5)\n'
            f'    at /work/source/{FIXTURE}:603:7\n']}
        passed = {'status': 'passed', 'fullName': SUITE+' '+ROUNDTRIP, 'failureMessages': []}
        self.report: dict = dict(COUNTS, testResults=[{'name': '/work/source/'+FIXTURE, 'status': 'failed', 'assertionResults': [failed, passed]+[{'status': 'skipped'} for _ in range(17)]}])

    def classify(self):
        return baseline_failure(self.report, self.trace, self.source, self.project)

    def failure(self):
        return self.report['testResults'][0]['assertionResults'][0]

    def test_formatter_truncation_accepts_only_with_independent_correlated_evidence(self):
        self.assertNotIn('_public_app', self.failure()['failureMessages'][0])
        result = self.classify()
        self.assertEqual(result['first_failing_row']['container_id'], self.ids[0])
        self.assertEqual(result['first_failing_row']['service'], 'agent-synthetic-search')
        self.assertEqual(result['public_app_connects'], 5)
        self.failure()['failureMessages'][0] = self.failure()['failureMessages'][0].replace('[ …(3) ]', '["some formatter output"]')
        self.classify()

    def test_unrelated_failure_even_with_public_network_word_and_valid_trace_refuses(self):
        self.failure()['failureMessages'][0] = self.failure()['failureMessages'][0].replace('expected ', 'expected _public_app unrelated ')
        for title in ['other test', TITLE+' other']:
            with self.subTest(title=title):
                self.failure()['title'] = title
                with self.assertRaises(AssertionError): self.classify()

    def test_wrong_exact_source_assertion_callsite_column_and_suite_refuse(self):
        original = copy.deepcopy(self.report)
        for old, new in [('579:59', '578:59'), ('579:59', '579:60'), ('603:7', '606:7'), ('mandatory-seal.test.ts', 'other.test.ts'), ('AssertionError:', 'TypeError:'), ('at inspect (', 'at unrelatedFunction (')]:
            with self.subTest(change=new):
                self.report = copy.deepcopy(original)
                self.failure()['failureMessages'][0] = self.failure()['failureMessages'][0].replace(old, new)
                with self.assertRaises(AssertionError): self.classify()
        self.report = copy.deepcopy(original); self.report['testResults'][0]['name'] += '.other'
        with self.assertRaises(AssertionError): self.classify()

    def test_wrong_source_bytes_refuse_even_at_same_line_numbers(self):
        self.source += b'\n// unrelated source\n'
        with self.assertRaises(AssertionError): self.classify()

    def test_missing_trace_source_project_and_report_evidence_refuse(self):
        for trace, source, project in [(None, self.source, self.project), (self.trace, None, self.project), (self.trace, self.source, None)]:
            with self.subTest(missing=(trace is None, source is None, project is None)):
                with self.assertRaises(AssertionError): baseline_failure(self.report, trace, source, project)
        for key in ['expectedContainers', 'expectedNetworkIds', 'events']:
            old = self.trace[key]; self.trace[key] = []
            with self.subTest(key=key):
                with self.assertRaises(AssertionError): self.classify()
            self.trace[key] = old
        self.failure()['failureMessages'] = []
        with self.assertRaises(AssertionError): self.classify()

    def test_wrong_container_network_id_network_name_and_role_refuse(self):
        original = copy.deepcopy(self.trace)
        for field, value in [('container', 'f'*64), ('name', self.project+'_unrelated'), ('type', 'overlay')]:
            with self.subTest(field=field):
                self.trace = copy.deepcopy(original)
                event = next(e for e in self.trace['events'] if e['Type'] == 'network' and e['Actor']['Attributes']['name'].endswith('_public_app'))
                event['Actor']['Attributes'][field] = value
                with self.assertRaises(AssertionError): self.classify()
        self.trace = copy.deepcopy(original)
        next(e for e in self.trace['events'] if e['Type'] == 'network')['Actor']['ID'] = 'f'*64
        with self.assertRaises(AssertionError): self.classify()
        self.trace = copy.deepcopy(original)
        self.trace['events'][1]['Actor']['Attributes']['com.docker.compose.service'] = 'other-service'
        with self.assertRaises(AssertionError): self.classify()

    def test_arbitrary_public_attachment_to_other_fixture_does_not_suffice(self):
        self.trace['events'] = [e for e in self.trace['events'] if e['Type'] != 'network' or not e['Actor']['Attributes']['name'].endswith('_public_app')]
        self.trace['events'].append({'Type': 'network', 'Action': 'connect', 'Actor': {'ID': 'e'*64, 'Attributes': {'container': 'f'*64, 'name': self.project+'_public_app', 'type': 'bridge'}}})
        with self.assertRaises(AssertionError): self.classify()

    def test_missing_one_positive_event_and_inspect_mismatch_refuse(self):
        original = copy.deepcopy(self.trace)
        self.trace['events'].pop()
        with self.assertRaises(AssertionError): self.classify()
        self.trace = copy.deepcopy(original)
        self.trace['expectedNetworkIds'][0][1][self.project+'_public_app'] = 'e'*64
        with self.assertRaises(AssertionError): self.classify()

    def test_trace_error_wrong_phase_duplicate_missing_sentinel_refuse(self):
        original = copy.deepcopy(self.trace)
        mutations = [lambda: self.trace.update(error='stream failed'), lambda: self.trace.update(checks=['later-phase']),
                     lambda: self.trace['events'].pop(0), lambda: self.trace['events'].append(self.trace['events'][-1]),
                     lambda: self.trace['expectedNetworkIds'].append(self.trace['expectedNetworkIds'][0])]
        for mutate in mutations:
            self.trace = copy.deepcopy(original); mutate()
            with self.assertRaises(AssertionError): self.classify()

    def test_any_other_test_failure_or_wrong_counts_refuse(self):
        self.report['numFailedTests'] = 2
        with self.assertRaises(AssertionError): self.classify()
        self.report['numFailedTests'] = 1
        self.report['testResults'][0]['assertionResults'][2]['status'] = 'failed'
        with self.assertRaises(AssertionError): self.classify()


if __name__ == '__main__': unittest.main()
