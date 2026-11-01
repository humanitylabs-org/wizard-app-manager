"""Quick update: swap only the Wizard UX shell image in place when that is all a release changes.

A release that differs from the installed one ONLY in the Wizard UX shell's pins (its component commit, image digest
and default-app source commit; name/about/notes/date/version are just words) needs none of the installer's 10-20
minutes: the App Manager, Agent Service, Hermes, Troubleshooter and every other app stay exactly as they are. This
module applies such a release the way the installer's own upgrade steps do, just without the rest of the rerun:

1. write the target release-pins.json (the published bytes, the same file get.sh writes);
2. refresh the App Manager's pinned index view from it, exactly like install.sh (rewrite the public pin asset,
   recreate the wizard-release-index sidecar and the App Manager so its index cache forgets the old commit);
3. `release_updates` moves the shell to the release's source commit (Runtipi's queued, backed-up app update; on an
   alongside box `app_update` rewrites the user-config override with the new image digest first);
4. the installer's `wam store-data` moves the Store adapter to the new shell commit (it runs from that commit);
5. the installer's read-back (get.sh's own read-back program, run from this checkout) records ~/.wizard-os/release.json
   exactly as a full run would, and the shell container must be healthy and serve the new image's files.

Any failure rolls every touched file and the shell back to the installed release and then hands the update to the
full installer (the documented detached rerun), so the owner still ends on the release they asked for.

Only alongside boxes whose last install read back cleanly (`pinned`) and whose local pins equal the published manifest
take this path; everything else (fresh-mode boxes, drift, a failed or interrupted run, any other component moving,
a hand-edited checkout) uses the full installer. The Agent Service is deliberately not swapped here: its release also
carries the host Hermes plugin and can need a Hermes restart, which only the installer coordinates.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

QUICK_APPS = ("wizard-ux",)
META = frozenset({"version", "name", "date", "about", "notes"})      # words only; never change what runs
SLOT_FIELDS = frozenset({"commit", "index_version", "note"})        # a quick app's default-app row may move these
FALLBACK_ENV = "WIZARD_QUICK_FALLBACK"   # "0": stop after a rollback instead of running the full installer (test rigs)
SHELL_HEALTH_SECONDS = 120
MANAGER_HEALTH_SECONDS = 180
_docs: dict[tuple[str, str], tuple[dict, bytes]] = {}             # release manifests are immutable once published


class QuickError(RuntimeError):
    pass


def _home(home: Path | None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _base(base: str | None) -> str:
    from .updates import RELEASE_BASE
    return (base or os.environ.get("WIZARD_RELEASE_BASE") or RELEASE_BASE).rstrip("/")


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def release_doc(version: str, base: str | None = None, timeout: float = 10) -> tuple[dict, bytes] | None:
    """The published manifest <base>/<version>.json and its exact bytes (cached: a release file never changes)."""
    from .updates import VERSION
    if not version or not VERSION.fullmatch(version):
        return None
    key = (_base(base), version)
    if key in _docs:
        return _docs[key]
    try:
        with urllib.request.urlopen(f"{key[0]}/{version}.json", timeout=timeout) as r:
            raw = r.read(2 * 1024 * 1024)
        doc = json.loads(raw)
    except (OSError, ValueError):
        return None                                   # misses are not cached: the next check retries
    if not isinstance(doc, dict) or doc.get("version") != version:
        return None
    _docs[key] = (doc, raw)
    return _docs[key]


def _repo_of(ref) -> str:
    return re.sub(r"(@.*|:[^/:]*)$", "", str(ref or ""))


def changes(have: dict, want: dict) -> list[str] | None:
    """The quick apps whose pins `want` moves, or None when anything else differs (then only the installer may apply it).

    [] means the two releases run exactly the same bits (only words differ)."""
    if not isinstance(have, dict) or not isinstance(want, dict) or set(have) != set(want):
        return None
    moved: set[str] = set()
    for key in have:
        a, b = have[key], want[key]
        if key in META or a == b:
            continue
        if key in ("components", "images"):
            if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
                return None
            for name in a:
                if a[name] == b[name]:
                    continue
                if name not in QUICK_APPS or not isinstance(a[name], dict) or not isinstance(b[name], dict):
                    return None
                if key == "components" and a[name].get("repo") != b[name].get("repo"):
                    return None
                if key == "images" and _repo_of(a[name].get("ref")) != _repo_of(b[name].get("ref")):
                    return None
                moved.add(name)
        elif key == "default_apps":
            if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
                return None
            for slot in a:
                ra, rb = a[slot], b[slot]
                if ra == rb:
                    continue
                if not isinstance(ra, dict) or not isinstance(rb, dict) or ra.get("app") not in QUICK_APPS:
                    return None
                if set(ra) - SLOT_FIELDS != set(rb) - SLOT_FIELDS or any(
                        ra[k] != rb[k] for k in ra if k not in SLOT_FIELDS):
                    return None                       # the slot's app, repo or install flags changed
                moved.add(ra["app"])
        else:
            return None                               # install, hermes or anything new: the full installer decides
    return sorted(moved)


def wam_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(wam_dir()), *args], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def checkout_state() -> tuple[str, bool]:
    """(HEAD commit, clean) of the App Manager checkout this code runs from."""
    return _git("rev-parse", "HEAD"), not _git("status", "--porcelain", "--untracked-files=no")


def plan(home: Path | None, want: str | None, base: str | None = None, checkout=None) -> dict:
    """Can `want` be applied as a quick update here? {"quick": bool, "apps": [...], "reason": "..."}."""
    from . import updates
    h = _home(home)
    no = lambda why: {"quick": False, "apps": [], "reason": why}   # noqa: E731
    receipt = _read(h / "release.json") or {}
    have = receipt.get("release")
    if receipt.get("mode") != "alongside" or not (h / "alongside.json").is_file():
        return no("only alongside boxes take the quick path")
    if receipt.get("status") != "pinned" or not isinstance(have, str):
        return no("the last install did not read back cleanly")
    if not want or want == have:
        return no("nothing to move to")
    if updates.install_state(h) in updates.ACTIVE:
        return no("an install is running")
    local = _read(h / "release-pins.json")
    if not local or local.get("version") != have:
        return no("this box's release pins are not the installed release")
    published = release_doc(have, base)
    if not published or published[0] != local:
        return no("the installed pins differ from the published release")
    try:
        installed_apps = {a.get("id"): a for a in json.loads((h / "apps.json").read_text()).get("apps") or []}
    except (OSError, ValueError, AttributeError):
        return no("could not read the installed apps")
    for row in (local.get("default_apps") or {}).values():
        app = installed_apps.get((row or {}).get("app"))
        if row and row.get("commit") and app and app.get("commit") != row["commit"]:
            # The box no longer matches its receipt (e.g. the owner updated Files): the quick read-back would fail
            # on it. Leave that box to the installer's documented release_updates behaviour, never start here.
            return no(f"{row['app']} moved since the last install")
    from . import release_pins
    pinned = (_read(h / "alongside.json") or {}).get("images") or {}
    for app, im in (local.get("images") or {}).items():
        if app in QUICK_APPS or app not in pinned:
            continue
        try:
            same = pinned[app] == release_pins.image_ref(im)
        except ValueError:
            same = False
        if not same:      # e.g. an owner/test image override: the read-back would put it back; leave it to the installer
            return no(f"{app} runs another image than the release")
    target = release_doc(want, base)
    if not target:
        return no(f"could not read release {want}")
    apps = changes(local, target[0])
    if apps is None:
        return no("the release changes more than the Wizard UX shell")
    commit, clean = (checkout or checkout_state)()
    if commit != ((local.get("components") or {}).get("wizard-app-manager") or {}).get("commit") or not clean:
        return no("the App Manager checkout is not the release's commit")
    try:
        tmp = h / f".quick-check-{os.getpid()}.json"
        tmp.write_bytes(target[1])
        try:
            release_pins.load(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
    except (ValueError, OSError, KeyError, TypeError) as e:
        return no(f"release {want} pins are invalid: {e}"[:200])
    return {"quick": True, "apps": apps, "reason": ""}


# ---------------------------------------------------------------- the runner


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(path.name + ".quick.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    tmp.replace(path)


class Ops:
    """The real box operations (a test passes its own)."""

    def __init__(self, home: Path, log):
        self.home, self.log = home, log
        self._c = None

    # -- helpers
    def config(self):
        if self._c is None:
            from .cli import Config
            self._c = Config()
        return self._c

    def docker(self) -> list[str]:
        from .agent_link import _docker
        return _docker()

    def runtipi_root(self) -> Path:
        return self.config().runtipi_root

    def files(self) -> list[Path]:
        """Every file this update can change (snapshotted first, restored on rollback)."""
        rt = self.runtipi_root()
        return [self.home / "release.json", self.home / "release-pins.json", self.home / "alongside.json",
                rt / "user-config" / "tipi-compose.yml", rt / "user-config" / ".wizard-release-index" / "release-pins.json",
                *[rt / "user-config" / "wizardapps" / app / "docker-compose.yml" for app in QUICK_APPS]]

    def dirs(self) -> list[Path]:
        """Directories this update can change: the staged store recipe (resolve_any rewrites it and its
        .wizard-selection before Runtipi's update; a rollback must not leave the target recipe staged)."""
        rt = self.runtipi_root()
        return [rt / "repos" / "wizardapps" / "apps" / app for app in QUICK_APPS]

    def container(self, app: str) -> str:
        p = subprocess.run(self.docker() + ["ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=30)
        names = [n for n in p.stdout.split() if n.startswith(app + "_wizardapps-")]
        return names[0] if len(names) == 1 else ""

    def digests(self, app: str) -> list[str]:
        c = self.container(app)
        if not c:
            return []
        d = self.docker()
        image = subprocess.run(d + ["inspect", "--format", "{{.Image}}", c], capture_output=True, text=True,
                               timeout=30).stdout.strip()
        out = subprocess.run(d + ["image", "inspect", "--format", "{{json .RepoDigests}}", image], capture_output=True,
                             text=True, timeout=30).stdout.strip()
        try:
            return [x.split("@", 1)[1] for x in json.loads(out or "[]") if "@" in x]
        except ValueError:
            return []

    # -- steps
    def refresh_index(self, pins_path: Path, manager_image: str) -> None:
        """install.sh's pinned-index refresh: rewrite the public pin asset, recreate the sidecar and the manager."""
        from . import release_pins
        root = self.runtipi_root()
        release_pins.configure_manager(str(root / "user-config" / "tipi-compose.yml"), manager_image, str(pins_path))
        compose = self.docker() + ["compose", "-p", "runtipi", "-f", "docker-compose.yml", "-f", "user-config/tipi-compose.yml"]
        for args in (["rm", "--stop", "--force", "wizard-release-index"],
                     ["up", "-d", "--force-recreate", "--wait", "wizard-release-index"],
                     ["up", "-d", "--force-recreate", "--no-deps", "runtipi"]):
            p = subprocess.run(compose + args, cwd=root, capture_output=True, text=True, timeout=600)
            if p.returncode:
                raise QuickError(f"compose {' '.join(args[:2])} failed: {(p.stderr or p.stdout).strip()[-200:]}")
        from .runtipi import read_env
        port = read_env(root / ".env").get("NGINX_PORT") or "80"
        deadline = time.time() + MANAGER_HEALTH_SECONDS
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5):
                    return
            except OSError:
                time.sleep(2)
        raise QuickError("the App Manager did not answer its health check after the index refresh")

    def update_apps(self, apps: list[str]) -> None:
        """The installer's release_updates: move release defaults (here only the shell) to the release's source commit."""
        from . import cli
        c = self.config()
        rt = c.runtipi()
        failed: dict = {}
        cli.release_updates(c, rt, failed, only=set(apps))   # never another default the owner moved
        bad = {k: v for k, v in failed.items()}
        if bad:
            raise QuickError("; ".join(f"{k}: {v}" for k, v in bad.items())[:300])
        cli.write_registry(c, rt)

    def ensure_image(self, app: str, digest: str) -> None:
        """An image-only move keeps the source commit, so release_updates skips it: update like get.sh's read-back."""
        if digest in self.digests(app):
            return
        from . import cli
        c = self.config()
        res = cli.app_update(c, app)
        cli.write_registry(c)
        if res.get("error"):
            raise QuickError(f"{app}: {res['error']}")
        for _ in range(10):                  # Runtipi rolls a failed pull back to the old container: say so now
            if digest in self.digests(app):
                return
            time.sleep(3)
        raise QuickError(f"{app} did not switch to {digest[:19]}… (image pull or start failed; Runtipi kept the old one)")

    def store_data(self) -> None:
        """The installer's `wam store-data`: the Store adapter runs from the installed shell's commit (store_data.py),
        so a moved shell needs it refreshed. Like the installer, a failure here degrades the Store, not the update."""
        from . import cli
        try:
            out = cli.store_data_cmd(self.config())
        except (Exception, SystemExit) as e:   # noqa: BLE001
            out = {"state": f"error: {type(e).__name__}: {e}"[:200]}
        state = out.get("state")
        self.log(f"App Store data for the installed shell: {state}"
                 + ("" if state in ("ready", "skipped") else " (degraded, as a full update would report it; `wam store-data` shows why)"))

    def readback(self, pins_path: Path) -> dict:
        """get.sh's own read-back (from this checkout's get.sh), so release.json is written exactly like a full run."""
        script = readback_program(wam_dir() / "get.sh")
        record = self.home / "release.json"
        p = subprocess.run([sys.executable, "-", str(pins_path), str(wam_dir()), "alongside", str(record), "0", "0"],
                           input=script, capture_output=True, text=True, timeout=1800)
        for line in (p.stdout + p.stderr).splitlines():
            self.log(line)
        rec = _read(record) or {}
        if p.returncode or rec.get("status") not in ("pinned", "drift"):
            raise QuickError(f"read-back: {rec.get('status') or 'no record'}; failed: "
                             + ", ".join(c["name"] for c in rec.get("checks", []) if not c.get("ok"))[:200])
        return rec

    def verify_shell(self, app: str, digest: str) -> None:
        """Healthy, on the pinned digest, and serving the new image's own files on its loopback port."""
        deadline = time.time() + SHELL_HEALTH_SECONDS
        state = "no container"
        while time.time() < deadline:
            c = self.container(app)
            if c:
                state = subprocess.run(self.docker() + ["inspect", "--format",
                                                        "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", c],
                                       capture_output=True, text=True, timeout=30).stdout.strip()
                if state in ("healthy", "running") and digest in self.digests(app):
                    break
            time.sleep(3)
        else:
            raise QuickError(f"{app} is not healthy on the release image ({state})")
        port = int((((_read(self.home / "alongside.json") or {}).get("shell")) or {}).get("port") or 0)
        if app != "wizard-ux" or not port:
            return
        want = subprocess.run(self.docker() + ["exec", self.container(app), "cat", "/usr/share/nginx/wizard-ux/index.html"],
                              capture_output=True, timeout=30).stdout
        got = b""
        for _ in range(20):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/wizard-os-home/index.html", timeout=5) as r:
                    got = r.read()
                if got == want:
                    return
            except OSError:
                pass
            time.sleep(2)
        raise QuickError("the shell does not serve the release image's files")

    def start_installer(self, version: str, auto: bool) -> bool:
        from .updates import INSTALLER
        env = ("WIZARD_HERMES_PROMOTION= " if auto else "") + f"WIZARD_RELEASE={version} "
        # pipefail: a failed download must not look like a started installer (`false | bash -s start` exits 0).
        r = subprocess.run(["bash", "-c", f"set -o pipefail; curl -fsSL {INSTALLER} | {env}bash -s start"], capture_output=True,
                           text=True, timeout=600)
        for line in (r.stdout + r.stderr).splitlines():
            self.log(line)
        return r.returncode == 0


def readback_program(get_sh: Path) -> str:
    """The Python read-back program get.sh runs after install.sh (its `<<'PY'` heredoc), verbatim."""
    lines = get_sh.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith('python3 - "$PINS" "$DIR" "${WIZARD_MODE:-fresh}" "$RECORD"') and ln.endswith("<<'PY' || vrc=$?"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "PY")
    return "\n".join(lines[start + 1:end]) + "\n"


def _lock(home: Path) -> list[int] | None:
    """Hold install.lock like the installer's runner (taken under install.start.lock, get.sh's order)."""
    fds: list[int] = []
    try:
        for name in ("install.start.lock", "install.lock"):
            fds.append(os.open(home / name, os.O_RDWR | os.O_CREAT, 0o600))
            fcntl.flock(fds[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        for fd in fds:
            os.close(fd)
        return None
    os.close(fds.pop(0))                   # keep only install.lock: a later `os.sh start` sees "already running"
    return fds


def _status(home: Path, **st) -> None:
    from .updates import boot_id
    path = home / "install-status.json"
    cur = _read(path) or {}
    if st.get("state") == "running" and st.get("step") == "starting":
        cur = {"tailscale_login_url": None, "wizard_url": None, "notes": [], "restart_pending": False,
               "started_at": _now(), "pid": os.getpid(), "boot_id": boot_id()}
    cur.update(st, quick=True, updated_at=_now())
    _atomic(path, (json.dumps(cur, indent=2) + "\n").encode())


def run(version: str, home: Path | None = None, base: str | None = None, auto: bool = False, ops=None,
        checkout=None) -> dict:
    """The detached quick-update runner (`wam updates --quick-apply <version>`)."""
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    logf = open(h / "install.log", "a")

    def log(msg: str) -> None:
        logf.write(f"{_now()} [quick] {msg}\n")
        logf.flush()

    fds = _lock(h)
    if fds is None:
        logf.close()
        return {"started": False, "quick": True, "note": "an install or update is already running"}
    ops = ops or Ops(h, log)
    snapshot: dict[Path, bytes | None] = {}
    dirs: dict[Path, Path] = {}
    touched = set_pins_env = False
    started = time.monotonic()
    result: dict = {"started": True, "quick": True, "release": version}
    try:
        p = plan(h, version, base, checkout)         # re-checked under the lock, before this run marks itself running
        _status(h, state="running", step="starting", message="")
        log(f"quick update to {version}: only the Wizard UX shell changes")
        if not p["quick"]:
            raise QuickError(f"not a quick update: {p['reason']}")
        doc = release_doc(version, base)
        assert doc is not None                        # plan() just read it
        target, raw = doc
        if not os.environ.get("WIZARD_RELEASE_PINS"):   # the wam wrapper sets it; a bare `python -m wam` may not
            os.environ["WIZARD_RELEASE_PINS"] = str(h / "release-pins.json")
            set_pins_env = True
        old = _read(h / "release-pins.json") or {}
        for f in ops.files():
            try:
                snapshot[f] = f.read_bytes()
            except OSError:
                snapshot[f] = None
        saved = h / ".quick-rollback"
        shutil.rmtree(saved, ignore_errors=True)
        for i, d in enumerate(ops.dirs()):
            dirs[d] = saved / str(i)
            if d.is_dir():
                shutil.copytree(d, dirs[d], symlinks=True)
        pins = h / "release-pins.json"
        from . import release_pins
        manager_image = release_pins.image_ref((target.get("images") or {})["wizard-app-manager"])
        touched = True
        _status(h, step="switching the shell", message="")
        _atomic(pins, raw, 0o644 if not snapshot.get(pins) else (os.stat(pins).st_mode & 0o777))
        ops.refresh_index(pins, manager_image)
        log("App Manager index now serves the release's pins")
        ops.update_apps(p["apps"])
        for app in p["apps"]:
            want = ((target.get("images") or {}).get(app) or {}).get("digest")
            if want:
                ops.ensure_image(app, want)
        ops.store_data()
        _status(h, step="checking", message="")
        rec = ops.readback(pins)
        for app in p["apps"]:
            want = ((target.get("images") or {}).get(app) or {}).get("digest")
            if want:
                ops.verify_shell(app, want)
        secs = round(time.monotonic() - started)
        log(f"quick update to {version} done in {secs}s ({rec.get('status')}, was {old.get('version')})")
        _status(h, state="done", step="done", exit_code=0, failure_reason=None, message="", seconds=secs)
        return {**result, "done": True, "seconds": secs, "status": rec.get("status")}
    except (Exception, SystemExit) as e:   # noqa: BLE001 - wam's CLI helpers exit with SystemExit; roll back on all
        why = f"{type(e).__name__}: {e}"[:300] if not isinstance(e, QuickError) else str(e)[:300]
        log(f"quick update failed: {why}")
        rolled = True
        if touched:
            rolled = _rollback(h, ops, snapshot, log, dirs)
        fallback = os.environ.get(FALLBACK_ENV, "1") != "0"
        if fallback:
            _status(h, state="running", step="handing over to the full update",
                    message=f"The quick update did not finish ({why}); running the full update instead.")
            os.close(fds.pop())                       # the installer's own runner takes the lock now
            ok = ops.start_installer(version, auto)
            log("full installer started" if ok else "full installer did not start")
            if not ok:
                _status(h, state="failed", exit_code=1, failure_reason=f"quick update failed ({why}); the full "
                        "update did not start", message="")
            return {**result, "done": False, "error": why, "rolled_back": rolled, "fallback": ok}
        _status(h, state="failed", exit_code=1, failure_reason=f"quick update failed: {why}", message="")
        return {**result, "done": False, "error": why, "rolled_back": rolled, "fallback": False}
    finally:
        shutil.rmtree(h / ".quick-rollback", ignore_errors=True)
        if set_pins_env:
            os.environ.pop("WIZARD_RELEASE_PINS", None)
        for fd in fds:
            os.close(fd)
        logf.close()


def _rollback(h: Path, ops, snapshot: dict, log, dirs: dict | None = None) -> bool:
    """Put back every touched file, the index view and the shell the box ran before. True when the old shell runs."""
    ok = True
    for d, copy in (dirs or {}).items():
        try:
            shutil.rmtree(d, ignore_errors=True)
            if copy.is_dir():
                shutil.copytree(copy, d, symlinks=True)
        except OSError as e:
            ok = False
            log(f"rollback: could not restore {d}: {e}")
    for f, data in snapshot.items():
        try:
            if data is None:
                f.unlink(missing_ok=True)
            else:
                _atomic(f, data, f.stat().st_mode & 0o777 if f.exists() else 0o600)
        except OSError as e:
            ok = False
            log(f"rollback: could not restore {f}: {e}")
    old = _read(h / "release-pins.json") or {}
    images = old.get("images") or {}

    def phase(what: str, fn) -> None:
        # Each phase is independent: one failing (e.g. Docker briefly away) must not skip putting back the rest.
        nonlocal ok
        try:
            fn()
        except (Exception, SystemExit) as e:   # noqa: BLE001
            ok = False
            log(f"rollback incomplete ({what}): {type(e).__name__}: {e}"[:300])

    from . import release_pins
    phase("release index", lambda: ops.refresh_index(h / "release-pins.json",
                                                     release_pins.image_ref(images["wizard-app-manager"])))
    phase("apps", lambda: ops.update_apps(list(QUICK_APPS)))
    for app in QUICK_APPS:
        want = (images.get(app) or {}).get("digest")
        if want:
            phase(app, lambda app=app, want=want: (ops.ensure_image(app, want), ops.verify_shell(app, want)))
    phase("store data", ops.store_data)        # the Store adapter back on the old shell's commit
    log(f"rolled back to {old.get('version')}" if ok else f"rollback to {old.get('version')} incomplete")
    return ok
