"""tools/side_branches.py reports leftover live/integration branches read-only (#556)."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('side_branches', Path(__file__).parents[1] / 'tools/side_branches.py')
assert spec is not None and spec.loader is not None
sb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sb)


def git(cwd, *args):
    return subprocess.check_output(('git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
                                    '-c', 'init.defaultBranch=main', '-c', 'commit.gpgsign=false') + args,
                                   cwd=cwd, text=True)


def commit(cwd, name, text):
    (Path(cwd) / name).write_text(text)
    git(cwd, 'add', name)
    git(cwd, 'commit', '-q', '-m', name)


class SideBranches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin, self.clone = root / 'origin', root / 'clone'
        self.origin.mkdir()
        o = str(self.origin)
        git(o, 'init', '-q')
        commit(o, 'base', 'base\n')
        # merged: its only commit is on main too
        git(o, 'checkout', '-q', '-b', 'integration/merged')
        commit(o, 'a', 'a\n')
        git(o, 'checkout', '-q', 'main')
        git(o, 'merge', '-q', '--ff-only', 'integration/merged')
        # cherry-picked onto main with a different sha: same patch id, counts as landed
        git(o, 'checkout', '-q', '-b', 'live/picked')
        commit(o, 'b', 'b\n')
        git(o, 'checkout', '-q', 'main')
        commit(o, 'c', 'c\n')
        git(o, 'cherry-pick', 'live/picked')
        # two commits not on main
        git(o, 'checkout', '-q', '-b', 'live-unique', 'main')
        commit(o, 'd', 'd\n')
        commit(o, 'e', 'e\n')
        # never matched
        git(o, 'checkout', '-q', '-b', 'owner-snapshot/keep', 'live-unique')
        git(o, 'checkout', '-q', '-b', 'feature/other', 'live-unique')
        git(o, 'checkout', '-q', 'main')
        subprocess.check_call(('git', 'clone', '-q', o, str(self.clone)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_matches_only_side_branch_prefixes(self):
        self.assertEqual(sb.side_branches('origin', cwd=str(self.clone)),
                         ['integration/merged', 'live-unique', 'live/picked'])

    def test_counts_unique_commits_by_patch_id(self):
        rows = {r['branch']: r['unique_commits'] for r in sb.report(cwd=str(self.clone))}
        self.assertEqual(rows, {'integration/merged': 0, 'live/picked': 0, 'live-unique': 2})

    def test_render_splits_safe_and_decision(self):
        text = sb.render(sb.report(cwd=str(self.clone)))
        self.assertIn('Side branches left: 3', text)
        self.assertIn('No unique commits (safe to delete): 2', text)
        self.assertIn('Unique commits, need a decision: 1', text)
        self.assertNotIn('owner-snapshot', text)

    def test_exit_code_is_zero_only_when_none_left(self):
        import os
        cwd = os.getcwd()
        try:
            os.chdir(self.clone)
            self.assertEqual(sb.main(['--json']), 1)
            for b in ('integration/merged', 'live/picked', 'live-unique'):
                git(str(self.clone), 'update-ref', '-d', f'refs/remotes/origin/{b}')
            self.assertEqual(sb.main([]), 0)
        finally:
            os.chdir(cwd)

    def test_never_writes_to_the_remote(self):
        src = (Path(__file__).parents[1] / 'tools/side_branches.py').read_text()
        for word in ('"push"', "'push'", '"branch", "-D"', 'update-ref', '"-d"'):
            self.assertNotIn(word, src)


if __name__ == '__main__':
    unittest.main()
