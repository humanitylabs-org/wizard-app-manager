"""The Wizard Builder bundle runs builder commands itself; every command that changes the box goes to the box's wam.

The bundle (~/.wizard-os/builder/current, first on PATH in builder sessions) is a frozen copy of wam taken when it was
last installed, while the box's own App Manager (~/.local/bin/wam, the copy its API service and timers run) moves on
with every update. On 2 Nov 2026 a `wam install` from a builder session ran an Oct 30 bundle that predated the
owner-gateway fix in links.py and rewrote Messenger's launcher to its raw port. So on a host with a box wam, only the
builder commands below run from the bundle; install, links, apps, updates, views, builder-bundle and everything else
exec the box's wam, and a stale bundle can never write the box's registry, routes or apps. Inside a stock agent container
there is no box wam, and the bundle keeps running every command itself, exactly as before.

The reverse also happens: the box's wam can be older than the bundle (7 Nov 2026: a bundle published from a newer
App Manager; the box's older wam called it outdated and its `builder-bundle install` put the older code back). So
`builder-bundle doctor` and `rollback` run in the bundle, and `builder-bundle install`/`fresh` reach the box's wam only
when its App Manager is not older than the bundle (or with --allow-downgrade).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent
MARKER = 'wizard-builder-bundle-v1'
GUARD = 'WAM_FROM_BUILDER_BUNDLE'   # set on the exec'd box wam: never delegate twice
# Source-level work on the app being built (wam/builder.py, wam/verify.py's throwaway loopback container): never the
# box's registry, routes, apps, views state or
# the builder bundle itself (`builder-skill` installs the bundle and `views` writes ~/.wizard-os/views, so both go to
# the box's wam).
BUILDER_COMMANDS = frozenset({'new', 'check', 'verify', 'install-local', 'update-local', 'publish', 'package-inline'})
# `builder-bundle` actions the bundle runs itself: they read or switch only ~/.wizard-os/builder, with this newer code.
BUNDLE_LOCAL = frozenset({'doctor', 'rollback'})


def bundle_root(source: Path = SOURCE) -> Path | None:
    """This wam's root when it runs from an installed builder bundle release, else None."""
    try:
        if json.loads((source / 'bundle.json').read_text()).get('owner') == MARKER:
            return source
    except (OSError, ValueError, AttributeError):
        pass
    return None


def box_wam(source: Path = SOURCE) -> Path | None:
    """The box's own wam command, when there is one and it is not itself a builder bundle launcher."""
    cmd = Path(os.environ.get('WAM_BOX_BIN') or Path.home() / '.local/bin/wam')
    try:
        if not cmd.is_file() or not os.access(cmd, os.X_OK):
            return None
        if MARKER in cmd.read_bytes()[:4096].decode(errors='replace'):
            return None
        real = cmd.resolve()
        if real == source or source in real.parents:
            return None
    except OSError:
        return None
    return cmd


def box_source(cmd: Path | None = None) -> Path | None:
    """The App Manager source the box's wam runs (its launcher's `cd <dir> && exec ...`), when it can be read."""
    cmd = cmd if cmd is not None else box_wam()
    if cmd is None:
        return None
    try:
        text = cmd.read_bytes()[:4096].decode(errors='replace')
    except OSError:
        return None
    m = re.search(r'''\bcd\s+(?:"([^"$`]+)"|'([^']+)'|([^\s"'$`;&]+))\s*&&\s*exec\b''', text)
    if not m:
        return None
    path = Path(m.group(1) or m.group(2) or m.group(3))
    return path if (path / 'wam').is_dir() else None


def _bundle_args(argv, source, cmd, out):
    """argv for the box's wam, or None after reporting that the box's older code would downgrade the bundle."""
    action = next((a for a in argv[1:] if not a.startswith('-')), None)
    if action not in ('install', 'fresh'):
        return argv
    from .builder_bundle import _keep_newer, stamp
    box = box_source(cmd)
    theirs = stamp(box) if box is not None else None
    kept = _keep_newer(source.parent.parent / 'current', theirs, '--allow-downgrade' in argv)
    if kept is not None:
        kept['box_app_manager'] = str(box) if box is not None else None
        out(json.dumps(kept, indent=2))
        return None
    if theirs == 'pre':   # that wam predates the flag; it would refuse the whole command
        argv = [a for a in argv if a != '--allow-downgrade']
    return argv


def delegate(argv: list[str], source: Path = SOURCE, env=None, execv=os.execv, out=print) -> bool:
    """Exec the box's wam for a box command run from the bundle. Returns False when this wam should run it."""
    env = os.environ if env is None else env
    if env.get(GUARD) or not argv or argv[0] in BUILDER_COMMANDS or argv[0] in ('-h', '--help'):
        return False
    if bundle_root(source) is None:
        return False
    if argv[0] == 'builder-bundle' and next((a for a in argv[1:] if not a.startswith('-')), None) in BUNDLE_LOCAL:
        return False
    cmd = box_wam(source)
    if cmd is None:
        return False
    if argv[0] == 'builder-bundle':
        argv = _bundle_args(argv, source, cmd, out)
        if argv is None:
            return True   # reported; the caller exits without running anything
    # The launcher prepends this frozen release to PYTHONPATH. A Python console
    # script must find its own installed wam, while retaining the owner's paths.
    if 'PYTHONPATH' in env:
        root = source.resolve()
        paths = []
        for entry in env['PYTHONPATH'].split(os.pathsep):
            try:
                if entry and Path(entry).resolve() == root:
                    continue
            except (OSError, RuntimeError, ValueError):
                pass  # An unresolvable unrelated entry is still the owner's to keep.
            paths.append(entry)
        if paths:
            env['PYTHONPATH'] = os.pathsep.join(paths)
        else:
            env.pop('PYTHONPATH')  # No owner entries remain: remove the injected variable.
    env[GUARD] = '1'
    execv(str(cmd), [str(cmd), *argv])
    return True   # only reached with a test double for execv
