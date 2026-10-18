"""Fakes for the backup engine tests: a whole Wizard box in a temp folder, a Runtipi client that acts on it, and
`docker`/`hermes` executables that emulate stock Hermes. Nothing here touches the real box, Runtipi or Docker."""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tarfile
import textwrap
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam.backup.box import Box  # noqa: E402
from wam.runtipi import RuntipiError  # noqa: E402

IMAGE = "ghcr.io/example/hermes-agent:test"

# ---------------------------------------------------------------- stock-Hermes stand-ins

HERMES_MODS = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/config.py": '''
        import os, yaml
        from pathlib import Path
        def _home(): return Path(os.environ["HERMES_HOME"])
        def get_config_path(): return _home() / "config.yaml"
        def read_raw_config():
            p = get_config_path()
            return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}
        def atomic_config_write(path, cfg):
            Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False))
        def load_env():
            p = _home() / ".env"; out = {}
            for line in (p.read_text().splitlines() if p.exists() else []):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1); out[k.strip()] = v.strip()
            return out
    ''',
    "cron/__init__.py": "",
    "cron/jobs.py": '''
        import json, os, time
        from pathlib import Path
        def _f(): return Path(os.environ["HERMES_HOME"]) / "cron" / "jobs.json"
        def load_jobs():
            return json.loads(_f().read_text()).get("jobs", []) if _f().exists() else []
        def _save(jobs): _f().write_text(json.dumps({"jobs": jobs}, indent=2))
        def _upd(job_id, **kw):
            jobs = load_jobs()
            for j in jobs:
                if j["id"] == job_id: j.update(kw)
            _save(jobs)
        def pause_job(job_id, reason=None):
            _upd(job_id, enabled=False, state="paused", paused_at="now", paused_reason=reason)
        def resume_job(job_id):
            _upd(job_id, enabled=True, state="scheduled", paused_at=None, paused_reason=None)
    ''',
    "gateway/__init__.py": "",
    "gateway/config.py": '''
        from enum import Enum
        class Platform(Enum):
            LOCAL = "local"; TELEGRAM = "telegram"; DISCORD = "discord"; SLACK = "slack"
            API_SERVER = "api_server"; WEBHOOK = "webhook"; SIGNAL = "signal"
    ''',
    "gateway/config_env.py": '''
        from gateway.config import Platform
        _ENV_ENABLE_CREDENTIALS = {Platform.TELEGRAM: ("TELEGRAM_BOT_TOKEN",), Platform.DISCORD: ("DISCORD_BOT_TOKEN",),
                                   Platform.SLACK: ("SLACK_BOT_TOKEN",)}
    ''',
}

# `hermes backup -o Z` / `hermes import --force Z` / `hermes gateway VERB`, on $HERMES_HOME. Like stock: backup zips
# the whole home (minus the code checkout); import extracts over it.
HERMES_PY = r'''
import json, os, sys, zipfile
from pathlib import Path
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f: f.write(json.dumps({"tool": "hermes", "argv": sys.argv[1:]}) + "\n")
home = Path(os.environ["HERMES_HOME"])
a = sys.argv[1:]
if a[0] == "backup":
    out = Path(a[a.index("-o") + 1])
    with zipfile.ZipFile(out, "w") as z:
        for p in sorted(home.rglob("*")):
            rel = p.relative_to(home).as_posix()
            if p.is_file() and not p.is_symlink() and not rel.startswith("hermes-agent/"):
                z.write(p, rel)
elif a[0] == "import":
    with zipfile.ZipFile(a[-1]) as z:
        z.extractall(home)
elif a[0] == "gateway":
    pass
else:
    sys.exit("fake hermes: unknown " + a[0])
'''

DOCKER_PY = r'''
import json, os, subprocess, sys
from pathlib import Path
def _mk(tarfile, name):
    ti = tarfile.TarInfo(name); ti.type = tarfile.DIRTYPE; ti.mode = 0o755; return ti
state = json.loads(Path(os.environ["FAKE_DOCKER_STATE"]).read_text())
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f: f.write(json.dumps({"tool": "docker", "argv": a}) + "\n")
if a[0] == "ps":
    tag = "label=com.docker.compose.project="
    proj = [x[len(tag):] for x in a if x.startswith(tag)][0]
    c = state.get(proj)
    print(c["id"] if c else "")
elif a[0] == "inspect" and "--format" in a:          # the App Manager's own image (appdata.AppData.image)
    print(os.environ.get("FAKE_HELPER_IMAGE", "example/wam:helper"))
elif a[0] == "volume" and a[1] == "inspect":
    sys.exit(0 if (Path(os.environ["FAKE_VOLUMES"]) / a[2]).is_dir() else 1)
elif a[0] == "inspect":
    out = []
    for c in state.values():
        if c["id"] in a[1:]:
            out.append({"Id": c["id"], "Config": {"Image": c["image"]},
                        "Mounts": [{"Source": c["home"], "Destination": "/opt/data"}]})
    print(json.dumps(out))
elif a[0] == "run":
    mounts, envs, tmpfs, i = {}, {}, [], 1
    while i < len(a):
        if a[i] == "-v":
            parts = a[i + 1].split(":")
            src = parts[0] if parts[0].startswith("/") else str(Path(os.environ["FAKE_VOLUMES"]) / parts[0])
            mounts[parts[1]] = src; i += 2
        elif a[i] == "-e":
            k, v = a[i + 1].split("=", 1); envs[k] = v; i += 2
        elif a[i] == "--mount":
            tmpfs.append(dict(x.split("=", 1) for x in a[i + 1].split(","))["destination"]); i += 2
        elif a[i] in ("-u", "--network"):
            i += 2
        elif a[i] == "--entrypoint":
            entry, image, rest = a[i + 1], a[i + 2], a[i + 3:]; break
        else:
            i += 1
    if entry == "tar":      # busybox tar -cf - -C /w NAMES... (appdata archive), as root with numeric owners
        assert image == os.environ.get("FAKE_HELPER_IMAGE", "example/wam:helper"), image
        import stat as _st, tarfile
        names = rest[rest.index("-C") + 2:]
        hosts = {}
        for n in names:
            if n == "volumes":
                for dst, src in mounts.items():
                    if dst.startswith("/w/volumes/"):
                        hosts["volumes/" + dst[len("/w/volumes/"):]] = src
            else:
                hosts[n] = mounts["/w/" + n]
        masked = {t[len("/w/"):] for t in tmpfs}
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.GNU_FORMAT) as t:
            if any(k.startswith("volumes/") for k in hosts):
                t.addfile(_mk(tarfile, "volumes"))
            for arc, src in sorted(hosts.items()):
                for dp, dns, fns in os.walk(src):
                    rel = os.path.relpath(dp, src)
                    base = arc if rel == "." else arc + "/" + rel
                    if base in masked:
                        dns[:] = []
                        t.addfile(_mk(tarfile, base))
                        continue
                    t.add(dp, base, recursive=False)
                    dns.sort()
                    for n in sorted(fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]):
                        pth = os.path.join(dp, n)
                        if _st.S_ISSOCK(os.lstat(pth).st_mode):
                            sys.stderr.write(f"tar: {base}/{n}: socket ignored\n"); continue
                        t.add(pth, base + "/" + n, recursive=False)
        sys.exit(0)
    if entry == "sh":       # appdata restore script: RT/V point at the host folders standing in for the mounts
        envs["RT"] = mounts["/rt"]
        envs["V"] = os.environ["FAKE_VOLUMES"]
        r = subprocess.run(["/bin/sh", "-c", rest[1]], env={**os.environ, **envs}, stdin=sys.stdin)
        sys.exit(r.returncode)
    assert image == os.environ["FAKE_IMAGE"], image
    def host(p):
        for dst, src in sorted(mounts.items(), key=lambda kv: -len(kv[0])):
            if p == dst or p.startswith(dst + "/"):
                return src + p[len(dst):]
        return p
    env = {**os.environ, "HERMES_HOME": mounts["/opt/data"], "PYTHONPATH": os.environ["FAKE_HERMES_MODS"]}
    if entry.endswith("/hermes"):
        r = subprocess.run([sys.executable, os.environ["FAKE_HERMES_PY"]] + [host(x) for x in rest], env=env)
    else:
        r = subprocess.run([sys.executable] + rest, env=env, stdin=sys.stdin)
    sys.exit(r.returncode)
else:
    sys.exit("fake docker: unsupported " + a[0])
'''


def write_tools(base: Path) -> dict:
    """bin/{docker,hermes} + stock-Hermes modules. Returns env entries."""
    bin_ = base / "bin"
    mods = base / "hermes-mods"
    bin_.mkdir(parents=True, exist_ok=True)
    for rel, text in HERMES_MODS.items():
        p = mods / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text))
    (base / "hermes_impl.py").write_text(HERMES_PY)
    for name, body in (("docker", DOCKER_PY), ("hermes", f"import runpy, sys\nrunpy.run_path({str(base / 'hermes_impl.py')!r}, run_name='__main__')\n")):
        p = bin_ / name
        p.write_text(f"#!{sys.executable}\n" + body)
        p.chmod(0o755)
    return {"bin": bin_, "mods": mods, "hermes_py": base / "hermes_impl.py"}


# ---------------------------------------------------------------- fake Runtipi

class FakeRuntipi:
    """Acts on the box's runtipi tree like Runtipi's own lifecycle and per-app backup/restore do."""

    def __init__(self, root: Path, docker_state: Path, agent_homes: dict[str, Path]) -> None:
        self.root, self.docker_state, self.agent_homes = root, docker_state, agent_homes
        self.apps: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self._save_docker()

    # helpers
    def _paths(self, urn):
        app_id, store = urn.split(":")
        return (self.root / "app-data" / store / app_id, self.root / "apps" / store / app_id,
                self.root / "user-config" / store / app_id, self.root / "backups" / store / app_id)

    def _save_docker(self):
        st = {}
        for urn, a in self.apps.items():
            if urn in self.agent_homes:
                st[urn.replace(":", "_")] = {"id": "c-" + urn.split(":")[0], "image": IMAGE, "home": str(self.agent_homes[urn])}
        self.docker_state.write_text(json.dumps(st))

    def add(self, urn, status="running", version=1, config=None):
        self.apps[urn] = {"status": status, "version": version, "config": config or {}}
        self._save_docker()

    # Runtipi API surface used by the engine
    def installed(self):
        return [{"info": {"urn": u}, "app": {"status": a["status"], "version": a["version"]}} for u, a in self.apps.items()]

    def app(self, urn):
        if urn not in self.apps:
            raise RuntipiError(f"GET /apps/{urn} -> 404")
        return {"app": dict(self.apps[urn])}

    def install(self, urn, form=None):
        self.calls.append(("install", urn))
        app_id, store = urn.split(":")
        repo = self.root / "repos" / store / "apps" / app_id
        if not repo.is_dir():
            raise RuntipiError(f"POST /app-lifecycle/{urn}/install -> 404: app not in store")
        data, inst, _, _ = self._paths(urn)
        shutil.copytree(repo, inst)
        data.mkdir(parents=True, exist_ok=True)
        (data / "app.env").write_text(f"APP_ID={app_id}\nGENERATED=fresh\n")
        if urn in self.agent_homes:
            self.agent_homes[urn].mkdir(parents=True, exist_ok=True)
        cfg = json.loads((inst / "config.json").read_text())
        self.apps[urn] = {"status": "running", "version": cfg.get("tipi_version", 1), "config": form or {}}
        self._save_docker()
        return {}

    def uninstall(self, urn, remove_backups=False):
        self.calls.append(("uninstall", urn))
        data, inst, _, _ = self._paths(urn)
        shutil.rmtree(data, ignore_errors=True)
        shutil.rmtree(inst, ignore_errors=True)
        self.apps.pop(urn, None)
        self._save_docker()
        return {}

    def start(self, urn):
        self.calls.append(("start", urn))
        self.apps[urn]["status"] = "running"
        # like Runtipi's setAppDataDirPermissions after every `compose up`: chmod -Rf a+rwx on the app's data
        data = self._paths(urn)[0]
        for dp, dns, fns in os.walk(data):
            for n in [*dns, *fns]:
                q = Path(dp) / n
                if not q.is_symlink() and (q.is_dir() or q.is_file()):
                    q.chmod(q.stat().st_mode | 0o777)
        return {}

    def stop(self, urn):
        self.calls.append(("stop", urn))
        self.apps[urn]["status"] = "stopped"
        return {}

    def restart(self, urn):
        self.calls.append(("restart", urn))
        self.apps[urn]["status"] = "running"
        return {}

    def backup_app(self, urn):
        self.calls.append(("backup", urn))
        data, inst, uc, bdir = self._paths(urn)
        bdir.mkdir(parents=True, exist_ok=True)
        name = f"{urn.split(':')[0]}-{time.time_ns()}.tar.gz"
        with tarfile.open(bdir / name, "w:gz") as t:
            t.add(str(data), "./app-data")
            t.add(str(inst), "./app")
            if uc.is_dir():
                t.add(str(uc), "./user-config")
        return {"requestId": "x"}

    def list_backups(self, urn):
        bdir = self._paths(urn)[3]
        return [{"id": p.name, "size": p.stat().st_size, "date": p.stat().st_mtime_ns // 10**6}
                for p in sorted(bdir.iterdir())] if bdir.is_dir() else []

    def delete_backup(self, urn, filename):
        (self._paths(urn)[3] / filename).unlink(missing_ok=True)
        return {}

    def restore_backup(self, urn, filename):
        self.calls.append(("restore", urn, filename))
        data, inst, uc, bdir = self._paths(urn)
        tmp = bdir / ".restore"
        shutil.rmtree(tmp, ignore_errors=True)
        with tarfile.open(bdir / filename) as t:
            t.extractall(tmp, filter="fully_trusted")   # our own archive; keeps modes like Runtipi's copy
        for d in (data, inst, uc):
            shutil.rmtree(d, ignore_errors=True)
        shutil.copytree(tmp / "app-data", data)
        shutil.copytree(tmp / "app", inst)
        if (tmp / "user-config").is_dir():
            shutil.copytree(tmp / "user-config", uc)
        shutil.rmtree(tmp)
        cfg = json.loads((inst / "config.json").read_text())
        self.apps[urn]["version"] = cfg.get("tipi_version", 1)
        return {"requestId": "y"}

    def download_backup(self, urn, filename, dst):
        shutil.copyfile(self._paths(urn)[3] / filename, dst)
        return Path(dst).stat().st_size

    def upload_backup(self, urn, src, filename):
        bdir = self._paths(urn)[3]
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, bdir / filename)
        return {}


# ---------------------------------------------------------------- boxes

def _w(p: Path, text: str, mode: int | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        p.chmod(mode)
    return p


def _package(root: Path, store: str, app_id: str, tipi: int, version: str, kind: str = "app") -> None:
    for base in (root / "repos" / store / "apps" / app_id,):
        _w(base / "config.json", json.dumps({"id": app_id, "tipi_version": tipi, "version": version}))
        _w(base / "docker-compose.yml", f"services:\n  {app_id}:\n    image: example/{app_id}:{version}\n")
        _w(base / "wizard.md", f"---\nid: {app_id}\nname: {app_id}\ntype: {kind}\nversion: {version}\n---\n")
        _w(base / ".wizard-commit", (app_id[0] * 40) + "\n")


def _install_now(box_root: Path, rt: FakeRuntipi, urn: str, status: str = "running") -> None:
    app_id, store = urn.split(":")
    shutil.copytree(rt.root / "repos" / store / "apps" / app_id, rt.root / "apps" / store / app_id)
    cfg = json.loads((rt.root / "apps" / store / app_id / "config.json").read_text())
    rt.add(urn, status, cfg["tipi_version"], {"SOME_FORM": "value"})


def hermes_home(home: Path, *, wiring: bool, messaging: bool, jobs: list[dict], marker: str) -> None:
    cfg = {"model": {"default": "some-model"}, "marker": marker}
    if wiring:
        cfg.update({"terminal": {"backend": "local", "cwd": "/opt/data/workspace"},
                    "mcp_servers": {"wizard": {"url": "http://wizard-agent-service:8800/mcp"}},
                    "skills": {"external_dirs": ["/opt/wizard/skills"]},
                    "plugins": {"enabled": ["wizard-os"]}})
        cfg.setdefault("platforms", {})["api_server"] = {"enabled": True, "extra": {"port": 8642}}
    env = []
    if messaging:
        cfg.setdefault("platforms", {}).update({"telegram": {"enabled": True, "extra": {"chat": "x"}},
                                                "discord": {"enabled": False}})
        env += ["TELEGRAM_BOT_TOKEN=tg-secret-token", "DISCORD_BOT_TOKEN=dc-token", "SLACK_BOT_TOKEN=sl-token"]
    env += [f"API_SERVER_KEY={marker}-api-key", f"WIZARD_MARK={marker}", f"OPENROUTER_API_KEY={marker}-provider"]
    _w(home / "config.yaml", yaml.safe_dump(cfg, sort_keys=False))
    _w(home / ".env", "\n".join(env) + "\n", 0o600)
    _w(home / "cron" / "jobs.json", json.dumps({"jobs": jobs}))
    _w(home / "state.db", f"sessions of {marker}")
    _w(home / "memories" / "MEMORY.md", f"memory of {marker}")
    _w(home / "logs" / "agent.log", f"log of {marker}")
    _w(home / "bin" / "tool", f"binary of {marker}")
    _w(home / "cache" / "x.bin", "cache")
    _w(home / "plugins" / "wizard-os" / "plugin.py", f"wizard plugin of {marker}")
    _w(home / "plugins" / "own-plugin" / "plugin.py", f"own plugin of {marker}")


class FakeBox:
    """One fake machine: HOME, ~/.wizard-os, ~/runtipi, fake tools and a FakeRuntipi."""

    def __init__(self, base: Path, tools: dict, name: str) -> None:
        self.base = base / name
        self.homedir = self.base / "home"
        self.home = self.homedir / ".wizard-os"
        self.runtipi = self.homedir / "runtipi"
        self.log = self.base / "tools.log"
        self.docker_state = self.base / "docker.json"
        self.homedir.mkdir(parents=True)
        self.env = {"HOME": str(self.homedir), "WIZARD_OS_HOME": str(self.home), "RUNTIPI_ROOT": str(self.runtipi),
                    "HERMES_HOME": str(self.homedir / ".hermes"), "TMPDIR": str(self.base / "tmp"),
                    "PATH": f"{tools['bin']}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                    "FAKE_LOG": str(self.log), "FAKE_DOCKER_STATE": str(self.docker_state), "FAKE_IMAGE": IMAGE,
                    "FAKE_HERMES_MODS": str(tools["mods"]), "FAKE_HERMES_PY": str(tools["hermes_py"]),
                    "FAKE_VOLUMES": str(self.base / "volumes"), "FAKE_HELPER_IMAGE": "example/wam:helper",
                    "PYTHONDONTWRITEBYTECODE": "1", "WAM_BACKUP_TEST": "1",
                    "WIZARD_HERMES_BIN": str(tools["bin"] / "hermes"), "WIZARD_HERMES_PYTHON": sys.executable,
                    "PYTHONPATH": str(tools["mods"])}
        (self.base / "tmp").mkdir()
        (self.base / "volumes").mkdir()
        self.agent_homes = {"hermes-agent:wizardapps": self.runtipi / "app-data/wizardapps/hermes-agent/data",
                            "agent-research:wizardapps": self.home / "agents/research/home"}
        self.rt = FakeRuntipi(self.runtipi, self.docker_state, self.agent_homes)
        self.systemctl_calls: list[list[str]] = []
        self.free = None

    def box(self, **kw) -> Box:
        return Box(env=self.env, rt=self.rt, after_restore=kw.pop("after_restore", lambda b: None),
                   systemctl=lambda args: self.systemctl_calls.append(args) or True,
                   disk_free=(lambda p: self.free) if self.free is not None else None, sleep=lambda s: None, **kw)

    def tool_calls(self) -> list[dict]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []


def make_source(base: Path, tools: dict) -> FakeBox:
    b = FakeBox(base, tools, "old")
    h, rt = b.home, b.runtipi
    _w(h / "settings.json", json.dumps({"name": "oldbox", "theme": "dark"}))
    _w(h / "launcher.json", json.dumps({"tiles": ["notes-app"]}))
    _w(h / "routes.json", "{}")
    _w(h / "app-bindings.json", json.dumps({"bindings": {"notes-app": "research"}}))
    _w(h / "links.json", json.dumps({"notes-app": "https://notes.example.ts.net"}))
    _w(h / "secrets" / "notes-app.json", json.dumps({"DB_PASSWORD": "old-generated-pw"}), 0o600)
    _w(h / "app-tokens" / "notes-app", "old-app-token", 0o600)
    _w(h / "api-token", "OLD-API-TOKEN\n", 0o600)
    _w(h / "mcp-token", "OLD-MCP-TOKEN\n", 0o600)
    _w(h / "runtipi-operator.json", json.dumps({"username": "owner@wizard.local", "password": "old-op"}), 0o600)
    _w(h / "agent-runtime" / "helper.json", "{}")
    _w(h / "apps.json", json.dumps({"apps": [
        {"id": "notes-app", "name": "Notes", "type": "view", "version": "1.4.0", "urn": "notes-app:wizardapps",
         "repo": "example/notes-app", "commit": "n" * 40, "provides": []},
        {"id": "files-app", "name": "Files", "type": "view", "version": "2.0.0", "urn": "files-app:wizardapps",
         "repo": "example/files-app", "commit": "f" * 40, "provides": ["files"]},
        {"id": "hermes-agent", "name": "Hermes", "type": "agent", "version": "0.9", "urn": "hermes-agent:wizardapps",
         "repo": "example/hermes-agent", "commit": "h" * 40, "provides": ["agent"]}]}))
    _w(h / "skills" / "notes-app" / "SKILL.md", "regenerated")
    _w(h / "cache" / "junk.bin", "cache")
    _w(h / "settings.json.bak1", "old backup copy")
    _w(h / "backup.json", json.dumps({"schema": 1, "enabled": True, "every": "daily", "at": "03:30", "keep": 7,
                                      "box_id": "oldboxid0001",
                                      "target": {"kind": "s3", "endpoint": "https://s3.example.com", "bucket": "b",
                                                 "prefix": "wizard/oldboxid0001/", "region": "auto"}}))
    _w(h / "secrets" / "backup-target.json", json.dumps({"access_key_id": "AK", "secret_access_key": "SK"}), 0o600)
    # extra agent
    ag = h / "agents" / "research"
    _w(ag / "agent.json", json.dumps({"schema": 1, "id": "research", "label": "Research", "harness": "hermes",
                                      "privacy": "standard",
                                      "runtime": {"kind": "container", "app_id": "agent-research", "port": 18650}}))
    _w(ag / "runtime.key", "research-runtime-key", 0o600)
    _w(ag / "gateway.key", "research-gateway-key", 0o600)
    _w(ag / "service-state.json", "{}")
    _w(ag / "package" / "docker-compose.yml", "services: {}\n")
    _w(ag / "upload-staging" / "partial.bin", "partial upload")
    hermes_home(ag / "home", wiring=True, messaging=False, marker="research",
                jobs=[{"id": "j3", "name": "digest", "enabled": True, "state": "scheduled"}])
    # main agent (Runtipi app hermes-agent)
    hermes_home(rt / "app-data/wizardapps/hermes-agent/data", wiring=True, messaging=True, marker="oldmain",
                jobs=[{"id": "j1", "name": "morning", "enabled": True, "state": "scheduled"},
                      {"id": "j2", "name": "weekly", "enabled": False, "state": "paused", "paused_at": "x"}])
    _w(rt / "app-data/wizardapps/hermes-agent/app.env", "HERMES=1\n")
    # apps
    _package(rt, "wizardapps", "notes-app", 3, "1.4.0")
    _package(rt, "wizardapps", "files-app", 5, "2.0.0")
    _package(rt, "wizardapps", "hermes-agent", 7, "0.9", "agent")
    _package(rt, "wizardapps", "agent-research", 7, "0.9", "agent")
    for urn in ("hermes-agent:wizardapps", "agent-research:wizardapps", "notes-app:wizardapps"):
        _install_now(rt, b.rt, urn)
    _install_now(rt, b.rt, "files-app:wizardapps", "stopped")
    _w(rt / "app-data/wizardapps/notes-app/app.env", "DB_PASSWORD=old-generated-pw\n")
    _w(rt / "app-data/wizardapps/notes-app/data/notes.db", "old notes database")
    _w(rt / "user-config/wizardapps/notes-app/app.env", "NOTES_EXTRA=1\n")
    _w(rt / "app-data/wizardapps/files-app/data/index.json", "old files index")
    _w(rt / "user-config/wizardapps/files-app/app.env", "WIZARD_TRUSTED_ORIGINS=http://old-box\n", 0o600)
    _w(rt / "VERSION", "v4.0.0-test")
    _w(rt / "media" / "Documents" / "plan.txt", "the owner's plan")
    _w(rt / "media" / "Photos" / "a.jpg", "jpegbytes" * 100)
    _w(rt / "media" / ".hidden", "dotfile")
    return b


def make_fresh(base: Path, tools: dict) -> FakeBox:
    b = FakeBox(base, tools, "new")
    h, rt = b.home, b.runtipi
    _w(h / "settings.json", json.dumps({"name": "newbox"}))
    _w(h / "api-token", "NEW-API-TOKEN\n", 0o600)
    _w(h / "mcp-token", "NEW-MCP-TOKEN\n", 0o600)
    _w(h / "runtipi-operator.json", json.dumps({"username": "owner@wizard.local", "password": "new-op"}), 0o600)
    _w(h / "secrets" / "files-app.json", json.dumps({"K": "new"}), 0o600)
    _w(h / "secrets" / "whoami.json", json.dumps({"K": "kept"}), 0o600)
    _w(h / "apps.json", json.dumps({"apps": []}))
    hermes_home(rt / "app-data/wizardapps/hermes-agent/data", wiring=True, messaging=False, marker="newmain", jobs=[])
    cfgp = rt / "app-data/wizardapps/hermes-agent/data/config.yaml"
    cfg = yaml.safe_load(cfgp.read_text())
    cfg["terminal"] = {"backend": "local", "cwd": "/new/box/workspace"}
    cfg["mcp_servers"]["wizard"] = {"url": "http://new-box-service:8800/mcp"}
    cfgp.write_text(yaml.safe_dump(cfg))
    _package(rt, "wizardapps", "files-app", 5, "2.0.0")
    _package(rt, "wizardapps", "hermes-agent", 7, "0.9", "agent")
    _package(rt, "wizardapps", "agent-research", 7, "0.9", "agent")
    _package(rt, "wizardapps", "whoami", 1, "1.0")
    _install_now(rt, b.rt, "hermes-agent:wizardapps")
    _install_now(rt, b.rt, "files-app:wizardapps")
    _install_now(rt, b.rt, "whoami:wizardapps")
    _w(rt / "app-data/wizardapps/files-app/data/index.json", "fresh empty index")
    _w(rt / "user-config/wizardapps/files-app/app.env", "WIZARD_TRUSTED_ORIGINS=http://new-box\n", 0o600)
    _w(rt / "app-data/wizardapps/whoami/data/x", "whoami data")
    _w(rt / "VERSION", "v4.0.0-test")
    _w(rt / "media" / "existing.txt", "made on the new box")
    return b


def zip_names(data: bytes) -> list[str]:
    import zipfile
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)
