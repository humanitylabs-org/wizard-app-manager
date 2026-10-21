"""The Wizard Troubleshooter on every box: the native host service, not an app container.

The Troubleshooter (humanitylabs-org/wizard-troubleshooter) is the way back when chat is down: a repair chat, health checks
and one-tap restarts at /troubleshoot/ on the owner's shell address. It has to keep working when Runtipi, the shell,
the Agent Service or Hermes are broken, and it has to be able to restart them, so it runs as the owner's systemd user
units beside App Manager (like `wam serve`), never as a Runtipi app.

`wam troubleshooter` (run by install.sh on every install and rerun):
- fetches the commit the release pins (`components.wizard-troubleshooter`), or the repo's main on an unpinned box,
  into ~/.local/share/wizard-troubleshooter/app, and proves the checkout is exactly that commit;
- uses a system Node >= 20, else downloads a pinned official Node build and checks its sha256;
- writes the two units plus a generated environment file describing THIS box: alongside (host Hermes unit + port) or
  fresh (Hermes is the hermes-agent app: no host port, restarted as its container), shell and Agent Service ports and
  containers from apps.json, the owner gate = the tailnet login this box is signed in as (never a fixed address);
- guardian is report-only (GUARDIAN_ACT=0) until the owner turns automatic repairs on;
- mounts /troubleshoot on every owner shell HTTPS port, never replacing someone else's route;
- leaves a hand-installed Troubleshooter (units not written by this command) exactly as it is.

WIZARD_TROUBLESHOOTER=0 skips it. Status lands in ~/.wizard-os/troubleshooter.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from pathlib import Path

REPO = "humanitylabs-org/wizard-troubleshooter"
PORT = 9691
PREFIX = "/troubleshoot"
MARKER = "# Written by `wam troubleshooter` (wizard-app-manager); rerun the installer to update."
UNITS = ("wizard-troubleshooter.service", "wizard-guardian.service", "wizard-guardian.timer")
# Official Node.js LTS builds (https://nodejs.org/dist/v22.23.2/SHASUMS256.txt), only when the box has no Node >= 20.
NODE_VERSION = "22.23.2"
NODE_SHA256 = {
    "x64": "d60acfe00a2932254bb0ad20e01b0d74397a0875595de719654b214f4b03f307",
    "arm64": "fff4078c5def658577f92c88db7db3bc0072924bfb93fe52c1e744a54e94abb8",
}


def _run(args, timeout=120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kw)


def paths(home: Path | None = None) -> dict:
    h = Path(home or Path.home())
    share = h / ".local/share/wizard-troubleshooter"
    return {"share": share, "app": share / "app", "node": share / "node", "env": share / "host.env",
            "units": h / ".config/systemd/user", "state": h / ".local/state/wizard-troubleshooter"}


def pinned_source() -> tuple[str, str, bool]:
    """(repo, ref, pinned). A release that names the Troubleshooter pins its full commit; otherwise main."""
    from . import release_pins
    rel = release_pins.load() or {}
    row = (rel.get("components") or {}).get("wizard-troubleshooter") or {}
    repo, commit = str(row.get("repo") or REPO), str(row.get("commit") or "")
    if row:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("release components.wizard-troubleshooter needs owner/repo and a full commit")
        return repo, commit, True
    return REPO, "main", False


def fetch(repo: str, ref: str, dest: Path) -> str:
    """Exactly `ref` of a public repo in dest (detached, clean). Returns the commit."""
    url = os.environ.get("WAM_TROUBLESHOOTER_REPO_URL") or f"https://github.com/{repo}.git"
    if not (dest / ".git").is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        if _run(["git", "init", "-q", str(dest)]).returncode:
            raise ValueError("git init failed")
    g = ["git", "-C", str(dest)]
    _run([*g, "remote", "remove", "origin"])
    _run([*g, "remote", "add", "origin", url])
    if (p := _run([*g, "fetch", "-q", "--depth", "1", "origin", ref], 300)).returncode:
        raise ValueError(f"could not fetch {repo} {ref[:12]}: {(p.stderr or p.stdout).strip()[-200:]}")
    if (p := _run([*g, "checkout", "-q", "--force", "--detach", "FETCH_HEAD"])).returncode:
        raise ValueError(f"checkout failed: {p.stderr.strip()[-200:]}")
    _run([*g, "clean", "-qfdx", "-e", "node_modules"])
    head = _run([*g, "rev-parse", "HEAD"]).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", ref) and head != ref:
        raise ValueError(f"checkout is {head[:12]}, not the pinned {ref[:12]}")
    if _run([*g, "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise ValueError("checkout is not clean")
    if not (dest / "server.mjs").is_file() or not (dest / "bin/guardian.mjs").is_file():
        raise ValueError("fetched tree is not the Troubleshooter (server.mjs / bin/guardian.mjs missing)")
    return head


def _node_ok(path: str) -> bool:
    try:
        return _run([path, "-e", "process.exit(+process.versions.node.split('.')[0] >= 20 ? 0 : 1)"], 20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def node(p: dict) -> str:
    """A Node >= 20 that does not live inside an agent's private tool folders (those move on agent updates)."""
    own = p["node"] / "bin/node"
    cands = [os.environ.get("WAM_TROUBLESHOOTER_NODE", ""), "/usr/bin/node", "/usr/local/bin/node", str(own)]
    for c in cands:
        if c and os.access(c, os.X_OK) and _node_ok(c):
            return c
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not arch:
        raise ValueError(f"no Node >= 20 on this box and no official build for {platform.machine()}")
    name = f"node-v{NODE_VERSION}-linux-{arch}"
    base = os.environ.get("WAM_NODE_DIST", f"https://nodejs.org/dist/v{NODE_VERSION}")
    tmp = p["share"] / f"{name}.tar.xz"
    p["share"].mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"{base}/{name}.tar.xz", timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != NODE_SHA256[arch]:
        tmp.unlink()
        raise ValueError("downloaded Node build does not match its published sha256")
    stage = p["share"] / "node.new"
    shutil.rmtree(stage, ignore_errors=True)
    with tarfile.open(tmp) as t:
        try:
            t.extractall(stage, filter="data")
        except TypeError:  # Python without extraction filters; the archive's sha256 was verified above
            t.extractall(stage)
    tmp.unlink()
    shutil.rmtree(p["node"], ignore_errors=True)
    (stage / name).rename(p["node"])
    shutil.rmtree(stage, ignore_errors=True)
    if not _node_ok(str(own)):
        raise ValueError("installed Node does not run")
    return str(own)


def _port_of(url: str | None) -> int | None:
    m = re.match(r"^http://(?:127\.0\.0\.1|localhost):(\d{1,5})(?:/|$)", str(url or ""))
    return int(m.group(1)) if m else None


def tailnet_owner() -> str:
    """The tailnet login this box is signed in as (its owner). Empty for a tagged node: any human on the tailnet."""
    try:
        from . import links
        d = json.loads(links._run(["tailscale", "status", "--json"]).stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""
    me = d.get("Self") or {}
    if me.get("Tags"):
        return ""
    login = ((d.get("User") or {}).get(str(me.get("UserID"))) or {}).get("LoginName") or ""
    return login if re.fullmatch(r"[A-Za-z0-9._+@-]{1,254}", login) else ""


def layout(wizard_home: Path, home: Path, runtipi_root: Path) -> dict:
    """This box's wiring, from App Manager's own records (apps.json, alongside.json)."""
    try:
        reg = json.loads((wizard_home / "apps.json").read_text())
    except (OSError, ValueError):
        reg = {}
    apps = {a.get("id"): a for a in reg.get("apps") or [] if isinstance(a, dict)}
    try:
        al = json.loads((wizard_home / "alongside.json").read_text())
    except (OSError, ValueError):
        al = {}
    alongside = al.get("mode") == "alongside"
    shell = next((a for a in apps.values() if a.get("category") == "interface" and a.get("health")), apps.get("wizard-ux") or {})
    service = apps.get("wizard-agent-service") or {}
    shell_port = (al.get("shell") or {}).get("port") if alongside else None
    shell_port = int(shell_port or _port_of(shell.get("health")) or _port_of((shell.get("view") or {}).get("local")) or 0)
    agent_port = (al.get("service") or {}).get("port") if alongside else None
    agent_port = int(agent_port or _port_of(service.get("health")) or _port_of((service.get("mcp") or {}).get("url")) or 0)
    container = lambda app: f"{app}_wizardapps-{app}-1"  # noqa: E731  Runtipi's compose project naming
    out = {"mode": "alongside" if alongside else "fresh", "shell_port": shell_port, "agent_port": agent_port,
           "shell_container": container(shell.get("id") or "wizard-ux"),
           "agent_container": container("wizard-agent-service")}
    if alongside:
        origin = str((al.get("agent") or {}).get("origin") or "http://127.0.0.1:8642")
        out["hermes_port"] = _port_of(origin) or 8642
        unit = "hermes-gateway.service"
        has_unit = _run(["systemctl", "--user", "cat", unit], 20).returncode == 0
        out["hermes_unit"] = unit if has_unit else ""
        out["hermes_container"] = ""
        out["chatgpt_auth"] = str(Path((al.get("agent") or {}).get("hermes_home") or home / ".hermes") / "auth.json")
    else:
        out["hermes_port"] = 0
        out["hermes_unit"] = ""
        out["hermes_container"] = "hermes-agent_wizardapps-hermes-agent-engine-1"
        out["chatgpt_auth"] = str(runtipi_root / "app-data/wizardapps/hermes-agent/data/auth.json")
    return out


def environment(lay: dict, host: str, origins: list[str], owner: str, home: Path) -> dict:
    docker = shutil.which("docker") or "docker"
    env = {
        "PORT": str(PORT),
        "WIZARD_PUBLIC_NAME": host,
        "WIZARD_PRIVATE_ORIGINS": ",".join(origins),
        "TROUBLESHOOT_FRONT": "tailscale-serve",
        "TROUBLESHOOT_OWNERS": owner,
        "TS_SOCKET": os.environ.get("WAM_TS_SOCKET", "/run/tailscale/tailscaled.sock"),
        "TAILSCALE_UNIT": "", "GATEWAY_MODE": "tailscale-serve", "GATEWAY_PORT": str(lay["shell_port"]), "GATEWAY_UNIT": "",
        "AUTH_PORT": "",
        "SHELL_PORT": str(lay["shell_port"]), "SHELL_CONTAINER": lay["shell_container"],
        "AGENT_PORT": str(lay["agent_port"]), "AGENT_CONTAINER": lay["agent_container"],
        "HERMES_PORT": str(lay["hermes_port"]), "HERMES_UNIT": lay["hermes_unit"], "HERMES_CONTAINER": lay["hermes_container"],
        "DOCKER_BIN": docker,
        "CHATGPT_AUTH_FILE": lay["chatgpt_auth"],
        "GUARDIAN_ACT": "0",
        "PATH": f"{home}/bin:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
    }
    if os.environ.get("DOCKER_HOST"):
        env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
    return env


def _units(app: Path, node_bin: str, env_file: Path) -> dict[str, str]:
    svc = f"""{MARKER}
[Unit]
Description=Wizard Troubleshooter (/troubleshoot/): repair chat, health checks, one-tap fixes
After=network-online.target

[Service]
WorkingDirectory={app}
EnvironmentFile={env_file}
ExecStart={node_bin} {app}/server.mjs
Restart=always
RestartSec=2
MemoryMax=2G

[Install]
WantedBy=default.target
"""
    guard = f"""{MARKER}
[Unit]
Description=Wizard Guardian: checks every link to the agent (report-only until the owner turns repairs on)

[Service]
Type=oneshot
WorkingDirectory={app}
EnvironmentFile={env_file}
ExecStart={node_bin} {app}/bin/guardian.mjs
TimeoutStartSec=900
"""
    timer = f"""{MARKER}
[Unit]
Description=Run the Wizard Guardian every minute

[Timer]
OnBootSec=60
OnUnitInactiveSec=60
AccuracySec=5s

[Install]
WantedBy=timers.target
"""
    return dict(zip(UNITS, (svc, guard, timer)))


def hand_installed(p: dict) -> bool:
    f = p["units"] / UNITS[0]
    try:
        return MARKER not in f.read_text()
    except OSError:
        return False


def ping(timeout_s: float = 30) -> bool:
    end = time.time() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{PREFIX}/api/ping", timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        if time.time() >= end:
            return False
        time.sleep(1)


def routes(host: str, shell_port: int, apply: bool) -> tuple[list[int], dict]:
    """/troubleshoot on every owner shell HTTPS port. Returns (ports routed, errors)."""
    from . import links
    target = f"http://127.0.0.1:{PORT}{PREFIX}"
    ok, errs = [], {}
    try:
        ports = links.gateway_shell_ports(links.serve_status(), host, shell_port)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return [], {"serve": str(e)[:200]}
    for port in ports:
        try:
            st = links.route_status(links.serve_status(), host, port, PREFIX, target)
            if st == "conflict":
                raise ValueError(f"existing Serve route on {port}{PREFIX} differs; left untouched")
            if st == "missing" and apply:
                r = links._run(["tailscale", "serve", "--bg", f"--https={port}", f"--set-path={PREFIX}", target], 60)
                if r.returncode:
                    raise ValueError((r.stderr or r.stdout).strip()[-200:] or "Tailscale Serve failed")
                if links.route_status(links.serve_status(), host, port, PREFIX, target) != "match":
                    raise ValueError("Serve route readback did not match")
            ok.append(port)
        except (OSError, subprocess.TimeoutExpired, ValueError) as e:
            errs[str(port)] = str(e)[:200]
    return ok, errs


def install(wizard_home: Path, runtipi_root: Path, home: Path | None = None, apply: bool = True) -> dict:
    """Install or update the native Troubleshooter. Returns a status record (also saved unless dry run)."""
    home = Path(home or Path.home())
    p = paths(home)
    res: dict = {"state": "ready", "port": PORT, "dir": str(p["app"])}

    def done(state: str, **kw) -> dict:
        res.update(state=state, **kw, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if apply:
            tmp = wizard_home / "troubleshooter.json.tmp"
            tmp.write_text(json.dumps(res, indent=2) + "\n")
            tmp.replace(wizard_home / "troubleshooter.json")
        return res

    if os.environ.get("WIZARD_TROUBLESHOOTER", "1") == "0":
        return done("skipped", reason="WIZARD_TROUBLESHOOTER=0")
    if hand_installed(p):
        return done("kept", reason=f"a hand-installed {UNITS[0]} exists; left exactly as it is")
    if not shutil.which("systemctl") or _run(["systemctl", "--user", "show-environment"], 20).returncode:
        return done("skipped", reason="no systemd user session on this box (the Troubleshooter needs one)")
    from . import links
    ts = links.tailnet()
    if ts["state"] != "Running" or not ts["host"]:
        return done("waiting", reason="Tailscale isn't connected yet; rerun the installer after `sudo tailscale up`")
    lay = layout(wizard_home, home, runtipi_root)
    res["layout"] = lay
    if not lay["shell_port"] or not lay["agent_port"]:
        return done("failed", reason="could not find the shell / Agent Service ports in apps.json (run `wam apps`)")
    repo, ref, pinned = pinned_source()
    res.update(repo=repo, ref=ref, pinned=pinned)
    owner = tailnet_owner()
    res["owner"] = owner or "(any person on this tailnet: the box is a tagged node)"
    try:
        shell_ports = links.gateway_shell_ports(links.serve_status(), ts["host"], lay["shell_port"])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        shell_ports = [lay["shell_port"]]
    origins = [f"https://{ts['host']}" + ("" if port == 443 else f":{port}") for port in shell_ports]
    env = environment(lay, ts["host"], origins, owner, home)
    if not apply:
        return {**res, "env": env, "dry_run": True}
    try:
        res["commit"] = fetch(repo, ref, p["app"])
        node_bin = node(p)
    except (OSError, subprocess.TimeoutExpired, ValueError, tarfile.TarError) as e:
        return done("failed", reason=str(e)[:300])
    res["node"] = node_bin
    p["share"].mkdir(parents=True, exist_ok=True)
    p["env"].write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    p["env"].chmod(0o600)
    p["units"].mkdir(parents=True, exist_ok=True)
    changed = False
    for name, body in _units(p["app"], node_bin, p["env"]).items():
        f = p["units"] / name
        if not f.is_file() or f.read_text() != body:
            f.write_text(body)
            changed = True
    sc = lambda *a: _run(["systemctl", "--user", *a], 60)  # noqa: E731
    sc("daemon-reload")
    if (r := sc("enable", UNITS[0], UNITS[2])).returncode:
        return done("failed", reason=f"systemctl enable failed: {(r.stderr or r.stdout).strip()[-200:]}")
    # Always restart: the checkout or the box's wiring may have changed even when the unit text did not.
    sc("restart", UNITS[0])
    sc("start", UNITS[2])
    res["units_changed"] = changed
    if not ping():
        return done("failed", reason=f"the service did not answer on 127.0.0.1:{PORT}{PREFIX}/api/ping")
    routed, errs = routes(ts["host"], lay["shell_port"], True)
    res.update(routes=[f"https://{ts['host']}:{port}{PREFIX}/" for port in routed], route_errors=errs)
    if errs:
        return done("degraded", reason="running, but /troubleshoot is not on every shell address: " + "; ".join(f"{k}: {v}" for k, v in errs.items()))
    return done("ready")


def describe(res: dict) -> str:
    st = res.get("state")
    if st == "ready":
        return f"Troubleshooter {str(res.get('commit', ''))[:12]} is running: " + ", ".join(res.get("routes") or [])
    return f"Troubleshooter {st}: {res.get('reason', '')}".rstrip(": ")
