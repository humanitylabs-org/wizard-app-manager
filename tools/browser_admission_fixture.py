#!/usr/bin/env python3
"""Synthetic WAM install/grant state for cross-repo browser-trust admission tests.

Drives the REAL App Manager registry writer (`cli.write_registry`) and browser grant CLI against
a throwaway Runtipi-shaped root, so the Agent Service can be tested against what WAM actually
writes instead of an injected resolver. Synthetic data only; never point it at a real box.

  python tools/browser_admission_fixture.py <dir> install notifications --repo oscarhenrycollins/notifications --port 18810
  python tools/browser_admission_fixture.py <dir> grant notifications notifications.list notifications.mark_read
  python tools/browser_admission_fixture.py <dir> revoke notifications [op ...]
  python tools/browser_admission_fixture.py <dir> update notifications --commit <40 hex>
  python tools/browser_admission_fixture.py <dir> uninstall notifications
  python tools/browser_admission_fixture.py <dir> install notifications --wizard-md <app checkout>/wizard.md

--wizard-md installs that exact manifest (the app's own browser_contract) instead of the synthetic one.

--compose <recipe> (install/update) installs that compose verbatim; a recipe declaring WIZARD_GATEWAY_KEY gets its
per-installation gateway key (<dir>/home/browser-gateway-keys/, <dir>/home/grants/agent-service.json, app env).

--listener-auth (install/update) gives the app a compose that takes WIZARD_APP_CREDENTIAL, so WAM mints its
per-installation listener credential (<dir>/home/browser-app-credentials/, app env in <dir>/runtipi/user-config/).

Every command rewrites <dir>/home/apps.json (and the shared app-data copy) and prints its path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['WIZARD_BROWSER_BOUNDARY'] = '1'
from wam import cli  # noqa: E402

CONTRACT = ('browser_contract:\n  version: 1\n  isolation: opaque\n'
            '  operations: [notifications.list, notifications.mark_read, state.local]\n'
            '  state: {schema_version: 1, persistent_bytes: 65536}\n')


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('dir')
    p.add_argument('action', choices=('install', 'update', 'uninstall', 'grant', 'revoke', 'write'))
    p.add_argument('app', nargs='?', default='notifications')
    p.add_argument('ops', nargs='*')
    p.add_argument('--repo', default='oscarhenrycollins/notifications')
    p.add_argument('--commit', default='c' * 40)
    p.add_argument('--port', type=int, default=18810)
    p.add_argument('--provides', default='notifications')
    p.add_argument('--no-contract', action='store_true')
    p.add_argument('--wizard-md', type=Path, help="install this app checkout's own wizard.md verbatim")
    p.add_argument('--listener-auth', action='store_true')
    p.add_argument('--compose', type=Path, help="install this app checkout's own runtipi compose verbatim "
                                                "(e.g. a WIZARD_GATEWAY_KEY recipe: WAM then mints its gateway key)")
    a = p.parse_args(argv)
    base = Path(a.dir).resolve()
    root, home, rows_file = base / 'runtipi', base / 'home', base / 'rows.json'
    rows = json.loads(rows_file.read_text()) if rows_file.is_file() else []
    d = root / 'apps' / 'wizardapps' / a.app
    if a.action in ('install', 'update'):
        d.mkdir(parents=True, exist_ok=True)
        if a.wizard_md:
            (d / 'wizard.md').write_text(a.wizard_md.read_text())
        else:
            (d / 'wizard.md').write_text(f'---\nid: {a.app}\nname: {a.app}\ntype: service\nversion: 1.0.0\n'
                                         f'provides: [{a.provides}]\nview: {{path: /{a.app}/, port: 8080}}\n'
                                         f"{'' if a.no_contract else CONTRACT}---\n")
        (d / 'config.json').write_text(json.dumps({'port': a.port}))
        (d / '.wizard-source').write_text(json.dumps({'repo': a.repo}))
        (d / '.wizard-commit').write_text(a.commit + '\n')
        if a.listener_auth:
            (d / 'docker-compose.yml').write_text(f'services:\n  {a.app}:\n    environment:\n'
                                                  '      WIZARD_BROWSER_BOUNDARY: ${WIZARD_BROWSER_BOUNDARY:-0}\n'
                                                  '      WIZARD_APP_CREDENTIAL: ${WIZARD_APP_CREDENTIAL:-}\n')
        if a.compose:
            (d / 'docker-compose.yml').write_text(a.compose.read_text())
        old = [r for r in rows if r['info']['urn'] == f'{a.app}:wizardapps']
        if a.action == 'install' or not old:
            # Runtipi row ids are a serial: never reused after uninstall, so a reinstall is a new installation.
            counter = base / 'next-serial'
            serial = int(counter.read_text()) if counter.is_file() else 1
            counter.parent.mkdir(parents=True, exist_ok=True)
            counter.write_text(str(serial + 1))
            rows = [r for r in rows if r not in old] + [{'app': {'id': serial, 'createdAt': 1761955200 + serial,
                                                                  'status': 'running', 'port': a.port},
                                                         'info': {'urn': f'{a.app}:wizardapps'}}]
    elif a.action == 'uninstall':
        rows = [r for r in rows if r['info']['urn'] != f'{a.app}:wizardapps']
        for f in sorted(d.glob('*')) if d.is_dir() else []:
            f.unlink()
        if d.is_dir():
            d.rmdir()
    rows_file.parent.mkdir(parents=True, exist_ok=True)
    rows_file.write_text(json.dumps(rows))
    c = SimpleNamespace(home=home, runtipi_root=root, host_ip=lambda: '127.0.0.1',
                        runtipi=lambda: SimpleNamespace(installed=lambda: json.loads(rows_file.read_text()),
                                                        req=lambda *x, **k: []))
    cli.Config = lambda: c  # the real CLI path, with this synthetic config
    if a.action in ('grant', 'revoke'):
        args = ['browser-grants', a.action, a.app] + [x for op in a.ops for x in ('--operation', op)]
        with open(os.devnull, 'w') as quiet:
            old, sys.stdout = sys.stdout, quiet
            try:
                rc = cli.main(args)
            finally:
                sys.stdout = old
        if rc:
            return rc
    else:
        cli.write_registry(c)
    print(home / 'apps.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
