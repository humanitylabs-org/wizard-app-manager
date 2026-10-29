"""Seal manager control anchors, never recursively chown an application's payload."""
from pathlib import Path
import os
import stat


def seal_app_controls(runtipi, uid, gid):
    root = Path(runtipi) / 'app-data'
    recipes = Path(runtipi) / 'apps'
    targets = [root]
    if recipes.is_symlink():
        raise ValueError('unsafe installed recipe root')
    if recipes.is_dir():
        for store in recipes.iterdir():
            if store.is_symlink():
                raise ValueError('unsafe installed store anchor')
            if not store.is_dir():
                continue
            targets.append(root / store.name)
            for app in store.iterdir():
                if app.is_symlink():
                    raise ValueError('unsafe installed app anchor')
                if app.is_dir():
                    targets.append(root / store.name / app.name)
    for target in targets:
        try:
            st = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError('unsafe app control anchor')
        os.chown(target, uid, gid, follow_symlinks=False)
        target.chmod(0o755)
