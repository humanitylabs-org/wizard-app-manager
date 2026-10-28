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
              '- All apps: `cap_add` may restore capabilities within Docker\'s default set: `AUDIT_WRITE CHOWN DAC_OVERRIDE FOWNER FSETID KILL MKNOD NET_BIND_SERVICE NET_RAW SETFCAP SETGID SETPCAP SETUID SYS_CHROOT`. Adding these after `cap_drop` gives no more power than a default container. Other capabilities (including `SYS_ADMIN`, `NET_ADMIN`, `SYS_PTRACE` and `ALL`) still require consent. This replaces the Files-only exception.',
              '- Agent Service: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os/hermes-view:/hermes:ro` and `${ROOT_FOLDER_HOST}/app-data/wizard-os:/wizard-os:ro` metadata projections.',
              '- Connection Router: exact read-only `${ROOT_FOLDER_HOST}/app-data/wizard-os:/run/wizard:ro` metadata and `${ROOT_FOLDER_HOST}/repos:/run/recipes:ro` recipe catalog.',
              '- `no-new-privileges:true` (or `=true`) is an allowlisted privilege-reducing option, not a seccomp/AppArmor exemption. Every other `security_opt` requires consent.',
              '- Same-app pinned source: read-only `${APP_SOURCE_DIR}`, `<root>/repos/<store>/apps/<id>/releases/<rev>` and `<app-data>/.wizard-source/<store>/<id>@<rev>` projections pass, in short or long mount syntax. Check exact prefixes, same app/store, traversal and physical resolution; writable binds, another app\'s pins and symlink escapes still require consent.\n- Manager wiring: regenerate the alongside shell/service/bundle and compare before exempting it. Builder and Claude-login shares exempt only their exact current generated fragment, derived from installed bundle/alongside state, not the override marker. Extra-agent home binds are derived from validated same-agent registry metadata, with finite source/service/target tuples. No broad host-network/app-id bypass exists.',
              '- Named volumes are allowed as required, including explicitly named/external Docker volumes; bind-style `driver_opts` and non-local volume drivers require approval.',
              '', '## Expansion and limits', '',
              '`APP_DATA_DIR` resolves to this app in Runtipi app-data (including configured appDataPath / RUNTIPI_APP_DATA_PATH); `ROOT_FOLDER_HOST/media` is the files root. Relative bind paths resolve against the app Compose project directory. Long-form mounts, `..`, existing filesystem symlinks and runtime socket sources are checked. Unknown path variables fail closed unless a default expands into an allowed root. Current app/user environment and the final install form are checked. Risk strings include resolved host-path/device/network identity and mount access mode, so changing these can introduce a new risk. Consent binds the sorted risk set, not recipe bytes; same/subset risks pass after image/comment-only updates.',
              '', 'The Notifications default HERMES_CRON_DIR fallback is app-local and passes; setting it to another app/home requires owner approval. These default-recipe results are supplemented by [the real installed-box audit](INSTALLED-AUDIT.md). The one-time upgrade snapshot preserves each installed app/store pair\'s current risk set with a timestamp and `grandfathered at upgrade` source. Doctor lists existing host-access apps in plain words. New or restored access outside that set still needs owner consent; safe new apps cannot grandfather themselves. Owner-local host-network overrides without reproducible manager provenance stay visible as host access, rather than being silently exempted.',
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
