"""Bounded offline probe regression only; no installed-runtime acceptance."""
import importlib.util
import os
from pathlib import Path
import shutil
import hashlib
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from manager_seal_installed_spike import probe

REPO = SCRIPTS.parent


class InstalledSpikeRefusals(unittest.TestCase):
    def stage(self, root):
        import json
        manifest = REPO/'docs/manager-seal-installed-spike-pins.json'
        # This is a historical archive refusal test, not a claim that the new
        # product sources still match the old bundle's source pin.
        pins = json.loads(manifest.read_text())['source_sha256']
        baseline = '118608e135ffa1d7f5114a2e9b7a059c50c04cf2'
        for rel, expected in pins.items():
            dst = root/rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = subprocess.check_output(['git', 'show', f'{baseline}:{rel}'], cwd=REPO, timeout=5)
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)
            dst.write_bytes(data)
        dst = root/'docs/manager-seal-installed-spike-pins.json'
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, dst)

    def test_source_drift_refuses_before_archive_or_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.stage(root)
            p = root/'packages/backend/src/app.service.ts'
            p.write_text(p.read_text().replace('await this.docker.pruneNetworks();', '/* drift */'))
            with self.assertRaisesRegex(AssertionError, 'source drift'):
                probe(root, root/'absent-archive', 'never-execute-node')

    def test_manifest_drift_refuses_before_source_or_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root/'docs/manager-seal-installed-spike-pins.json'
            p.parent.mkdir()
            p.write_text('{}')
            with self.assertRaisesRegex(AssertionError, 'source manifest drift'):
                probe(root, root/'absent-archive', 'never-execute-node')

    def test_archive_drift_refuses_before_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.stage(root)
            archive = root/'archive.tar.gz'
            archive.write_bytes(b'not saved archive')
            with self.assertRaisesRegex(AssertionError, 'saved archive drift'):
                probe(root, archive, 'never-execute-node')

    def test_pins_and_production_paths_are_nonempty(self):
        import json
        pins = json.loads((REPO/'docs/manager-seal-installed-spike-pins.json').read_text())
        self.assertEqual(len(pins['source_sha256']), 17)
        self.assertTrue(all(REPO.joinpath(p).is_file() for p in pins['source_sha256']))


if __name__ == '__main__':
    unittest.main()
