"""Setup recommendations. Discovery and image caching only; installation stays in Store.

A cached Docker image is not an installed app: never prepare a recipe, create a
container, write apps.json or touch an owner's credentials here.
"""
from __future__ import annotations

import fcntl
import json
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import yaml

from . import index, slots, store_selection


def catalog():
    # Recommendations are OS policy, not release defaults. Old pinned releases must
    # not silently lose optional recommendations or turn them into bootstrap apps.
    return [dict(d) for d in json.loads(slots.DEFAULTS_FILE.read_text())["slots"].values()
            if d.get("install") == "on-demand" and d.get("recommended") and d.get("app")]


def listing(c):
    try:
        installed = {a["id"] for a in json.loads((c.home / "apps.json").read_text()).get("apps", [])}
    except (OSError, ValueError):
        installed = set()
    rows = []
    for d in catalog():
        row = {"id": d["app"], "name": d["name"], "summary": d.get("summary", ""),
               "installed": d["app"] in installed, "selection": None}
        try:
            entry = index.resolve(c.index, d["repo"])
            body = {"source": "standard", "repo": d["repo"],
                    "commit": entry.get("commit") or entry.get("install", {}).get("commit") or "",
                    "operation": "install"}
            store_selection.resolve(c, d["app"], body)
            row["selection"] = body
        except (ValueError, RuntimeError, OSError):
            # Not installable here (unpublished or unresolvable): leave it out of Setup instead of showing a
            # greyed card a new owner can do nothing with. It appears once the index publishes it.
            continue
        rows.append(row)
    return {"apps": rows}


def recipe_images(selection, fetch=None):
    """Read only the indexed immutable recipe; never clone, compile or execute it."""
    entry = selection["entry"]
    path = (entry.get("manifest") or {}).get("runtime", {}).get("compose", "runtipi/docker-compose.yml")
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path.split("/") or path.startswith("/"):
        raise ValueError("invalid compose path")
    url = f"https://raw.githubusercontent.com/{selection['repo']}/{selection['commit']}/{path}"
    if fetch is None:
        def download(url):
            with urllib.request.urlopen(url, timeout=30) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("recipe too large")
                return raw
        fetch = download
    compose = yaml.safe_load(fetch(url))
    if not isinstance(compose, dict) or not isinstance(compose.get("services"), dict):
        raise ValueError("invalid compose services")
    images = []
    for service in compose.get("services", {}).values():
        if not isinstance(service, dict):
            raise ValueError("invalid service")
        image = service.get("image", "")
        if not isinstance(image, str):
            raise ValueError("invalid image")
        image = re.sub(r"\$\{[A-Z_][A-Z_0-9]*:-([^}]+)\}", r"\1", image)
        # Refuse unresolved/local-only images and options. No shell is involved.
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:@-]+", image) or "/" not in image or "://" in image:
            raise ValueError("no published pullable image")
        if image not in images:
            images.append(image)
    if not images:
        raise ValueError("no published images")
    return images


def docker_argv() -> list[str]:
    """`docker`, or `sudo -n docker` where the user can't reach Docker yet (install.sh's own fallback; WAM#836)."""
    from .network import docker_cmd
    return docker_cmd()


def _pull(c, d, runner, fetch, docker) -> dict:
    """Cache one app's indexed images (never install it). A row: cached (with commit/images) or unavailable."""
    row = {"id": d["app"], "state": "unavailable"}
    try:
        entry = index.resolve(c.index, d["repo"])
        selection = store_selection.resolve(c, d["app"], {
            "source": "standard", "repo": d["repo"],
            "commit": entry.get("commit") or entry.get("install", {}).get("commit") or "",
            "operation": "install"})
        images = recipe_images(selection, fetch)
        for image in images:
            if runner([*docker, "image", "inspect", image], capture_output=True, timeout=30).returncode == 0:
                continue
            argv = ["nice", "-n", "19", *docker, "pull", image]
            if shutil.which("ionice"):
                argv = ["ionice", "-c", "3", *argv]
            runner(argv, capture_output=True, timeout=900, check=True)
        row.update(state="cached", commit=selection["commit"], images=images)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, yaml.YAMLError):
        # No raw subprocess output/credentials in browser-visible status.
        row["error"] = "Image download unavailable; installation can retry normally."
    return row


def _run(c, apps, name, runner=None, fetch=None):
    """Pull `apps` one after another under <home>/<name>.lock and record the result in <home>/<name>.json."""
    runner = runner or subprocess.run
    c.home.mkdir(parents=True, exist_ok=True)
    with (c.home / f"{name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "running"}
        docker = docker_argv() if apps else []
        rows = [_pull(c, d, runner, fetch, docker) for d in apps]
        result = {"state": "finished", "apps": rows}
        tmp = c.home / f"{name}.json.new"
        tmp.write_text(json.dumps(result) + "\n")
        tmp.replace(c.home / f"{name}.json")
        return result


def prefetch(c, runner=None, fetch=None):
    """Sequential best-effort idle-class pulls, with cross-process exclusion.

    nice/ionice lower the CLI's priority, not Docker daemon network bandwidth.
    Do not claim a network throttle; downloads never block setup or installation.
    """
    return _run(c, [d for d in catalog() if d.get("prefetch")], "prefetch", runner, fetch)


# Private first (WAM#836): a box installed with WIZARD_PRIVATE_FIRST=1 offers Tinfoil first in Welcome, so its
# image is cached in the background as soon as the choice is recorded, instead of on the owner's Connect tap.
PRIVATE_FIRST = "prefetch-private-first"


def private_first_apps(c) -> list[dict]:
    """defaults.json `private_first_prefetch.apps` when the box's recorded private-first choice is on, minus apps
    already installed. Empty (nothing to do) on every other box."""
    from . import private_first
    if private_first.choice(c.home) != "1":
        return []
    rows = (json.loads(slots.DEFAULTS_FILE.read_text()).get("private_first_prefetch") or {}).get("apps") or []
    try:
        installed = {a["id"] for a in json.loads((c.home / "apps.json").read_text()).get("apps", [])}
    except (OSError, ValueError, KeyError, TypeError):
        installed = set()
    return [dict(d) for d in rows if d.get("app") and d.get("repo") and d["app"] not in installed]


def prefetch_private_first(c, runner=None, fetch=None):
    """`wam prefetch --private-first`: cache the private-first apps' images; never installs or starts them."""
    apps = private_first_apps(c)
    if not apps:
        return {"state": "skipped", "apps": []}
    return _run(c, apps, PRIVATE_FIRST, runner, fetch)


def private_first_done(c) -> bool:
    try:
        r = json.loads((c.home / f"{PRIVATE_FIRST}.json").read_text())
        return r.get("state") == "finished" and all(a.get("state") == "cached" for a in r.get("apps", []))
    except (OSError, ValueError, AttributeError):
        return False


def spawn_private_first(c, popen=None):
    """Start `wam prefetch --private-first` detached (its own session), so `wam private-first on` in the installer
    returns at once and the download outlives it. Best effort: a failure here only means the download waits for the
    App Manager service's next start (resume) or the owner's Connect tap."""
    if not private_first_apps(c):
        return None
    popen = popen or subprocess.Popen
    try:
        c.home.mkdir(parents=True, exist_ok=True)
        log = (c.home / f"{PRIVATE_FIRST}.log").open("a")
        try:
            popen([sys.executable, "-m", "wam", "prefetch", "--private-first"], cwd=str(Path(__file__).resolve().parent.parent),
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        finally:
            log.close()
        return "started"
    except OSError:
        return None


_start_lock = threading.Lock()
_worker = None


def start(c):
    """Called after Setup's first successful /seen; one resumable background task."""
    global _worker
    with _start_lock:
        marker = c.home / "prefetch-requested"
        if _worker is not None and _worker.is_alive():
            return {"state": "running"}
        # Completed requests stay idempotent. CLI `wam prefetch` explicitly retries
        # unavailable images (and checks Docker's cache after image pruning).
        if marker.exists() and (c.home / "prefetch.json").exists():
            return json.loads((c.home / "prefetch.json").read_text())
        marker.touch(mode=0o600)
        _worker = threading.Thread(target=prefetch, args=(c,), daemon=True, name="wam-prefetch")
        _worker.start()
        return {"state": "running"}


def resume(c):
    if (c.home / "prefetch-requested").exists() and not (c.home / "prefetch.json").exists():
        start(c)
    # Private first (WAM#836): every App Manager start finishes a download that was cut off (reboot, pull error).
    if private_first_apps(c) and not private_first_done(c):
        threading.Thread(target=prefetch_private_first, args=(c,), daemon=True, name="wam-prefetch-private-first").start()
