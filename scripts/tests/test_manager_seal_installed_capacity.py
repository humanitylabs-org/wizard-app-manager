import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

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
        self.assertTrue(capacity.admission(3*2**30, capacity.RESERVE+2**30)['admitted'])
        self.assertFalse(capacity.admission(3*2**30+1, capacity.RESERVE+2**30)['admitted'])
        self.assertFalse(capacity.admission(0, capacity.RESERVE+2**30-1)['admitted'])

    def test_approved_task_only_expansion_admits_previously_refused_peak(self):
        self.assertEqual(capacity.RETAINED_PEAK, 4294967296)
        retained, free = 2251644928, 299713110016
        with patch.object(capacity, 'RETAINED_PEAK', 3221225472):
            old = capacity.admission(retained, free)
        new = capacity.admission(retained, free)
        self.assertEqual(old['retained_plus_next_peak_bytes'], 3325386752)
        self.assertEqual(new['retained_plus_next_peak_bytes'], 3325386752)
        self.assertFalse(old['admitted'])
        self.assertEqual(len(old['blockers']), 1)
        self.assertTrue(new['admitted'])
        self.assertEqual(new['blockers'], [])
        self.assertEqual(new['retained_plus_next_peak_limit_bytes'], 4294967296)
        self.assertEqual(old['free_required_bytes'], new['free_required_bytes'])

    def test_expansion_keeps_one_gib_per_run_max(self):
        self.assertEqual(capacity.ALLOWANCE, 1073741824)
        with self.assertRaises(AssertionError):
            capacity.admission(0, 299713110016, next_peak=1073741825)
        # Already allocated is a deduction, not permission to raise the run cap.
        with self.assertRaises(AssertionError):
            capacity.admission(1, 299713110016, already_allocated=1, next_peak=1073741825)

    def test_expansion_keeps_two_hundred_gib_reserve_and_pending_only(self):
        self.assertEqual(capacity.RESERVE, 214748364800)
        own = 4096
        required = 214748364800 + 1073741824 - own
        admitted = capacity.admission(2251644928+own, required, already_allocated=own)
        refused = capacity.admission(2251644928+own, required-1, already_allocated=own)
        self.assertTrue(admitted['admitted'])
        self.assertEqual(admitted['free_required_bytes'], required)
        self.assertEqual(admitted['retained_plus_next_peak_bytes'], 3325386752)
        self.assertFalse(refused['admitted'])
        self.assertEqual(refused['blockers'], ['free below 200GiB reserve plus not-yet-allocated footprint'])

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

    def test_metadata_entry_cap_still_refuses_without_partial_totals(self):
        self.assertEqual(capacity.ENTRY_CAP, 65536)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); stage = root/'manager-seal-a'; stage.mkdir()
            (stage/'one').write_bytes(b'owned')
            with patch.object(capacity, 'ENTRY_CAP', 2):
                self.assertEqual(sum(r['entries'] for r in capacity.inventory(root, time.monotonic()+2)), 2)
                (stage/'two').write_bytes(b'owned')
                with self.assertRaisesRegex(RuntimeError, 'entry cap exceeded'):
                    capacity.inventory(root, time.monotonic()+2)

    def test_no_matching_stages_is_empty_not_whole_host_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'unrelated').mkdir()
            self.assertEqual(capacity.inventory(root, time.monotonic()+2), [])


if __name__ == '__main__':
    unittest.main()
