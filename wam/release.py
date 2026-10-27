"""`wam release`: the owner's own release, their commits reapplied on top of the Wizard OS channel (wizard-os#186).

The shell (wizard-ux) a box runs is normally the image its channel's release pins. An owner who wants changes of
their own that main does not have (or does not have yet) keeps them as commits on a branch of a local wizard-ux
checkout. This module keeps that branch on top of the channel and serves it:

  wam release init [--name "<Owner>'s release"] [--channel latest|stable] [--repo PATH] [--push REMOTE] ...
      set up the checkout under the work dir (default ~/.wizard-os/release/), the owner's branch from the channel's
      wizard-ux source, and the hourly timer + on-demand update unit
  wam release check      fetch the channel and record what is new upstream (pending.json); builds nothing
  wam release update     merge the newest upstream under the owner's commits, test, build, switch (Settings > Update)
  wam release rebuild    build the owner's branch on the base it already has (after the owner/agent commits to it)
  wam release auto       the hourly timer: Automatic -> update, Ask me -> check (updates.json "mode")
  wam release status     what is served, what is pending, the last status line
  wam release off        remove the timer/units and put the channel's shell image back (the checkout is kept)

Safety rules (each one is tested):
- one run at a time (a lock); a `building` marker (pid) while an update/rebuild runs;
- upstream is MERGED, never rebased: history is kept, a clash is solved once, no owner commit is ever dropped;
- a clash is resolved in a throwaway worktree by a pluggable resolver (default: the box's Hermes agent `hermes -z`);
  the result only counts if the merge is committed, both parents are ancestors, no conflict markers remain and every
  test passes. Otherwise the box keeps its build, the status says CONFLICT and the owner gets one notification per
  upstream commit through the box's Notifications app (found in apps.json, if installed);
- only committed work is tested and built (in a temporary worktree), tests run before anything is switched;
- the new image is switched in through the shell's user-config override, and if the shell does not come back serving
  this build the override is restored byte for byte and the old container recreated;
- pushing the branch is off unless the owner names a remote.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CONFIG = "my-release.json"            # in the Wizard OS home; its presence (with a status file) makes a release box
DEFAULT_NAME = "My release"
DEFAULT_BRANCH = "release/mine"
DEFAULT_APP = "wizard-ux"
STORE = "wizardapps"
DEFAULT_TEST = "node --test tests/*.test.mjs"
MANIFEST = "web/my-release.json"      # what the shell's Settings > My release reads (served by the shell itself)
UPSTREAM_REF = "refs/wam-release/upstream"
UNIT = "wam-release"                  # wam-release.service + .timer (auto), wam-release-update.service (Update)
UPDATE_UNIT = f"{UNIT}-update.service"
CHANNEL_LABEL = {"main": "Main", "latest": "Latest", "stable": "Stable"}
NOTIFY_TOPIC = "wizard"
KEEP_BACKUPS = 5
SHA = re.compile(r"[0-9a-f]{4,40}")


# ---------------------------------------------------------------- where things live

def wizard_home(home: Path | None = None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def _read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1) + "\n")
    tmp.replace(path)


def config(home: Path | None = None) -> dict:
    return _read_json(wizard_home(home) / CONFIG)


def work_dir(home: Path | None = None, cfg: dict | None = None) -> Path:
    cfg = config(home) if cfg is None else cfg
    d = cfg.get("dir") or os.environ.get("WAM_RELEASE_DIR")
    return Path(d).expanduser() if d else wizard_home(home) / "release"


@dataclass
class Paths:
    dir: Path

    def __getattr__(self, name):          # status, pending, built, building, lock, notified
        files = {"status": "status", "pending": "pending.json", "built": "built.json", "building": "building",
                 "lock": "lock", "notified": "clash-notified", "logs": "logs", "backups": "backups"}
        if name in files:
            return self.dir / files[name]
        raise AttributeError(name)


def paths(home: Path | None = None, cfg: dict | None = None) -> Paths:
    return Paths(work_dir(home, cfg))


def enabled(home: Path | None = None) -> bool:
    """A release box: the owner set up a release (not turned off) and it has served a build (built.json). Until the
    first build the box updates the stock way, so `init` alone changes nothing the owner sees."""
    cfg = config(home)
    return bool(cfg) and cfg.get("enabled", True) is not False and paths(home, cfg).built.is_file()


# ---------------------------------------------------------------- the contract Settings > Updates reads

def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sha(v) -> str | None:
    return v if isinstance(v, str) and SHA.fullmatch(v) else None


def last_line(p: Paths) -> str:
    try:
        lines = [ln.strip() for ln in p.status.read_text().splitlines() if ln.strip()]
    except OSError:
        return ""
    return re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", lines[-1]) if lines else ""


def building(p: Paths) -> bool:
    return p.building.exists() and _pid_alive(_read_json(p.building).get("pid"))


def release(home: Path | None = None) -> dict:
    """{name, base, build, behind, new, building, failed, checked_at} (the shell's contract; do not rename fields),
    plus private `_latest` / `_checked` for updates.status()."""
    cfg = config(home)
    p = paths(home, cfg)
    pending, built = _read_json(p.pending), _read_json(p.built)
    last = last_line(p)
    try:
        behind = max(0, int(pending.get("behind") or 0))
    except (TypeError, ValueError):
        behind = 0
    new = [{"sha": str(c.get("sha", ""))[:12], "title": str(c.get("title", ""))[:200]}
           for c in (pending.get("new") or []) if isinstance(c, dict)][:20]
    name = cfg.get("name")
    return {"name": name if isinstance(name, str) and name.strip() else DEFAULT_NAME,
            "base": _sha(built.get("base")), "build": _sha(built.get("build")), "behind": behind, "new": new,
            "building": building(p), "failed": last if last.startswith(("FAIL", "CONFLICT")) else None,
            "checked_at": pending.get("checked_at") if isinstance(pending.get("checked_at"), str) else None,
            "changes": _read_json(p.dir / "items-state.json").get("items"),
            "staging": _read_json(p.dir / "staging.json") or None,
            "held": _read_json(p.dir / "holds.json").get("items", []),
            "resolver_merges": _read_json(p.dir / "resolvers.json").get("merges", []),
            "_latest": _sha(pending.get("latest")), "_checked": bool(pending)}


# ---------------------------------------------------------------- running things (all injectable for tests)

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _probe(url: str, timeout: float = 3) -> dict | None:
    """GET a loopback URL; JSON object, {} for a non-JSON 2xx, None when it is not up."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            raw = r.read(65536)
    except (OSError, ValueError):
        return None
    try:
        v = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def _post(url: str, doc: dict, timeout: float = 20) -> bool:
    req = urllib.request.Request(url, data=json.dumps(doc).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except (OSError, ValueError):
        return False


@dataclass
class Ctx:
    home: Path
    cfg: dict
    run: Callable = subprocess.run            # docker / systemctl / compose (tests inject a fake)
    probe: Callable = _probe                  # is the new shell up and serving this build?
    post: Callable = _post                    # notifications
    wait: float = 30                          # seconds the new shell gets to come up
    sleep: Callable = time.sleep
    resolver: Callable | None = None          # tests: replaces the resolver command
    pr_lookup: Callable | None = None         # tests: GitHub REST at the boundary
    p: Paths = field(init=False)

    def __post_init__(self):
        self.p = paths(self.home, self.cfg)

    @property
    def src(self) -> Path:
        return Path(self.cfg.get("repo") or self.p.dir / "src").expanduser()

    @property
    def branch(self) -> str:
        return self.cfg.get("branch") or DEFAULT_BRANCH

    @property
    def channel(self) -> str:
        c = self.cfg.get("channel")
        return c if c in CHANNEL_LABEL else "latest"

    @property
    def label(self) -> str:
        return CHANNEL_LABEL[self.channel]

    @property
    def name(self) -> str:
        n = self.cfg.get("name")
        return n if isinstance(n, str) and n.strip() else DEFAULT_NAME

    def git(self, *args, cwd: Path | None = None, check: bool = True, **kw) -> str:
        r = subprocess.run(["git", "-C", str(cwd or self.src), *self.identity(), *args], capture_output=True,
                           text=True, **kw)
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])}: {(r.stderr or r.stdout).strip()[-300:]}")
        return r.stdout.strip()

    def ok(self, *args, cwd: Path | None = None, **kw) -> bool:
        """A git command's success (merge --ff-only, is-ancestor, push)."""
        return subprocess.run(["git", "-C", str(cwd or self.src), *self.identity(), *args], capture_output=True,
                              text=True, **kw).returncode == 0

    def identity(self) -> list[str]:
        ident = self.cfg.get("git_identity") or {}
        out = []
        if ident.get("name"):
            out += ["-c", f"user.name={ident['name']}"]
        if ident.get("email"):
            out += ["-c", f"user.email={ident['email']}"]
        return out

    def log(self, name: str) -> Path:
        self.p.logs.mkdir(parents=True, exist_ok=True)
        return self.p.logs / name

    def say(self, line: str) -> str:
        self.p.dir.mkdir(parents=True, exist_ok=True)
        self.p.status.write_text(f"{_now()} {line}\n")
        return line


class GitError(RuntimeError):
    pass


def ctx(home: Path | None = None, **kw) -> Ctx:
    h = wizard_home(home)
    cfg = config(h)
    if not cfg:
        raise SystemExit("no release set up here; run `wam release init` first")
    return Ctx(h, cfg, **kw)


@contextmanager
def _locked(p: Paths):
    p.dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(p.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


@contextmanager
def _worktree(c: Ctx, ref: str, prefix: str):
    w = Path(tempfile.mkdtemp(prefix=prefix))
    w.rmdir()
    c.git("worktree", "add", "-q", "--detach", str(w), ref)
    if (c.src / "node_modules").is_dir():
        (w / "node_modules").symlink_to(c.src / "node_modules")
    try:
        yield w
    finally:
        c.git("worktree", "remove", "--force", str(w), check=False)
        shutil.rmtree(w, ignore_errors=True)
        c.git("worktree", "prune", check=False)


def _tests(c: Ctx, cwd: Path, log: Path) -> bool:
    cmd = c.cfg.get("test_cmd", DEFAULT_TEST)
    if not cmd:
        return True
    with open(log, "ab") as f:
        f.write(f"\n== {_now()} {cmd} in {cwd}\n".encode())
        f.flush()
        try:
            r = subprocess.run(["bash", "-c", cmd], cwd=cwd, stdout=f, stderr=subprocess.STDOUT,
                               timeout=int(c.cfg.get("test_timeout", 600)))
        except subprocess.TimeoutExpired:
            f.write(b"tests timed out\n")
            return False
    return r.returncode == 0


# ---------------------------------------------------------------- the channel's upstream

def _release_base() -> str:
    from . import updates
    return (os.environ.get("WIZARD_RELEASE_BASE") or updates.RELEASE_BASE).rstrip("/")


def _fetch_text(url: str, limit: int = 1 << 20) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read(limit).decode()
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def stable_component(app: str = DEFAULT_APP, base: str | None = None) -> dict | None:
    """The stable release's pin for the shell source: {"version", "repo", "commit"} (None when unreadable)."""
    from . import updates
    base = (base or _release_base()).rstrip("/")
    v = (_fetch_text(f"{base}/stable", 64) or "").strip()
    if not updates.VERSION.fullmatch(v or "-"):
        return None
    try:
        comp = (json.loads(_fetch_text(f"{base}/{v}.json") or "{}").get("components") or {}).get(app) or {}
    except ValueError:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", str(comp.get("commit", ""))):
        return None
    return {"version": v, "repo": comp.get("repo"), "commit": comp["commit"]}


def fetch_upstream(c: Ctx) -> str:
    """Fetch the channel and point refs/wam-release/upstream at what it names now. Returns that commit.
    Latest = the source's main; Stable = the wizard-ux commit the stable release pins."""
    remote = c.cfg.get("remote") or "origin"
    c.git("fetch", "-q", remote, c.cfg.get("upstream_branch") or "main", timeout=300)
    if c.channel == "stable":
        comp = stable_component(c.cfg.get("app") or DEFAULT_APP, c.cfg.get("release_base"))
        if not comp:
            raise GitError("could not read the stable release's shell commit")
        if c.git("cat-file", "-t", comp["commit"], check=False) != "commit":
            c.git("fetch", "-q", remote, comp["commit"], timeout=300)
        target = comp["commit"]
    else:
        target = c.git("rev-parse", "FETCH_HEAD")
    c.git("update-ref", UPSTREAM_REF, target)
    return target


def check(c: Ctx) -> dict:
    """What the channel has that this release is not built on yet. Reads git, writes pending.json, builds nothing."""
    target = fetch_upstream(c)
    from . import release_policy as policy
    policy.items(c)
    head = c.git("rev-parse", c.branch)
    behind = int(c.git("rev-list", "--count", f"{head}..{target}"))
    new = []
    out = c.git("log", "--no-merges", "-n", "20", "--format=%h%x1f%s", f"{head}..{target}")
    for line in filter(None, out.splitlines()):
        sha, title = line.split("\x1f", 1)
        new.append({"sha": sha, "title": title})
    rec = {"behind": behind, "latest": c.git("rev-parse", "--short=8", target), "target": target, "new": new,
           "channel": c.channel, "checked_at": _now()}
    _write_json(c.p.pending, rec)
    return {k: rec[k] for k in ("behind", "latest", "checked_at")}


# ---------------------------------------------------------------- merging a clash

def resolver_argv(c: Ctx) -> list[str] | None:
    """The resolver command, or None for no automatic resolution. Config "resolver": "auto" (default: the box's
    Hermes agent when it is installed), "none", or a shell command (gets WAM_RELEASE_* env and the prompt)."""
    r = c.cfg.get("resolver", "auto")
    if r in (None, "", "none", False):
        return None
    if r == "auto":
        hermes = shutil.which("hermes")
        return [hermes, "-z"] if hermes else None
    return ["bash", "-c", str(r)]


def resolve_prompt(c: Ctx, w: Path, short: str, files: str, msg: str) -> str:
    return (
        f"You are resolving a git merge conflict in {w} (a throwaway worktree of the Wizard shell, wizard-ux). "
        f"Only edit files inside {w}.\nThe owner's own release ({c.name}, our side, HEAD) is being updated with the "
        f"newest {c.label} (MERGE_HEAD, {short}). Conflicted files: {files}\n"
        "Rules: the owner's behaviour wins where it is theirs (their own changes and their tests for them); "
        "everywhere else take upstream. Where both sides changed the same logic, keep both, so the owner keeps "
        "their behaviour and gets upstream's fix. Never delete a feature from either side.\n"
        f"Steps: resolve every conflict, remove all conflict markers, run `{c.cfg.get('test_cmd', DEFAULT_TEST)}` "
        f"in {w} and fix until every test passes, then `git add -A` (not node_modules) and "
        f"`git commit --no-edit -m \"{msg}\"`. Do not push. Finish with one line: RESOLVED or FAILED and why.")


def _accept(c: Ctx, w: Path, ours: str, theirs: str, log: Path) -> str | None:
    """Trust only the result, never the resolver's report."""
    def why(reason):
        with open(log, "a") as f:
            f.write(f"rejected: {reason}\n")
        return None
    if c.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=w, check=False):
        return why("merge not committed")
    head = c.git("rev-parse", "HEAD", cwd=w)
    for ref, what in ((theirs, "upstream"), (ours, "the owner's release")):
        if not c.ok("merge-base", "--is-ancestor", ref, head, cwd=w):
            return why(f"{what} is not in the result")
    if c.git("status", "--porcelain", cwd=w):
        return why("uncommitted edits left")
    marks = subprocess.run(["git", "-C", str(w), "grep", "-nIE", r"^(<<<<<<<|=======|>>>>>>>)( |$)", head, "--",
                            ".", ":!node_modules"], capture_output=True, text=True)
    if marks.returncode == 0:
        return why("conflict markers left")
    if not _tests(c, w, log):
        return why("tests fail after the resolve")
    return head


def merge_upstream(c: Ctx, theirs: str) -> tuple[str | None, bool]:
    """Merge `theirs` into the branch head in a throwaway worktree. (merge commit or None, clashed)."""
    ours = c.git("rev-parse", "HEAD")
    short = c.git("rev-parse", "--short=8", theirs)
    msg = f"Merge {c.label} {short} into {c.name}"
    log = c.log("resolve.log")
    log.write_text(f"{_now()} merging {c.label} {short} into {ours[:8]}\n")
    with _worktree(c, ours, "wam-release-merge.") as w:
        with open(log, "a") as f:
            r = subprocess.run(["git", "-C", str(w), *c.identity(), "merge", "-q", "--no-edit", "-m", msg, theirs],
                               stdout=f, stderr=subprocess.STDOUT)
        if r.returncode == 0:
            return c.git("rev-parse", "HEAD", cwd=w), False
        files = " ".join(c.git("diff", "--name-only", "--diff-filter=U", cwd=w).split())
        if not files:
            return None, True
        env = {**os.environ, "WAM_RELEASE_WORKTREE": str(w), "WAM_RELEASE_OURS": ours, "WAM_RELEASE_THEIRS": theirs,
               "WAM_RELEASE_FILES": files, "WAM_RELEASE_MESSAGE": msg}
        prompt = resolve_prompt(c, w, short, files, msg)
        if c.resolver:
            c.resolver(w, env, prompt)
        else:
            argv = resolver_argv(c)
            if not argv:
                with open(log, "a") as f:
                    f.write("no resolver on this box (config \"resolver\"); the owner decides\n")
                return None, True
            with open(log, "a") as f:
                try:
                    subprocess.run([*argv, prompt], cwd=w, env={**env, "WAM_RELEASE_PROMPT": prompt}, stdout=f,
                                   stderr=subprocess.STDOUT, timeout=int(c.cfg.get("resolve_timeout", 2400)))
                except (OSError, subprocess.TimeoutExpired) as e:
                    f.write(f"resolver did not finish: {e}\n")
        accepted = _accept(c, w, ours, theirs, log)
        if accepted:
            c.git("notes", "--ref=wam-release-resolvers", "add", "-f", "-m",
                  f"WAM-Resolver: true\nOurs: {ours}\nUpstream: {theirs}\nConflicts: {files}", accepted)
        return accepted, True


# ---------------------------------------------------------------- telling the owner

def notifications_url(home: Path) -> str | None:
    """The box's Notifications app (whatever provides `notifications` in apps.json), on loopback."""
    for a in _read_json(home / "apps.json").get("apps") or []:
        if not isinstance(a, dict) or "notifications" not in (a.get("provides") or []):
            continue
        if a.get("status") not in (None, "running"):
            continue
        health = str(a.get("health") or "")
        m = re.match(r"(http://127\.0\.0\.1:\d{2,5})/", health)
        if m:
            return m[1] + "/"
    return None


def notify_clash(c: Ctx, short: str, files: list[str]) -> bool:
    """One notification per upstream commit that clashes; nothing when no Notifications app is installed."""
    if c.p.notified.is_file() and c.p.notified.read_text().strip() == short:
        return False
    url = notifications_url(c.home)
    if not url:
        return False
    msg = (f"{c.label} {short} clashes with one of your changes and it couldn't be settled safely, so your Wizard "
           "stays on its current build. Reply here and your agent will work it out with you.")
    if files:
        msg += "\nTouched: " + " ".join(files[:8])
    msg += f"\nDetails: {c.p.logs / 'resolve.log'} on the box."
    if c.post(url, {"topic": c.cfg.get("notify_topic") or NOTIFY_TOPIC, "title": "Your release needs you to finish an update",
                    "message": msg, "priority": 4, "tags": ["warning"]}):
        c.p.notified.write_text(short + "\n")
        return True
    return False


# ---------------------------------------------------------------- serving a build

def runtipi_root(home: Path) -> Path:
    cfg = _read_json(home / "app-manager.json")
    return Path(os.environ.get("RUNTIPI_ROOT", cfg.get("runtipi_root", Path.home() / "runtipi"))).expanduser()


def shell_entry(home: Path, app: str) -> dict:
    for a in _read_json(home / "apps.json").get("apps") or []:
        if isinstance(a, dict) and a.get("id") == app:
            return a
    return {}


def override_path(c: Ctx) -> Path:
    app = c.cfg.get("app") or DEFAULT_APP
    return Path(c.cfg.get("override") or runtipi_root(c.home) / "user-config" / STORE / app / "docker-compose.yml")


def with_image(text: str | None, app: str, image: str) -> str:
    """The override with this service's image pinned to a local build (pull_policy never); a minimal one if none."""
    if not text or not text.strip():
        return f"# Written by wam release: the owner's own build of {app}.\nservices:\n  {app}:\n    image: {image}\n    pull_policy: never\n"
    lines = text.splitlines()
    out, in_svc, done, svc_indent = [], False, False, 0
    for ln in lines:
        m = re.match(r"^(\s*)(\S[^:]*):\s*$", ln)
        if m and m[2] == app:
            in_svc, svc_indent = True, len(m[1])
            out.append(ln)
            continue
        if in_svc and ln.strip() and len(ln) - len(ln.lstrip()) <= svc_indent:
            if not done:
                pad = " " * (svc_indent + 2)
                out += [f"{pad}image: {image}", f"{pad}pull_policy: never"]
                done = True
            in_svc = False
        if in_svc and re.match(r"^\s*(image|pull_policy):", ln):
            if not done:
                pad = ln[:len(ln) - len(ln.lstrip())]
                out += [f"{pad}image: {image}", f"{pad}pull_policy: never"]
                done = True
            continue
        out.append(ln)
    if in_svc and not done:
        pad = " " * (svc_indent + 2)
        out += [f"{pad}image: {image}", f"{pad}pull_policy: never"]
        done = True
    if not done:
        raise ValueError(f"the override has no `{app}:` service to pin")
    return "\n".join(out) + "\n"


def recreate(c: Ctx) -> bool:
    """docker compose up -d for the shell app, with Runtipi's generated recipe plus its user-config override."""
    app = c.cfg.get("app") or DEFAULT_APP
    if c.cfg.get("recreate_cmd"):
        argv = ["bash", "-c", c.cfg["recreate_cmd"]]
    else:
        root = runtipi_root(c.home)
        gen = root / "apps" / STORE / app / "docker-compose.generated.yml"
        argv = ["docker", "compose", "-p", f"{app}_{STORE}"]
        env = root / "app-data" / STORE / app / "app.env"
        if env.is_file():
            argv += ["--env-file", str(env)]
        argv += ["-f", str(gen), "-f", str(override_path(c)), "up", "-d"]
    try:
        r = c.run(argv, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def serving(c: Ctx, build: str) -> bool:
    """The shell came back and serves this build's my-release.json (else at least its health URL answers)."""
    a = shell_entry(c.home, c.cfg.get("app") or DEFAULT_APP)
    local = str((a.get("view") or {}).get("local") or "")
    health = str(a.get("health") or "")
    deadline = time.monotonic() + c.wait
    while True:
        if local.startswith("http://127.0.0.1:"):
            doc = c.probe(local.rstrip("/") + "/my-release.json")
            if doc and doc.get("build") == build:
                return True
        elif health.startswith("http://127.0.0.1:") and c.probe(health) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        c.sleep(1)


def _backup(c: Ctx, path: Path, text: str | None) -> None:
    c.p.backups.mkdir(parents=True, exist_ok=True)
    stock = c.p.backups / "override.before-release"
    if not stock.exists():                  # what `wam release off` puts back
        stock.write_text(text if text is not None else "")
    (c.p.backups / f"override.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}").write_text(text or "")
    olds = sorted(x for x in c.p.backups.glob("override.2*"))
    for x in olds[:-KEEP_BACKUPS]:
        x.unlink(missing_ok=True)


def switch(c: Ctx, image: str, build: str) -> tuple[bool, str | None]:
    """Point the shell at `image` and recreate it; restore the old override exactly if the new build doesn't come up.
    (ok, old image or None)."""
    path = override_path(c)
    old = path.read_text() if path.is_file() else None
    m = re.search(r"^\s*image:\s*(\S+)", old or "", re.M)
    was = m[1] if m else None
    if was == image:
        return True, was
    app = c.cfg.get("app") or DEFAULT_APP
    new = with_image(old, app, image)
    _backup(c, path, old)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    if recreate(c) and serving(c, build):
        return True, was
    if old is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(old)
    recreate(c)
    return False, was


def manifest(c: Ctx, base: str, head: str) -> dict:
    from . import release_policy as policy
    changes = policy.items(c)
    return {"name": c.name, "base": {"ref": base, "label": f"{c.label} {base}"}, "build": head,
            "source_revision": head, "changes": changes, "built_at": _now(),
            "resolver_merges": _read_json(c.p.dir / "resolvers.json").get("merges", [])}


# ---------------------------------------------------------------- update / rebuild

def build(c: Ctx, action: str, *, policy_checked=False, head_override=None, stage_only=False) -> dict:
    """Build a verified, origin-reachable source. Caller holds the release lock."""
    from . import release_policy as policy
    if not policy_checked:
        return policy.execute(c, action)
    src = c.src
    if c.git("branch", "--show-current") != c.branch:
        return {"state": "failed", "status": c.say(f"FAIL {src} is not on {c.branch}")}
    if c.git("status", "--porcelain", "--untracked-files=no"):
        return {"state": "failed", "status": c.say(f"FAIL uncommitted edits in {src}")}
    head_full = head_override or c.git("rev-parse", "HEAD")
    policy.pushed(c, head_full)
    base = c.git("rev-parse", "--short=8", c.git("merge-base", head_full, UPSTREAM_REF))
    head = head_full[:8]
    tests_log = c.log("tests.log")
    tests_log.write_text("")
    with _worktree(c, head_full, "wam-release-src.") as w:
        if not _tests(c, w, tests_log):
            return {"state": "failed", "status": c.say(f"FAIL tests on {head} (log {tests_log}); kept the running build")}
        m = manifest(c, base, head_full)
        mp = w / (c.cfg.get("manifest") or MANIFEST)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({**m, "build": head}, indent=1) + "\n")
        image = f"{c.cfg.get('image_name') or (c.cfg.get('app') or DEFAULT_APP)}:release-{head}"
        try:
            r = c.run(["docker", "build", "-q", "--label", f"org.opencontainers.image.revision={head_full}",
                       "--label", f"ai.wizards.source-revision={head_full}", "-t", image, str(w)],
                      capture_output=True, text=True, timeout=1800)
            built_ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            built_ok = False
        if not built_ok:
            return {"state": "failed", "status": c.say(f"FAIL image build {head}; kept the running build")}
    if stage_only:
        return {"state": "staged", "build": head, "source_revision": head_full, "image": image,
                "status": c.say("STAGED your update is ready to review; the running build is unchanged")}
    ok, was = switch(c, image, head)
    if not ok:
        return {"state": "rolled_back", "status": c.say(f"FAIL new build {head} did not come up; rolled back to {was}")}
    _write_json(c.p.built, {"base": base, "build": head, "source_revision": head_full, "image": image,
                           "built_at": _now(), "previous_image": was})
    pushed = None
    if c.cfg.get("push"):
        pushed = c.ok("push", "-q", c.cfg["push"], f"{head_full}:refs/heads/{c.branch}", timeout=120)
    n = len(m["changes"])
    line = c.say(f"OK {c.name} {head} on {c.label} {base} · {n} changes of your own")
    if action == "update":
        try:
            check(c)
        except GitError:
            pass
    return {"state": "ok", "status": line, "build": head, "base": base, "image": image, "changes": n,
            **({"pushed": pushed} if pushed is not None else {})}


def run(action: str, home: Path | None = None, **kw) -> dict:
    """check / update / rebuild / auto, under the lock (a second run returns busy)."""
    from . import updates
    from . import release_policy as policy
    approve = kw.pop("approve", ())
    c = ctx(home, **kw)
    if action == "auto":
        action = "update"  # fixes cannot wait for an owner's feature-update preference
    with _locked(c.p) as got:
        if not got:
            return {"state": "busy", "note": "a release run is already going"}
        if action == "check":
            try:
                return {"state": "checked", **check(c)}
            except GitError as e:
                return {"state": "failed", "error": str(e)}
        c.p.building.write_text(json.dumps({"pid": os.getpid(), "started_at": _now(), "action": action}) + "\n")
        try:
            return policy.execute(c, action, approve)
        except GitError as e:
            conflict = str(e).startswith("CONFLICT")
            return {"state": "conflict" if conflict else "failed",
                    "status": c.say(str(e) if conflict else f"FAIL {e}"),
                    **({"notified": e.notified} if isinstance(e, policy.Conflict) else {})}
        finally:
            c.p.building.unlink(missing_ok=True)


# ---------------------------------------------------------------- init / off / units

def unit_files(unit_dir: Path, wam: str, env: dict[str, str]) -> dict[Path, str]:
    envs = "".join(f"Environment={k}={v}\n" for k, v in env.items())
    svc = ("[Service]\nType=oneshot\nTimeoutStartSec=3600\n" + envs)
    return {
        unit_dir / f"{UNIT}.service": ("[Unit]\nDescription=Wizard OS: keep the owner's release on top of the "
                                       "channel (Automatic: update; Ask me: check)\n\n" + svc +
                                       f"ExecStart={wam} release auto\n"),
        unit_dir / UPDATE_UNIT: ("[Unit]\nDescription=Wizard OS: update the owner's release now (Settings > Update)\n\n"
                                 + svc + f"ExecStart={wam} release update\n"),
        unit_dir / f"{UNIT}.timer": ("[Unit]\nDescription=Hourly: keep the owner's release on top of the channel\n\n"
                                     "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=300\nPersistent=true\n\n"
                                     "[Install]\nWantedBy=timers.target\n"),
    }


def _systemctl(enabled: bool):
    if not enabled:
        return lambda *a: None
    return lambda *a: subprocess.run(["systemctl", "--user", *a], capture_output=True, timeout=60)


def _wam_path() -> str:
    return shutil.which("wam") or str(Path.home() / ".local/bin/wam")


def install_units(unit_dir: Path | None = None, systemctl: bool = True, home: Path | None = None) -> list[str]:
    ud = unit_dir or Path.home() / ".config/systemd/user"
    ud.mkdir(parents=True, exist_ok=True)
    env = {"PATH": "%h/.local/bin:%h/bin:/usr/local/bin:/usr/bin:/bin"}
    sock = f"/run/user/{os.getuid()}/docker.sock"
    if os.environ.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    elif os.path.exists(sock):
        env["DOCKER_HOST"] = f"unix://{sock}"
    if home or os.environ.get("WIZARD_OS_HOME"):
        env["WIZARD_OS_HOME"] = str(wizard_home(home))
    files = unit_files(ud, _wam_path(), env)
    for p, text in files.items():
        p.write_text(text)
    run_ = _systemctl(systemctl)
    run_("daemon-reload")
    run_("enable", "--now", f"{UNIT}.timer")
    return sorted(p.name for p in files)


def remove_units(unit_dir: Path | None = None, systemctl: bool = True) -> list[str]:
    ud = unit_dir or Path.home() / ".config/systemd/user"
    run_ = _systemctl(systemctl)
    run_("disable", "--now", f"{UNIT}.timer")
    gone = []
    for p in unit_files(ud, "wam", {}):
        if p.exists():
            p.unlink()
            gone.append(p.name)
    run_("daemon-reload")
    return gone


def source_url(app: str = DEFAULT_APP, base: str | None = None) -> str:
    comp = stable_component(app, base) or {}
    repo = comp.get("repo") or f"humanitylabs-org/{app}"
    return f"https://github.com/{repo}"


def init(home: Path | None = None, name: str | None = None, channel: str = "latest", repo: str | None = None,
         source: str | None = None, branch: str = DEFAULT_BRANCH, push: str | None = None, resolver: str = "auto",
         dir: str | None = None, test_cmd: str | None = None, timer: bool = True, unit_dir: Path | None = None,
         systemctl: bool = True, release_base: str | None = None) -> dict:
    """Set up the owner's release. Never clobbers an existing checkout or branch: they are adopted."""
    from . import release_policy as policy
    if not policy.developer(home):
        raise SystemExit("turn on Developer mode in Settings first")
    if channel not in CHANNEL_LABEL:
        raise SystemExit("--channel must be main, latest or stable")
    h = wizard_home(home)
    old = config(h)
    cfg = {**old, "name": name or old.get("name") or DEFAULT_NAME, "channel": channel, "branch": branch,
           "resolver": resolver, "push": push or None, "enabled": True}
    if dir:
        cfg["dir"] = str(Path(dir).expanduser())
    if test_cmd is not None:
        cfg["test_cmd"] = test_cmd
    if release_base:
        cfg["release_base"] = release_base
    p = paths(h, cfg)
    p.dir.mkdir(parents=True, exist_ok=True)
    if repo:
        cfg["repo"] = str(Path(repo).expanduser().resolve())
        src = Path(cfg["repo"])
        if not (src / ".git").exists():
            raise SystemExit(f"{src} is not a git checkout")
    else:
        cfg.pop("repo", None)
        src = p.dir / "src"
        if not (src / ".git").exists():
            url = source or source_url(DEFAULT_APP, release_base)
            r = subprocess.run(["git", "clone", "-q", url, str(src)], capture_output=True, text=True, timeout=900)
            if r.returncode != 0:
                raise SystemExit(f"could not clone {url}: {(r.stderr or r.stdout).strip()[-300:]}")
    if not subprocess.run(["git", "-C", str(src), "config", "user.name"], capture_output=True, text=True).stdout.strip():
        cfg.setdefault("git_identity", {"name": cfg["name"], "email": "release@wizard.invalid"})
    _write_json(h / CONFIG, cfg)
    c = Ctx(h, cfg)
    policy.require(c)
    target = fetch_upstream(c)
    have = c.git("rev-parse", "-q", "--verify", f"refs/heads/{branch}", check=False)
    if not have:
        c.git("branch", branch, target)
    if c.git("branch", "--show-current") != branch:
        if c.git("status", "--porcelain", "--untracked-files=no"):
            raise SystemExit(f"{src} has uncommitted edits; commit or stash them, then run init again")
        c.git("checkout", "-q", branch)
    c.git("update-ref", "refs/wam-release/initial-base", c.git("merge-base", "HEAD", target))
    check(c)
    units = install_units(unit_dir, systemctl, home) if timer else []
    return {"name": cfg["name"], "channel": channel, "branch": branch, "checkout": str(src), "dir": str(p.dir),
            "created_branch": not have, "units": units, "push": cfg["push"],
            "next": "commit your changes to the branch, then `wam release rebuild`"}


def off(home: Path | None = None, keep_build: bool = False, unit_dir: Path | None = None, systemctl: bool = True,
        **kw) -> dict:
    """Stop: remove the units; unless --keep-build put the shell's override from before the first switch back and
    recreate it. The checkout, its branch and the work dir are kept."""
    c = ctx(home, **kw)
    with _locked(c.p) as got:
        if not got:
            return {"state": "busy", "note": "a release run is going; try again when it finishes"}
        units = remove_units(unit_dir, systemctl)
        restored = False
        stock = c.p.backups / "override.before-release"
        if not keep_build and stock.exists():
            path = override_path(c)
            text = stock.read_text()
            if text:
                path.write_text(text)
            else:
                path.unlink(missing_ok=True)
            restored = recreate(c)
        _write_json(c.home / CONFIG, {**c.cfg, "enabled": False})
        return {"state": "off", "units_removed": units, "shell_restored": restored, "kept": str(c.src)}


def status(home: Path | None = None) -> dict:
    h = wizard_home(home)
    cfg = config(h)
    if not cfg:
        return {"enabled": False, "note": "no release set up here (`wam release init`)"}
    rel = release(h)
    rel.pop("_latest"); rel.pop("_checked")
    p = paths(h, cfg)
    return {"enabled": enabled(h), **rel, "channel": cfg.get("channel", "latest"), "branch": cfg.get("branch"),
            "checkout": cfg.get("repo") or str(p.dir / "src"), "last": last_line(p) or None,
            "image": _read_json(p.built).get("image")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wam release", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="set up the owner's release branch, checkout and timer")
    i.add_argument("--name"); i.add_argument("--channel", choices=list(CHANNEL_LABEL), default="latest")
    i.add_argument("--repo", help="adopt an existing wizard-ux checkout instead of cloning one")
    i.add_argument("--source", help="clone URL (default: the shell source the stable release pins)")
    i.add_argument("--branch", default=DEFAULT_BRANCH); i.add_argument("--dir", help="work dir (default ~/.wizard-os/release)")
    i.add_argument("--push", metavar="REMOTE", help="push the branch to this remote after each good build (off by default)")
    i.add_argument("--resolver", default="auto", help='"auto" (the box\'s Hermes agent), "none", or a shell command')
    i.add_argument("--test-cmd", help=f"tests run before every switch (default: {DEFAULT_TEST})")
    i.add_argument("--no-timer", action="store_true")
    for n in ("check", "update", "rebuild", "auto", "status"):
        sub.add_parser(n)
    go = sub.add_parser("go", help="promote reviewed staging; hold named items")
    go.add_argument("--hold", action="append", default=[], help="full SHA, PR URL or exact title to keep waiting")
    item = sub.add_parser("item", help="register an existing change without rewriting history")
    item.add_argument("status", choices=["proposed", "mine"])
    item.add_argument("--pr"); item.add_argument("--sha"); item.add_argument("--title")
    o = sub.add_parser("off"); o.add_argument("--keep-build", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        res = init(name=a.name, channel=a.channel, repo=a.repo, source=a.source, branch=a.branch, push=a.push,
                   resolver=a.resolver, dir=a.dir, test_cmd=a.test_cmd, timer=not a.no_timer)
    elif a.cmd == "item":
        from . import release_policy as policy
        res = policy.register(ctx(), a.status, a.pr, a.sha, a.title)
    elif a.cmd == "go":
        from . import release_policy as policy
        c = ctx()
        batch = _read_json(c.p.dir / "staging.json")
        held = _read_json(c.p.dir / "holds.json").get("items", [])
        pending_items = policy.pending(c, fetch_upstream(c))
        choices = {x["sha"]: x for x in [*held, *pending_items] if x["kind"] != "fix"}
        holds = set()
        for name in a.hold:
            matches = [sha for sha, x in choices.items() if name in (sha, x.get("pr"), x["title"])]
            if len(matches) != 1:
                ap.error("--hold must identify exactly one current item")
            holds.add(matches[0])
        if holds == set(choices) and holds:
            res = {"state": "waiting", "note": "all features held; live build unchanged"}
        else:
            res = run("go", approve=list(set(choices) - holds))
    elif a.cmd == "status":
        res = status()
    elif a.cmd == "off":
        res = off(keep_build=a.keep_build)
    else:
        res = run(a.cmd)
    print(json.dumps(res, indent=2))
    return 0 if res.get("state") in (None, "ok", "checked", "busy", "off", "staged", "waiting") else (2 if res.get("state") == "conflict" else 1)


if __name__ == "__main__":
    sys.exit(main())
