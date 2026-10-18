"""This box, as the backup engine sees it: paths, mode, apps, agents, stock Hermes, free space and tree digests.

Everything that touches the outside world goes through a `Box`, so tests swap in a fake Runtipi client, fake
`docker`/`hermes` executables on PATH, a fake `systemctl` and a fake free-space probe, and point HOME,
WIZARD_OS_HOME, RUNTIPI_ROOT and HERMES_HOME at a temp folder.

Agents (wizard-os spec/AGENTS.md):
- the main agent `hermes` is the Runtipi app `hermes-agent` (home: runtipi/app-data/wizardapps/hermes-agent/data, at
  /opt/data in its container) on a full install, or the host's own Hermes in alongside mode (alongside.json);
- extra agents live in <wizard home>/agents/<id>/ (agent.json schema 1, home/ bind-mounted at /opt/data) and run as
  the Runtipi app `agent-<id>`.
Their homes are only ever read and written with stock `hermes backup` and `hermes import --force`, run from the
agent's own container image with the home bind-mounted (container agents) or from the host `hermes` with HERMES_HOME
(the alongside host agent). Stock Hermes is never modified.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import alongside, registry
from ..runtipi import Runtipi, RuntipiError, read_env

STORE = "wizardapps"
MAIN_AGENT = "hermes"
MAIN_APP = "hermes-agent"
AGENT_ID = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
APP_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
RESTORE_DIR = ".wizard-restore"

# Wizard home entries that never travel and are never replaced by an import:
# per-machine identity and wiring (this box's tokens, operator login, runtime helper, mode and links to its own
# Hermes, its App Manager config and GitHub login), regenerated state, and the backup engine's own working state.
PER_MACHINE = {"api-token", "mcp-token", "runtipi-operator.json", "agent-runtime", "alongside.json", "linked.json",
               "app-manager.json", "github-token", "host-dashboard.env"}
REGENERATED = {"apps.json", "skills", "system-skills", "cache", "dev", "staged-apps"}
ENGINE = {"restores", "backup-work", "backup-status.json", "backup.lock", "agents"}
NEVER_CAPTURE = PER_MACHINE | REGENERATED | ENGINE


def skip_home_entry(rel: str, is_dir: bool) -> bool:
    """Capture rule for the Wizard home (relative paths). `agents/` is captured per agent."""
    top = rel.split("/", 1)[0]
    if top in NEVER_CAPTURE:
        return True
    name = rel.rsplit("/", 1)[-1]
    return ".bak" in name or name.endswith((".tmp", ".lock", ".pid")) or ".tmp-" in name


@dataclass
class Agent:
    id: str
    main: bool
    home: Path
    runtime: str                     # "container" | "host"
    urn: str | None = None           # the Runtipi app for a container agent
    folder: Path | None = None       # extra agents: <wizard home>/agents/<id>
    meta: dict = field(default_factory=dict)

    def describe(self) -> dict:
        return {"id": self.id, "main": self.main, "runtime": self.runtime, "app": (self.urn or "").split(":")[0] or None,
                "label": self.meta.get("label") or ("Main agent" if self.main else self.id)}


def log(msg: str) -> None:
    print(f"wam backup: {msg}", file=sys.stderr, flush=True)


def is_test_env(env=os.environ) -> bool:
    return bool(env.get("PYTEST_CURRENT_TEST") or env.get("WAM_BACKUP_TEST"))


class Box:
    def __init__(self, home: Path | None = None, runtipi_root: Path | None = None, *, rt=None, env=None,
                 runner=None, systemctl=None, disk_free=None, after_restore=None, sleep=None) -> None:
        self.env = dict(os.environ if env is None else env)
        self.home = Path(home or self.env.get("WIZARD_OS_HOME") or Path.home() / ".wizard-os").expanduser()
        cfg = {}
        try:
            cfg = json.loads((self.home / "app-manager.json").read_text())
        except (OSError, ValueError):
            pass
        self.runtipi_root = Path(runtipi_root or self.env.get("RUNTIPI_ROOT") or cfg.get("runtipi_root")
                                 or Path.home() / "runtipi").expanduser()
        self._rt = rt
        self._runner = runner
        self.systemctl = systemctl
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.after_restore = after_restore
        self.sleep = sleep or time.sleep

    # ------------------------------------------------------------ Runtipi
    @property
    def rt(self) -> Runtipi:
        if self._rt is None:
            rt = Runtipi.from_install(self.runtipi_root)
            rt.ensure_operator(self.home / "runtipi-operator.json")
            self._rt = rt
        return self._rt

    @property
    def runner(self) -> "HermesRunner":
        if self._runner is None:
            self._runner = HermesRunner(self)
        return self._runner

    def installed(self) -> dict[str, dict]:
        """urn -> {urn, id, store, status, version (Runtipi tipi_version)} for every installed app (every store)."""
        out = {}
        for row in self.rt.installed():
            info, app = row.get("info") or {}, row.get("app") or {}
            urn = info.get("urn") or ""
            if ":" not in urn:
                continue
            app_id, store = urn.split(":", 1)
            out[urn] = {"urn": urn, "id": app_id, "store": store, "status": app.get("status"),
                        "version": app.get("version")}
        return out

    def app_status(self, urn: str) -> str:
        try:
            return ((self.rt.app(urn) or {}).get("app") or {}).get("status") or "missing"
        except RuntipiError:
            return "missing"

    def wait_status(self, urn: str, want: set[str], timeout: float = 900, not_in: set[str] | None = None) -> str:
        deadline = time.monotonic() + timeout
        st = "unknown"
        while True:
            st = self.app_status(urn)
            if (st in want) if not not_in else (st not in not_in):
                return st
            if time.monotonic() > deadline:
                return st
            self.sleep(2)

    # ------------------------------------------------------------ paths and facts
    def mode(self) -> str:
        return "alongside" if alongside.load(self.home) else "full"

    def files_root(self) -> Path:
        """The owner's files: Runtipi's media/ folder (the Files app and agents see it); one opaque tree."""
        if self.env.get("WIZARD_FILES_ROOT"):
            return Path(self.env["WIZARD_FILES_ROOT"]).expanduser()
        envf = self.runtipi_root / ".env"
        try:
            mp = (read_env(envf).get("RUNTIPI_MEDIA_PATH") or "").strip() if envf.is_file() else ""
        except OSError:
            mp = ""
        if mp and mp.startswith("/"):
            return Path(mp) / "media"
        return self.runtipi_root / "media"

    def agents_root(self) -> Path:
        stock = self.runtipi_root / "app-data" / "wizard-os" / "agents"
        own = self.home / "agents"
        return stock if (stock.is_dir() and not own.is_dir()) else own

    def work_dir(self) -> Path:
        d = Path(self.env.get("WAM_BACKUP_WORK") or self.home / "backup-work")
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d

    def versions(self) -> dict:
        out = {"wam": _git_head(Path(__file__).resolve().parents[2])}
        try:
            out["runtipi"] = (self.runtipi_root / "VERSION").read_text().strip()[:40]
        except OSError:
            out["runtipi"] = None
        return out

    def source(self) -> dict:
        return {"arch": platform.machine(), "os": platform.system().lower(), "mode": self.mode(),
                "versions": self.versions()}

    def registry_rows(self) -> dict[str, dict]:
        try:
            reg = json.loads((self.home / "apps.json").read_text())
            return {a["id"]: a for a in reg.get("apps", []) if isinstance(a, dict) and a.get("id")}
        except (OSError, ValueError, KeyError):
            return {}

    def app_package(self, store: str, app_id: str) -> Path:
        return self.runtipi_root / "apps" / store / app_id

    def store_package(self, store: str, app_id: str) -> Path:
        return self.runtipi_root / "repos" / store / "apps" / app_id

    def is_agent_app(self, app_id: str, row: dict | None = None) -> bool:
        row = row or {}
        return app_id == MAIN_APP or app_id.startswith("agent-") or row.get("type") == "agent" \
            or "agent" in (row.get("provides") or [])

    def wiring_apps(self) -> set[str]:
        """Apps whose user-config is this box's wiring (alongside overrides for the shell, Agent Service, bundle)."""
        from .. import slots
        out = {alongside.LEGACY_BUNDLE}
        for s, d in slots.load_defaults().items():
            if s in (alongside.SHELL_SLOT, alongside.SERVICE_SLOT, alongside.AGENT_SLOT):
                if d.get("app"):
                    out.add(d["app"])
        for app_id, row in self.registry_rows().items():
            if set(row.get("provides") or []) & {"shell", "agent-service", "agent"}:
                out.add(app_id)
        return out

    # ------------------------------------------------------------ agents
    def agents(self, installed: dict[str, dict] | None = None) -> list[Agent]:
        out: list[Agent] = []
        st = alongside.load(self.home)
        if st and (st.get("agent") or {}).get("hermes_home"):
            out.append(Agent(MAIN_AGENT, True, Path(st["agent"]["hermes_home"]), "host"))
        else:
            urn = f"{MAIN_APP}:{STORE}"
            if installed is None or urn in installed:
                home = self.runtipi_root / "app-data" / STORE / MAIN_APP / "data"
                if installed is not None or home.is_dir():
                    out.append(Agent(MAIN_AGENT, True, home, "container", urn))
        root = self.agents_root()
        if root.is_dir():
            for d in sorted(root.iterdir()):
                meta = read_agent_json(d)
                if not meta or meta["id"] == MAIN_AGENT:
                    continue
                rtm = meta.get("runtime") or {}
                if rtm.get("kind", "container") != "container":
                    continue
                app_id = rtm.get("app_id") or f"agent-{meta['id']}"
                out.append(Agent(meta["id"], False, d / "home", "container", f"{app_id}:{STORE}", d, meta))
        return out

    def main_agent(self) -> Agent | None:
        for a in self.agents():
            if a.main:
                return a
        return None


def read_agent_json(d: Path) -> dict | None:
    try:
        meta = json.loads((d / "agent.json").read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("schema") != 1 or not AGENT_ID.match(str(meta.get("id", ""))) \
            or meta["id"] != d.name:
        return None
    return meta


def _git_head(repo: Path) -> str | None:
    try:
        head = (repo / ".git" / "HEAD").read_text().strip() if (repo / ".git").is_dir() else None
        if head and head.startswith("ref: "):
            ref = repo / ".git" / head[5:]
            if ref.is_file():
                return ref.read_text().strip()
            packed = repo / ".git" / "packed-refs"
            for line in packed.read_text().splitlines() if packed.is_file() else []:
                if line.endswith(" " + head[5:]):
                    return line.split()[0]
            return None
        return head
    except OSError:
        return None


# ---------------------------------------------------------------- stock Hermes

class HermesError(RuntimeError):
    pass


class HermesRunner:
    """Runs stock Hermes for one agent. Container agents: `docker run` of the agent app's own image with the home
    bind-mounted at /opt/data, no network, as the home's owner (adapted from try-wizard wizard-import-hermes).
    The host agent: the host `hermes` with HERMES_HOME. Output never echoes secrets."""

    IMAGE_HERMES = "/opt/hermes/.venv/bin/hermes"
    IMAGE_PYTHON = "/opt/hermes/.venv/bin/python"

    def __init__(self, box: Box) -> None:
        self.box = box

    def _run(self, argv: list[str], *, env=None, stdin: str | None = None, timeout: int = 3600) -> str:
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                               env=env or self.box.env, stdin=None if stdin is not None else subprocess.DEVNULL)
        except FileNotFoundError:
            raise HermesError(f"{Path(argv[0]).name} is not installed on this box") from None
        except subprocess.TimeoutExpired:
            raise HermesError(f"{Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''} timed out") from None
        if p.returncode:
            tail = (p.stderr or p.stdout or "").strip()[-400:]
            raise HermesError(f"{Path(argv[0]).name} {' '.join(argv[1:2])} failed: {tail}")
        return p.stdout

    # ---- container agents
    def image(self, agent: Agent) -> str:
        project = (agent.urn or "").replace(":", "_")
        ids = self._run(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"]).split()
        if not ids:
            raise HermesError(f"the agent app {agent.urn} has no container on this box")
        info = json.loads(self._run(["docker", "inspect", *ids]) or "[]")
        for c in info:
            if any(m.get("Destination") == "/opt/data" for m in c.get("Mounts") or []):
                return c["Config"]["Image"]
        raise HermesError(f"no container of {agent.urn} mounts an agent home at /opt/data")

    def _docker(self, agent: Agent, entry: str, args: list[str], mounts: list[tuple[Path, str, bool]],
                stdin: str | None = None, timeout: int = 3600) -> str:
        st = os.stat(agent.home)
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "-u", f"{st.st_uid}:{st.st_gid}",
               "-e", "HERMES_HOME=/opt/data", "-e", "HOME=/opt/data", "-v", f"{agent.home}:/opt/data"]
        for src, dst, ro in mounts:
            cmd += ["-v", f"{src}:{dst}" + (":ro" if ro else "")]
        cmd += ["--entrypoint", entry, self.image(agent), *args]
        return self._run(cmd, stdin=stdin, timeout=timeout)

    # ---- host agent
    def host_binary(self) -> str:
        b = self.box.env.get("WIZARD_HERMES_BIN") or shutil.which("hermes", path=self.box.env.get("PATH")) \
            or alongside.hermes_binary()
        if not b:
            raise HermesError("the host `hermes` command was not found (set WIZARD_HERMES_BIN)")
        return b

    def host_python(self) -> str:
        if self.box.env.get("WIZARD_HERMES_PYTHON"):
            return self.box.env["WIZARD_HERMES_PYTHON"]
        b = Path(self.host_binary()).resolve()
        try:
            first = b.open("rb").readline().decode(errors="replace").strip()
        except OSError:
            first = ""
        if first.startswith("#!"):
            parts = first[2:].split()
            if parts and Path(parts[0]).name == "env" and len(parts) > 1:
                return shutil.which(parts[1], path=self.box.env.get("PATH")) or parts[1]
            if parts:
                return parts[0]
        raise HermesError("cannot find the Python that runs the host Hermes (set WIZARD_HERMES_PYTHON)")

    def _host_env(self, agent: Agent) -> dict:
        return {**self.box.env, "HERMES_HOME": str(agent.home)}

    # ---- the three operations
    def backup(self, agent: Agent, out_zip: Path) -> None:
        """Stock `hermes backup` (SQLite backup API, Hermes' own excludes) into out_zip."""
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        if agent.runtime == "host":
            self._run([self.host_binary(), "backup", "-o", str(out_zip)], env=self._host_env(agent))
        else:
            os.chmod(out_zip.parent, 0o777)   # the one-off container runs as the home's owner
            try:
                self._docker(agent, self.IMAGE_HERMES, ["backup", "-o", f"/work/{out_zip.name}"],
                             [(out_zip.parent, "/work", False)])
            finally:
                os.chmod(out_zip.parent, 0o700)
        if not out_zip.is_file():
            raise HermesError(f"hermes backup wrote no archive for agent {agent.id}")

    def import_zip(self, agent: Agent, zip_path: Path) -> None:
        """Stock `hermes import --force` of zip_path into the agent's home."""
        if agent.runtime == "host":
            self._run([self.host_binary(), "import", "--force", str(zip_path)], env=self._host_env(agent))
        else:
            os.chmod(zip_path, 0o644)
            os.chmod(zip_path.parent, 0o711)
            self._docker(agent, self.IMAGE_HERMES, ["import", "--force", f"/import/{zip_path.name}"],
                         [(zip_path.parent, "/import", True)])

    def python(self, agent: Agent, script: str, stdin: str) -> str:
        """Run a short script with stock Hermes' own Python (its config, cron and gateway modules)."""
        if agent.runtime == "host":
            return self._run([self.host_python(), "-c", script], env=self._host_env(agent), stdin=stdin)
        return self._docker(agent, self.IMAGE_PYTHON, ["-c", script], [], stdin=stdin)

    def gateway(self, agent: Agent, verb: str) -> bool:
        """Stop/start the host agent's gateway (stock `hermes gateway stop|start`). Container agents use Runtipi."""
        try:
            self._run([self.host_binary(), "gateway", verb], env=self._host_env(agent), timeout=120)
            return True
        except HermesError as e:
            log(f"note: hermes gateway {verb}: {e}")
            return False


# ---------------------------------------------------------------- free space, digests

def need_space(box: Box, path: Path, need: int, what: str) -> None:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    free = box.disk_free(p)
    if free < need:
        raise SpaceError(f"not enough free space for {what}: need {human(need)}, {human(free)} free at {p}")


class SpaceError(RuntimeError):
    pass


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


DIGEST_HASH_MAX = 4 << 20


def tree_digest(root: Path, skip=None, content: bool = True) -> tuple[str, int]:
    """sha256 over every entry under root: path, type, mode and, for files, the size plus the content hash (always
    with content=True; else only up to 4 MiB, larger files by mtime, which a rename keeps). Links are recorded by
    target, unreadable files as such. Returns (hex, entry count)."""
    h = hashlib.sha256()
    n = 0
    root = Path(root)
    if not root.exists():
        return h.hexdigest(), 0
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(root).as_posix()
        dirnames.sort()
        keep = []
        for d in dirnames:
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if skip and skip(rel, True):
                continue
            keep.append(d)
        dirnames[:] = keep
        for name in sorted(keep + filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            p = dp / name
            if name in filenames and skip and skip(rel, False):
                continue
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                continue
            n += 1
            line = f"{rel}\0{stat.S_IFMT(st.st_mode)}\0{stat.S_IMODE(st.st_mode)}"
            if stat.S_ISLNK(st.st_mode):
                line += "\0" + os.readlink(p)
            elif stat.S_ISREG(st.st_mode):
                line += f"\0{st.st_size}"
                if content or st.st_size <= DIGEST_HASH_MAX:
                    try:
                        fh = hashlib.sha256()
                        with open(p, "rb") as f:
                            while b := f.read(1 << 20):
                                fh.update(b)
                        line += "\0" + fh.hexdigest()
                    except OSError:
                        line += "\0unreadable"
                else:
                    line += f"\0{st.st_mtime_ns}"
            h.update(line.encode("utf-8", "surrogateescape") + b"\n")
    return h.hexdigest(), n


def box_digest(box: Box, agents: list[Agent] | None = None) -> dict:
    """What an import may change, as digests: the Wizard home (owner state), Runtipi app-data/user-config/apps, the
    owner's files and each agent home. Regenerated and engine-owned entries are left out."""
    def home_skip(rel, is_dir):
        top = rel.split("/", 1)[0]
        return top in (REGENERATED | {"restores", "backup-work", "backup.lock", "backup-status.json"}) \
            or rel.endswith(".lock")

    def rt_skip(rel, is_dir):
        return rel == "wizard-os" or rel.startswith("wizard-os/") and not rel.startswith("wizard-os/agents")

    out = {"wizard-os": tree_digest(box.home, home_skip)[0]}
    for part in ("app-data", "user-config", "apps"):
        out[f"runtipi/{part}"] = tree_digest(box.runtipi_root / part, rt_skip if part == "app-data" else None)[0]
    out["files"] = tree_digest(box.files_root(), lambda rel, d: rel == RESTORE_DIR or rel.startswith(RESTORE_DIR + "/"),
                               content=False)[0]
    from .agents import keep_in_place
    for a in agents if agents is not None else box.agents():
        if a.runtime == "host":
            out[f"agent/{a.id}"] = tree_digest(a.home, lambda rel, d: keep_in_place(rel))[0]
    return out
