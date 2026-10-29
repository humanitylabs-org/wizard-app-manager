"""Focused runner regressions; invoked by the parent-admitted early worker."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS/(name+'.py'))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load('manager-seal-build')
early = load('manager-seal-early')


class ManagerSealRunners(unittest.TestCase):
    def test_qa_tracked_source_collision_uses_fresh_named_output_and_preserves_prior(self):
        with tempfile.TemporaryDirectory(prefix='manager-seal-runner-') as tmp:
            root = Path(tmp)
            (root/'qa').mkdir()
            source = root/'qa/tracked-fixture'
            source.write_text('preserve source')
            output = build.output_directory(root, 'focused')
            prior = output/'focused-receipt.json'
            prior.write_text('preserve previous evidence')
            self.assertEqual(output, root/'manager-seal-results')
            self.assertEqual(build.output_directory(root, 'image'), output)
            with self.assertRaises(FileExistsError):
                build.output_directory(root, 'focused')
            self.assertEqual(source.read_text(), 'preserve source')
            self.assertEqual(prior.read_text(), 'preserve previous evidence')

    def test_early_dependency_scratch_is_tmpfs_capped_no_swap_readonly_no_socket_no_host_cache(self):
        cmd = early.create_command(Path('/srv/work/manager-seal-synthetic'), 'synthetic', 1234, 1234, 'a'*40, 'b'*64)
        self.assertEqual(cmd[cmd.index('--memory')+1], str(early.MEMORY))
        self.assertEqual(cmd[cmd.index('--memory-swap')+1], str(early.MEMORY))
        self.assertIn('--read-only', cmd)
        self.assertIn('--pull=never', cmd)
        self.assertIn('--cap-drop=ALL', cmd)
        self.assertIn('--security-opt=no-new-privileges:true', cmd)
        self.assertIn('--log-driver=none', cmd)
        tmpfs = cmd[cmd.index('--tmpfs')+1]
        self.assertIn('size='+str(early.SCRATCH), tmpfs)
        self.assertIn('mode=0700,uid=1234,gid=1234', tmpfs)
        mounts = [cmd[i+1] for i, arg in enumerate(cmd) if arg == '--mount']
        self.assertEqual(len(mounts), 2)
        self.assertTrue(all(m.endswith(',readonly') for m in mounts))
        self.assertNotIn('docker.sock', ' '.join(cmd))
        self.assertNotIn('--privileged', cmd)
        self.assertEqual(early.RESERVE, 214748364800)
        self.assertEqual(build.FOOTPRINT, {'focused': 17179869184, 'image': 42949672960})


if __name__ == '__main__':
    unittest.main()
