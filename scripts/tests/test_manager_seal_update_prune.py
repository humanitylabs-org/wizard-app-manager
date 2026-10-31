import ast
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manager_seal_installed_path as rig
import manager_seal_post_update_replay as replay

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT/'scripts/tests/fixtures/manager-seal-update-ece1410307f5'


class UpdatePruneAccounting(unittest.TestCase):
    def setUp(self):
        self.receipt, self.rows, self.events = replay.load(FIXTURE)
        self.kwargs = dict(expected_denials=9, network_names=[rig.PROJECT+'_'+n for n in rig.NETS],
            expected_cycles=3, install_prune_window=self.receipt['install_guard_sequence_window'],
            update_prune_window=(401, 646), expected_probe_rows=self.rows[2:11])

    def check(self, rows=None, **kwargs):
        return rig.reconcile_guard_accounting(self.rows if rows is None else rows, **dict(self.kwargs, **kwargs))

    def test_full_actual_tail_real_bytes_and_old_line_332_failure(self):
        result = replay.replay(ROOT)
        self.assertEqual(result['full_actual_run_tail_statements'], 12)
        self.assertTrue(result['full_tail_pass'])
        self.assertEqual(result['later_expectations_changed'], [])
        self.assertEqual(result['old_code_failure']['line'], 332)

    def test_exact_two_local_prunes_and_three_network_cycles(self):
        result = self.check()
        self.assertEqual(result['expected_refusals'], 11)
        self.assertEqual(result['install_prune_refusals'], 1)
        self.assertEqual(result['update_prune_refusals'], 1)
        self.assertEqual(result['start_restart_prune_refusals'], 0)
        self.assertEqual(result['paired_network_creations'], 12)

    def test_prune_in_start_or_restart_window_fails(self):
        for sequence in (100, 300):
            rows = copy.deepcopy(self.rows)
            rows[631], rows[sequence-1] = rows[sequence-1], rows[631]
            for i, row in enumerate(rows, 1): row['sequence'] = i
            with self.subTest(sequence=sequence), self.assertRaisesRegex(AssertionError, 'prune outside update window'):
                self.check(rows)

    def test_third_prune_fails(self):
        for sequence in (19, 100, 300, 632, 647):
            rows = copy.deepcopy(self.rows)
            rows.insert(sequence-1, copy.deepcopy(rows[631]))
            for i, row in enumerate(rows, 1): row['sequence'] = i
            with self.subTest(sequence=sequence), self.assertRaises(AssertionError): self.check(rows)

    def test_untyped_prune_fails(self):
        rows = copy.deepcopy(self.rows); del rows[631]['reason']
        with self.assertRaisesRegex(AssertionError, 'unexpected guard refusal'): self.check(rows)

    def test_forwarded_prune_fails(self):
        rows = copy.deepcopy(self.rows)
        rows.append({'sequence': 647, 'operation': 'forward', 'route': '/containers/prune'})
        with self.assertRaisesRegex(AssertionError, 'prune forwarded'): self.check(rows)

    def test_closed_shape_and_bool_types(self):
        for patch in ({'extra': False}, {'filters': {'label': {'runtipi.appurn='+rig.URN: 1}}},
                      {'route': '/networks/prune'}, {'filters': {'label': {'runtipi.appurn=foreign': True}}}):
            rows = copy.deepcopy(self.rows); rows[631].update(patch)
            with self.subTest(patch=patch), self.assertRaises(AssertionError): self.check(rows)

    def test_update_bounds_missing_or_exclude_prune_fails(self):
        for window in (None, (632, 646), (401, 631), (False, 646), (401, 647), (646, 401)):
            with self.subTest(window=window), self.assertRaises(AssertionError): self.check(update_prune_window=window)

    def test_no_install_window_fails(self):
        with self.assertRaises(AssertionError): self.check(install_prune_window=None)

    def test_probe_identity_drift_still_fails(self):
        probes = copy.deepcopy(self.rows[2:11]); probes[0]['reason'] = 'changed'
        with self.assertRaisesRegex(AssertionError, 'probe refusal identity drift'): self.check(expected_probe_rows=probes)

    def test_patch_window_is_captured_immediately_around_transaction(self):
        tree = ast.parse((ROOT/'scripts/manager_seal_installed_path.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Rig')
        run = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'run')
        capture = next(n for n in ast.walk(run) if isinstance(n, ast.Try)
            and any(isinstance(x, ast.Assign) and ast.unparse(x.targets[0]) == 'third' for x in n.body))
        i = next(i for i, n in enumerate(capture.body) if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == 'third')
        self.assertEqual(ast.unparse(capture.body[i-1]), 'update_lower = len(creation_ledger())')
        self.assertEqual(ast.unparse(capture.body[i+1]), 'update_upper = len(creation_ledger())')
        self.assertIn("self.receipt['update_guard_sequence_window'] = [update_lower, update_upper]", ast.unparse(capture.body[i+2]))


if __name__ == '__main__': unittest.main()
