"""OS-owned, content-addressed builder distribution; no Hermes core patches.

The same complete source bundle executes on the host (including SSH terminals)
and in stock agent containers. Generated app skills are never used as storage.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

import yaml

SOURCE = Path(__file__).resolve().parent.parent
SCHEMA = 1
MARKER = 'wizard-builder-bundle-v1'
RUNTIME = '/opt/wizard-builder'


def _files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'bundle.json'
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _switch(link, target):
    tmp = link.with_name('.' + link.name + f'-{os.getpid()}')
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target, target_is_directory=True)
    tmp.replace(link)


def install(os_home, source=SOURCE):
    """Publish an immutable complete release, then atomically activate it.

    Retain earlier releases for rollback/in-flight readers. Never delete custom
    skills or the generated app registry. Refuse unmanaged path collisions.
    """
    home, source = Path(os_home).resolve(), Path(source).resolve()
    base = home / 'builder'
    if base.is_symlink() or (base.exists() and (not (base / '.owner').is_file() or (base / '.owner').read_text().strip() != MARKER)):
        raise ValueError(f'Unmanaged builder destination: {base}; preserving it')
    skill = home / 'system-skills' / 'wizard-builder'
    expected = base / 'current' / 'system-skills' / 'wizard-builder'
    if skill.exists() or skill.is_symlink():
        if not skill.is_symlink() or os.readlink(skill) != str(expected):
            raise ValueError(f'Unmanaged wizard-builder skill: {skill}; preserving it')
    for name in ('wam', 'templates/hello-wizard', 'system-skills/wizard-builder', 'wizard-views'):
        if not (source / name).is_dir():
            raise ValueError(f'Incomplete source bundle: missing {name}; reinstall from a complete source release')
    base.mkdir(parents=True, exist_ok=True)
    _atomic(base / '.owner', MARKER + '\n')
    # Non-login SSH/local terminal shells often omit the installer's venv/PATH.
    # Keep this host-specific binding outside the portable release; containers
    # deliberately select their own stock Hermes interpreter instead.
    _atomic(base / 'python', sys.executable + '\n')
    releases = base / 'releases'
    releases.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=base) as td:
        stage = Path(td)
        stage.chmod(0o755)
        for name in ('wam', 'templates', 'system-skills', 'wizard-views'):
            shutil.copytree(source / name, stage / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(source / 'LICENSE', stage / 'LICENSE')
        (stage / 'bin').mkdir()
        launcher = stage / 'bin' / 'wam'
        launcher.write_text('#!/bin/sh\n# wizard-builder-bundle-v1\n'
                            'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
                            'export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"\n'
                            'export PYTHONDONTWRITEBYTECODE=1\n'
                            'PY=python3\n'
                            'if [ -x /opt/hermes/.venv/bin/python ]; then\n'
                            '  PY=/opt/hermes/.venv/bin/python\n'
                            'elif [ -r "$ROOT/../../python" ]; then\n'
                            '  IFS= read -r HOST_PY < "$ROOT/../../python"\n'
                            '  [ ! -x "$HOST_PY" ] || PY=$HOST_PY\n'
                            'fi\n'
                            'exec "$PY" -m wam "$@"\n')
        launcher.chmod(0o755)
        files = _files(stage)
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        manifest = {'owner': MARKER, 'schema': SCHEMA, 'sha256': digest, 'files': files}
        (stage / 'bundle.json').write_text(json.dumps(manifest, indent=2) + '\n')
        dest = releases / digest
        preserved = None
        if dest.exists():
            try:
                valid = _files(dest) == files and json.loads((dest / 'bundle.json').read_text()) == manifest
            except (OSError, ValueError):
                valid = False
            if not valid:
                preserved = str(dest)
                repairs = sorted(releases.glob(digest + '-repair-*'))
                dest = next((p for p in repairs if (p / 'bundle.json').is_file()
                             and (p / 'bundle.json').read_text() == (stage / 'bundle.json').read_text()
                             and _files(p) == files), releases / (digest + '-repair-' + uuid.uuid4().hex[:8]))
        if not dest.exists():
            shutil.copytree(stage, dest)
    current = base / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError(f'Unmanaged builder current: {current}')
    _switch(current, Path('releases') / dest.name)
    skill.parent.mkdir(parents=True, exist_ok=True)
    if not skill.is_symlink():
        _switch(skill, expected)
    bindir = home / 'bin'
    bindir.mkdir(exist_ok=True)
    cmd = bindir / 'wam'
    # A wrapper rather than a symlink keeps dirname independent of invocation.
    import shlex
    script = '#!/bin/sh\n# ' + MARKER + '\nexec ' + shlex.quote(str(base / 'current/bin/wam')) + ' "$@"\n'
    if cmd.exists() and MARKER not in cmd.read_text():
        raise ValueError(f'Unmanaged command: {cmd}; preserving it')
    _atomic(cmd, script)
    cmd.chmod(0o755)
    return {'state': 'installed', 'sha256': digest, 'skill': str(skill / 'SKILL.md'),
            'command': str(cmd), 'system_skills': str(skill.parent), 'schema': SCHEMA,
            'release': dest.name, 'preserved_modified_release': preserved}


def rollback(os_home, revision):
    """Select only a verified retained release; never delete source/user data."""
    if not revision or not re.fullmatch(r'[0-9a-f]{64}(?:-repair-[0-9a-f]{8})?', revision):
        raise ValueError('Expected exact retained release id from an install receipt')
    base = Path(os_home) / 'builder'
    dest = base / 'releases' / revision
    manifest = json.loads((dest / 'bundle.json').read_text())
    files = _files(dest)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if (manifest.get('schema') != SCHEMA or manifest.get('owner') != MARKER
            or files != manifest.get('files') or digest != manifest.get('sha256')):
        raise ValueError('Retained release is incompatible or modified; rollback refused')
    _switch(base / 'current', Path('releases') / revision)
    return {'state': 'rolled_back', 'sha256': digest, 'release': revision}


def check_collisions(hermes_home, os_home, cfg=None, runtime=None):
    home, osh = Path(hermes_home), Path(os_home).resolve()
    cfg = cfg if cfg is not None else (yaml.safe_load((home / 'config.yaml').read_text()) or {} if (home / 'config.yaml').exists() else {})
    if not isinstance(cfg, dict) or not isinstance(cfg.get('skills', {}), dict):
        raise ValueError('Expected config/skills mapping; preserving config')
    dirs = (cfg.get('skills') or {}).get('external_dirs') or []
    if not isinstance(dirs, list):
        raise ValueError('skills.external_dirs must be a list; preserving config')
    allowed = {(osh / 'system-skills').resolve()}
    if runtime:
        allowed.add(Path(runtime))
    for root in [home / 'skills', *[Path(d).expanduser() for d in dirs]]:
        if root.resolve() in allowed:
            continue
        if root.is_dir():
            named = root / 'wizard-builder'
            if named.exists() or named.is_symlink():
                raise ValueError(f'wizard-builder name collision at {named}; preserving owner skill')
            skills = list(root.rglob('SKILL.md'))
            skills += [p / 'SKILL.md' for p in root.rglob('*')
                       if p.is_symlink() and (p / 'SKILL.md').is_file()]
            for p in skills:
                data = p.read_text()
                try:
                    meta = yaml.safe_load(data.split('---', 2)[1]) if data.startswith('---') else {}
                except (IndexError, yaml.YAMLError):
                    meta = {}
                if p.parent.name == 'wizard-builder' or (isinstance(meta, dict) and meta.get('name') == 'wizard-builder'):
                    raise ValueError(f'wizard-builder name collision at {p}; preserving owner skill')
    return dirs


def register(hermes_home, os_home, runtime=None):
    home = Path(hermes_home)
    cfgp = home / 'config.yaml'
    cfg = yaml.safe_load(cfgp.read_text()) or {} if cfgp.exists() else {}
    dirs = check_collisions(home, os_home, cfg, runtime)
    path = runtime or str(Path(os_home).resolve() / 'system-skills')
    if path not in dirs:
        cfg.setdefault('skills', {})['external_dirs'] = [*dirs, path]
        _atomic(cfgp, yaml.safe_dump(cfg, sort_keys=False))
    return path


def fresh(c, app_id='hermes-agent', store='wizardapps'):
    """Prepare the stock Runtipi Hermes data directory and additive override.

    Does not replace the terminal backend: VM factory SSH still runs host tools;
    local terminals use the read-only mounted bundle and stock Python environment.
    """
    receipt = install(c.home)
    data = c.runtipi_root / 'app-data' / store / app_id / 'data'
    override = c.runtipi_root / 'user-config' / store / app_id / 'docker-compose.yml'
    before = (override.read_bytes() if override.exists() else None,
              (data / 'config.yaml').read_bytes() if (data / 'config.yaml').exists() else None)
    doc = yaml.safe_load(override.read_text()) or {} if override.exists() else {}
    service = doc.setdefault('services', {}).setdefault(app_id + '-engine', {})
    mount = {'type': 'bind', 'source': str(Path(c.home).resolve() / 'builder'), 'target': RUNTIME, 'read_only': True}
    vols = service.setdefault('volumes', [])
    for v in vols:
        target = v.get('target') if isinstance(v, dict) else str(v).split(':')[1] if ':' in str(v) else ''
        if target == RUNTIME and v != mount:
            raise ValueError(f'Builder mount collision in {override}; preserving override')
    env = service.setdefault('environment', {})
    if not isinstance(env, dict):
        raise ValueError(f'Expected environment mapping in {override}; preserving override')
    if env.get('WIZARD_BUILDER_ROOT', RUNTIME) != RUNTIME:
        raise ValueError(f'Builder environment collision in {override}')
    register(data, c.home, RUNTIME + '/current/system-skills')
    if mount not in vols:
        vols.append(mount)
    env['WIZARD_BUILDER_ROOT'] = RUNTIME
    _atomic(override, yaml.safe_dump(doc, sort_keys=False))
    after = (override.read_bytes(), (data / 'config.yaml').read_bytes())
    pending = Path(c.home) / 'builder/restart-required'
    if before != after:
        _atomic(pending, 'Builder discovery/mount changed; restart stock Hermes\n')
    return {**receipt, 'hermes_home': str(data), 'override': str(override), 'restart_required': pending.exists()}


def health(os_home, hermes_home=None, runtime=None):
    rows = []
    base = Path(os.environ.get('WIZARD_BUILDER_ROOT') or Path(os_home) / 'builder')
    try:
        manifest = json.loads((base / 'current/bundle.json').read_text())
        if manifest.get('schema') != SCHEMA or manifest.get('owner') != MARKER:
            raise ValueError('incompatible bundle schema/owner')
        files = _files(base / 'current')
        if files != manifest['files']:
            raise ValueError('bundle missing or modified files')
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        if digest != manifest['sha256']:
            raise ValueError('bundle digest mismatch')
        p = subprocess.run([str(base / 'current/bin/wam'), 'new', '--help'], capture_output=True, text=True, timeout=15)
        if p.returncode:
            raise ValueError('tooling unavailable (Python 3.10+/PyYAML required): ' + p.stderr[-300:])
        rows.append(('Wizard builder bundle and host tooling', True, digest))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        rows.append(('Wizard builder bundle and host tooling', False, str(e) + '; repair: wam builder-bundle install'))
    if hermes_home:
        try:
            cfg = yaml.safe_load((Path(hermes_home) / 'config.yaml').read_text()) or {}
            dirs = check_collisions(hermes_home, os_home, cfg, runtime)
            want = runtime or str(Path(os_home).resolve() / 'system-skills')
            rows.append(('Hermes discovers wizard-builder', want in dirs, want))
        except (OSError, ValueError, yaml.YAMLError) as e:
            rows.append(('Hermes discovers wizard-builder', False, str(e)))
    return rows


def runtime_health(c):
    """Probe the real stock container's configured terminal route, read-only.

    Never claim host success proves a remote terminal. No backend/key changes.
    Custom remote workers must provision WAM there; doctor names that failure.
    """
    label = 'Wizard builder agent terminal reachability'
    try:
        data = c.runtipi_root / 'app-data/wizardapps/hermes-agent/data'
        cfg = yaml.safe_load((data / 'config.yaml').read_text()) or {}
        terminal = cfg.get('terminal') or {}
        p = subprocess.run(['docker', 'ps', '-q', '--filter',
                            'label=com.docker.compose.service=hermes-agent-engine'],
                           capture_output=True, text=True, timeout=15, check=True)
        ids = p.stdout.split()
        if len(ids) != 1:
            raise ValueError('expected one running stock Hermes engine; found ' + str(len(ids)))
        backend = terminal.get('backend', 'local')
        if backend == 'local':
            command = [RUNTIME + '/current/bin/wam', 'new', '--help']
        elif backend == 'ssh':
            host, user = terminal.get('ssh_host'), terminal.get('ssh_user')
            if not host or not user:
                raise ValueError('SSH terminal has no configured host/user')
            command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
                       '-p', str(terminal.get('ssh_port', 22))]
            if terminal.get('ssh_key'):
                command += ['-i', str(terminal['ssh_key'])]
            command += [str(user) + '@' + str(host), '"$HOME/.wizard-os/bin/wam" new --help']
        else:
            raise ValueError(f'backend {backend!r} needs builder provisioning on its execution worker')
        p = subprocess.run(['docker', 'exec', '-u', '1000:1000', ids[0], *command],
                           capture_output=True, text=True, timeout=20)
        if p.returncode:
            raise ValueError(f'{backend} terminal cannot execute builder: {p.stderr[-250:]}')
        return [(label, True, f'{backend}: real engine tooling probe passed')]
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return [(label, False, str(e) + '; prepare: wam builder-bundle fresh; restart agent to adopt mount; SSH requires its configured trusted host/key')]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='wam builder-bundle')
    ap.add_argument('action', choices=['install', 'doctor', 'fresh', 'rollback'])
    ap.add_argument('--revision', help='rollback: exact retained release id from install receipt')
    ap.add_argument('--hermes-home', type=Path)
    ap.add_argument('--restart', action='store_true', help='fresh only: adopt changed discovery/mount through Runtipi')
    a = ap.parse_args(argv)
    from .registry import home
    try:
        if a.action == 'doctor':
            rows = health(home(), a.hermes_home)
            print(json.dumps(rows, indent=2))
            return 0 if all(r[1] for r in rows) else 1
        if a.action == 'rollback':
            out = rollback(home(), a.revision)
        elif a.action == 'fresh':
            from .cli import Config
            c = Config()
            out = fresh(c)
            if a.restart and out['restart_required']:
                rt = c.runtipi()
                rt.req('POST', '/app-lifecycle/hermes-agent:wizardapps/stop', {}, timeout=300)
                rt.wait_status('hermes-agent:wizardapps', {'stopped'}, timeout=600)
                rt.req('POST', '/app-lifecycle/hermes-agent:wizardapps/start', {}, timeout=300)
                rt.wait_status('hermes-agent:wizardapps', {'running'}, timeout=900)
                (c.home / 'builder/restart-required').unlink(missing_ok=True)
                out['restart_required'] = False
                out['restarted'] = True
        else:
            if a.hermes_home:
                check_collisions(a.hermes_home, home())
            out = install(home())
            if a.hermes_home:
                out['registered'] = register(a.hermes_home, home())
        print(json.dumps(out, indent=2))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f'builder-bundle: {e}', file=__import__('sys').stderr)
        return 1
