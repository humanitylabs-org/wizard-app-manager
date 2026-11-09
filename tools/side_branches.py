#!/usr/bin/env python3
"""Read-only report of leftover live/integration side branches (issue #556).

Live == main: `integration/*`, `live/*` and `live-*` branches on the remote are
left over from live-first integration. This lists each one and says whether it
still carries commits that are not on main, compared by patch id (`git cherry`),
so a cherry-picked or squash-equivalent commit counts as landed.

It never deletes or pushes anything. People with push rights act on the list.
`owner-snapshot/*` branches are never matched (some live work is only there).

Usage: python3 tools/side_branches.py [--remote origin] [--main main] [--json]
Exit 0 when no side branches are left, 1 when some are, 2 on a git error.
Run `git fetch --prune <remote>` first so the remote refs are current.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

PATTERNS = ("integration/", "live/", "live-")


def git(*args: str, cwd: str | None = None) -> str:
    return subprocess.check_output(("git",) + args, cwd=cwd, text=True, stderr=subprocess.PIPE)


def is_side_branch(name: str) -> bool:
    return name.startswith(PATTERNS)


def side_branches(remote: str, cwd: str | None = None) -> list[str]:
    prefix = f"refs/remotes/{remote}/"
    out = git("for-each-ref", "--format=%(refname)", prefix, cwd=cwd)
    names = [line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)]
    return sorted(n for n in names if n != "HEAD" and is_side_branch(n))


def unique_commits(remote: str, main: str, branch: str, cwd: str | None = None) -> int:
    """Commits on the branch whose patch is not on main (`git cherry` '+' lines)."""
    out = git("cherry", f"{remote}/{main}", f"{remote}/{branch}", cwd=cwd)
    return sum(1 for line in out.splitlines() if line.startswith("+"))


def last_commit_date(remote: str, branch: str, cwd: str | None = None) -> str:
    return git("log", "-1", "--format=%cs", f"{remote}/{branch}", cwd=cwd).strip()


def report(remote: str = "origin", main: str = "main", cwd: str | None = None) -> list[dict]:
    rows = []
    for name in side_branches(remote, cwd=cwd):
        rows.append({
            "branch": name,
            "last": last_commit_date(remote, name, cwd=cwd),
            "unique_commits": unique_commits(remote, main, name, cwd=cwd),
        })
    return rows


def render(rows: list[dict]) -> str:
    safe = [r for r in rows if r["unique_commits"] == 0]
    keep = sorted((r for r in rows if r["unique_commits"]), key=lambda r: -r["unique_commits"])
    lines = [f"Side branches left: {len(rows)}"]
    lines.append(f"No unique commits (safe to delete): {len(safe)}")
    lines += [f"  {r['branch']}  (last {r['last']})" for r in safe]
    lines.append(f"Unique commits, need a decision: {len(keep)}")
    lines += [f"  {r['branch']}  {r['unique_commits']} unique  (last {r['last']})" for r in keep]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--main", default="main")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        rows = report(args.remote, args.main)
    except subprocess.CalledProcessError as e:
        print(f"git failed: {(e.stderr or '').strip()}", file=sys.stderr)
        return 2
    print(json.dumps(rows, indent=2) if args.json else render(rows))
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
