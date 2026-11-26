"""Install what the owner's agent built on this Wizard, with one owner tap (#997, switch wizard/install-agent-builds).

The agent builds an app with `wam new` into its own `~/.wizard-os/dev/<id>`, which lives on this box at
`<app-data>/<store>/<agent app>/data/home/.wizard-os/dev/<id>`. The agent can't install anything itself (no Docker in
its container, and Store writes from the box are refused: store_install.from_box). It ends its build with an install
link (`wam build-link`) naming only the app id and the source hash; the owner taps Install on another device, and the
Store route (`POST /wizard-os-home/store-install/<id>`, the same owner gate as every Store install) sends the
selection `{"source": "build", "build": "<id>", "sha256": "<64 hex>", "operation": "install" | "update"}`.

Everything is resolved here, server-side: the folder is looked up by id in the data folders of the installed agent
apps (never a path from the request), copied into a snapshot, and only the snapshot is checked and installed:
  - its source hash must equal the link's (a file changed after the link was made: refused, nothing installed);
  - `wam check --proposal` must pass, the compose must ask for no host access (`wizardmd.compose_risks`), and nothing
    is built from the snapshot (inline packaging or ready images only);
then it is written into the Wizard store folder like a private GitHub app (private.write_recipe), pinned with
`.wizard-selection`, and installed or updated through the normal lifecycle (cli.install / app_updates.store_update).
The installed record `.wizard-source` says `{"source": "agent-build", "agent": <agent app>, "sha256": ...}`; it also
carries `repo: local/<id>` + `local: true`, the identity Remove app already knows for apps with no GitHub repo.

With the switch off nothing here runs: the Store route answers a `build` selection exactly as before (refused) and
there is no `_builds/<id>` read.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

SWITCH = "wizard/install-agent-builds"
SOURCE = "agent-build"
SHA256 = re.compile(r"[a-f0-9]{64}")
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# Under <app-data>/<store>/<agent app>/: the agent's terminal HOME is /opt/data/home (the Hermes engine's data folder
# is mounted at /opt/data); a shell whose HOME is /opt/data itself writes one level up.
DEVS = (("data", "home", ".wizard-os", "dev"), ("data", ".wizard-os", "dev"))
MARKER = ".install-agent-builds.json"            # in each agent's dev folder: what `wam build-link` reads
CHANGED = "The app changed since your agent offered it. Ask it for a new link."
CURRENT = "already current"


class BuildError(ValueError):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def switch_on(home=None) -> bool:
    """Whether the owner turned wizard/install-agent-builds on (tests monkeypatch this)."""
    from . import feature_switches
    return feature_switches.is_on(SWITCH, home, timeout=4)


# --- where the agents keep their builds -------------------------------------------------------------------------

def agents(c) -> list[str]:
    """Installed agent apps of the Wizard store: hermes-agent and every app whose recipe says wizard_type agent."""
    from . import cli
    out = []
    root = Path(c.runtipi_root) / "apps" / cli.STORE
    try:
        rows = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and ID.fullmatch(p.name))
    except OSError:
        return []
    for p in rows:
        try:
            cfg = json.loads((p / "config.json").read_text())
        except (OSError, ValueError):
            cfg = {}
        if p.name == "hermes-agent" or (isinstance(cfg, dict) and cfg.get("wizard_type") == "agent"):
            out.append(p.name)
    return out


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def dev_folders(c, agent: str, *, create: bool = False) -> list[Path]:
    """The agent's `.../.wizard-os/dev` folders (DEVS) whose every part below app-data is a real folder: a symlink
    anywhere could point the App Manager at a folder outside the agent's own data. `create`: make the first one when
    its home exists (sync_markers, so a fresh build finds its marker at once)."""
    from . import cli, packaging
    top = packaging.app_data_root(Path(c.runtipi_root)) / cli.STORE / agent
    out = []
    for n, parts in enumerate(DEVS):
        p, ok = top, _real_dir(top)
        for i, part in enumerate(parts):
            if not ok:
                break
            p = p / part
            if create and n == 0 and i >= 2 and not os.path.lexists(p):
                try:
                    p.mkdir()
                    p.chmod(0o777)   # like the rest of the agent's data folder: the agent's own user writes here
                except OSError:
                    pass
            ok = _real_dir(p)
        if ok:
            out.append(p)
    return out


def find(c, build: str) -> tuple[str, Path]:
    """(agent app, folder) of the build named `build`. Refuses an id no agent holds, a symlink, and the same id in
    two agents (which one the owner meant can't be told)."""
    if not isinstance(build, str) or not ID.fullmatch(build):
        raise BuildError("bad app id")
    hits, links = [], []
    for agent, base in ((a, b) for a in agents(c) for b in dev_folders(c, a)):
        cand = base / build
        try:
            mode = os.lstat(cand).st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or not cand.resolve().is_relative_to(base.resolve()):
            links.append(agent)
            continue
        hits.append((agent, cand))
    if len(hits) > 1:
        raise BuildError(f"Two copies of an app called {build} were built ({', '.join(a for a, _ in hits)}); "
                         f"ask the agent you meant to rename its copy.", 409)
    if not hits:
        if links:
            raise BuildError(f"{build} in {links[0]} is not a plain folder (a link); nothing installed.", 400)
        raise BuildError(f"No agent on this Wizard has built an app called {build}.", 404)
    return hits[0]


# --- snapshot and checks ----------------------------------------------------------------------------------------

def snapshot(folder: Path, into: Path) -> tuple[Path, str]:
    """Copy the build's source files (builder.source_files: no symlinks, no secrets, size limits) into `into`;
    returns (snapshot, its source hash). Everything after this reads only the snapshot."""
    from . import builder
    snap = into / "source"
    snap.mkdir(parents=True)
    try:
        files = builder.source_files(folder)
    except ValueError as e:
        raise BuildError(f"Not installed: {e}") from None
    root = folder.resolve()
    for p in files:
        dest = snap / p.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest)
    return snap, builder.source_hash(snap)


def _errors(report: dict) -> list[str]:
    return [f"{r.get('rule')}: {r.get('detail')}".strip(": ") for r in report.get("rules") or []
            if not r.get("ok") and r.get("severity") != "warning"]


def check(snap: Path, build: str) -> tuple[dict, str, Path, dict, list[str]]:
    """(manifest, wizard.md text, config path, compose, problems) of the snapshot. `problems` empty = installable."""
    from . import builder, wizardmd
    import yaml
    problems: list[str] = []
    try:
        wtext = (snap / "wizard.md").read_text()
        d = wizardmd.front_matter(wtext)
    except (OSError, ValueError) as e:
        return {}, "", snap, {}, [f"wizard.md can't be read ({type(e).__name__})"]
    if not isinstance(d, dict) or d.get("id") != build:
        return d if isinstance(d, dict) else {}, wtext, snap, {}, [f"wizard.md id is not {build}"]
    try:
        report = builder.check_app(snap, proposal=True)
    except Exception as e:  # noqa: BLE001 - a checker crash is a failed check, never an install
        report = {"ok": False, "rules": [{"rule": "wam check", "ok": False, "severity": "error",
                                          "detail": f"{type(e).__name__}: {str(e)[:200]}"}]}
    if not report.get("ok"):
        problems += _errors(report) or ["wam check failed"]
    rt = d.get("runtime") or {}
    cfg_p, comp_p = snap / str(rt.get("config", "")), snap / str(rt.get("compose", ""))
    compose: dict = {}
    if (not rt.get("config") or not rt.get("compose") or not cfg_p.resolve().is_relative_to(snap.resolve())
            or not comp_p.resolve().is_relative_to(snap.resolve()) or not cfg_p.is_file() or not comp_p.is_file()):
        problems.append("wizard.md runtime.config/compose missing")
        return d, wtext, cfg_p, compose, problems
    ctext = comp_p.read_text()
    risks = [r for r in wizardmd.compose_risks(ctext) if not any(r in p for p in problems)]   # check_app may say it
    if risks:
        problems.append("it asks for host-level access: " + "; ".join(risks))
    try:
        compose = yaml.safe_load(ctext) or {}
    except yaml.YAMLError:
        problems.append("its compose file is not valid YAML")
        compose = {}
    if rt.get("package") == "build" or any(isinstance(s, dict) and s.get("build")
                                           for s in (compose.get("services") or {}).values()):
        problems.append("it builds an image from its own files (build:); only inline apps and ready-made "
                        "images can be installed from your agent's build")
    return d, wtext, cfg_p, compose, problems


def _pullable(compose: dict) -> list[str]:
    """Images Docker can't download without a login (none when there is no Docker CLI: the install says it then)."""
    from . import private
    docker = shutil.which("docker")
    if not docker:
        return []
    bad = []
    with tempfile.TemporaryDirectory() as cfg:   # an empty Docker config: no saved logins
        env = {**os.environ, "DOCKER_CONFIG": cfg}
        for img in private.images(compose):
            try:
                if subprocess.run([docker, "image", "inspect", img], capture_output=True, timeout=30).returncode == 0:
                    continue
                if subprocess.run([docker, "pull", "-q", img], capture_output=True, env=env, timeout=600).returncode:
                    bad.append(img)
            except (OSError, subprocess.SubprocessError):
                bad.append(img)
    return bad


def commit_of(sha256: str) -> str:
    """The 40-hex identity a build has where the store folder expects a commit (`.wizard-commit`, pins, Remove)."""
    return sha256[:40]


def installed(c, app_id: str) -> dict | None:
    """The installed app's agent-build record, or None when it is not installed or not an agent build."""
    from . import cli
    if not isinstance(app_id, str) or not ID.fullmatch(app_id):
        return None
    apps = os.path.realpath(os.path.join(str(c.runtipi_root), "apps", cli.STORE))
    record = os.path.realpath(os.path.join(apps, app_id, ".wizard-source"))
    if not record.startswith(apps + os.sep):   # an app id names one folder in the store, nothing else
        return None
    try:
        with open(record, encoding="utf-8") as f:
            src = json.loads(f.read())
    except (OSError, ValueError):
        return None
    return src if isinstance(src, dict) and src.get("source") == SOURCE else None


def _version(c, app_id: str) -> str | None:
    from . import app_updates, cli
    return app_updates._version_name(Path(c.runtipi_root) / "apps" / cli.STORE / app_id)


# --- the Install sheet's read -----------------------------------------------------------------------------------

def describe(c, build: str) -> dict:
    """GET _builds/<id>: what the sheet shows. Read-only (the snapshot is a throwaway copy)."""
    from . import cli
    agent, folder = find(c, build)
    with tempfile.TemporaryDirectory(prefix=".agent-build-") as tmp:
        snap, digest = snapshot(folder, Path(tmp))
        d, _, _, _, problems = check(snap, build)
    have = installed(c, build)
    is_installed = build in cli.installed_ids(c.runtipi())
    return {"id": build, "agent": agent, "name": str(d.get("name") or build), "summary": str(d.get("summary") or ""),
            "version": str(d.get("version") or ""), "sha256": digest, "check": "failed" if problems else "ok",
            **({"problems": problems[:5]} if problems else {}),
            "installed": ({"sha256": have.get("sha256"), "version": _version(c, build)} if have and is_installed
                          else False),
            # an app with this id that is NOT an agent build (from the Store): the sheet can't replace it
            **({"other_install": True} if is_installed and not have else {})}


# --- the selection ------------------------------------------------------------------------------------------------

def resolve(c, app_id: str, body: dict) -> dict:
    """The server-side selection for a `build` body (store_selection.resolve hands it here when the switch is on)."""
    if set(body) != {"source", "build", "sha256", "operation"}:
        raise ValueError("exact source, build, sha256 and operation required; ask your agent for a new link")
    build, sha, operation = body["build"], body["sha256"], body["operation"]
    if not all(isinstance(x, str) for x in (build, sha, operation)) or not ID.fullmatch(build) \
            or not SHA256.fullmatch(sha) or operation not in ("install", "update"):
        raise ValueError("invalid build selection")
    if build != app_id:
        raise ValueError("the build and the app id differ")
    return {"source": "build", "build": build, "sha256": sha, "operation": operation,
            # what store_install.start compares a second tap against
            "repo": f"local/{build}", "commit": commit_of(sha)}


def current(c, selection: dict) -> bool:
    """An update to exactly the build that is installed: nothing to do (`already current`)."""
    from . import cli
    have = installed(c, selection["build"])
    return bool(selection["operation"] == "update" and have and have.get("sha256") == selection["sha256"]
                and selection["build"] in cli.installed_ids(c.runtipi()))


def precheck(c, selection: dict) -> None:
    """Refuse at once (BuildError) what prepare would refuse: no such build, changed since the link, a failed check."""
    _, folder = find(c, selection["build"])
    with tempfile.TemporaryDirectory(prefix=".agent-build-") as tmp:
        snap, digest = snapshot(folder, Path(tmp))
        if digest != selection["sha256"]:
            raise BuildError(CHANGED, 409)
        problems = check(snap, selection["build"])[4]
    if problems:
        raise BuildError("Not installed: " + "; ".join(problems)[:600], 422)


_prepare_lock = threading.Lock()


def prepare(c, selection: dict) -> dict:
    """Find, snapshot, check and write the build into the Wizard store folder. Returns the install record."""
    from . import cli, packaging, private
    build, sha = selection["build"], selection["sha256"]
    agent, folder = find(c, build)
    with _prepare_lock, tempfile.TemporaryDirectory(prefix=".agent-build-") as tmp:
        snap, digest = snapshot(folder, Path(tmp))
        if digest != sha:
            raise BuildError(CHANGED, 409)
        d, wtext, cfg_p, compose, problems = check(snap, build)
        if problems:
            raise BuildError("Not installed: " + "; ".join(problems)[:600], 422)
        bad = _pullable(compose)
        if bad:
            raise BuildError("Not installed: these images can't be downloaded without a login: " + ", ".join(bad), 422)
        commit = commit_of(sha)
        root = Path(c.runtipi_root)
        try:
            from .mirrors import resolve_images
            resolve_images(compose)
            rt = d.get("runtime") or {}
            target = packaging.source_dir(packaging.app_data_root(root), cli.STORE, build, commit)
            packaging.prepare_package(rt.get("package"), compose, snap, target, build, commit)
        except packaging.PackagingError as e:
            raise BuildError(f"Not installed: {e}", 422) from None
        source_doc = {"source": SOURCE, "agent": agent, "sha256": sha, "repo": f"local/{build}", "local": True}
        src_url = str(d.get("source") or "")
        private.write_recipe(root, cli.STORE, snap, d, wtext, cfg_p, compose, commit,
                             source_url=src_url if src_url.startswith("https://") else "https://aiwizards.com/",
                             source_doc=source_doc, selection={"repo": f"local/{build}", "commit": commit})
    return {"id": build, "repo": f"local/{build}", "uses": d.get("uses") or [], "private": True,
            "install": {"commit": commit}, "agent": agent}


def execute(c, app_id: str, selection: dict) -> dict:
    """Install or update the build (store_install.start runs this in its job thread)."""
    from . import app_updates, cli
    ids = cli.installed_ids(c.runtipi())
    operation = selection["operation"]
    if operation == "install" and app_id in ids:
        raise ValueError("already installed; ask your agent for a new link to update it")
    if operation == "update":
        if app_id not in ids:
            raise ValueError("not installed; ask your agent for a new link to install it")
        if installed(c, app_id) is None:
            raise ValueError("this app came from the Store, not from your agent; nothing changed")
        if current(c, selection):
            return {"status": "running", "note": CURRENT}
    folder = Path(c.runtipi_root) / "repos" / cli.STORE / "apps" / app_id
    before = _keep(folder) if operation == "update" else None
    try:
        record = prepare(c, selection)
    except BaseException:
        _put_back(folder, before)
        raise
    if operation == "install":
        try:
            return cli.install(c, app_id, _record=record)
        except BaseException:
            if app_id not in cli.installed_ids(c.runtipi()):
                shutil.rmtree(folder, ignore_errors=True)   # nothing installed: leave no recipe behind
            raise
    # an app update: the installed data stays; a failed check rolls back from the update's own backup
    result = app_updates.store_update(c, app_id, lambda: cli.app_update(c, app_id))
    cli.write_registry(c)
    if result.get("error"):
        _put_back(folder, before)   # the store recipe follows the version that runs again
    return result


def _keep(folder: Path) -> Path | None:
    if not folder.is_dir():
        return None
    keep = Path(tempfile.mkdtemp(prefix=".agent-build-before-"))
    shutil.copytree(folder, keep / "recipe", symlinks=True)
    return keep


def _put_back(folder: Path, keep: Path | None) -> None:
    if keep is None:
        return
    try:
        from . import dirswap
        tmp = folder.with_name(f"{folder.name}.back-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(keep / "recipe", tmp, symlinks=True)
        dirswap.replace_dir(tmp, folder)
    except OSError:
        pass
    finally:
        shutil.rmtree(keep, ignore_errors=True)


# --- what `wam build-link` reads inside the agent --------------------------------------------------------------

def shell_origin(c) -> str:
    """The shell's own https origin (the one Tailscale Serve maps the Store route on), or '' when unknown."""
    from . import store_install as si
    try:
        lp = Path(c.home) / "links.json"
        host = (json.loads(lp.read_text()) if lp.is_file() else {}).get("host")
        got = sorted(si.served_origins(host)) if host else []
    except (OSError, ValueError):
        got = []
    got = got or sorted(si.gateway_origins(c.home))
    return got[0] if got else ""


def sync_markers(c, on: bool | None = None) -> list[str]:
    """Write (switch on) or remove (off) `dev/.install-agent-builds.json` in every agent's dev folder, so the agent's
    `wam build-link` knows install links work on this Wizard and which origin to print. Returns the folders changed.
    A forged marker only makes the agent print a link the Store route then refuses."""
    on = switch_on(c.home) if on is None else on
    body = json.dumps({"on": True, "origin": shell_origin(c)}) + "\n" if on else ""
    changed = []
    for base in (b for a in agents(c) for b in dev_folders(c, a, create=on)):
        m = base / MARKER
        try:
            if os.path.islink(m):
                m.unlink()
            have = m.read_text() if m.is_file() else None
            if on and have != body:
                tmp = base / f".{MARKER}.{os.getpid()}"
                tmp.write_text(body)
                tmp.chmod(0o644)
                tmp.replace(m)
                changed.append(str(base))
            elif not on and have is not None:
                m.unlink()
                changed.append(str(base))
        except OSError:
            continue
    return changed


def start_watch(c, every: float = 60.0) -> None:
    """`wam serve`: keep the agents' markers in step with the switch (the owner flips it in the shell or Tracker)."""
    def loop():
        while True:
            try:
                sync_markers(c)
            except Exception:  # noqa: BLE001 - never takes the API down
                pass
            time.sleep(every)
    threading.Thread(target=loop, name="agent-builds-markers", daemon=True).start()
