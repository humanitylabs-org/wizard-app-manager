"""Human Handoff for the box's host agent (humanitylabs-org/wizard-os#775), behind the switch `wizard/human-handoff`.

Connected accounts' "Set up the owner's Google app" (wizard-connectors skill, steps 3-8) runs its shipped
`google_cloud_setup.py` through the `hermes-human-handoff` CLI (doctor/start --purpose oauth/run/status/stop: a
fresh disposable browser profile and a private phone takeover over Tailscale Serve) and needs Python Playwright.
Nothing installed either, so the agent could never do the one-sign-in setup and the owner fell back to the manual flow.

With the switch on (Settings > Try new features or the WizardOS Tracker; off by default, always off on For everyone),
`ensure()` gives the box's host agent the pinned public release humanitylabs-org/hermes-human-handoff v0.1.0:

  - the system pieces it needs that are missing (Xvfb, x11vnc, python3-venv, git) with `sudo -n apt-get`;
  - its own scripts/install.sh from the pinned commit: a private venv with Playwright 1.62.0 and websockify, the
    verified noVNC v1.7.0, a Playwright Chromium when there is no system browser (plus that Chromium's libraries,
    `sudo -n playwright install-deps chromium`), `~/.local/bin/hermes-human-handoff`, the `human-browser-handoff`
    skill in $HERMES_HOME/skills and its five-minute cleanup timer;
  - then `hermes-human-handoff doctor`, recorded in ~/.wizard-os/human-handoff.json (`wam human-handoff` shows it).

It runs on the hourly app-update tick, and at once when `wam feature-switches` / the Tracker flips the switch
(`spawn()`). Turning the switch off again removes only what this command installed (the release's own
uninstall.sh, which refuses while a handoff is active). A hand-installed copy is never touched.

Only for a host agent (a `hermes` on this box: alongside boxes). On a fresh box the agent is the stock Hermes
container, which can't run a host browser takeover; that engine is not changed here, so the state says so.
With the switch off nothing is installed, read or written.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SWITCH = "wizard/human-handoff"
REPO = "https://github.com/humanitylabs-org/hermes-human-handoff.git"
TAG = "v0.1.0"
COMMIT = "7ae46d31e1d50533d9d7c8c01b774340b62f7e09"
STATE = "human-handoff.json"          # in the Wizard home (~/.wizard-os)
LOG = "human-handoff.log"
CLI = "hermes-human-handoff"
SKILL = "human-browser-handoff"
APT_FOR = {"Xvfb": "xvfb", "x11vnc": "x11vnc", "git": "git"}
TIMEOUT = 1800


def _home(home: Path | None) -> Path:
    from .updates import _home as h
    return h(home)


def switch_on(home: Path | None = None) -> bool | None:
    """Whether the owner turned on `wizard/human-handoff`: True, False, or None when look.json can't be read right
    now (an Agent Service restart must never uninstall it). On For everyone it is always off (tests monkeypatch this)."""
    from . import feature_switches as fs
    try:
        if fs._channel(home) != "latest":
            return False
        doc, _ = fs._pages(home, 4).get()
        if not fs._readable(doc):
            return None
        return fs._tweaks(doc).get(SWITCH, {}).get("on") is True
    except (fs.SwitchError, OSError, ValueError):
        return None


def hermes_home() -> Path:
    from .alongside import hermes_home as h
    return h()


def host_agent() -> str | None:
    """The host Hermes binary, when the box's agent runs on the host (where this CLI must run). None on a fresh box."""
    from .alongside import hermes_binary
    return hermes_binary()


def paths() -> dict[str, Path]:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return {"src": data / "wizard-human-handoff" / "src",                  # the pinned source (uninstall.sh lives here)
            "prefix": data / "hermes-human-handoff",                       # install.sh's own venv + noVNC
            "bin": Path.home() / ".local" / "bin" / CLI,
            "skill": hermes_home() / "skills" / SKILL / "SKILL.md"}


def load(home: Path | None = None) -> dict:
    try:
        st = json.loads((_home(home) / STATE).read_text())
    except (OSError, ValueError):
        return {}
    return st if isinstance(st, dict) else {}


def _save(home: Path | None, st: dict) -> dict:
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    st = {**st, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = h / (STATE + ".tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n")
    os.replace(tmp, h / STATE)
    return st


@contextlib.contextmanager
def _locked(home: Path | None):
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    with open(h / "human-handoff.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _run(argv, run=subprocess.run, timeout: int = TIMEOUT, env: dict | None = None, cwd: str | None = None):
    try:
        return run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(argv, 127, "", str(e))


def _tail(p) -> str:
    return ((p.stderr or "") + (p.stdout or "")).strip()[-400:]


def _venv_ok(run=subprocess.run) -> bool:
    p = _run(["python3", "-c", "import ensurepip, venv"], run, 30)   # install.sh runs `python3 -m venv`
    return p.returncode == 0


def missing_packages(which=shutil.which, run=subprocess.run) -> list[str]:
    """Ubuntu/Debian packages the release needs that this box lacks."""
    out = [pkg for cmd, pkg in APT_FOR.items() if not which(cmd)]
    if not _venv_ok(run):
        out.append("python3-venv")
    return out


def _sudo_apt(pkgs: list[str], run=subprocess.run) -> tuple[bool, str]:
    lock = ["-o", "DPkg::Lock::Timeout=600"]
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    p = _run(["sudo", "-n", "apt-get", *lock, "update", "-qq"], run, 900, env)
    if p.returncode == 0:
        p = _run(["sudo", "-n", "apt-get", *lock, "install", "-y", "-qq", *pkgs], run, 1200, env)
    return p.returncode == 0, _tail(p)


def doctor(binary: str, run=subprocess.run) -> dict:
    """`hermes-human-handoff doctor` as JSON: {ready, missing, ...} (never secrets: it prints paths and booleans)."""
    p = _run([binary, "doctor"], run, 60)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    keep = {k: doc.get(k) for k in ("ready", "missing", "browser", "tailnet_connected", "serve_access") if k in doc}
    if "tailscale_error" in doc:
        keep["tailscale_error"] = str(doc["tailscale_error"])[:200]
    keep["ready"] = bool(p.returncode == 0 and doc.get("ready"))
    if not doc:
        keep["error"] = _tail(p) or f"exit {p.returncode}"
    return keep


def _fetch(src: Path, run=subprocess.run) -> str | None:
    """The pinned release into `src`, proven to be exactly COMMIT. Returns an error, or None."""
    tmp = src.with_name(src.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    p = _run(["git", "-c", "advice.detachedHead=false", "clone", "--quiet", "--depth", "1", "--branch", TAG, REPO,
              str(tmp)], run, 300)
    if p.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return f"could not fetch {TAG}: {_tail(p)}"
    head = _run(["git", "-C", str(tmp), "rev-parse", "HEAD"], run, 30).stdout.strip()
    if head != COMMIT:
        shutil.rmtree(tmp, ignore_errors=True)
        return f"{TAG} is {head[:12] or 'unknown'}, expected {COMMIT[:12]}: not installed"
    shutil.rmtree(src, ignore_errors=True)
    os.replace(tmp, src)
    return None


def _playwright_browser(binary: str, run=subprocess.run) -> bool:
    return "ms-playwright" in str(doctor(binary, run).get("browser") or "")


def _browser_starts(binary: str, run=subprocess.run) -> bool:
    browser = str(doctor(binary, run).get("browser") or "")
    if not browser:
        return False
    return _run([browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--dump-dom", "about:blank"],
                run, 60).returncode == 0


def ensure(home: Path | None = None, on: bool | None = None, run=subprocess.run, which=shutil.which) -> dict:
    """Bring this box in line with the switch. Off and never installed: does nothing at all (no file is written)."""
    if on is None:
        try:
            on = switch_on(home)
        except Exception:  # noqa: BLE001 - an unreadable switch reads as unknown
            on = None
    if not on and not load(home).get("installed_by_wam"):
        return {"state": "off", "switch": SWITCH}
    if on is None:                                   # unknown right now: keep what is installed, try next hour
        return {**load(home), "note": "switch could not be read; left as it is"}
    with _locked(home) as got:
        if not got:
            return {"state": "busy", "note": "another `wam human-handoff` run is going"}
        return _ensure(home, on, run, which)


def _ensure(home, on, run, which) -> dict:
    st = load(home)
    if not on:
        if st.get("installed_by_wam"):
            return remove(home, run)
        return {"state": "off", "switch": SWITCH}
    p = paths()
    base = {"switch": SWITCH, "release": TAG, "commit": COMMIT}
    if not host_agent():                              # keep what an earlier run installed, so off still removes it
        keep = {k: st[k] for k in ("installed_by_wam", "commit") if k in st}
        return _save(home, {**base, **keep, "state": "no-host-agent",
                            "note": "this box's agent is the stock Hermes container; Human Handoff runs only next to "
                                    "a host Hermes, so Connected accounts' Google setup stays manual here"})
    if p["bin"].exists() and not st.get("installed_by_wam"):
        return _save(home, {**base, "state": "hand-installed", "installed_by_wam": False,
                            "note": f"{p['bin']} was installed by hand; left as it is",
                            "doctor": doctor(str(p["bin"]), run)})
    fresh = (st.get("installed_by_wam") and st.get("commit") == COMMIT and p["bin"].exists()
             and p["skill"].is_file() and (p["src"] / "scripts" / "uninstall.sh").is_file())
    if not fresh:
        need = missing_packages(which, run)
        if need:
            ok, why = _sudo_apt(need, run)
            if not ok:
                return _save(home, {**base, "state": "needs-packages", "packages": need,
                                    "remedy": "sudo apt-get install -y " + " ".join(need) + ", then: wam human-handoff --ensure",
                                    "error": why})
        if not which("tailscale"):
            return _save(home, {**base, "state": "needs-tailscale",
                                "remedy": "install and sign in to Tailscale, then: wam human-handoff --ensure"})
        err = _fetch(p["src"], run)
        if err:
            return _save(home, {**base, "state": "failed", "error": err})
        env = {**os.environ, "HERMES_HOME": str(hermes_home())}
        r = _run(["bash", str(p["src"] / "scripts" / "install.sh")], run, TIMEOUT, env, str(p["src"]))
        if not p["bin"].exists():                         # install.sh exits 2 after installing when doctor fails
            return _save(home, {**base, "state": "failed", "error": _tail(r)})
        st = _save(home, {**base, "state": "installed", "installed_by_wam": True})
    deps = st.get("browser_deps")
    if deps not in ("ok", "installed") and _playwright_browser(str(p["bin"]), run):
        if _browser_starts(str(p["bin"]), run):
            deps = "ok"
        else:                                             # Playwright's Chromium needs libraries a stock box lacks
            venv_py = str(p["prefix"] / "venv" / "bin" / "python")
            q = _run(["sudo", "-n", venv_py, "-m", "playwright", "install-deps", "chromium"], run, 1200)
            deps = ("installed" if q.returncode == 0 and _browser_starts(str(p["bin"]), run)
                    else "needs: sudo " + venv_py + " -m playwright install-deps chromium")
    d = doctor(str(p["bin"]), run)
    state = "ready" if d.get("ready") else "not-ready"
    out = {**base, "state": state, "installed_by_wam": True, "cli": str(p["bin"]), "skill": str(p["skill"]),
           "doctor": d}
    if deps:
        out["browser_deps"] = deps
    return _save(home, out)


def remove(home: Path | None = None, run=subprocess.run) -> dict:
    """Switch turned off: remove what `ensure` installed, with the release's own uninstall.sh."""
    p = paths()
    script = p["src"] / "scripts" / "uninstall.sh"
    if script.is_file():
        env = {**os.environ, "HERMES_HOME": str(hermes_home())}
        r = _run(["bash", str(script)], run, 300, env)
        if r.returncode != 0:
            return _save(home, {**load(home), "state": "remove-waiting", "error": _tail(r),
                                "note": "kept: a handoff is still active; removed on the next run"})
    shutil.rmtree(p["src"].parent, ignore_errors=True)
    with contextlib.suppress(OSError):
        (_home(home) / STATE).unlink()
    return {"state": "off", "switch": SWITCH, "removed": True}


def status(home: Path | None = None) -> dict:
    st = load(home)
    return st or {"state": "off", "switch": SWITCH}


def spawn(home: Path | None = None, popen=None) -> str | None:
    """Run `wam human-handoff --ensure` detached (its own session): a switch flip returns at once and the install
    outlives the request. Best effort: otherwise the hourly tick does it."""
    popen = popen or subprocess.Popen
    try:
        h = _home(home)
        h.mkdir(parents=True, exist_ok=True)
        with (h / LOG).open("a") as log:
            popen([sys.executable, "-m", "wam", "human-handoff", "--ensure"],
                  cwd=str(Path(__file__).resolve().parent.parent),
                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return "started"
    except OSError:
        return None


def tick(home: Path | None = None) -> dict:
    """The hourly app-update tick: never raises, never slows the tick when the switch is off (one look.json read)."""
    try:
        return ensure(home)
    except Exception as e:  # noqa: BLE001
        return {"state": "error", "error": str(e)[:200]}


def describe(res: dict) -> str:
    s = res.get("state")
    if s == "off":
        return f"Human Handoff: off (switch {SWITCH})." + (" Removed what it had installed." if res.get("removed") else "")
    if s == "ready":
        return f"Human Handoff {TAG}: ready ({res.get('cli')}); doctor passes."
    lines = [f"Human Handoff {TAG}: {s}"]
    for k in ("note", "remedy", "error", "browser_deps"):
        if res.get(k):
            lines.append(f"  {k}: {res[k]}")
    if isinstance(res.get("doctor"), dict) and res["doctor"].get("missing"):
        lines.append("  doctor missing: " + ", ".join(map(str, res["doctor"]["missing"])))
    return "\n".join(lines)
