import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/'manager_seal_installed_capacity.py'
spec = importlib.util.spec_from_file_location('capacity', SCRIPT)
assert spec is not None and spec.loader is not None
capacity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capacity)


class RetainedStageAccounting(unittest.TestCase):
    def test_reconciliation_does_not_promote_per_run_cap_to_lifetime_quota(self):
        decision = capacity.admission(1433038848, 225618149376)
        self.assertTrue(decision['admitted'])
        self.assertEqual(decision['retained_plus_next_peak_bytes'], 2506780672)
        self.assertEqual(decision['free_required_bytes'], 215822106624)

    def test_retained_is_not_double_counted_against_free(self):
        self.assertTrue(capacity.admission(2*2**30, capacity.RESERVE+2**30)['admitted'])
        self.assertFalse(capacity.admission(2*2**30+1, capacity.RESERVE+2**30)['admitted'])
        self.assertFalse(capacity.admission(0, capacity.RESERVE+2**30-1)['admitted'])

    def test_newstage_already_deducted_and_retained_is_not_reset(self):
        decision = capacity.admission(2*2**30+100, capacity.RESERVE+2**30-100, already_allocated=100)
        self.assertTrue(decision['admitted'])
        self.assertEqual(decision['retained_plus_next_peak_bytes'], 3*2**30)
        self.assertEqual(decision['not_yet_allocated_bytes'], 2**30-100)
        with self.assertRaises(AssertionError):
            capacity.admission(0, capacity.RESERVE, next_peak=2**30+1)

    def test_scoped_inode_dedup_and_no_symlink_follow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root/'manager-seal-a'
            second = root/'manager-seal-b'
            first.mkdir()
            second.mkdir()
            file = first/'proof'
            file.write_bytes(b'synthetic-proof')
            os.link(file, second/'same-inode')
            outside = root/'not-task-owned'
            outside.mkdir()
            (outside/'unrelated').write_bytes(b'never-count')
            (second/'external-link').symlink_to(outside, target_is_directory=True)
            rows = capacity.inventory(root, time.monotonic()+2)
            self.assertEqual(len(rows), 2)
            self.assertEqual(sum(row['regular_file_bytes'] for row in rows), len(b'synthetic-proof'))
            self.assertEqual(sum(row['entries'] for row in rows), 4)  # two dirs, one file inode, one symlink
            self.assertGreaterEqual(sum(row['allocated_bytes'] for row in rows), file.stat().st_blocks*512)

    def test_symlink_task_root_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'manager-seal-link').symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, 'refuse symlink task root'):
                capacity.inventory(root, time.monotonic()+2)

    def test_expired_deadline_refuses_instead_of_partial_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'manager-seal-a').mkdir()
            with self.assertRaisesRegex(RuntimeError, 'deadline exceeded'):
                capacity.inventory(root, time.monotonic()-1)

    def test_no_matching_stages_is_empty_not_whole_host_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'unrelated').mkdir()
            self.assertEqual(capacity.inventory(root, time.monotonic()+2), [])


if __name__ == '__main__':
    unittest.main()
