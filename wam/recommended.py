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
            row["unavailable"] = "Not published for this Wizard yet. Try again after an update."
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


def prefetch(c, runner=None, fetch=None):
    """Sequential best-effort idle-class pulls, with cross-process exclusion.

    nice/ionice lower the CLI's priority, not Docker daemon network bandwidth.
    Do not claim a network throttle; downloads never block setup or installation.
    """
    runner = runner or subprocess.run
    c.home.mkdir(parents=True, exist_ok=True)
    with (c.home / "prefetch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "running"}
        rows = []
        for d in catalog():
            if not d.get("prefetch"):
                continue
            row = {"id": d["app"], "state": "unavailable"}
            try:
                entry = index.resolve(c.index, d["repo"])
                selection = store_selection.resolve(c, d["app"], {
                    "source": "standard", "repo": d["repo"],
                    "commit": entry.get("commit") or entry.get("install", {}).get("commit") or "",
                    "operation": "install"})
                images = recipe_images(selection, fetch)
                for image in images:
                    if runner(["docker", "image", "inspect", image], capture_output=True, timeout=30).returncode == 0:
                        continue
                    argv = ["nice", "-n", "19", "docker", "pull", image]
                    if shutil.which("ionice"):
                        argv = ["ionice", "-c", "3", *argv]
                    runner(argv, capture_output=True, timeout=900, check=True)
                row.update(state="cached", commit=selection["commit"], images=images)
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, yaml.YAMLError):
                # No raw subprocess output/credentials in browser-visible status.
                row["error"] = "Image download unavailable; installation can retry normally."
            rows.append(row)
        result = {"state": "finished", "apps": rows}
        tmp = c.home / "prefetch.json.new"
        tmp.write_text(json.dumps(result) + "\n")
        tmp.replace(c.home / "prefetch.json")
        return result


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
