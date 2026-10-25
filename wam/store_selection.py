"""Server-side Store selections. Browser identifiers select policy; they never supply policy."""
from __future__ import annotations
import json
import os
import re
import subprocess
from pathlib import Path
from . import index, test_catalog

SHA = re.compile(r"[a-f0-9]{40}")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def resolve(c, app_id, body):
    if body == {}:
        # Backward-compatible old Standard Get. Never let an ID-only caller opt into
        # a private source, first-result repository, update or channel switch.
        if test_catalog.resolve(app_id):
            raise ValueError('private apps require an exact source-qualified selection; refresh the Store')
        rows = index.search(c.index, id=app_id)
        if len(rows) != 1 or rows[0].get('id') != app_id:
            raise ValueError('ambiguous or unavailable Standard app; refresh the Store')
        row = rows[0]
        body = {'source':'standard', 'repo':row.get('repo'),
                'commit':row.get('commit') or row.get('install', {}).get('commit') or '', 'operation':'install'}
    if not isinstance(body, dict) or set(body) != {'source', 'repo', 'commit', 'operation'}:
        raise ValueError('exact source, repo, commit and operation required; refresh the Store')
    source, repo, commit, operation = (body[k] for k in ('source', 'repo', 'commit', 'operation'))
    if not all(isinstance(x, str) for x in (source, repo, commit, operation)) or not ID.fullmatch(source) or not REPO.fullmatch(repo) or (commit and not SHA.fullmatch(commit)) or operation not in ('install', 'update', 'switch'):
        raise ValueError('invalid release selection')
    if source == 'owner-private':
        entry = test_catalog.resolve(app_id)
    elif source == 'standard':
        # Resolve the exact repository, never the index's first matching app id.
        entry = index.resolve(c.index, repo)
    else:
        home = Path(os.environ.get('WIZARD_ALPHA_HOME', Path.home() / '.local/share/wizard-alpha'))
        try:
            cfg = json.loads((home / 'config.json').read_text())
            snap = json.loads((home / 'snapshot.json').read_text())
        except (OSError, ValueError):
            raise ValueError('Alpha source is not subscribed on this Wizard') from None
        if cfg.get('sourceId') != source or snap.get('source', {}).get('id') != source:
            raise ValueError('source is not subscribed')
        entry = next((a for a in snap.get('apps', []) if a.get('id') == app_id), None)
    pin = (entry or {}).get('commit') or (entry or {}).get('install', {}).get('commit') or ''
    if not entry or entry.get('id') != app_id or entry.get('repo') != repo or pin != commit:
        raise ValueError('selected release changed or is not authorized; refresh and reselect')
    if not SHA.fullmatch(pin):
        raise ValueError('selected release has no immutable commit; refresh the Store')
    # Copy only server-resolved metadata, never browser-supplied URLs/commands/images.
    if entry.get('architectures'):
        try:
            test_catalog.check_architecture(entry)
        except RuntimeError as e:
            raise ValueError(str(e)) from None
    return {'source': source, 'repo': repo, 'commit': pin, 'operation': operation, 'entry': entry}


def execute(c, app_id, selection):
    from . import cli, private
    source, entry = selection['source'], selection['entry']
    if app_id == 'hermes-agent':
        from . import hermes_lock
        recipe = hermes_lock.load()['agent_recipe']
        if any(selection[k] != recipe[k] for k in ('repo', 'commit')):
            raise ValueError('Hermes is frozen: Store selection differs from the shared exact lock')
        # Reject before prepare (or the Alpha adapter) can replace staged files or
        # .wizard-selection. The downstream lifecycle guard is too late for this.
        if selection['operation'] != 'install':
            hermes_lock.authorize()
    installed = cli.installed_ids(c.runtipi())
    if selection['operation'] == 'install' and app_id in installed:
        raise ValueError('already installed; refresh and choose an explicit release change')
    if selection['operation'] != 'install' and app_id not in installed:
        raise ValueError('not installed; refresh before installing')
    if app_id in installed:
        try:
            existing = json.loads((c.runtipi_root / 'apps' / cli.STORE / app_id / '.wizard-source').read_text())
        except (OSError, ValueError):
            raise ValueError('installed repository identity unavailable; nothing changed') from None
        if existing.get('repo') != selection['repo']:
            raise ValueError('installed repository conflict; nothing changed')
    if source not in ('standard', 'owner-private'):
        # Use the shipped, owner-local adapter. It revalidates subscription, release assets,
        # installed identity, explicit update policy and authenticated activation receipts.
        from . import store_data
        reg = json.loads((c.home / 'apps.json').read_text())
        sha = store_data.shell_commit(reg)
        if not sha:
            raise ValueError('installed shell has no pinned Alpha adapter')
        module = Path.home() / '.local/share/wizard-store-data' / sha / 'tools/wizard-alpha.mjs'
        node = store_data.find_node()
        if not node or not module.is_file():
            raise ValueError('installed Alpha adapter unavailable; update Wizard')
        argv = [node, str(module), 'install', '--source', source, '--app', app_id, '--commit', selection['commit']]
        if selection['operation'] != 'install':
            argv.append('--update')
        result = subprocess.run(argv, capture_output=True, text=True, timeout=1200)
        if result.returncode:
            try:
                reason = json.loads(result.stderr).get('error', 'Alpha installation failed')
            except ValueError:
                reason = 'Alpha installation failed'
            raise ValueError(reason)
        return {'status': 'running', **json.loads(result.stdout)}
    # An exact local recipe remains under normal Runtipi install/update/backup handling.
    record = private.prepare(c.runtipi_root, cli.STORE, selection['repo'],
                             release=entry if source == 'owner-private' else None,
                             source_pin={'id': app_id, 'repo': selection['repo'], 'commit': selection['commit']} if source == 'standard' else None)
    folder = c.runtipi_root / 'repos' / cli.STORE / 'apps' / app_id
    # The maintained manager respects this owner-local pin instead of replacing it with
    # a later public index response. It is not a remote-catalog field.
    (folder / '.wizard-selection').write_text(json.dumps({'repo': selection['repo'], 'commit': selection['commit']}) + '\n')
    if selection['operation'] == 'install':
        result = cli.install(c, app_id, _record=record)
    else:
        result = cli.app_update(c, app_id)
        cli.write_registry(c)
    installed_dir = c.runtipi_root / 'apps' / cli.STORE / app_id
    actual = json.loads((installed_dir / '.wizard-source').read_text())
    actual_commit = (installed_dir / '.wizard-commit').read_text().strip()
    if (actual.get('repo'), actual_commit) != (selection['repo'], selection['commit']):
        raise ValueError('installed release identity did not match the selection')
    return result
