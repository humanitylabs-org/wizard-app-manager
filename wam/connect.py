"""Connect installed apps' tools (MCP) to every installed agent, so a fresh box works without hand edits.

After any install, uninstall or registry refresh, each running agent app gets one MCP connection per other
installed app that declares `mcp:` in its wizard.md, at the app's internal address on the App Manager network
(e.g. http://transcriber:8000/mcp). Apps that were removed lose their connection.

Only connections the App Manager created are ever touched; it records them in ~/.wizard-os/connections.json,
so anything the owner added by hand stays exactly as it is.

Each agent is changed through its own supported command, never by editing its files. An agent that the App
Manager doesn't know how to configure is skipped and reported, not guessed at.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# How to reach an agent's own config command. Keyed by app id; the compose service and user come from the
# app's runtipi compose. New agents add a row here (or, later, declare it in wizard.md).
AGENTS = {
    "hermes-agent": {
        "service": "hermes-agent-engine",
        "user": "hermes",
        "set": ["hermes", "config", "set", "mcp_servers.{name}.url", "{url}"],
        "unset": ["hermes", "config", "unset", "mcp_servers.{name}"],
        "reload": "restart",   # the gateway reads mcp_servers at start; restart the one container that changed
    },
}

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
URL_RE = re.compile(r"^http://[a-z0-9][a-z0-9-]{0,62}:[0-9]{1,5}/[A-Za-z0-9/_.-]*$")


def _docker(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def container_for(app_id: str, store: str, service: str) -> str | None:
    r = _docker(["ps", "-q", "--filter", f"label=com.docker.compose.project={app_id}_{store}",
                 "--filter", f"label=com.docker.compose.service={service}"])
    ids = r.stdout.split()
    return ids[0] if r.returncode == 0 and len(ids) == 1 else None


def wanted(reg: dict, agent_id: str) -> dict[str, str]:
    """name -> internal MCP url for every installed app with tools, except the agent itself and other agents."""
    out = {}
    for a in reg.get("apps") or []:
        m = a.get("mcp") or {}
        if a["id"] == agent_id or a.get("type") == "agent" or not m.get("internal"):
            continue
        if a.get("status") not in ("running", None):
            continue
        if NAME_RE.match(a["id"]) and URL_RE.match(m["internal"]):
            out[a["id"]] = m["internal"]
    return out


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def sync(reg: dict, store: str, home: Path, dry_run: bool = False) -> dict:
    """Bring every running agent's App Manager-made connections in line with what is installed."""
    state_file = home / "connections.json"
    state = _load(state_file)
    report: dict = {}
    for a in reg.get("apps") or []:
        spec = AGENTS.get(a["id"])
        if a.get("type") != "agent":
            continue
        if not spec:
            report[a["id"]] = {"skipped": "the App Manager doesn't know how to configure this agent yet"}
            continue
        if a.get("status") != "running":
            report[a["id"]] = {"skipped": f"agent is {a.get('status')}"}
            continue
        cid = container_for(a["id"], store, spec["service"])
        if not cid:
            report[a["id"]] = {"skipped": f"no running {spec['service']} container"}
            continue
        mine: dict[str, str] = dict(state.get(a["id"]) or {})
        want = wanted(reg, a["id"])
        added, removed, failed = [], [], {}
        for name, url in want.items():
            if mine.get(name) == url:
                continue
            cmd = [x.format(name=name, url=url) for x in spec["set"]]
            if not dry_run:
                r = _docker(["exec", "-u", spec["user"], cid, *cmd])
                if r.returncode != 0:
                    failed[name] = (r.stderr or r.stdout).strip()[-200:]
                    continue
            mine[name] = url
            added.append(name)
        for name in [n for n in mine if n not in want]:
            cmd = [x.format(name=name) for x in spec["unset"]]
            if not dry_run:
                r = _docker(["exec", "-u", spec["user"], cid, *cmd])
                if r.returncode != 0 and "not" not in (r.stderr + r.stdout).lower():
                    failed[name] = (r.stderr or r.stdout).strip()[-200:]
                    continue
            mine.pop(name, None)
            removed.append(name)
        reloaded = False
        if (added or removed) and not dry_run and spec.get("reload") == "restart":
            reloaded = _docker(["restart", cid], timeout=180).returncode == 0
        state[a["id"]] = mine
        report[a["id"]] = {"connected": sorted(mine), "added": added, "removed": removed,
                           "reloaded": reloaded, **({"failed": failed} if failed else {})}
    if not dry_run:
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(state_file)
    return report
