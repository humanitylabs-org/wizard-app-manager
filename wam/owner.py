"""Owner identity belongs to the App Manager, never to an agent-readable proof token."""
from __future__ import annotations
import ipaddress
import json
from pathlib import Path


def check(handler, *, post=False) -> str | None:
    from . import store_install as si
    try:
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return 'owner only: loopback peer required'
    except ValueError:
        return 'owner only: invalid peer'
    # The same listener as the Store (7791). Other listener ports need their actual
    # local port when matching /proc/net/tcp, not an identity-header-only shortcut.
    local = getattr(getattr(handler, "server", None), "server_address", (None, si.PORT))[1]
    uid = si.peer_uid(handler.client_address[1]) if local == si.PORT else si.peer_uid(handler.client_address[1], local)
    if uid != 0:
        return 'only reachable through tailscale serve (root-owned peer required)'
    owner = si.owner_login()  # includes an explicit tagged-node refusal
    pin = Path(handler.cfg.home) / 'owner-login'
    if pin.is_file() and pin.read_text().strip().lower() != owner.lower():
        return 'owner login changed; an administrator must review the owner pin'
    if not owner or (handler.headers.get('Tailscale-User-Login') or '').lower() != owner.lower():
        return 'owner only'
    if post:
        lp = Path(handler.cfg.home) / 'links.json'
        host = (json.loads(lp.read_text()) if lp.is_file() else {}).get('host')
        return si.check_request(handler.headers, owner, si.served_origins(host) if host else set())
    return None


def execute(c, path: str, body: dict):
    """Finite owner actions, shared by bearer API and owner Store listener."""
    from . import cli, host_policy, network, updates
    if path == '/api/updates':
        if set(body) == {'channel'}:
            return updates.set_channel(str(body['channel']))
        if set(body) == {'mode'}:
            return updates.set_mode(str(body['mode']))
        raise ValueError('send mode or channel')
    if path == '/api/updates/apply':
        return updates.apply()
    if path == '/api/network':
        action, app_id = body.get('action'), str(body.get('app', ''))
        if action not in ('allow', 'deny', 'off', 'on'):
            raise ValueError('send action: allow, deny, off or on')
        if action == 'allow':
            # Network allow no longer grants an unversioned approval. It must refer
            # to the exact pending recipe already reviewed by the owner.
            host_policy.approve(c.home, app_id, str(body.get('sha256', '')))
            network.set_host_access_allowed(c.home, app_id, True)
        elif action == 'deny':
            with host_policy.LOCK:
                state = host_policy._read(c.home)
                state.get('approvals', {}).pop(app_id, None)
                host_policy._write(c.home, state)
            network.set_host_access_allowed(c.home, app_id, False)
        else:
            network.set_enabled(c.home, action == 'on')
        return network.reconcile(c)
    if path == '/api/grants':
        app_id, target = str(body.get('app', '')), str(body.get('to', ''))
        import re
        if not host_policy.ID.fullmatch(app_id) or not re.fullmatch(r'(?:slot:)?[a-z0-9][a-z0-9-]{1,40}', target):
            raise ValueError('invalid app or slot id')
        if body.get('action') not in ('grant', 'revoke'):
            raise ValueError('send grant or revoke')
        result = c.runtipi().req('POST', f'/apps/{app_id}:wizardapps/app-grants',
                                 {'to': target, 'approve': body['action'] == 'grant'})
        cli.network_step(c)
        cli.write_registry(c)
        return result
    if path == '/api/backup/import':
        from .backup.box import Box
        from .backup import restore, cli as backup_cli, schedule
        from .backup.filesystem import Path as BackupPath
        box = Box()
        file = BackupPath(str(body.get('file', '')))
        if not file.is_absolute():
            raise ValueError('absolute backup file required')
        keys = backup_cli._keys_to_open(box, None, file)
        with schedule.lock(box):
            return restore.import_bundle(box, file, keys, yes=True)
    raise ValueError('unknown owner action')


PATHS = {'/api/grants', '/api/network', '/api/updates', '/api/updates/apply', '/api/backup/import'}
