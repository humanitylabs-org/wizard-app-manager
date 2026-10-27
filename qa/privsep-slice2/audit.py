#!/usr/bin/env python3
"""Reproduce the admission audit from pinned, byte-for-byte source recipes. No network or Docker."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from wam import host_policy


def report():
    here = Path(__file__).resolve().parent
    rows = json.loads((here / 'sources.json').read_text())
    defaults = {v['app'] for v in json.loads((ROOT / 'wam/defaults.json').read_text())['slots'].values() if v.get('app')}
    assert defaults == {r['app'] for r in rows if r['default']}, 'default audit is incomplete'
    assert len({r['key'] for r in rows}) == len(rows), 'duplicate recipe'
    lines = ['# Slice 2 store and default-app admission audit', '',
             'Reproduce with `python qa/privsep-slice2/audit.py --check` (PyYAML required).', '',
             'The fixtures are byte-for-byte recipes at the commits in `sources.json`. '
             'Every current recipe in the store checkout is included, plus the default-app source recipes '
             'not in that checkout. No app was executed or deployed.', '',
             '## Store recipes', '', '| App | Result | Reason | Source |', '|---|---|---|---|']
    default_lines, results = [], []
    for row in rows:
        source = f"[{row['repo']}@{row['commit'][:12]}](https://github.com/{row['repo']}/tree/{row['commit']})"
        if row['path'] is None:
            default_lines.append(f"| {row['app']} | N/A (native host component) | No Compose recipe; installed as a host service by `wam.troubleshooter`, not admitted through app install. No host-access bypass added. | {source} |")
            continue
        text = (here / 'recipes' / (row['key'] + '.yml')).read_text()
        risks = host_policy.risks(text, row['app'])
        results.append({**row, 'risks': risks})
        status = 'needs approval' if risks else 'pass'
        reason = '; '.join(risks) if risks else 'Only allowlisted settings, app-owned data/files, pinned read-only source or named volumes; reviewed exceptions below where applicable.'
        line = f"| {row['app']} | {status} | {reason.replace('|', '/')} | {source} |"
        if row['key'].startswith('store-'):
            lines.append(line)
        if row['default']:
            default_lines.append(line)
    store = [r for r in results if r['key'].startswith('store-')]
    default_recipes = [r for r in results if r['default']]
    assert len(store) == 8
    lines += ['', '## System/default apps', '', '| App | Result | Reason | Source |', '|---|---|---|---|', *default_lines,
              '', '## Totals', '',
              f"- Store: {len(store)} recipes; {sum(not r['risks'] for r in store)} pass; {sum(bool(r['risks']) for r in store)} need owner approval.",
              f"- Defaults: {len(default_recipes)} Compose recipes; {sum(not r['risks'] for r in default_recipes)} pass; {sum(bool(r['risks']) for r in default_recipes)} need owner approval; {len(defaults)-len(default_recipes)} native component without a Compose recipe.",
              '', '## Reviewed exceptions (finite, in `wam/host_policy.py`)', '',
              '- Files: exact service `files`, `cap_drop: [ALL]` and exact `cap_add: [CHOWN, SETUID, SETGID]`, needed to switch uid for filebrowser. A different app/service or capability set is not covered.',
              '- Agent Service: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os/hermes-view:/hermes:ro` and `${ROOT_FOLDER_HOST}/app-data/wizard-os:/wizard-os:ro` metadata projections.',
              '- Connection Router: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os:/run/wizard:ro` metadata and `${ROOT_FOLDER_HOST}/repos:/run/recipes:ro` recipe catalog.',
              '- `no-new-privileges:true` (or `=true`) is an allowlisted privilege-reducing option, not a seccomp/AppArmor exemption. Every other `security_opt` requires consent.',
              '- `${APP_SOURCE_DIR}` is the manager-created pinned source directory, accepted read-only, with traversal rejected and explicitly supplied paths checked against the app-specific managed source name.',
              '- Named volumes are allowed as required, including explicitly named/external Docker volumes; bind-style `driver_opts` and non-local volume drivers require approval.',
              '', '## Expansion and limits', '',
              '`APP_DATA_DIR` resolves to this app in Runtipi app-data (including configured appDataPath / RUNTIPI_APP_DATA_PATH); `ROOT_FOLDER_HOST/media` is the files root. Relative bind paths resolve against the app Compose project directory. Long-form mounts, `..`, existing filesystem symlinks and runtime socket sources are checked. Unknown path variables fail closed unless a default expands into an allowed root. Current app/user environment and the final install form are checked and mount-affecting values are bound to consent.',
              '', 'The Notifications default HERMES_CRON_DIR fallback is app-local and passes; setting it to another app/home requires owner approval. These results describe the default recipes, not owner-specific alongside overrides. Legacy alongside host networking/home mounts deliberately need explicit recipe approval; no app-id-only host-network exception was added.',
              '', 'This is source/policy admission evidence, not a VM or live-box conformance claim. The agent still shares the service uid until Slice 3; a human administrator can use the local CLI. No live box, stock Hermes, host privileges or release were changed.', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    text = report()
    target = Path(__file__).with_name('STORE-AUDIT.md')
    if '--check' in sys.argv:
        assert target.read_text() == text, 'audit document is stale'
        print('PASS: all pinned recipes and default coverage match STORE-AUDIT.md')
    else:
        print(text, end='')
