"""runtime.package (spec/WIZARD_MD.md "Packaging", DECISIONS #76): source | build | image.

Same contract as packages/backend/src/modules/app-stores/packaging.ts: source/build apps get their pinned
commit (without .git) copied to <app-data>/.wizard-source/<store>/<app>@<commit12>; the App Manager sets
${APP_SOURCE_DIR} to that host path at install. build services build from that copy as
wizard-local/<app>-<service>:<commit12>.
"""
from __future__ import annotations

import os
import posixpath
import shutil
from pathlib import Path

PACKAGES = {"source", "build", "image"}
SOURCE_ROOT = ".wizard-source"
SOURCE_LIMIT_BYTES = 256 * 1024 * 1024


class PackagingError(ValueError):
    pass


def source_dir(app_data_root: Path, store: str, app: str, commit: str) -> Path:
    return Path(app_data_root) / SOURCE_ROOT / store / f"{app}@{commit[:12]}"


def app_data_root(runtipi_root: Path) -> Path:
    """Host app-data folder: RUNTIPI_APP_DATA_PATH from the box's .env, else the Runtipi root."""
    env = Path(runtipi_root) / ".env"
    base = ""
    if env.is_file():
        for line in env.read_text(errors="replace").splitlines():
            if line.startswith("RUNTIPI_APP_DATA_PATH="):
                base = line.split("=", 1)[1].strip().strip('"').strip("'")
    base = (base or str(runtipi_root)).rstrip("/")
    while base.endswith("/app-data"):
        base = base[: -len("/app-data")]
    return Path(base) / "app-data"


def repo_path(p) -> str | None:
    raw = str("." if p is None else p)
    if raw.startswith("/") or "${" in raw:
        return None
    out: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                return None
            out.pop()
        else:
            out.append(seg)
    return "/".join(out)


def copy_source(src: Path, dst: Path, limit: int = SOURCE_LIMIT_BYTES) -> None:
    root = Path(src).resolve()
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root and ".git" in dirnames:
            dirnames.remove(".git")
        out = Path(dst) / here.relative_to(root)
        out.mkdir(parents=True, exist_ok=True)
        for name in list(dirnames) + filenames:
            a = here / name
            if a.is_symlink():
                target = os.readlink(a)
                if os.path.isabs(target) or not (a.parent / target).resolve().is_relative_to(root):
                    raise PackagingError(f"source symlink {a.relative_to(root)} points outside the app")
                os.symlink(target, out / name)
                if name in dirnames:
                    dirnames.remove(name)
            elif a.is_file():
                total += a.stat().st_size
                if total > limit:
                    raise PackagingError(f"app source is larger than {limit // 1048576} MB")
                shutil.copy2(a, out / name)


def prune(app_data: Path, store: str, app: str, keep: str) -> None:
    d = Path(app_data) / SOURCE_ROOT / store
    for p in d.glob(f"{app}@*") if d.is_dir() else []:
        if p.name != keep and p.is_dir() and not p.is_symlink():
            from .source_integrity import pristine, record_path
            if pristine(p):
                shutil.rmtree(p, ignore_errors=True)
                record_path(p).unlink(missing_ok=True)


def prepare_package(pkg, compose: dict, src: Path, target: Path, app: str, commit: str) -> bool:
    """Validate the compose shape for `pkg`, rewrite build services, copy the source. True when source was copied."""
    if pkg is None:
        return False
    kind = str(pkg)
    if kind not in PACKAGES:
        raise PackagingError(f"runtime.package must be source, build or image, not {kind!r}")
    services = {n: s for n, s in (compose.get("services") or {}).items() if isinstance(s, dict)}
    builds = {n: s for n, s in services.items() if s.get("build")}
    if kind != "build" and builds:
        raise PackagingError(f"{kind} apps build nothing; remove build: from {', '.join(builds)}")
    if kind == "build" and not builds:
        raise PackagingError("build apps need a build: section")
    if kind == "image":
        return False
    for name, s in builds.items():
        b = {"context": s["build"]} if isinstance(s["build"], str) else dict(s["build"])
        ctx = repo_path(b.get("context"))
        if ctx is None:
            raise PackagingError(f"{name}: build.context must be a path inside the repo")
        df = repo_path(posixpath.join(ctx or ".", str(b.get("dockerfile") or "Dockerfile")))
        if df is None or (ctx and not df.startswith(ctx + "/")):
            raise PackagingError(f"{name}: build.dockerfile must stay inside the context")
        b["context"] = str(Path(target) / ctx) if ctx else str(target)
        s["build"] = b
        s["image"] = f"wizard-local/{app}-{name}:{commit[:12]}"
        s["pull_policy"] = "build"
    from .source_integrity import inventory, record
    target = Path(target)
    if target.exists() or target.is_symlink():
        if inventory(target) != inventory(Path(src)):
            raise PackagingError("Owner source differs: keep or merge your changes before updating; nothing replaced")
        record(target)
        return True
    tmp = Path(f"{target}.tmp-{os.getpid()}")
    shutil.rmtree(tmp, ignore_errors=True)
    copy_source(src, tmp)
    # Never remove a destination which appeared while copying.
    tmp.rename(target)
    record(target)
    return True
