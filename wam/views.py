"""Customized app views — candidate for wizard-os #30 (proposal spec/VIEWS.md, DECISIONS #52).

One folder per customized app: <home>/views/<app-id>/. No folder means the app's own screen opens
unchanged; nothing is copied at install.

  overlay  The owner's CSS (and optional JS) is added to the app's own HTML on the way to the
           browser. The app is untouched. If an app update removes what the overlay targets, the
           overlay switches itself off and leaves a note.
  own      The owner's copy of the screen source, in git: branch `upstream` holds the app's source
           at the installed version, `main` holds the owner's changes. A build that passes becomes
           the served version; until then the app's own screen keeps working.

App updates land on `upstream` and merge into `main`. A merge conflict or a failed build keeps the
last working version on screen and records exactly what needs deciding (`resolve --keep mine|upstream`).
`revert` puts the app's own screen back; history stays. State lives on disk, so routes survive a
restart of the serving process.

The door (`serve`) is the reference for what the Agent Service mounts same-origin:
  /views/_index.json          which URL the launcher should open for each customized app
  /views/<id>/...             the owner's active build (own mode), else a redirect to /apps/<id>/
  /views/<id>/api/...         forwarded to that app's declared `api` and nowhere else
  /apps/<id>/...              the app itself, with the overlay added when one is active
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import builder

GIT_ENV = {'GIT_AUTHOR_NAME': 'Wizard', 'GIT_AUTHOR_EMAIL': 'wizard@localhost', 'GIT_COMMITTER_NAME': 'Wizard',
           'GIT_COMMITTER_EMAIL': 'wizard@localhost', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_TERMINAL_PROMPT': '0'}
STATIC_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8',
                '.svg': 'image/svg+xml', '.json': 'application/json', '.png': 'image/png', '.jpg': 'image/jpeg',
                '.webp': 'image/webp', '.woff2': 'font/woff2', '.ico': 'image/x-icon'}
CSP = "default-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'self'"
SIMPLE_SELECTOR = re.compile(r'^(#[A-Za-z][\w-]*|\.[A-Za-z][\w-]*|[a-z][a-z0-9]*)$')
KEEP_BUILDS = 3


class ViewError(ValueError):
    pass


# ---------------------------------------------------------------- state ----

def _home(home=None):
    return (Path(home) if home is not None else builder._home()).resolve()


def view_dir(app_id, home=None):
    return _home(home) / 'views' / builder._id(app_id)


def load(app_id, home=None):
    p = view_dir(app_id, home) / 'view.json'
    return json.loads(p.read_text()) if p.is_file() else None


def _save(app_id, home, view):
    view['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    builder._json(view_dir(app_id, home) / 'view.json', view)
    return view


def _require(app_id, home):
    view = load(app_id, home)
    if not view:
        raise ViewError(f'{app_id} is not customized.')
    return view


@contextlib.contextmanager
def _locked(app_id, home, create=False):
    d = view_dir(app_id, home)
    if d.is_symlink():
        raise ViewError(f'views/{app_id} is a symlink; refusing to use it.')
    if not d.is_dir():
        if not create:
            raise ViewError(f'{app_id} is not customized.')
        d.mkdir(parents=True)
    with open(d / '.lock', 'a+') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield d


def _loopback(url):
    u = urlsplit(url)
    if u.scheme != 'http' or u.hostname not in ('127.0.0.1', 'localhost') or not u.port:
        raise ViewError(f'App address must be a loopback http URL, got {url!r}.')
    return f'http://{u.hostname}:{u.port}'


def app_record(app_id, home=None):
    """Where the installed app lives: a staged developer app first, then the Wizard registry."""
    home = _home(home)
    st = home / 'staged-apps' / builder._id(app_id) / 'stage.json'
    if st.is_file():
        r = json.loads(st.read_text())
        image = None
        with contextlib.suppress(Exception):
            image = yaml.safe_load(Path(r['compose']).read_text())['services'][app_id]['image']
        return {'id': app_id, 'source': r['source'], 'upstream': _loopback(r['url']), 'api': r.get('api') or {},
                'mcp': r.get('mcp') or {}, 'view': r.get('view') or {}, 'version': r.get('source_sha256'), 'image': image}
    reg = home / 'apps.json'
    if reg.is_file():
        for a in json.loads(reg.read_text()).get('apps', []):
            if a.get('id') == app_id and (a.get('view') or {}).get('upstream'):
                return {'id': app_id, 'source': a.get('source_dir'), 'upstream': _loopback(a['view']['upstream']),
                        'api': a.get('api') or {}, 'mcp': a.get('mcp') or {}, 'view': a.get('view') or {},
                        'version': a.get('source_sha256') or a.get('version'),
                        'image': a.get('image')}
    raise ViewError(f'{app_id} is not installed on this Wizard.')


# ---------------------------------------------------------------- git ------

def _git(src, *args, check=True):
    r = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false', '-C', str(src), *args],
                       capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    if check and r.returncode:
        raise ViewError(f'git {" ".join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-400:]}')
    return r


def _replace_tree(src, snapshot_root):
    """Make the checked-out branch's files exactly the app source at snapshot_root."""
    for p in src.iterdir():
        if p.name != '.git':
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    for f in builder.source_files(snapshot_root):
        dest = src / f.relative_to(snapshot_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)


def _ensure_main(src):
    """Recover from an interrupted operation: finish nothing half-way, and be on `main`. Owner work is
    always committed on main before any branch switch, so discarding a dirty non-main tree loses nothing."""
    marker = src / '.git' / 'WZ_MERGE'
    if (src / '.git' / 'MERGE_HEAD').exists():
        if marker.exists():
            # A merge Wizard started and never finished (crash): undo it.
            _git(src, 'merge', '--abort', check=False)
        elif _unmerged(src):
            raise ViewError('A merge in src/ is unfinished; finish it (resolve the conflict markers and commit) or abort it, then apply.')
        else:
            # The owner (or agent) merged by hand and resolved everything: keep it.
            _git(src, 'add', '-A')
            _git(src, 'commit', '-q', '--no-edit')
    marker.unlink(missing_ok=True)
    branch = _git(src, 'rev-parse', '--abbrev-ref', 'HEAD', check=False).stdout.strip()
    if branch != 'main':
        _git(src, 'reset', '-q', '--hard', check=False)
        _git(src, 'clean', '-fdq', check=False)
        _git(src, 'checkout', '-q', 'main')


def _unmerged(src):
    out = _git(src, 'diff', '--name-only', '-z', '--diff-filter=U', check=False).stdout
    return [p for p in out.split('\0') if p]


def _owner_paths_ok(src, view_path):
    """Everything the owner changed on main relative to the app (committed or not) is screen source."""
    prefix = view_path.strip('/') + '/'
    changed = [p for p in _git(src, 'diff', '--name-only', '-z', '--no-renames', 'upstream...HEAD').stdout.split('\0') if p]
    outside = [p for p in changed if not p.startswith(prefix)]
    if outside:
        raise ViewError('Only the screen source (' + prefix + ') belongs in a custom view; changed outside it: ' + ', '.join(outside[:5]))


def _commit_owner(src, view_path):
    """Commit the owner's pending edits on main. Only the screen's source may change."""
    status = _git(src, 'status', '--porcelain', '-uall', '--no-renames').stdout.splitlines()
    if not status:
        return False
    prefix = view_path.strip('/').rstrip('/') + '/'
    outside = [line[3:] for line in status if not line[3:].strip('"').startswith(prefix)]
    if outside:
        raise ViewError('Only the screen source (' + prefix + ') belongs in a custom view; changed outside it: ' + ', '.join(outside[:5]))
    _git(src, 'add', '-A')
    _git(src, 'commit', '-q', '-m', 'Owner changes')
    return True


# ---------------------------------------------------------------- build ----

def _run_build(work, view, image):
    """Run the app's declared build command. It is the app author's code, so by default it runs inside
    the app's own image: no network, read-only root, no capabilities, source in and dist out over a pipe
    (no host mounts). WIZARD_VIEWS_BUILD=host is a developer/test opt-in."""
    build = (view.get('build') or '').strip()
    if not hasattr(tarfile, 'data_filter'):
        raise ViewError('Custom views need Python with tarfile data filters (3.12+, 3.11.4+ or 3.10.12+).')
    if not build or build == 'none':
        return work / view['dist'], ''
    where = os.environ.get('WIZARD_VIEWS_BUILD', 'container')
    if where == 'host':
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(work.parent), 'LANG': 'C.UTF-8'}
        try:
            r = subprocess.run(['sh', '-c', build], cwd=work, env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise ViewError('Build took longer than 10 minutes; the previous version stays on screen.')
        if r.returncode:
            raise ViewError('Build failed; the previous version stays on screen.\n' + (r.stdout + r.stderr)[-2000:])
        return work / view['dist'], (r.stdout + r.stderr)[-2000:]
    if where != 'container' or not image:
        raise ViewError('No isolated build image for this app; the screen cannot be rebuilt safely.')
    src_tar = work.parent / 'in.tar'
    with tarfile.open(src_tar, 'w') as tf:
        tf.add(work, arcname='.')
    script = ('set -e; mkdir -p /tmp/w; cd /tmp/w; tar xf -; eval "$WZ_BUILD" >&2; '
              'case "$WZ_DIST" in /*|*..*) echo "bad dist" >&2; exit 2;; esac; tar cf - -C "$WZ_DIST" .')
    cmd = ['docker', 'run', '-i', '--rm', '--pull', 'never', '--network', 'none', '--cpus', '1', '--read-only', '--tmpfs', '/tmp:rw,exec,size=256m',
           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--memory', '512m', '--pids-limit', '256',
           '--user', '1000:1000', '-e', 'HOME=/tmp', '-e', 'WZ_BUILD=' + build, '-e', 'WZ_DIST=' + view['dist'],
           '--entrypoint', 'sh', image, '-c', script]
    try:
        with open(src_tar, 'rb') as stdin:
            r = subprocess.run(cmd, stdin=stdin, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired:
        raise ViewError('Build took longer than 10 minutes; the previous version stays on screen.')
    log = r.stderr.decode('utf-8', 'replace')[-2000:]
    if r.returncode:
        raise ViewError('Build failed; the previous version stays on screen.\n' + log)
    out = work.parent / 'built'
    out.mkdir()
    out_tar = work.parent / 'out.tar'
    out_tar.write_bytes(r.stdout)
    with tarfile.open(out_tar) as tf:
        tf.extractall(out, filter='data')
    return out, log


def _build(d, view, commit, image=None):
    """Build `commit` of the owner's source in a scratch copy; publish dist only if the build passes."""
    out = d / 'builds' / commit
    if (out / '.ok').is_file():
        return out
    (d / 'builds').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=d) as t:
        work = Path(t) / 'work'
        work.mkdir()
        archive = Path(t) / 'src.tar'
        _git(d / 'src', 'archive', '--format=tar', '-o', str(archive), commit)
        with tarfile.open(archive) as tf:
            tf.extractall(work, filter='data')
        dist, log = _run_build(work, view, image)
        dist = dist.resolve()
        if not dist.is_dir() or not dist.is_relative_to(Path(t).resolve()):
            raise ViewError(f"Build output {view['dist']!r} is missing or outside the source.")
        stage = Path(t) / 'dist'
        for p in sorted(dist.rglob('*')):
            if p.is_symlink():
                raise ViewError(f'Build output contains a symlink ({p.relative_to(dist)}); refusing to serve it.')
            if p.is_file():
                dest = stage / p.relative_to(dist)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, dest)
        if not (stage / 'index.html').is_file():
            raise ViewError('Build output has no index.html.')
        (stage / '.ok').write_text(json.dumps({'commit': commit, 'log': log}))
        stage.rename(out)
    builds = sorted((p for p in (d / 'builds').iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    for old in builds[:-KEEP_BUILDS]:
        if old.name != view.get('serving'):
            shutil.rmtree(old, ignore_errors=True)
    return out


# ---------------------------------------------------------------- overlay --

def _read_regular(d, name, limit=128 * 1024):
    """Read views/<id>/overlay/<name> only if it is a regular file (no symlinks anywhere on the
    way) under `limit`. Returns None if absent or unsafe; checked on every read, not just at apply."""
    for p in (d, d / 'overlay'):
        try:
            if not os.path.isdir(p) or os.path.islink(p):
                return None
        except OSError:
            return None
    try:
        fd = os.open(d / 'overlay' / name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > limit:
            return None
        return f.read(limit + 1)[:limit]


def _overlay_requires(d):
    raw = _read_regular(d, 'overlay.json')
    req = json.loads(raw).get('requires', []) if raw else []
    if not isinstance(req, list) or not all(isinstance(s, str) and 0 < len(s) < 120 for s in req):
        raise ViewError('overlay.json "requires" must be a list of CSS selectors.')
    return req


def _missing_selectors(app, requires):
    """Selectors in `requires` that the app's current screen no longer has (simple #id/.class/tag
    checks on the served HTML; anything fancier is checked in the browser by the overlay loader)."""
    try:
        html = _fetch(app['upstream'], '/')[2].decode('utf-8', 'replace')
    except OSError as e:
        raise ViewError(f"Can't reach {app['id']} to check the overlay: {e}")
    missing = []
    for s in requires:
        if not SIMPLE_SELECTOR.match(s):
            continue
        if s[0] == '#':
            hit = re.search(r'''\bid\s*=\s*["']?''' + re.escape(s[1:]) + r'''(["'\s>])''', html)
        elif s[0] == '.':
            hit = re.search(r'''\bclass\s*=\s*["'][^"']*(?<![\w-])''' + re.escape(s[1:]) + r'''(?![\w-])''', html)
        else:
            hit = re.search(r'<' + re.escape(s) + r'[\s>]', html, re.I)
        if not hit:
            missing.append(s)
    return missing


def _check_overlay_files(d):
    o = d / 'overlay'
    for name in ('overlay.css', 'overlay.js', 'overlay.json'):
        p = o / name
        if (p.exists() or p.is_symlink()) and _read_regular(d, name) is None:
            raise ViewError(f'{name} must be a regular file under 128 KB (no symlinks).')
    if _read_regular(d, 'overlay.css') is None:
        raise ViewError('An overlay needs overlay/overlay.css.')


# ---------------------------------------------------------------- lifecycle

def customize(app_id, mode='own', home=None):
    """Start a custom view. Returns where the agent makes the owner's changes."""
    if mode not in ('own', 'overlay'):
        raise ViewError('mode is own or overlay.')
    app = app_record(app_id, home)
    if load(app_id, home):
        raise ViewError(f'{app_id} is already customized; edit it, or use revert / status.')
    try:
        return _customize(app_id, mode, home, app)
    except BaseException:
        with contextlib.suppress(ViewError), _locked(app_id, home) as d:
            if not (d / 'view.json').exists():
                shutil.rmtree(d, ignore_errors=True)
        raise


def _customize(app_id, mode, home, app):
    with _locked(app_id, home, create=True) as d:
        if (d / 'view.json').exists():
            raise ViewError(f'{app_id} is already customized; edit it, or use revert / status.')
        if mode == 'overlay':
            (d / 'overlay').mkdir(exist_ok=True)
            (d / 'overlay' / 'overlay.css').write_text(f'/* Owner changes to {app_id}. Target stable ids/classes and list them in overlay.json. */\n')
            builder._json(d / 'overlay' / 'overlay.json', {'requires': []})
            view = {'schema': 1, 'app': app_id, 'mode': 'overlay', 'base': app['version'], 'active': False,
                    'state': 'editing', 'suspended': False, 'note': None}
            _save(app_id, home, view)
            return {**view, 'edit': str(d / 'overlay')}
        src_meta = app['view'].get('source') if isinstance(app['view'], dict) else None
        if not isinstance(src_meta, dict) or src_meta.get('repo') or not app.get('source'):
            raise ViewError(f'{app_id} does not ship its screen source locally (view.source); use an overlay, or a from-scratch screen on its api.')
        src = d / 'src'
        if src.exists():
            shutil.rmtree(src)
        src.mkdir()
        _git(src, 'init', '-q', '-b', 'upstream')
        _replace_tree(src, Path(app['source']))
        _git(src, 'add', '-A')
        _git(src, 'commit', '-q', '-m', f"App {app_id} at {str(app['version'])[:12]}")
        _git(src, 'checkout', '-q', '-b', 'main')
        view = {'schema': 1, 'app': app_id, 'mode': 'own', 'base': app['version'], 'path': src_meta['path'],
                'build': src_meta.get('build'), 'dist': src_meta['dist'], 'active': False, 'state': 'editing',
                'serving': None, 'pending': None, 'conflicts': [], 'note': None}
        _save(app_id, home, view)
        return {**view, 'edit': str(src / src_meta['path'])}


def apply(app_id, home=None):
    """Build and switch to the owner's current changes. On failure the app's screen (or the last
    working custom version) stays on screen."""
    app = app_record(app_id, home)
    with _locked(app_id, home) as d:
        view = load(app_id, home)
        if not view:
            raise ViewError(f'{app_id} is not customized.')
        if view['mode'] == 'overlay':
            _check_overlay_files(d)
            missing = _missing_selectors(app, _overlay_requires(d))
            if missing:
                raise ViewError("The overlay targets things this app's screen doesn't have: " + ', '.join(missing))
            view.update(active=True, state='active', suspended=False, note=None, base=app['version'])
            return _save(app_id, home, view)
        src = d / 'src'
        _ensure_main(src)
        _commit_owner(src, view['path'])
        merged = _git(src, 'merge-base', '--is-ancestor', 'upstream', 'HEAD', check=False).returncode == 0
        if view['state'] == 'update_conflict' and not merged:
            raise ViewError('An app update is waiting on a decision: resolve --keep mine|upstream first '
                            '(or merge upstream into main by hand, then apply).')
        _owner_paths_ok(src, view['path'])
        commit = _git(src, 'rev-parse', 'HEAD').stdout.strip()
        _build(d, view, commit, app.get('image'))
        if view.get('pending') and merged:
            # The update is on main (merged earlier, or by hand) and now builds: the update is done.
            view.update(base=view['pending'], pending=None, conflicts=[], build_log=None)
        view.update(active=True, serving=commit, state='active', note=None)
        return _save(app_id, home, view)


def _settle(src, keep):
    """Finish a conflicted merge by choosing a side per conflicted path, including modify/delete."""
    side = '2' if keep == 'mine' else '3'   # index stage 2 = main (owner), 3 = upstream (app)
    for path in _unmerged(src):
        stages = {line.split()[2] for line in _git(src, 'ls-files', '-u', '--', path).stdout.splitlines()}
        if side in stages:
            _git(src, 'checkout', '--ours' if keep == 'mine' else '--theirs', '--', path)
            _git(src, 'add', '--', path)
        else:
            _git(src, 'rm', '-q', '--', path)
    _git(src, 'commit', '-q', '--no-edit')


def _merge_and_build(app_id, home, d, view, app, keep=None):
    src = d / 'src'
    args = ['merge', '--no-edit', '-m', f"App update {str(app['version'])[:12]}"]
    if keep:
        args += ['-X', 'ours' if keep == 'mine' else 'theirs']
    _owner_paths_ok(src, view['path'])
    marker = src / '.git' / 'WZ_MERGE'
    marker.write_text(str(app['version']))
    r = _git(src, *args, 'upstream', check=False)
    if r.returncode and keep and (src / '.git' / 'MERGE_HEAD').exists():
        _settle(src, keep)
        marker.unlink(missing_ok=True)
    elif r.returncode:
        conflicts = _unmerged(src)
        _git(src, 'merge', '--abort', check=False)
        _git(src, 'reset', '-q', '--hard', check=False)
        marker.unlink(missing_ok=True)
        if not conflicts:
            raise ViewError('The update could not be merged: ' + (r.stderr or r.stdout).strip()[-300:])
        view.update(state='update_conflict', pending=app['version'], conflicts=conflicts,
                    note=(f"{app_id} updated and its screen changed where yours did ({', '.join(conflicts) or 'see git'}). "
                          'Your version stays on screen. Decide: keep mine, or take upstream.'))
        return _save(app_id, home, view)
    marker.unlink(missing_ok=True)
    commit = _git(src, 'rev-parse', 'HEAD').stdout.strip()
    try:
        _build(d, view, commit, app.get('image'))
    except ViewError as e:
        lines = [x for x in str(e).splitlines() if x.strip()]
        view.update(state='update_build_failed', pending=app['version'], conflicts=[], build_log=str(e)[-2000:],
                    note=f'{app_id} updated but your version no longer builds ({lines[-1][:160] if lines else "build failed"}); '
                         'the last working version stays on screen.')
        return _save(app_id, home, view)
    view.update(base=app['version'], pending=None, conflicts=[], note=None, serving=commit, build_log=None,
                state='active' if view['active'] else ('reverted' if view.get('serving') else 'editing'))
    return _save(app_id, home, view)


def update(app_id, home=None):
    """Bring a custom view onto the app's newly installed version."""
    app = app_record(app_id, home)
    with _locked(app_id, home) as d:
        view = load(app_id, home)
        if not view:
            raise ViewError(f'{app_id} is not customized.')
        if view['mode'] == 'overlay':
            view['base'] = app['version']
            missing = _missing_selectors(app, _overlay_requires(d)) if view['active'] else []
            if missing:
                view.update(suspended=True, state='suspended',
                            note=f"{app_id} updated and your change stopped applying ({', '.join(missing)} is gone). The app opens normally until it's fixed.")
            return _save(app_id, home, view)
        if view['base'] == app['version'] and view['state'] not in ('update_conflict', 'update_build_failed'):
            return {**view, 'update': 'already_current'}
        src = d / 'src'
        _ensure_main(src)
        _commit_owner(src, view['path'])
        if not Path(app['source']).is_dir():
            raise ViewError(f"{app_id}'s installed source is missing; nothing changed.")
        _git(src, 'checkout', '-q', 'upstream')
        try:
            _replace_tree(src, Path(app['source']))
            _git(src, 'add', '-A')
            if _git(src, 'status', '--porcelain').stdout.strip():
                _git(src, 'commit', '-q', '-m', f"App {app_id} at {str(app['version'])[:12]}")
        except BaseException:
            with contextlib.suppress(Exception):
                _ensure_main(src)  # best effort; the original error is the one to report
            raise
        _ensure_main(src)
        return _merge_and_build(app_id, home, d, view, app)


def resolve(app_id, keep, home=None):
    """Decide a conflicting update: keep the owner's lines, or take the app's. Non-conflicting
    changes from the update come in either way."""
    if keep not in ('mine', 'upstream'):
        raise ViewError('keep is mine or upstream.')
    app = app_record(app_id, home)
    with _locked(app_id, home) as d:
        view = load(app_id, home)
        if not view or view.get('state') != 'update_conflict':
            raise ViewError(f'{app_id} has no update waiting on a decision.')
        _ensure_main(d / 'src')
        _commit_owner(d / 'src', view['path'])
        return _merge_and_build(app_id, home, d, view, app, keep=keep)


def revert(app_id, home=None):
    """Put the app's own screen back. The custom version and its history stay."""
    with _locked(app_id, home):
        view = load(app_id, home)
        if not view:
            raise ViewError(f'{app_id} is not customized.')
        view.update(active=False, state='reverted', note=None)
        return _save(app_id, home, view)


def reactivate(app_id, home=None):
    view = _require(app_id, home)
    if view['mode'] == 'overlay':
        return apply(app_id, home)
    with _locked(app_id, home) as d:
        view = load(app_id, home)
        if not (view.get('serving') and (d / 'builds' / view['serving'] / '.ok').is_file()):
            raise ViewError('No working build to switch back to; apply first.')
        pending = view.get('pending') and view['state'] in ('update_conflict', 'update_build_failed')
        view.update(active=True, state=view['state'] if pending else 'active')
        return _save(app_id, home, view)


def open_url(app_id, home=None):
    view = load(app_id, home)
    if view and view['mode'] == 'own' and view['active'] and view.get('serving') and \
            (view_dir(app_id, home) / 'builds' / view['serving'] / '.ok').is_file():
        return f'/views/{app_id}/'
    return f'/apps/{app_id}/'


def index(home=None):
    out = {}
    root = _home(home) / 'views'
    for p in sorted(root.glob('*/view.json')) if root.is_dir() else []:
        app_id = p.parent.name
        if not builder.ID.fullmatch(app_id):
            continue
        v = json.loads(p.read_text())
        out[app_id] = {'open': open_url(app_id, home), 'mode': v['mode'], 'active': v['active'], 'state': v['state'],
                       'customized': True, 'note': v.get('note')}
    return {'schema': 1, 'apps': out}


# ---------------------------------------------------------------- door -----

def _fetch(base, path, method='GET', body=None, headers=None):
    u = urlsplit(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=15)
    try:
        h = {'Host': f'{u.hostname}:{u.port}', **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        return r.status, dict(r.getheaders()), r.read(8 * 1024 * 1024)
    finally:
        c.close()


def overlay_loader(app_id, d):
    """Checks the overlay still matches, then loads the owner's script as its own file so that a
    syntax error, a runtime error or a failed load all switch the overlay off."""
    req = json.dumps(_overlay_requires(d))
    has_js = 'true' if _read_regular(d, 'overlay.js') else 'false'
    return (f'''(() => {{ 'use strict';
  const base = '/views/{app_id}/', requires = {req}, ownerJs = base + '_overlay-owner.js';
  let done = false;
  const off = why => {{ if (done) return; done = true;
    document.querySelectorAll('[data-wz-overlay]').forEach(n => n.remove());
    fetch(base + '_overlay-report', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify(why) }}).catch(() => {{}}); }};
  addEventListener('error', e => {{ if (String(e.filename || '').endsWith(ownerJs)) off({{ error: String(e.message || 'script error').slice(0, 200) }}); }});
  const run = () => {{
    const missing = requires.filter(s => {{ try {{ return !document.querySelector(s); }} catch (e) {{ return true; }} }});
    if (missing.length) return off({{ missing }});
    if (!{has_js}) return;
    const s = document.createElement('script');
    s.src = ownerJs; s.dataset.wzOverlay = '';
    s.onerror = () => off({{ error: 'overlay script failed to load' }});
    document.head.appendChild(s);
  }};
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
}})();
''').encode()


class Door(BaseHTTPRequestHandler):
    server_version = 'WizardViews/0.1'
    home: Path = None
    hosts: set = set()
    origins: set = set()

    def log_message(self, format, *args):
        pass

    def _send(self, code, body: 'bytes | dict | list' = b'', ctype='application/json', extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _refused(self, write):
        if (self.headers.get('Host') or '').lower() not in self.hosts:
            self._send(403, {'error': 'Unknown host.'})
            return True
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' and self.headers.get('Sec-Fetch-Mode') != 'navigate':
            self._send(403, {'error': 'Cross-site requests refused.'})
            return True
        if write:
            origin = (self.headers.get('Origin') or '').lower()
            if (origin and origin not in self.origins) or (not origin and self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                self._send(403, {'error': 'Cross-origin writes refused.'})
                return True
        return False

    def do_GET(self):
        self._route()

    def do_HEAD(self):
        self._route()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def _route(self):
        write = self.command not in ('GET', 'HEAD')
        if self._refused(write):
            return
        u = urlsplit(self.path)
        path = u.path
        if path == '/views/_index.json' and not write:
            return self._send(200, index(self.home))
        m = re.fullmatch(r'/(views|apps)/([a-z0-9][a-z0-9-]{1,40})(/.*)?', path)
        if not m:
            return self._send(404, {'error': 'Not found.'})
        kind, app_id, rest = m.group(1), m.group(2), m.group(3) or ''
        if any(seg in ('.', '..') for seg in rest.split('/')) or '//' in rest or '\\' in rest or '%' in rest:
            return self._send(404, {'error': 'Not found.'})
        if not rest:
            return self._send(308, b'', 'text/plain', {'Location': f'/{kind}/{app_id}/' + (('?' + u.query) if u.query else '')})
        try:
            app = app_record(app_id, self.home)
        except ViewError as e:
            return self._send(404, {'error': str(e)})
        view = load(app_id, self.home)
        d = view_dir(app_id, self.home)
        if kind == 'apps':
            # The app's own screen and its declared API. Its MCP and anything else that writes stay
            # behind the agent's authenticated door, not the browser's.
            api_path = ((app['api'] if isinstance(app['api'], dict) else {}).get('path') or '/api').rstrip('/')
            in_api = rest == api_path or rest.startswith(api_path + '/')
            mcp = ((app.get('mcp') if isinstance(app.get('mcp'), dict) else {}).get('path') or '/mcp').rstrip('/')
            if (write and not in_api) or rest == mcp or rest.startswith(mcp + '/'):
                return self._send(404, {'error': 'Not found.'})
            return self._proxy(app, rest, u.query, view, d)
        # /views/<id>/...
        if rest in ('/_overlay.css', '/_overlay-owner.js') and view and view['mode'] == 'overlay' and not write:
            data = _read_regular(d, 'overlay.css' if rest.endswith('.css') else 'overlay.js')
            if data is None:
                return self._send(404, {'error': 'Not found.'})
            return self._send(200, data, 'text/css; charset=utf-8' if rest.endswith('.css') else 'text/javascript; charset=utf-8')
        if rest == '/_overlay.js' and view and view['mode'] == 'overlay':
            return self._send(200, overlay_loader(app_id, d), 'text/javascript; charset=utf-8')
        if rest == '/_overlay-report' and write and view and view['mode'] == 'overlay':
            return self._overlay_report(app_id, d)
        if (rest == '/api' or rest.startswith('/api/')) and view and view['mode'] == 'own':
            api_path = ((app['api'] if isinstance(app['api'], dict) else {}).get('path') or '/api').rstrip('/')
            target = api_path + rest[len('/api'):]
            mcp = ((app.get('mcp') if isinstance(app.get('mcp'), dict) else {}).get('path') or '/mcp').rstrip('/')
            if target == mcp or target.startswith(mcp + '/'):
                return self._send(404, {'error': 'Not found.'})
            return self._forward(app, target, u.query)
        if view and view['mode'] == 'own' and view['active'] and view.get('serving') and not write:
            return self._static(d / 'builds' / view['serving'], rest)
        if write:
            return self._send(404, {'error': 'Not found.'})
        return self._send(307, b'', 'text/plain', {'Location': f'/apps/{app_id}/'})

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not 0 <= n <= 1024 * 1024:
            raise ViewError('Request too large.')
        return self.rfile.read(n) if n else None

    def _forward(self, app, path, query):
        try:
            body = self._body() if self.command not in ('GET', 'HEAD') else None
        except ViewError as e:
            return self._send(413, {'error': str(e)})
        headers = {k: self.headers[k] for k in ('Content-Type', 'Accept') if self.headers.get(k)}
        try:
            status, h, data = _fetch(app['upstream'], path + (('?' + query) if query else ''), self.command, body, headers)
        except OSError:
            return self._send(502, {'error': f"{app['id']} is not responding."})
        self._relay(status, h, data)

    def _relay(self, status, h, data):
        extra = {k: v for k, v in h.items() if k.lower() in ('location', 'content-security-policy')}
        self._send(status, data, h.get('Content-Type', 'application/octet-stream'), extra)

    def _proxy(self, app, rest, query, view, d):
        try:
            body = self._body() if self.command not in ('GET', 'HEAD') else None
        except ViewError as e:
            return self._send(413, {'error': str(e)})
        headers = {k: self.headers[k] for k in ('Content-Type', 'Accept') if self.headers.get(k)}
        try:
            status, h, data = _fetch(app['upstream'], rest + (('?' + query) if query else ''), self.command, body, headers)
        except OSError:
            return self._send(502, {'error': f"{app['id']} is not responding."})
        ctype = h.get('Content-Type', '')
        if (status == 200 and ctype.startswith('text/html') and view and view['mode'] == 'overlay'
                and view['active'] and not view.get('suspended')):
            tags = (f'<link rel="stylesheet" data-wz-overlay href="/views/{app["id"]}/_overlay.css">'
                    f'<script defer data-wz-overlay src="/views/{app["id"]}/_overlay.js"></script>').encode()
            i = data.lower().find(b'</head>')
            data = data[:i] + tags + data[i:] if i >= 0 else tags + data
        self._relay(status, h, data)

    def _static(self, root, rest):
        rel = 'index.html' if rest in ('', '/') else rest.lstrip('/')
        parts = rel.split('/')
        if any(p in ('', '.', '..') or p.startswith('.') for p in parts):
            return self._send(404, {'error': 'Not found.'})
        f = root.joinpath(*parts)
        kind = STATIC_TYPES.get(f.suffix.lower())
        if not kind or f.is_symlink() or not f.is_file() or not f.resolve().is_relative_to(root.resolve()):
            return self._send(404, {'error': 'Not found.'})
        self._send(200, f.read_bytes(), kind, {'Content-Security-Policy': CSP})

    def _overlay_report(self, app_id, d):
        try:
            why = json.loads(self._body() or b'{}')
        except (ValueError, ViewError):
            why = {}
        missing = [str(s)[:120] for s in (why.get('missing') or [])][:10]
        error = str(why.get('error') or '')[:200]
        with _locked(app_id, self.home):
            view = load(app_id, self.home)
            if view and view['mode'] == 'overlay' and view['active'] and not view.get('suspended'):
                what = ('it no longer finds ' + ', '.join(missing)) if missing else ('its script failed: ' + error)
                view.update(suspended=True, state='suspended', note=f"Your change to {app_id} stopped applying ({what}). The app opens normally until it's fixed.")
                _save(app_id, self.home, view)
        self._send(204)


def serve(home=None, port=18900, bind='127.0.0.1', hosts=(), origins=()):
    loop = {f'127.0.0.1:{port}', f'localhost:{port}'}
    handler = type('BoundDoor', (Door,), {'home': _home(home), 'hosts': loop | {h.lower() for h in hosts},
                                          'origins': {'http://' + h for h in loop} | {o.lower() for o in origins}})
    return ThreadingHTTPServer((bind, port), handler)


# ---------------------------------------------------------------- CLI ------

def main(argv=None):
    ap = argparse.ArgumentParser(prog='wam views', description='Customize how an installed app looks, and keep it through updates.')
    sub = ap.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('customize'); c.add_argument('app'); c.add_argument('--mode', choices=['own', 'overlay'], default='own')
    for name in ('apply', 'update', 'revert', 'reactivate', 'status'):
        sub.add_parser(name).add_argument('app')
    r = sub.add_parser('resolve'); r.add_argument('app'); r.add_argument('--keep', choices=['mine', 'upstream'], required=True)
    sub.add_parser('index')
    s = sub.add_parser('serve'); s.add_argument('--port', type=int, default=18900); s.add_argument('--bind', default='127.0.0.1')
    s.add_argument('--trust-host', action='append', default=[]); s.add_argument('--trust-origin', action='append', default=[])
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'serve':
            httpd = serve(port=a.port, bind=a.bind, hosts=a.trust_host, origins=a.trust_origin)
            print(f'views door on {a.bind}:{a.port} for {_home()}', flush=True)
            httpd.serve_forever()
            return 0
        if a.cmd == 'customize':
            out = customize(a.app, a.mode)
        elif a.cmd == 'resolve':
            out = resolve(a.app, a.keep)
        elif a.cmd == 'index':
            out = index()
        elif a.cmd == 'status':
            out = {**(load(a.app) or {'app': a.app, 'customized': False}), 'open': open_url(a.app)}
        else:
            out = {'apply': apply, 'update': update, 'revert': revert, 'reactivate': reactivate}[a.cmd](a.app)
        print(json.dumps(out, indent=2))
        return 0
    except (ViewError, OSError, subprocess.CalledProcessError) as e:
        print(f'views: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
