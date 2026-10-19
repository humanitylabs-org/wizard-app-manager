"""Read-only numeric host resources. Runtipi credentials never leave this process.

The exact Tailscale Serve route exposes this projection, not the manager API.
No authentication headers supplied by a browser are forwarded to Runtipi.
"""
import math
import shutil

FIELDS = ('cpuLoad', 'percentUsedMemory', 'percentUsed', 'memoryTotal', 'diskUsed', 'diskSize')
PATH = '/api/system/load'


def _host_disk(path):
    """Host disk in whole GiB, rounded like the App Manager does. None when it cannot be read."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    size, free = round(u.total / 1024 ** 3), round(u.free / 1024 ** 3)
    if size <= 0:
        return None
    return {'diskSize': size, 'diskUsed': size - free, 'percentUsed': round((size - free) / size * 100)}


def read(rt, disk_path=None):
    raw = rt.req('GET', '/system/load', timeout=8)
    if not isinstance(raw, dict):
        raise ValueError('invalid resource snapshot')
    out = {k: v if type(v := raw.get(k)) in (int, float) and math.isfinite(v) else None for k in FIELDS}
    # Inside its container the App Manager can see no host filesystem and reports disk as 0 GiB of 0 GiB.
    # This host process can: fill the three disk fields from the Runtipi data disk instead of showing an empty meter.
    if not out.get('diskSize') and disk_path is not None and (disk := _host_disk(disk_path)):
        out.update(disk)
    return out


def publish(host, port, api_port, apply=True):
    """Publish just this GET route beside the shell. Never replace an owner's route."""
    from . import links
    target = f'http://127.0.0.1:{api_port}{PATH}'
    out = {'path': PATH, 'target': target, 'state': 'ready'}
    if not apply:
        return {**out, 'dry_run': True}
    status = links.route_status(links.serve_status(), host, port, PATH, target)
    if status == 'conflict':
        raise ValueError('existing resource Serve route differs; left untouched')
    if status == 'missing':
        r = links._run(['tailscale', 'serve', '--bg', f'--https={port}', f'--set-path={PATH}', target], 60)
        if r.returncode:
            raise ValueError('resource Serve route could not be published')
    if links.route_status(links.serve_status(), host, port, PATH, target) != 'match':
        raise ValueError('resource Serve route readback did not match')
    return out
