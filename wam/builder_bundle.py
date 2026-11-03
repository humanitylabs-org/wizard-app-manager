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
import stat
import subprocess
import sys
import tempfile
import uuid

import yaml

SOURCE = Path(__file__).resolve().parent.parent
SCHEMA = 1
MARKER = 'wizard-builder-bundle-v1'
RUNTIME = '/opt/wizard-builder'
COMPANION_SKILLS = ('wizard-guide',)


def _files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'bundle.json'
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def _foreign(path):
    """A file another uid owns. The stock Hermes container remaps its user to the recipe's HERMES_UID (1000) and keeps
    config.yaml 0640, so on a box whose Wizard user is not uid 1000 (the default `wizard` user created next to an
    existing uid-1000 account) the installer can neither read it nor replace it without changing its owner."""
    try:
        return path.exists() and path.stat().st_uid != os.geteuid()
    except OSError:
        return False


def _sudo(args, **kw):
    """The fresh installer's passwordless sudo (the same fallback the Docker probe uses); never prompts."""
    try:
        return subprocess.run(['sudo', '-n', *args], capture_output=True, timeout=30, check=True, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        detail = getattr(e, 'stderr', b'') or b''
        detail = detail.decode(errors='replace') if isinstance(detail, bytes) else str(detail)
        raise PermissionError(f'{args[-1]} belongs to another user and sudo -n is unavailable: {detail.strip()[-200:] or e}')


def _read_bytes(path):
    try:
        return path.read_bytes()
    except PermissionError:
        return _sudo(['cat', '--', str(path)]).stdout


def _read_text(path):
    return _read_bytes(path).decode()


def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if _foreign(path):
        # Keep the agent's own owner and mode: replacing it as this user would lock the agent out of its config.
        st = path.stat()
        d = tempfile.mkdtemp()
        try:
            src = Path(d) / path.name
            src.write_text(text)
            stage = path.with_name(f'.{path.name}.{os.getpid()}.wam')
            _sudo(['install', '-o', str(st.st_uid), '-g', str(st.st_gid), '-m', oct(st.st_mode & 0o777)[2:], str(src), str(stage)])
            _sudo(['mv', '-f', '--', str(stage), str(path)])
        finally:
            shutil.rmtree(d, ignore_errors=True)
        return
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


def _legacy_skill(path):
    """Only the exact previously shipped manual installer copy is migratable."""
    if not path.is_dir() or path.is_symlink() or any(p.is_symlink() for p in path.rglob('*')):
        return False
    expected = json.loads((Path(__file__).with_name('builder_legacy.json')).read_text())['files']
    return _files(path) == expected


def _local_snapshot(path):
    """Exact local-copy bytes/modes, including hidden files and empty dirs.

    Unlike release hashing, omit nothing. Links/special files are refused.
    """
    entries = {}
    for p in [path, *sorted(path.rglob('*'))]:
        st = p.lstat()
        row: dict = {'mode': stat.S_IMODE(st.st_mode)}
        if stat.S_ISDIR(st.st_mode):
            row['kind'] = 'directory'
        elif stat.S_ISREG(st.st_mode):
            row.update(kind='file', sha256=hashlib.sha256(p.read_bytes()).hexdigest())
        else:
            raise ValueError(f'Local builder copy contains a link or special file: {p}; preserving it')
        entries[str(p.relative_to(path))] = row
    return entries


def _local_spec(variant):
    spec = json.loads(Path(__file__).with_name('builder_local_copy.json').read_text())
    if variant != spec['variant']:
        raise ValueError('Unknown exact local-copy migration; preserving it')
    return spec


def _matching_local(path, variant):
    if _local_snapshot(path) != _local_spec(variant)['entries']:
        raise ValueError('Local builder copy differs from reviewed bytes/modes; preserving it')


def _publication_parents(home):
    # These are fixed, immediate children of the explicitly selected home.
    # Do not follow an owner redirect while publishing or restoring discovery.
    for name in ('system-skills', 'bin'):
        path = home / name
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f'Unmanaged directory: {path}; preserving it')


def _publication_snapshot(path):
    if path.is_symlink():
        raise ValueError(f'Unmanaged publication file: {path}; preserving it')
    if not path.exists():
        return None
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
        raise ValueError(f'Unmanaged publication file: {path}; preserving it')
    return path.read_bytes(), stat.S_IMODE(st.st_mode)


def _restore_publication(path, before):
    if before is None:
        path.unlink(missing_ok=True)
        return
    data, mode = before
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install(os_home, source=SOURCE, *, migrate_local_copy=None, hermes_home=None):
    """Publish a complete release, with bounded recovery of discovery on failure.

    Retain earlier releases for rollback/in-flight readers. Never delete custom
    skills or the generated app registry. Refuse unmanaged path collisions.
    """
    home, source = Path(os_home).resolve(), Path(source).resolve()
    _publication_parents(home)
    base = home / 'builder'
    if base.is_symlink() or (base.exists() and (not (base / '.owner').is_file() or (base / '.owner').read_text().strip() != MARKER)):
        raise ValueError(f'Unmanaged builder destination: {base}; preserving it')
    skill = home / 'system-skills' / 'wizard-builder'
    expected = base / 'current' / 'system-skills' / 'wizard-builder'
    legacy = False if migrate_local_copy else _legacy_skill(skill)
    local = False
    if migrate_local_copy:
        _local_spec(migrate_local_copy)
        _matching_local(skill, migrate_local_copy)
        local = True
    if skill.exists() or skill.is_symlink():
        if not legacy and not local and (not skill.is_symlink() or os.readlink(skill) != str(expected)):
            raise ValueError(f'Unmanaged wizard-builder skill: {skill}; preserving it')
    current = base / 'current'
    if current.exists() and not current.is_symlink():
        raise ValueError(f'Unmanaged builder current: {current}')
    cmd = home / 'bin/wam'
    command_before = _publication_snapshot(cmd)
    if cmd.exists() and MARKER not in cmd.read_text():
        raise ValueError(f'Unmanaged command: {cmd}; preserving it')
    python_before = _publication_snapshot(base / 'python')
    cfgp, config_before = None, None
    if hermes_home is not None:
        cfgp = Path(hermes_home).resolve() / 'config.yaml'
        if not cfgp.parent.is_dir():
            raise ValueError('Hermes home must be an existing directory; preserving it')
        config_before = _publication_snapshot(cfgp)
        check_collisions(cfgp.parent, home, migrate_local_copy=migrate_local_copy)
    reservation = None
    if legacy or local:
        prefix = 'local-copy-' + str(migrate_local_copy) if local else 'legacy-skill'
        reservation = base / (prefix + '-' + uuid.uuid4().hex[:8])
        for path in (reservation, reservation.with_suffix('.json')):
            if path.exists() or path.is_symlink():
                raise ValueError(f'Occupied preservation destination: {path}; preserving it')
    for name in ('wam', 'templates/hello-wizard', 'system-skills/wizard-builder', 'wizard-views'):
        if not (source / name).is_dir():
            raise ValueError(f'Incomplete source bundle: missing {name}; reinstall from a complete source release')
    base.mkdir(parents=True, exist_ok=True)
    _atomic(base / '.owner', MARKER + '\n')
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
    skill.parent.mkdir(parents=True, exist_ok=True)
    saved = None
    previous = os.readlink(current) if current.is_symlink() else None
    skill_before = os.readlink(skill) if skill.is_symlink() else None
    companions = []
    # Journal only absent links we may create; retain every existing owner path.
    for extra in COMPANION_SKILLS:
        link = skill.parent / extra
        if not link.exists() and not link.is_symlink():
            companions.append((link, base / 'current' / 'system-skills' / extra))
    reserved_dir = reserved_receipt = False
    touched_files = []
    registered = None
    try:
        # Keep this host binding outside portable releases, inside recovery.
        touched_files.append((base / 'python', python_before))
        _atomic(base / 'python', sys.executable + '\n')
        if legacy or local:
            assert reservation is not None
            if local:
                _matching_local(skill, migrate_local_copy)
            # Exclusively reserve BOTH names, including for legacy migration.
            # Rename replaces only our own empty reservation, never owner bytes.
            reservation.mkdir()
            reserved_dir = True
            with reservation.with_suffix('.json').open('x'):
                pass
            reserved_receipt = True
            skill.rename(reservation)  # Same filesystem: original bytes/modes/ownership.
            saved = reservation
            if local:
                _matching_local(saved, migrate_local_copy)
                _atomic(saved.with_suffix('.json'), json.dumps({
                    'provenance': 'reviewed-custom-local-copy',
                    'variant': migrate_local_copy, 'entries': _local_snapshot(saved),
                }, indent=2) + '\n')
            else:
                reservation.with_suffix('.json').unlink()
                reserved_receipt = False
        _switch(current, Path('releases') / dest.name)
        if not skill.is_symlink():
            _switch(skill, expected)
        for link, target in companions:
            if target.is_dir():
                _switch(link, target)
        (home / 'bin').mkdir(exist_ok=True)
        # A wrapper rather than a symlink keeps dirname independent of invocation.
        import shlex
        script = '#!/bin/sh\n# ' + MARKER + '\nexec ' + shlex.quote(str(base / 'current/bin/wam')) + ' "$@"\n'
        touched_files.append((cmd, command_before))
        _atomic(cmd, script)
        cmd.chmod(0o755)
        if cfgp is not None:
            touched_files.append((cfgp, config_before))
            registered = register(cfgp.parent, home)
    except BaseException as error:
        failures = []
        def recover(label, action):
            try:
                action()
            except BaseException:
                failures.append(str(label))
        # Attempt every recovery even if storage/permissions prevent one of them.
        for path, before in reversed(touched_files):
            recover(path, lambda p=path, b=before: _restore_publication(p, b))
        for link, target in companions:
            if link.is_symlink() and os.readlink(link) == str(target):
                recover(link, link.unlink)
        def restore_skill():
            if saved is not None:
                if skill.is_symlink() and os.readlink(skill) == str(expected):
                    skill.unlink()
                if skill.exists() or skill.is_symlink():
                    raise ValueError('Discovery changed during recovery')
                saved.rename(skill)
            elif skill_before is None and skill.is_symlink() and os.readlink(skill) == str(expected):
                skill.unlink()
        recover(skill, restore_skill)
        recover(current, lambda: current.unlink(missing_ok=True) if previous is None else _switch(current, Path(previous)))
        if reservation is not None and reserved_receipt and (saved is None or not saved.exists()):
            recover(reservation.with_suffix('.json'), reservation.with_suffix('.json').unlink)
        if reservation is not None and reserved_dir and saved is None:
            recover(reservation, reservation.rmdir)
        if failures:
            # No promise of atomicity under a second I/O failure: the caller gets
            # exact recovery targets and the original backup id, even on CLI failure.
            raise OSError('Incomplete builder recovery: ' + json.dumps({
                'state': 'recovery_required', 'failed_paths': failures,
                'preserved_copy': str(saved) if saved is not None else None,
                'original_location': str(saved if saved is not None and saved.exists() else skill),
                'previous_current': previous, 'skill': str(skill),
                'current_target': os.readlink(current) if current.is_symlink() else None,
                'command': str(cmd), 'config': str(cfgp) if cfgp else None,
            }, sort_keys=True)) from error
        raise
    return {'state': 'installed', 'sha256': digest, 'skill': str(skill / 'SKILL.md'),
            'command': str(cmd), 'system_skills': str(skill.parent), 'schema': SCHEMA,
            'release': dest.name, 'preserved_modified_release': preserved,
            'preserved_local_copy': str(saved) if local else None,
            **({'registered': registered} if cfgp is not None else {})}


def restore_local_copy(os_home, revision):
    """Undo only a reviewed local migration; retain all managed releases."""
    spec = json.loads(Path(__file__).with_name('builder_local_copy.json').read_text())
    if not re.fullmatch(re.escape('local-copy-' + spec['variant'] + '-') + r'[0-9a-f]{8}', revision or ''):
        raise ValueError('Expected exact preserved local-copy id from install receipt')
    home = Path(os_home).resolve()
    _publication_parents(home)
    base = home / 'builder'
    if base.is_symlink() or (base / '.owner').read_text().strip() != MARKER:
        raise ValueError('Unmanaged builder destination; preserving it')
    saved = base / revision
    _matching_local(saved, spec['variant'])
    receipt = json.loads(saved.with_suffix('.json').read_text())
    if receipt != {'provenance': 'reviewed-custom-local-copy', 'variant': spec['variant'], 'entries': spec['entries']}:
        raise ValueError('Local-copy preservation receipt differs; restore refused')
    skill = home / 'system-skills/wizard-builder'
    expected = base / 'current/system-skills/wizard-builder'
    if not skill.is_symlink() or os.readlink(skill) != str(expected):
        raise ValueError('Unmanaged wizard-builder skill; restore refused')
    skill.unlink()
    try:
        saved.rename(skill)
    except BaseException:
        _switch(skill, expected)
        raise
    return {'state': 'restored_local_copy', 'variant': spec['variant'], 'skill': str(skill)}


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


def check_collisions(hermes_home, os_home, cfg=None, runtime=None, *, migrate_local_copy=None):
    home, osh = Path(hermes_home), Path(os_home).resolve()
    cfg = cfg if cfg is not None else (yaml.safe_load(_read_text(home / 'config.yaml')) or {} if (home / 'config.yaml').exists() else {})
    if not isinstance(cfg, dict) or not isinstance(cfg.get('skills', {}), dict):
        raise ValueError('Expected config/skills mapping; preserving config')
    dirs = (cfg.get('skills') or {}).get('external_dirs') or []
    if not isinstance(dirs, list):
        raise ValueError('skills.external_dirs must be a list; preserving config')
    def configured_path(value):
        if not isinstance(value, str):
            raise ValueError('Skill directory paths must be strings; preserving config')
        path = Path(os.path.expanduser(os.path.expandvars(value)))
        # Stock Hermes resolves relative external/create directories at its
        # profile home, not at the installer's current working directory.
        return path if path.is_absolute() else home / path

    create_dir = (cfg.get('skills') or {}).get('create_dir')
    roots = [home / 'skills', *[configured_path(d) for d in dirs]]
    if create_dir:
        roots.append(configured_path(create_dir))
    allowed = {(osh / 'system-skills').resolve()}
    if runtime:
        allowed.add(Path(runtime))
    for root in roots:
        if root.resolve() in allowed:
            continue
        if root.is_dir():
            # Stock skill_view recursively accepts legacy flat names, except
            # Markdown owned by an ancestor skill package (loaded via file_path).
            for flat in root.rglob('wizard-builder.md'):
                relative = flat.relative_to(root)
                package_owned = any(
                    (root.joinpath(*relative.parts[:depth]) / 'SKILL.md').is_file()
                    for depth in range(1, len(relative.parts)))
                if not package_owned:
                    raise ValueError(f'wizard-builder name collision at {flat}; preserving owner skill')
            named = root / 'wizard-builder'
            # The old manual installer created this precise symlink. Keep it
            # (including across unlink) but never accept a different target.
            legacy_alias = named.is_symlink() and named.resolve() == (osh / 'system-skills/wizard-builder').resolve()
            legacy_alias = legacy_alias and (_legacy_skill(osh / 'system-skills/wizard-builder')
                or (osh / 'system-skills/wizard-builder').is_symlink())
            if (not legacy_alias and migrate_local_copy and named.is_symlink()
                    and named.resolve() == (osh / 'system-skills/wizard-builder').resolve()):
                _matching_local(osh / 'system-skills/wizard-builder', migrate_local_copy)
                legacy_alias = True
            if named.exists() or named.is_symlink():
                if not legacy_alias:
                    raise ValueError(f'wizard-builder name collision at {named}; preserving owner skill')
            skills = list(root.rglob('SKILL.md'))
            skills += [p / 'SKILL.md' for p in root.rglob('*')
                       if p.is_symlink() and (p / 'SKILL.md').is_file()]
            for p in skills:
                if legacy_alias and p.parent == named:
                    continue
                try:
                    data = p.read_text()
                except FileNotFoundError:
                    # Gone between the listing and the read: an App Manager registry rewrite replaced or removed it.
                    # A vanished file can't collide; the remaining files are still checked.
                    continue
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
    cfg = yaml.safe_load(_read_text(cfgp)) or {} if cfgp.exists() else {}
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
              _read_bytes(data / 'config.yaml') if (data / 'config.yaml').exists() else None)
    # The override carries Compose merge tags (`networks: !override`, written by `wam network`); safe_load can't
    # read them and safe_dump would drop them, so read and write it with the tag-preserving YAML helpers.
    from .network import dump_yaml, load_yaml
    doc = load_yaml(override.read_text()) or {} if override.exists() else {}
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
    _atomic(override, dump_yaml(doc))
    after = (override.read_bytes(), _read_bytes(data / 'config.yaml'))
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
        if shutil.which('git') is None:
            raise ValueError('tooling unavailable: git is required for source scaffolding')
        p = subprocess.run([str(base / 'current/bin/wam'), 'new', '--help'], capture_output=True, text=True, timeout=15)
        if p.returncode:
            raise ValueError('tooling unavailable (Python 3.10+/PyYAML required): ' + p.stderr[-300:])
        # The App Manager moved on (a checkout update, a hand fix) without reinstalling the bundle: builder sessions
        # would run older wam code. Box commands are delegated (bundle_delegate.py); this still flags the drift.
        if not (SOURCE / 'bundle.json').is_file() and (SOURCE / 'wam').is_dir():
            ours = {'wam/' + k: v for k, v in _files(SOURCE / 'wam').items()}
            if ours != {k: v for k, v in files.items() if k.startswith('wam/')}:
                raise ValueError(f'bundle is older than (or differs from) this App Manager at {SOURCE}')
        rows.append(('Wizard builder bundle and host tooling', True, digest))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
        rows.append(('Wizard builder bundle and host tooling', False, str(e) + '; repair: wam builder-bundle install'))
    if hermes_home:
        try:
            cfg = yaml.safe_load(_read_text(Path(hermes_home) / 'config.yaml')) or {}
            dirs = check_collisions(hermes_home, os_home, cfg, runtime)
            want = runtime or str(Path(os_home).resolve() / 'system-skills')
            enabled = 'wizard-builder' not in ((cfg.get('skills') or {}).get('disabled') or [])
            binding = runtime is not None or (Path(want) / 'wizard-builder').resolve() == (base / 'current/system-skills/wizard-builder').resolve()
            rows.append(('Hermes discovers wizard-builder', want in dirs and enabled and binding,
                         want + ('' if enabled else '; disabled by owner config') + ('' if binding else '; system skill binding missing or replaced')))
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
        cfg = yaml.safe_load(_read_text(data / 'config.yaml')) or {}
        terminal = cfg.get('terminal') or {}
        docker = ['docker']
        ps = ['ps', '-q', '--filter', 'label=com.docker.compose.service=hermes-agent-engine']
        try:
            p = subprocess.run(docker + ps, capture_output=True, text=True, timeout=15, check=True)
        except subprocess.CalledProcessError as error:
            # Fresh installs use passwordless sudo before Docker group membership
            # is active. Probe the same local daemon; never switch a remote/rootless
            # context or prompt for credentials from doctor.
            detail = (error.stderr or '').lower()
            if (os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT')
                    or 'permission denied' not in detail or '/var/run/docker.sock' not in detail):
                raise
            docker = ['sudo', '-n', 'docker']
            p = subprocess.run(docker + ps, capture_output=True, text=True, timeout=15, check=True)
        ids = p.stdout.split()
        if len(ids) != 1:
            raise ValueError('expected one running stock Hermes engine; found ' + str(len(ids)))
        backend = terminal.get('backend', 'local')
        if backend == 'local':
            command = [RUNTIME + '/current/bin/wam', 'builder-bundle', 'doctor']
        elif backend == 'ssh':
            host, user = terminal.get('ssh_host'), terminal.get('ssh_user')
            if not host or not user:
                raise ValueError('SSH terminal has no configured host/user')
            command = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'StrictHostKeyChecking=yes',
                       '-p', str(terminal.get('ssh_port', 22))]
            if terminal.get('ssh_key'):
                command += ['-i', str(terminal['ssh_key'])]
            command += [str(user) + '@' + str(host), '"$HOME/.wizard-os/bin/wam" builder-bundle doctor']
        else:
            raise ValueError(f'backend {backend!r} needs builder provisioning on its execution worker')
        p = subprocess.run(docker + ['exec', '-u', '1000:1000', ids[0], *command],
                           capture_output=True, text=True, timeout=20)
        if p.returncode:
            raise ValueError(f'{backend} terminal cannot execute builder: {p.stderr[-250:]}')
        return [(label, True, f'{backend}: real engine tooling probe passed')]
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return [(label, False, str(e) + '; prepare: wam builder-bundle fresh; restart agent to adopt mount; SSH requires its configured trusted host/key')]


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='wam builder-bundle')
    ap.add_argument('action', choices=['install', 'doctor', 'fresh', 'rollback', 'restore-local-copy'])
    ap.add_argument('--migrate-local-copy', help='install only: reviewed exact custom-copy variant; no arbitrary hashes')
    ap.add_argument('--revision', help='rollback: exact retained release id from install receipt')
    ap.add_argument('--hermes-home', type=Path)
    ap.add_argument('--restart', action='store_true', help='fresh only: adopt changed discovery/mount through Runtipi')
    a = ap.parse_args(argv)
    from .registry import home
    try:
        if a.migrate_local_copy and a.action != 'install':
            raise ValueError('--migrate-local-copy is supported only by install')
        if a.action == 'doctor':
            rows = health(home(), a.hermes_home)
            print(json.dumps(rows, indent=2))
            return 0 if all(r[1] for r in rows) else 1
        if a.action == 'restore-local-copy':
            out = restore_local_copy(home(), a.revision)
        elif a.action == 'rollback':
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
            out = install(home(), migrate_local_copy=a.migrate_local_copy, hermes_home=a.hermes_home)
        print(json.dumps(out, indent=2))
        return 0
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f'builder-bundle: {e}', file=__import__('sys').stderr)
        return 1
