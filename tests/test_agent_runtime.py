import copy
import unittest
from wam.agent_runtime import agent_runtime

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.w = {'type': 'agent', 'agent_runtime': {'id': 'example-agent', 'label': 'Example', 'harness': 'another-harness', 'port': 18659, 'model': {'provider': 'wizard-router', 'model': 'reply'}}}
    def test_generic_harness(self):
        result = agent_runtime(self.w)
        assert result is not None
        self.assertEqual(result['harness'], 'another-harness')
    def test_bad_declarations(self):
        for field, value in [('id', 'main'), ('id', '../path'), ('port', True), ('port', 80), ('label', ''), ('model', {})]:
            with self.subTest(field=field, value=value):
                w = copy.deepcopy(self.w); w['agent_runtime'][field] = value
                with self.assertRaises(ValueError): agent_runtime(w)
        self.w['agent_runtime']['url'] = 'http://arbitrary'
        with self.assertRaises(ValueError): agent_runtime(self.w)
    def test_no_private_runtime_or_implicit_admission(self):
        self.assertIsNone(agent_runtime({}))
        self.w['privacy'] = 'private'
        with self.assertRaises(ValueError): agent_runtime(self.w)

class RegistryTests(unittest.TestCase):
    def test_installed_projection_and_uninstall_are_credential_free(self):
        import json
        import tempfile
        from pathlib import Path
        import yaml
        from wam import registry
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            app = root / 'apps/wizardapps/example-runtime'
            app.mkdir(parents=True)
            runtime = {'id': 'example-agent', 'label': 'Example', 'harness': 'other-harness', 'port': 18659, 'model': {'provider': 'wizard-router', 'model': 'reply'}}
            manifest = {'id': 'example-runtime', 'type': 'agent', 'agent_runtime': runtime, 'requires': {'models': {'reply': {'tier': 'standard', 'tested_on': 'fixture-model', 'abilities': []}}}}
            (app / 'wizard.md').write_text('---\n' + yaml.safe_dump(manifest) + '---\n')
            (app / 'config.json').write_text('{}')
            rows = [{'app': {'status': 'running', 'port': 28659}, 'info': {'urn': 'example-runtime:wizardapps'}}]
            reg = registry.build(rows, 'wizardapps', '127.0.0.1', root)
            projected = reg['apps'][0]
            self.assertEqual(projected['agent_runtime'], {**runtime, 'host_port': 28659})
            self.assertEqual(projected['requires'], manifest['requires'])
            self.assertNotIn('key', json.dumps(projected))
            self.assertEqual(registry.build([], 'wizardapps', '127.0.0.1', root)['apps'], [])

if __name__ == '__main__': unittest.main()
