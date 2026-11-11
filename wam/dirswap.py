"""Replace a directory with a freshly built one, even when the old one can't be deleted.

`shutil.rmtree(dst, ignore_errors=True); tmp.rename(dst)` silently keeps whatever rmtree could not remove
(files Runtipi or an app container wrote as root, a read-only subfolder, ...) and then fails with a misleading
`[Errno 39] Directory not empty: <tmp> -> <dst>`. Renaming the old folder aside first needs only write access to
the parent folder, so the new copy always lands; the aside copy is then removed best-effort.
"""
import os
import pwd
import shutil
import time
from pathlib import Path

STALE_SECONDS = 600


class ReplaceError(OSError):
    """The old directory could not even be moved aside; nothing was changed."""


def _owner(p: Path) -> str:
    try:
        uid = p.lstat().st_uid
    except OSError:
        return "unknown owner"
    try:
        return f"{pwd.getpwuid(uid).pw_name} (uid {uid})"
    except KeyError:
        return f"uid {uid}"


def _remove(p: Path) -> None:
    """Best effort: an aside copy we can't fully delete is harmless (hidden, never read)."""
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


def aside_prefix(dst: Path) -> str:
    return f".{Path(dst).name}.old-"


def replace_dir(new: Path, dst: Path) -> None:
    """Move `new` to `dst`, replacing any existing `dst`. `new` must be in the same filesystem (normally a
    sibling). On failure `dst` is left as it was: either never moved, or moved back."""
    new, dst = Path(new), Path(dst)
    parent = dst.parent
    aside = None
    if dst.exists() or dst.is_symlink():
        aside = parent / f"{aside_prefix(dst)}{os.getpid()}"
        _remove(aside)
        if aside.exists() or aside.is_symlink():
            aside = parent / f"{aside_prefix(dst)}{os.getpid()}-{os.urandom(4).hex()}"
        try:
            os.rename(dst, aside)
        except OSError as e:
            raise ReplaceError(
                e.errno, f"can't replace {dst}: moving it aside failed ({e.strerror}). {dst} belongs to "
                f"{_owner(dst)} and its folder {parent} to {_owner(parent)}; this runs as uid {os.getuid()}. "
                f"Give that folder back to this user (or run as its owner) and try again") from e
    try:
        os.rename(new, dst)
    except OSError as e:
        if aside is not None:
            try:
                os.rename(aside, dst)   # roll back: the old copy is still the live one
            except OSError as back:
                raise OSError(e.errno, f"can't put the new {dst} in place ({e.strerror}) and the old copy could "
                                       f"not be moved back from {aside} ({back.strerror})") from e
        raise
    if aside is not None:
        _remove(aside)
    # Earlier runs' leftovers (e.g. root-owned files we could not delete then). Skip recent ones: a concurrent
    # replace may still need its aside copy for rollback (rename sets ctime, so ctime is the aside age).
    for stale in parent.glob(aside_prefix(dst) + "*"):
        try:
            if time.time() - stale.lstat().st_ctime > STALE_SECONDS:
                _remove(stale)
        except OSError:
            pass
