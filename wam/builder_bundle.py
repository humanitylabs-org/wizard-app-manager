"""OS-owned, content-addressed builder distribution; no Hermes core patches.

The same complete source bundle executes on the host (including SSH terminals)
and in stock agent containers. Generated app skills are never used as storage.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

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
        os.chmod(tmp, 0o644)
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
    if base.is_symlink() or (base.exists() and not (base / '.owner').is_file()):
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
    releases = base / 'releases'
    releases.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=base) as td:
        stage = Path(td)
        for name in ('wam', 'templates', 'system-skills', 'wizard-views'):
            shutil.copytree(source / name, stage / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(source / 'LICENSE', stage / 'LICENSE')
        (stage / 'bin').mkdir()
        launcher = stage / 'bin' / 'wam'
        launcher.write_text('#!/bin/sh\n# wizard-builder-bundle-v1\n'
                            'ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)\n'
                            'export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"\n'
                            'export PYTHONDONTWRITEBYTECODE=1\n'
                            'PY=python3\n'
                            '[ ! -x /opt/hermes/.venv/bin/python ] || PY=/opt/hermes/.venv/bin/python\n'
                            'exec "$PY" -m wam "$@"\n')
        launcher.chmod(0o755)
        files = _files(stage)
        digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        manifest = {'owner': MARKER, 'schema': SCHEMA, 'sha256': digest, 'files': files}
        (stage / 'bundle.json').write_text(json.dumps(manifest, indent=2) + '\n')
        dest = releases / digest
        if dest.exists():
            if _files(dest) != files:
                raise ValueError(f'Installed release was modified: {dest}; preserving it')
        else:
            shutil.copytree(stage, dest)
    current = base / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError(f'Unmanaged builder current: {current}')
    _switch(current, Path('releases') / digest)
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
            'command': str(cmd), 'system_skills': str(skill.parent), 'schema': SCHEMA}


def check_collisions(hermes_home, os_home, cfg=None, runtime=None):
    home, osh = Path(hermes_home), Path(os_home).resolve()
    cfg = cfg if cfg is not None else yaml.safe_load((home / 'config.yaml').read_text()) or {}
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
            for p in root.rglob('SKILL.md'):
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
    doc = yaml.safe_load(override.read_text()) or {} if override.exists() else {}
    service = doc.setdefault('services', {}).setdefault(app_id + '-engine', {})
    mount = {'type': 'bind', 'source': str(Path(c.home).resolve() / 'builder'), 'target': RUNTIME, 'read_only': True}
    vols = service.setdefault('volumes', [])
    for v in vols:
        target = v.get('target') if isinstance(v, dict) else str(v).split(':')[1] if ':' in str(v) else ''
        if target == RUNTIME and v != mount:
            raise ValueError(f'Builder mount collision in {override}; preserving override')
    register(data, c.home, RUNTIME + '/current/system-skills')
    if mount not in vols:
        vols.append(mount)
    env = service.setdefault('environment', {})
    if not isinstance(env, dict):
        raise ValueError(f'Expected environment mapping in {override}; preserving override')
    if env.get('WIZARD_BUILDER_ROOT', RUNTIME) != RUNTIME:
        raise ValueError(f'Builder environment collision in {override}')
    env['WIZARD_BUILDER_ROOT'] = RUNTIME
    _atomic(override, yaml.safe_dump(doc, sort_keys=False))
    return {**receipt, 'hermes_home': str(data), 'override': str(override), 'restart_required': True}


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


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='wam builder-bundle')
    ap.add_argument('action', choices=['install', 'doctor', 'fresh'])
    ap.add_argument('--hermes-home', type=Path)
    a = ap.parse_args(argv)
    from .registry import home
    try:
        if a.action == 'doctor':
            rows = health(home(), a.hermes_home)
            print(json.dumps(rows, indent=2))
            return 0 if all(r[1] for r in rows) else 1
        if a.action == 'fresh':
            from .cli import Config
            out = fresh(Config())
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
