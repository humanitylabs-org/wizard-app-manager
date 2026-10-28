#!/usr/bin/env python3
"""Audit installed recipes and overrides, without touching the installation.

Only reads paths under --root and the manager/agent registries under --home.
Does not instantiate cli.Config, contact an API/Docker, or persist approvals.
"""
import argparse
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from wam import host_policy as policy  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--home', type=Path, help='manager registry home (default: ROOT/../.wizard-os)')
    parser.add_argument('--grandfather-dry-run', action='store_true', help='simulate the upgrade snapshot in memory')
    parser.add_argument('--json', action='store_true', help='sanitized machine-readable table')
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    if not (root / 'apps').is_dir():
        parser.error('--root must contain apps/')
    c = SimpleNamespace(runtipi_root=root, home=(args.home or root.parent / '.wizard-os').expanduser().resolve())
    rows = policy.installed_risks(c)
    approvals = policy.migration_plan(c) if args.grandfather_dry_run else {}
    table = []
    for row in rows:
        key = policy.approval_key(row['app'], row['store'])
        blocked = bool(row['risks']) and not policy.covers(approvals.get(key), row['risks'])
        table.append({'app': row['app'], 'store': row['store'], 'blocked': blocked,
                      'categories': policy.risk_categories(row['risks'])})
    report = {'apps': len(table), 'flagged': sum(bool(r['categories']) for r in table),
              'blocked': sum(r['blocked'] for r in table), 'grandfather_dry_run': args.grandfather_dry_run,
              'rows': table}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print('| Store | App | Result | Categories |')
        print('|---|---|---|---|')
        for row in table:
            result = 'BLOCKED' if row['blocked'] else 'grandfathered (simulated)' if row['categories'] else 'PASS'
            print(f"| {row['store']} | {row['app']} | {result} | {', '.join(row['categories']) or 'none'} |")
        print(f"Apps: {report['apps']}; flagged: {report['flagged']}; blocked: {report['blocked']}.")
        print('Read-only. No approvals saved, no Docker/API calls, no installation writes.')
    return int(report['blocked'] > 0)


if __name__ == '__main__':
    raise SystemExit(main())
