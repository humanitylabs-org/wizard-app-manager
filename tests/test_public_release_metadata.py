import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseMetadataTests(unittest.TestCase):
    def test_default_points_to_public_metadata_and_retains_override(self):
        script = (ROOT / 'get.sh').read_text()
        self.assertIn('RELEASE_BASE="${WIZARD_RELEASE_BASE:-https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/main/releases}"', script)
        self.assertNotIn('RELEASE_BASE="${WIZARD_RELEASE_BASE:-https://raw.githubusercontent.com/humanitylabs-org/wizard-os/', script)

    def test_stable_is_a_complete_exact_split_snapshot(self):
        version = (ROOT / 'releases/stable').read_text().strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        release = json.loads((ROOT / 'releases' / f'{version}.json').read_text())
        self.assertEqual(release['version'], version)
        for name in ('wizard-app-manager', 'wizard-ux', 'wizard-agent-service'):
            self.assertRegex(release['components'][name]['commit'], r'^[0-9a-f]{40}$')
            self.assertRegex(release['images'][name]['digest'], r'^sha256:[0-9a-f]{64}$')
            self.assertEqual(set(release['images'][name]['architectures']), {'amd64', 'arm64'})

    def test_historical_022_copy_is_byte_identical_to_canonical(self):
        data = (ROOT / 'releases/0.2.2.json').read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), '8a0d3394c92e01984069647c8e6f2f7f21ddd5ef7a26c8e84daa18429d747c84')

    def test_stable_has_no_retired_default_and_matches_the_slot_table(self):
        version = (ROOT / 'releases/stable').read_text().strip()
        release = json.loads((ROOT / 'releases' / f'{version}.json').read_text())
        slots = json.loads((ROOT / 'wam/defaults.json').read_text())['slots']
        self.assertNotIn('ai-accounts', release['default_apps'])
        for slot, row in release['default_apps'].items():
            self.assertIn(slot, slots)
            if row:
                self.assertEqual(row['app'], slots[slot]['app'])

    def test_historical_030_copy_is_byte_identical_to_canonical(self):
        data = (ROOT / 'releases/0.3.0.json').read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), '87c606848eb2e177ce30f3824e172ec776a4894a1d3ac0a3993710fd6deda644')


if __name__ == '__main__':
    unittest.main()
