"""The local Runtipi store: only the apps this box installed, each pinned to the commit the index listed.

Runtipi stays stock. It reads this store like any other app store (a git repo with apps/<id>/), served on
the Runtipi network by a tiny git container (see store_server.py), so nothing new listens on the host.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from . import wizardmd

STORE_CONTAINER = "wizard-store"
STORE_IMAGE = "buildpack-deps:bookworm-scm@sha256:8ea58fbc817960cd62d512044956ff00cb94865e549eeec32c12139413262004"
STORE_PORT = 8080


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fetch_commit(url: str, commit: str, cache: Path) -> Path:
    """Exactly one commit of one repo (no branch following). Cached per commit."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise RuntimeError(f"refusing to install {url}: the index did not give a full commit sha")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", url.removeprefix("https://").removesuffix(".git"))
    dest = cache / f"{slug}@{commit[:12]}"
    if (dest / ".git").is_dir() and _run(["git", "rev-parse", "HEAD"], cwd=dest).stdout.strip() == commit:
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    for cmd in (["git", "init", "-q"], ["git", "fetch", "-q", "--depth", "1", url, commit], ["git", "checkout", "-q", "FETCH_HEAD"]):
        r = _run(cmd, cwd=dest)
        if r.returncode:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"fetch failed: {url}@{commit[:12]}: {r.stderr.strip()[:300]}")
    return dest


def local_commit(root: Path) -> str:
    r = _run(["git", "rev-parse", "HEAD"], cwd=root)
    return r.stdout.strip() if r.returncode == 0 else "local"


def meta(root: Path, origin: str, commit: str) -> dict:
    d = wizardmd.front_matter((root / "wizard.md").read_text())
    cfg = json.loads((root / d["runtime"]["config"]).read_text())
    return {
        "id": d["id"], "name": d.get("name", d["id"]), "type": d["type"], "category": d.get("category"),
        "version": str(d["version"]), "summary": d["summary"], "source": d.get("source"), "origin": origin,
        "system": bool(d.get("system", False)), "provides": d.get("provides") or [], "uses": d.get("uses") or [],
        "needs": d.get("needs") or [], "mcp": d.get("mcp"), "skill": d.get("skill"), "view": d.get("view"),
        "health": d.get("health"), "recommends": d.get("recommends"), "port": cfg.get("port"),
        "architectures": cfg.get("supported_architectures") or [], "commit": commit, "publisher": d.get("publisher"),
    }


def loopback_compose(text: str) -> str:
    """Publish the app's main port on 127.0.0.1 only.

    Runtipi's own "open port" publishes on every interface, which on a VPS (and always with rootless Docker,
    whose port driver bypasses the host firewall) means the public internet. We install with openPort off and
    add an explicit loopback mapping instead, so the app answers on the box and Tailscale or Cloudflare Tunnel
    carry it further. Apps that already declare their own ports keep them.
    """
    doc = yaml.safe_load(text)
    for svc in (doc.get("services") or {}).values():
        xr = (svc or {}).get("x-runtipi") or {}
        if not xr.get("is_main") or not xr.get("internal_port"):
            continue
        if not svc.get("ports"):
            svc["ports"] = [f"127.0.0.1:${{APP_PORT}}:{xr['internal_port']}"]
        # Without internal_port, Runtipi's "Open port" toggle (on by default in its install form) adds nothing,
        # so installing from the Runtipi dashboard is just as box-only as `wam install`.
        xr.pop("internal_port", None)
    return yaml.safe_dump(doc, sort_keys=False, width=1000, allow_unicode=True)


def needs_amd64_emulation(cfg: dict, machine: str | None = None, binfmt: Path | None = None) -> bool:
    """amd64-only app on an arm64 box that can emulate amd64 (Runtipi refuses it otherwise)."""
    import platform as _platform
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(machine or _platform.machine(), machine or _platform.machine())
    archs = cfg.get("supported_architectures") or []
    return arch == "arm64" and "amd64" in archs and "arm64" not in archs and amd64_emulation(binfmt or BINFMT)


def pin_amd64(text: str) -> str:
    """Run every service of the app as linux/amd64 (through the box's Rosetta/qemu emulation)."""
    doc = yaml.safe_load(text)
    for svc in (doc.get("services") or {}).values():
        if isinstance(svc, dict):
            svc.setdefault("platform", "linux/amd64")
    return yaml.safe_dump(doc, sort_keys=False, width=1000, allow_unicode=True)


def add_app(out: Path, root: Path, m: dict, bump_from: str | None = None) -> None:
    """Write one app into the local store. `bump_from` is the commit this box had before: if the commit changed,
    tipi_version is raised past the old local value so Runtipi shows its own "Update available" badge even when
    the author forgot to bump it."""
    d = wizardmd.front_matter((root / "wizard.md").read_text())
    dst = out / "apps" / m["id"]
    old_tipi = 0
    if (dst / "config.json").is_file():
        try:
            old_tipi = int(json.loads((dst / "config.json").read_text()).get("tipi_version") or 0)
        except (ValueError, TypeError):
            old_tipi = 0
    shutil.rmtree(dst, ignore_errors=True)
    (dst / "metadata").mkdir(parents=True)
    cfg = json.loads((root / d["runtime"]["config"]).read_text())
    cfg["id"] = m["id"]
    if bump_from and bump_from != m["commit"]:
        cfg["tipi_version"] = max(int(cfg.get("tipi_version") or 1), old_tipi + 1)
    elif old_tipi > int(cfg.get("tipi_version") or 1):
        cfg["tipi_version"] = old_tipi
    cat = {"agent": ["ai", "featured"], "interface": ["ai"], "system": ["utilities"]}.get(m.get("category") or "")
    if cat:
        cfg["categories"] = cat
    (dst / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    compose = loopback_compose((root / d["runtime"]["compose"]).read_text())
    if needs_amd64_emulation(cfg):
        cfg["supported_architectures"] = sorted(set(cfg.get("supported_architectures") or []) | {"arm64"})
        (dst / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        compose = pin_amd64(compose)
    (dst / "docker-compose.yml").write_text(compose)
    rt_dir = (root / d["runtime"]["config"]).parent
    for cand in (rt_dir / "metadata" / "logo.jpg", rt_dir / "logo.jpg", root / "metadata" / "logo.jpg"):
        if cand.is_file():
            shutil.copy2(cand, dst / "metadata" / "logo.jpg")
            break
    desc = wizardmd.body((root / "wizard.md").read_text())
    if not desc.lstrip().startswith("# "):
        desc = f"# {m['name']}\n\n{desc}"
    (dst / "metadata" / "description.md").write_text(desc + "\n")
    sk = out / "skills" / m["id"]
    shutil.rmtree(sk, ignore_errors=True)
    if m.get("skill"):
        sk.mkdir(parents=True)
        shutil.copy2(root / m["skill"], sk / "SKILL.md")


def remove_app(out: Path, app_id: str) -> None:
    shutil.rmtree(out / "apps" / app_id, ignore_errors=True)
    shutil.rmtree(out / "skills" / app_id, ignore_errors=True)


def read_catalog(out: Path) -> dict:
    p = out / "catalog.json"
    return json.loads(p.read_text()) if p.is_file() else {"apps": [], "skipped": []}


def write_catalog(out: Path, metas: list[dict]) -> dict:
    cat = {"apps": sorted(metas, key=lambda a: a["id"]), "skipped": []}
    (out / "apps").mkdir(parents=True, exist_ok=True)
    (out / "catalog.json").write_text(json.dumps(cat, indent=2) + "\n")
    (out / "README.md").write_text(
        "# Wizard apps on this box (generated)\n\nWritten by wizard-app-manager: only the apps installed here, each pinned "
        "to the commit the Wizard App Index listed and re-checked locally. Do not edit by hand.\n\n"
        + "\n".join(f"- `{a['id']}` {a['version']} @ {str(a['commit'])[:12]} ({a['origin']})" for a in cat["apps"]) + "\n")
    return cat


def commit_store(out: Path) -> str:
    """Keep the store as a git repo so Runtipi can pull it. Returns HEAD."""
    out.mkdir(parents=True, exist_ok=True)
    if not (out / ".git").is_dir():
        _run(["git", "init", "-q", "-b", "main"], cwd=out)
    env = dict(os.environ, GIT_AUTHOR_NAME="wizard-app-manager", GIT_AUTHOR_EMAIL="wam@localhost",
               GIT_COMMITTER_NAME="wizard-app-manager", GIT_COMMITTER_EMAIL="wam@localhost")
    subprocess.run(["git", "add", "-A"], cwd=out, check=True, env=env)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out, env=env).returncode:
        subprocess.run(["git", "commit", "-q", "-m", "update installed apps"], cwd=out, check=True, env=env)
    _run(["git", "update-server-info"], cwd=out)
    return _run(["git", "rev-parse", "HEAD"], cwd=out).stdout.strip()


def runtipi_network() -> str:
    r = _run(["docker", "network", "ls", "--format", "{{.Name}}"])
    for name in r.stdout.split():
        if name.endswith("tipi_main_network"):
            return name
    raise RuntimeError("Runtipi's main Docker network not found; is Runtipi running?")


def ensure_store_server(out: Path) -> str:
    """Run the store's git server on the Runtipi network (nothing on the host). Returns the URL Runtipi uses."""
    url = f"http://{STORE_CONTAINER}:{STORE_PORT}/store.git"
    state = _run(["docker", "inspect", "-f", "{{.State.Running}}", STORE_CONTAINER]).stdout.strip()
    if state == "true":
        return url
    _run(["docker", "rm", "-f", STORE_CONTAINER])
    server = Path(__file__).resolve().with_name("store_server.py")
    r = _run(["docker", "run", "-d", "--name", STORE_CONTAINER, "--restart", "unless-stopped",
              "--network", runtipi_network(), "--read-only", "--tmpfs", "/tmp:size=16m",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "128m", "--pids-limit", "64",
              "-v", f"{out}:/srv/store:ro", "-v", f"{server}:/srv/store_server.py:ro",
              STORE_IMAGE, "python3", "/srv/store_server.py"], timeout=600)
    if r.returncode:
        raise RuntimeError(f"could not start {STORE_CONTAINER}: {r.stderr.strip()[:300]}")
    # Runtipi clones as soon as the store is registered: wait until the server answers on the network.
    import time
    probe = ["docker", "exec", STORE_CONTAINER, "python3", "-c",
             f"import urllib.request;urllib.request.urlopen('http://127.0.0.1:{STORE_PORT}/healthz',timeout=2)"]
    for _ in range(60):
        if _run(probe, timeout=10).returncode == 0:
            return url
        time.sleep(0.5)
    raise RuntimeError(f"{STORE_CONTAINER} started but never answered on :{STORE_PORT}")


BINFMT = Path("/proc/sys/fs/binfmt_misc")


def amd64_emulation(binfmt: Path = BINFMT) -> bool:
    """An arm64 box that runs amd64 images through Rosetta (Apple VMs) or qemu-user."""
    return any((binfmt / n).exists() for n in ("rosetta", "qemu-x86_64"))


def matches_arch(app: dict, arch: str, binfmt: Path = BINFMT) -> bool:
    norm = {"x86_64": "amd64", "aarch64": "arm64"}.get(arch, arch)
    archs = app.get("architectures") or []
    if not archs or norm in archs:
        return True
    if norm == "arm64" and "amd64" in archs and amd64_emulation(binfmt):
        print(f"note: {app.get('id')} is amd64-only; running it under amd64 emulation (slower)", file=sys.stderr)
        return True
    return False


# ---------------------------------------------------------------- Wizard UX skin for the stock dashboard

SKIN_CONTAINER = "wizard-skin"
SKIN_IMAGE = "nginx:1.29-alpine@sha256:5616878291a2eed594aee8db4dade5878cf7edcb475e59193904b198d9b830de"
SKIN_ROUTE = "wizard-skin.yml"


def _dashboard_middlewares() -> list[str]:
    """Whatever the stock dashboard router already uses (e.g. an SSO header), so the skin changes nothing else."""
    r = _run(["docker", "inspect", "runtipi", "-f", '{{index .Config.Labels "traefik.http.routers.dashboard.middlewares"}}'])
    names = [m.strip() for m in r.stdout.strip().split(",") if m.strip() and m.strip() != "<no value>"]
    return [m if "@" in m else f"{m}@docker" for m in names]


def skin_on(runtipi_root: Path, home: Path) -> str:
    # Copy the skin to a stable place: bind-mounting the git checkout breaks when git replaces the folder.
    src = Path(__file__).resolve().parent.parent / "skin"
    skin_dir = home / "skin"
    shutil.rmtree(skin_dir, ignore_errors=True)
    shutil.copytree(src, skin_dir)
    _run(["docker", "rm", "-f", SKIN_CONTAINER])
    r = _run(["docker", "run", "-d", "--name", SKIN_CONTAINER, "--restart", "unless-stopped", "--user", "101:101",
              "--network", runtipi_network(), "--read-only", "--tmpfs", "/tmp:size=32m,uid=101,gid=101",
              "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--memory", "64m", "--pids-limit", "64",
              "-v", f"{skin_dir / 'nginx.conf'}:/etc/nginx/nginx.conf:ro", "-v", f"{skin_dir}:/srv/skin:ro",
              SKIN_IMAGE], timeout=600)
    if r.returncode:
        raise RuntimeError(f"could not start {SKIN_CONTAINER}: {r.stderr.strip()[:300]}")
    mws = _dashboard_middlewares()
    route = {"http": {
        "routers": {"wizard-skin": {"rule": "PathPrefix(`/`)", "priority": 20, "entryPoints": ["web"],
                                    "service": "wizard-skin", **({"middlewares": mws} if mws else {})}},
        "services": {"wizard-skin": {"loadBalancer": {"servers": [{"url": f"http://{SKIN_CONTAINER}:8080"}]}}},
    }}
    dyn = runtipi_root / "traefik" / "dynamic"
    (dyn / SKIN_ROUTE).write_text("# Written by `wam skin on`; remove with `wam skin off`.\n"
                                  + yaml.safe_dump(route, sort_keys=False))
    return str(dyn / SKIN_ROUTE)


def skin_off(runtipi_root: Path) -> None:
    (runtipi_root / "traefik" / "dynamic" / SKIN_ROUTE).unlink(missing_ok=True)
    _run(["docker", "rm", "-f", SKIN_CONTAINER])
