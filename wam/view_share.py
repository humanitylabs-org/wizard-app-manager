"""Read-only screen projection for the Agent Service's existing wizard-os mount.

Never share src/, git history, build logs, or arbitrary view.json fields. Publication
is atomic per build and metadata is switched last, exactly like the owner's build.
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path


def shared_home(home):
    config = Path(home) / 'app-manager.json'
    cfg = json.loads(config.read_text()) if config.is_file() else {}
    root = os.environ.get('RUNTIPI_ROOT') or cfg.get('runtipi_root')
    # An isolated views home with no manager config must never touch another install.
    if not root and Path(home).resolve() == (Path.home() / '.wizard-os').resolve():
        root = Path.home() / 'runtipi'  # the same stock default as Config
    if not root:
        return None
    return Path(root).expanduser() / 'app-data' / 'wizard-os'


def publish(app_id, home, target=None):
    from . import views
    shared = Path(target) if target is not None else shared_home(home)
    if shared is None:
        return
    view = views.load(app_id, home)
    if not view:
        return
    dest = shared / 'views' / app_id
    if dest.resolve() == views.view_dir(app_id, home).resolve():
        return  # already using the owner home directly; never replace its metadata
    if any(p.is_symlink() for p in (shared, shared / 'views', dest)):
        raise views.ViewError('The shared views folder must not be a symlink.')
    dest.mkdir(parents=True, exist_ok=True)
    for directory in (shared, shared / 'views', dest):
        directory.chmod(0o755)
    serving = view.get('serving')
    if view.get('mode') == 'own' and serving:
        if not isinstance(serving, str) or not re.fullmatch(r'[0-9a-f]{40,64}', serving):
            raise views.ViewError('Invalid view build identifier.')
        source = views.view_dir(app_id, home) / 'builds' / serving
        build = dest / 'builds' / serving
        if (source / '.ok').is_file() and not build.exists():
            build.parent.mkdir(exist_ok=True)
            build.parent.chmod(0o755)
            with tempfile.TemporaryDirectory(dir=dest) as tmp:
                stage = Path(tmp) / 'build'
                stage.mkdir()
                for f in source.rglob('*'):
                    if f.is_symlink():
                        raise views.ViewError('A shared view build must not contain symlinks.')
                    if f.is_file():
                        out = stage / f.relative_to(source)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        out.write_bytes(b'{}' if f.name == '.ok' else f.read_bytes())
                        out.chmod(0o644)
                for d in [stage, *[p for p in stage.rglob('*') if p.is_dir()]]:
                    d.chmod(0o755)
                stage.rename(build)
    data = {key: view.get(key) for key in ('mode', 'active', 'suspended', 'serving')}
    if view.get('mode') == 'own':
        data['api_path'] = view.get('api_path') or '/api'
    temp = dest / 'view.json.tmp'
    temp.write_text(json.dumps(data))
    temp.chmod(0o644)
    temp.replace(dest / 'view.json')
    # Retain only the same small set of builds the owner retains.
    for old in (dest / 'builds').glob('*'):
        if old.name != serving and not (views.view_dir(app_id, home) / 'builds' / old.name).exists():
            shutil.rmtree(old)
