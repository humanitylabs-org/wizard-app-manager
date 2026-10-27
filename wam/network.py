"""App-to-app network isolation: the declared connections are the only paths between Wizard apps.

Stock Runtipi puts every app's main service on one shared Docker network (`runtipi_tipi_main_network`), so any app
can open a connection to any other. Here the network topology is the access control (wizard-os spec/PERMISSIONS.md,
layer 1, per-connection form):

- `wizard-app-<id>`: the app's own bridge (not internal). Its internet access and its published loopback port.
  Only that app's services are on it.
- `wizard-link-<consumer>--<provider>`: one internal network per declared connection, holding exactly the
  consumer's services and the provider's main service, which answers there as `<provider id>` (and its compose
  service name). Two consumers of the same provider sit on different links, so they cannot reach each other.
- `wizard-gw-<id>` (only when the gateway runs in a container, i.e. a fresh box): the shell, the Agent Service and
  the agent join every app's gateway network, so the tool gateway keeps its `http://<id>:<port>` addresses. Alongside
  boxes run the gateway on the host network and reach apps on their published 127.0.0.1 ports, which never change.
- `wizard-core` (fresh boxes): the shell, the Agent Service and the agent among themselves.

A connection is declared by the consumer's wizard.md `uses:` (an app id or a slot), `requires.models/services`
(the Connection Router), or an approved app grant. Nothing else connects two apps: an undeclared pair has no shared
network, which Docker enforces. No secrets are involved.

The topology lives in each app's stock Runtipi user-config compose override (a block marked `x-wizard-networks`, the
rest of the file is kept), so every start, restart, update and reboot that Runtipi does keeps it. `reconcile` writes
those blocks, creates/removes the networks, connects/disconnects running containers live where that is enough, and
restarts (through the App Manager API) only the apps that still sit on the shared network. `disable` undoes all of it.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

SHARED = "tipi_main_network"                 # Runtipi's logical name for the shared network in generated composes
SHARED_DOCKER = "runtipi_tipi_main_network"  # and its Docker name (RUNTIPI_DOCKER_NETWORK can change it)
APP_PREFIX, LINK_PREFIX, GW_PREFIX, CORE_NET = "wizard-app-", "wizard-link-", "wizard-gw-", "wizard-core"
LABEL = "org.wizard-os.network"              # kind: app | link | gateway | core
MARK = "x-wizard-networks"                   # top-level key in the override: what wam set (so it can be removed)
HEADER = "# App networks: the block marked x-wizard-networks is written by wam (`wam network reconcile`).\n"
POOL, PREFIXLEN = "10.255.0.0/16", 27        # outside Runtipi's 10.128-10.254 app subnets and Docker's defaults
CORE_SLOTS = ("shell", "agent-service", "agent")
LEGACY_BUNDLE = "wizard-shell"
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


class NetworkError(RuntimeError):
    pass


OURS = re.compile(r"(?:wizard-(?:app|link|gw)-[a-z0-9-]+|wizard-core)")


def ours(name: str) -> bool:
    """A network wam manages. Compose project networks of apps whose id starts with `wizard-` look similar
    (`wizard-claude_wizardapps_wizard-claude_wizardapps_network`) but always contain an underscore."""
    return bool(OURS.fullmatch(name))


# ---------------------------------------------------------------- tag-preserving YAML (overrides use !reset/!override)

class _Tagged:
    def __init__(self, tag: str, value):
        self.tag, self.value = tag, value

    def __eq__(self, other):
        return isinstance(other, _Tagged) and (self.tag, self.value) == (other.tag, other.value)


class _Loader(yaml.SafeLoader):
    pass


class _Dumper(yaml.SafeDumper):
    pass


def _tagged(loader, tag, node):
    make = {yaml.ScalarNode: loader.construct_scalar, yaml.SequenceNode: loader.construct_sequence,
            yaml.MappingNode: loader.construct_mapping}[type(node)]
    return _Tagged(tag, make(node, deep=True) if not isinstance(node, yaml.ScalarNode) else make(node))


def _represent(dumper, value):
    node = dumper.represent_data(value.value)
    node.tag = value.tag
    return node


_Loader.add_multi_constructor("!", lambda loader, tag, node: _tagged(loader, "!" + tag, node))
_Dumper.add_representer(_Tagged, _represent)


def load_yaml(text: str):
    return yaml.load(text, Loader=_Loader)


def dump_yaml(doc) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, default_flow_style=False)


def _plain(v):
    return v.value if isinstance(v, _Tagged) else v


# ---------------------------------------------------------------- settings

def _cfg(home: Path) -> dict:
    try:
        cfg = json.loads((home / "app-manager.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def _save_cfg(home: Path, cfg: dict) -> None:
    p = home / "app-manager.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(p)


def enabled(home: Path) -> bool:
    """On unless switched off: WIZARD_APP_ISOLATION=0, or app-manager.json `app_isolation: false` (`wam network off`)."""
    env = os.environ.get("WIZARD_APP_ISOLATION")
    if env is not None and env.strip() != "":
        return env.strip().lower() not in ("0", "false", "off", "no")
    return _cfg(home).get("app_isolation") is not False


def set_enabled(home: Path, on: bool) -> None:
    cfg = _cfg(home)
    if on:
        cfg.pop("app_isolation", None)
    else:
        cfg["app_isolation"] = False
    _save_cfg(home, cfg)


def host_access_allowed(home: Path) -> set[str]:
    """Apps the owner allowed to escape isolation (`wam install <id> --allow-host-access`, `wam network allow <id>`)."""
    got = _cfg(home).get("host_access_allowed") or []
    return {str(x) for x in got if isinstance(x, str) and ID.fullmatch(x)}


def set_host_access_allowed(home: Path, app_id: str, on: bool) -> None:
    if not ID.fullmatch(app_id):
        raise NetworkError(f"not an app id: {app_id}")
    cfg = _cfg(home)
    have = host_access_allowed(home)
    have = have | {app_id} if on else have - {app_id}
    if have:
        cfg["host_access_allowed"] = sorted(have)
    else:
        cfg.pop("host_access_allowed", None)
    _save_cfg(home, cfg)


# The App Manager (Runtipi fork, packages/backend/src/modules/docker/wizard-networks.ts) reads this before every
# `compose up` of a Wizard app, whoever asked for it (wam, the App Store views, Runtipi's own UI): with isolation on it
# never puts an app's unexposed services on the shared network and refuses networks the app does not own, unless
# the owner allowed that app. Missing file = isolation on, nothing allowed (fail closed).
POLICY_FILE = "wizard-app-networks.json"


def policy_path(runtipi_root: Path) -> Path:
    return Path(runtipi_root) / "user-config" / POLICY_FILE


def write_policy(c, on: bool) -> bool:
    """Mirror wam's switch and the owner's host-access allowances where the App Manager enforces them."""
    p = policy_path(c.runtipi_root)
    body = json.dumps({"schema": 1, "enabled": bool(on), "allow_host_access": sorted(host_access_allowed(c.home))},
                      indent=2) + "\n"
    if p.is_file() and p.read_text() == body:
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".wam-tmp")
    tmp.write_text(body)
    os.chmod(tmp, 0o644)
    tmp.replace(p)
    return True


def pool(home: Path) -> ipaddress.IPv4Network:
    try:
        cfg = json.loads((home / "app-manager.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    return ipaddress.IPv4Network(str(cfg.get("network_pool") or POOL))


# ---------------------------------------------------------------- names

def link_name(consumer: str, provider: str) -> str:
    """One name per (consumer, provider), never shared by two pairs. App ids may contain `--` themselves
    (`alpha--beta`→`charlie` vs `alpha`→`beta--charlie`), so the readable form is used only when it can be split one
    way; otherwise the name is a hash of the pair (which never contains `--`, so it can't equal a readable name)."""
    if "--" not in consumer and "--" not in provider and not consumer.endswith("-") and not provider.startswith("-"):
        return f"{LINK_PREFIX}{consumer}--{provider}"
    import hashlib
    digest = hashlib.sha256((consumer + "\0" + provider).encode()).hexdigest()[:24]
    return f"{LINK_PREFIX}h-{digest}"


def app_net(app_id: str) -> str:
    return f"{APP_PREFIX}{app_id}"


def gw_net(app_id: str) -> str:
    return f"{GW_PREFIX}{app_id}"


# ---------------------------------------------------------------- host access (installer gate)

def network_escapes(compose: dict, app_id: str | None = None, store: str = "wizardapps") -> list[str]:
    """Networks in a recipe that are not the app's own: a pre-existing (`external`) network, a fixed Docker `name`
    outside the app's compose project (another app's network, `runtipi_tipi_main_network`, a reserved Wizard network),
    a non-bridge driver, or a service on Docker's default bridge or in another container's namespace. Any of them
    would let the app reach apps it never declared, so it is refused unless the owner allows host access.
    Same rule as the App Manager's start-time gate (packages/backend/src/modules/docker/wizard-networks.ts)."""
    out = []
    own = f"{app_id}_{store}_" if app_id else None
    top = (compose.get("networks") or {}) if isinstance(compose, dict) else {}
    for key, d in (top.items() if isinstance(top, dict) else []):
        if str(key) == SHARED:   # Runtipi's own entry: the builder rewrites it, and wam strips it from isolated apps
            continue
        d = _plain(d) or {}
        d = d if isinstance(d, dict) else {}
        ext = _plain(d.get("external"))
        name = str(_plain(d.get("name")) or (ext.get("name") if isinstance(ext, dict) else "") or "")
        driver = str(_plain(d.get("driver")) or "")
        if ours(name) or ours(str(key)):
            out.append(f"network {key}: attaches to the reserved Wizard network {name or key} (reaches other apps)")
        elif ext:
            out.append(f"network {key}: attaches to the existing network {name or key}, which this app does not own "
                       "(reaches other apps)")
        elif name and not (own and name.startswith(own)):
            out.append(f"network {key}: uses the fixed Docker name {name}, outside this app (another app or Runtipi "
                       "can share it)")
        if driver and driver != "bridge":
            out.append(f"network {key}: driver {driver} (not an app-private bridge)")
    for name, s in ((compose.get("services") or {}) if isinstance(compose, dict) else {}).items():
        s = _plain(s)
        if not isinstance(s, dict):
            continue
        mode = str(_plain(s.get("network_mode")) or "").lower()
        if mode == "host":
            out.append(f"{name}: network_mode: host (shares the server's network, bypasses app isolation)")
        elif mode.startswith("container:"):   # another container's namespace (e.g. runtipi's); service: stays allowed
            out.append(f"{name}: network_mode: {mode} (shares another container's network, bypasses app isolation)")
        elif mode == "bridge" or mode == "default":
            out.append(f"{name}: network_mode: {mode} (Docker's default bridge, shared with other containers)")
    return out


def host_access(compose: dict | str, app_id: str | None = None, store: str = "wizardapps") -> list[str]:
    """What in a recipe escapes app isolation: host networking, networks the app does not own, privileged mode,
    the container runtime socket."""
    if isinstance(compose, str):
        try:
            compose = load_yaml(compose) or {}
        except yaml.YAMLError as e:
            return [f"compose does not parse ({str(e)[:80]})"]
    out = network_escapes(compose, app_id, store)
    for name, s in ((compose.get("services") or {}) if isinstance(compose, dict) else {}).items():
        s = _plain(s)
        if not isinstance(s, dict):
            continue
        if _plain(s.get("privileged")) is True:
            out.append(f"{name}: privileged (full control of the server)")
        for v in _plain(s.get("volumes")) or []:
            src = v.split(":")[0] if isinstance(v, str) else str((v or {}).get("source", ""))
            if any(x in src for x in ("docker.sock", "containerd.sock", "podman.sock")):
                out.append(f"{name}: mounts the container runtime socket {src} (controls every app)")
    return out


# ---------------------------------------------------------------- reading what is installed (files only)

def _front(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    try:
        return (yaml.safe_load(m.group(1)) or {}) if m else {}
    except yaml.YAMLError:
        return {}


def _service_nets(svc: dict) -> dict:
    nets = _plain(svc.get("networks")) or {}
    if isinstance(nets, list):
        return {str(n): {} for n in nets}
    return {str(k): (_plain(v) or {}) for k, v in nets.items()} if isinstance(nets, dict) else {}


def override_path(runtipi_root: Path, store: str, app_id: str) -> Path:
    return Path(runtipi_root) / "user-config" / store / app_id / "docker-compose.yml"


def source_services(doc: dict) -> dict:
    """Match ComposeBuilder: active-architecture mappings merge, arrays replace."""
    import copy
    services = copy.deepcopy(_plain(doc.get("services")) or {})
    overrides = (_plain(doc.get("x-runtipi")) or {}).get("overrides") or []
    if not overrides:
        return services
    from .test_catalog import host_architecture
    architecture = host_architecture()
    override = next((o for o in overrides if o.get("architecture") == architecture), {})

    def merge(base, incoming):
        if isinstance(base, dict) and isinstance(incoming, dict):
            result = copy.deepcopy(base)
            for key, value in incoming.items():
                result[key] = merge(result.get(key), value)
            return result
        return copy.deepcopy(incoming)

    return merge(services, override.get("services") or {})


def read_app(runtipi_root: Path, store: str, app_id: str, fresh: bool = False) -> dict | None:
    """One installed (or about-to-be-installed) Wizard app as the planner needs it. Prefers the compose Runtipi
    generated (what actually runs), else the recipe from the installed or store copy (before the first install)."""
    root = Path(runtipi_root)
    installed, stored = root / "apps" / store / app_id, root / "repos" / store / "apps" / app_id
    wm = next((d / "wizard.md" for d in (installed, stored) if (d / "wizard.md").is_file()), None)
    gen = installed / "docker-compose.generated.yml"
    order = (stored, installed) if fresh else (installed, stored)   # fresh: the recipe an update is about to apply
    recipe = next((d / "docker-compose.yml" for d in order if (d / "docker-compose.yml").is_file()), None)
    if fresh:
        wm = next((d / "wizard.md" for d in order if (d / "wizard.md").is_file()), None)
    if not recipe:
        return None
    try:
        rdoc = load_yaml(recipe.read_text()) or {}
        gdoc = load_yaml(gen.read_text()) if gen.is_file() and not fresh else None
    except (OSError, yaml.YAMLError) as e:
        return {"id": app_id, "custom": f"compose does not parse ({str(e)[:80]})", "services": {}, "uses": []}
    w = _front(wm.read_text()) if wm else {}
    req = w.get("requires") if isinstance(w.get("requires"), dict) else {}
    rsvcs = source_services(rdoc)
    multi = len([s for s in rsvcs.values() if s]) > 1
    services = {}
    for name, rs in rsvcs.items():
        rs = _plain(rs) or {}
        xr = _plain(rs.get("x-runtipi")) or {}
        gs = _plain(((gdoc or {}).get("services") or {}).get(name)) or {}
        nm = str(_plain(rs.get("network_mode")) or _plain(gs.get("network_mode")) or "")
        if gdoc is not None and gs:
            # Only networks the installed recipe defines (plus Runtipi's own): a generated compose can be stale, e.g.
            # written for an update the App Manager then refused, and must never carry a foreign network into the
            # override (seen live: a refused `sneak` attachment reappeared as `sneak: null` and broke the app).
            own = {str(k) for k in (rdoc.get("networks") or {})} | {f"{app_id}_{store}_network", SHARED}
            nets = {k: v for k, v in _service_nets(gs).items() if k in own}
        else:   # what Runtipi's compose builder will generate (compose.builder.ts buildService)
            nets = {} if nm else _service_nets(rs)
            if not nm and multi:
                nets[f"{app_id}_{store}_network"] = {"gw_priority": 0}
            if not nm and (xr.get("is_main") or xr.get("add_to_main_network")):
                nets[SHARED] = {"gw_priority": 1}
        labels = _plain(gs.get("labels")) or {}
        if isinstance(labels, list):
            labels = dict(str(x).split("=", 1) for x in labels if "=" in str(x))
        top = (gdoc or rdoc).get("networks") or {}

        def docker_name(logical: str) -> str:
            d = _plain(top.get(logical)) or {}
            return str(d.get("name")) if isinstance(d, dict) and d.get("name") else f"{app_id}_{store}_{logical}"
        services[name] = {
            "docker_networks": sorted(docker_name(k) for k in nets if k != SHARED),
            "main": bool(xr.get("is_main")),
            "on_shared": SHARED in nets or bool(xr.get("is_main") or xr.get("add_to_main_network")) and not nm,
            "network_mode": nm,
            "networks": {k: v for k, v in nets.items()},
            "exposed": str(labels.get("traefik.enable", "")).lower() == "true",
        }
    uses = [str(u) for u in (w.get("uses") or []) if isinstance(u, str)]
    out = {"id": app_id, "name": w.get("name") or app_id, "uses": uses, "provides": [str(p) for p in w.get("provides") or []],
           "router": bool(req.get("models") or req.get("services")), "services": services, "custom": None,
           "host_access": host_access(rdoc, app_id, store)}
    taken = sorted({n for s in services.values() for n in s["docker_networks"] if ours(n)})
    if taken:   # a recipe must never attach itself to a link or gateway network: refused at install, never isolated
        out["custom"] = f"its compose attaches to reserved Wizard networks ({', '.join(taken)})"
        out["host_access"].append(f"attaches to reserved Wizard networks: {', '.join(taken)}")
    escapes = [e for e in network_escapes(rdoc, app_id, store) if "network_mode: host " not in e]
    if escapes and not out["custom"]:   # networks it does not own: refused at install/start, never reported isolated
        out["custom"] = "its compose attaches to networks outside the app: " + "; ".join(escapes)[:300]
    ov = override_path(root, store, app_id)
    if ov.is_file():
        text = ov.read_text()
        try:
            odoc = load_yaml(text) or {}
        except yaml.YAMLError as e:
            out["custom"] = f"its user-config compose override does not parse ({str(e)[:80]})"
            return out
        mark = odoc.get(MARK) if isinstance(odoc, dict) else None
        mine = set(mark.get("services") or []) if isinstance(mark, dict) else set()
        for name, s in ((odoc.get("services") or {}) if isinstance(odoc, dict) else {}).items():
            s = _plain(s) or {}
            if name not in services:
                continue
            if _plain(s.get("network_mode")):
                services[name]["network_mode"] = str(_plain(s.get("network_mode")))
            elif "networks" in s and name not in mine and services[name]["on_shared"]:
                out["custom"] = f"its user-config override sets the networks of {name} by hand"
        if text.startswith("# WAM private-provider internal bootstrap"):
            out["custom"] = "a sealed private provider (its own internal networks)"
    return out


def read_apps(runtipi_root: Path, store: str, ids, extra=(), fresh=()) -> list[dict]:
    out = []
    for app_id in sorted(set(ids) | set(extra)):
        if ID.fullmatch(app_id) and (a := read_app(runtipi_root, store, app_id, fresh=app_id in fresh)):
            out.append(a)
    return out


# ---------------------------------------------------------------- the plan (pure: unit-tested without Docker)

def _isolatable(a: dict) -> tuple[str, str]:
    """(state, why) of one app. Only `isolated` apps leave the shared network."""
    svcs = a.get("services") or {}
    shared = [n for n, s in svcs.items() if s.get("on_shared")]
    if a.get("custom"):
        return "custom", a["custom"]
    if any(s.get("network_mode") == "host" for s in svcs.values()):
        return "host-network", "runs on the server's own network (the gateway, or an app the owner allowed)"
    if any(str(s.get("network_mode") or "").startswith("container:") for s in svcs.values()):
        return "custom", "shares another container's network (allowed by the owner); not isolated"
    if any(svcs[n].get("exposed") for n in shared):
        return "exposed", "published through Runtipi's reverse proxy, which needs the shared network"
    if not shared:
        return "no-network", "none of its services take part in app networking"
    return "isolated", ""


def _gname(to: str) -> str:
    return str(to).removeprefix("slot:")


def plan(apps: list[dict], grants: list | None = None, external_slots=None, on: bool = True,
         slot_choice: dict | None = None, multi_slots=None, approved: list | None = None) -> dict:
    """Desired topology. `apps`: read_app() records; `grants`: apps.json grants; `external_slots`: slots filled from
    outside Runtipi (alongside: the host's Hermes), by slot name and by the app id that normally fills it.
    `approved`: the App Manager's approved grants when known (wizard-grants.json). Then a `uses` / `requires`
    declaration links only while its grant is approved: the App Manager approves it at install, and the owner's
    revoke (`wam grants revoke`) takes the network away too. A grant the App Manager approved automatically for a
    declaration (approved: install) never links on its own, so dropping the declaration in an update drops the link;
    an owner's grant (approved: owner) does. Returns {apps, networks, edges, gateway}."""
    by_id = {a["id"]: a for a in apps}
    external_slots = set(external_slots or ())
    for a in apps:
        a["state"], a["why"] = _isolatable(a) if on else ("shared", "isolation is off")
    providers: dict[str, list[str]] = {}
    for a in apps:
        for s in a.get("provides") or []:
            providers.setdefault(s, []).append(a["id"])
    slot_choice, multi_slots = slot_choice or {}, set(multi_slots or ())

    def active(slot: str) -> list[str]:
        """The provider(s) the owner has active for a spot: every provider of a multi spot, else the saved choice."""
        cands = providers.get(slot) or []
        if len(cands) <= 1 or slot in multi_slots:
            return list(cands)
        chosen = slot_choice.get(slot)
        return [chosen if chosen in cands else sorted(cands)[0]]
    # The gateway components are the ACTIVE shell / Agent Service / agent only: an installed but inactive replacement
    # provider is an ordinary app and never joins every app's gateway network.
    core = {p for s in CORE_SLOTS for p in active(s)} | ({LEGACY_BUNDLE} & set(by_id))
    if "hermes-agent" in by_id and not (set(providers.get("agent") or []) - {"hermes-agent"} and "hermes-agent" not in core):
        core.add("hermes-agent")   # the agent app, when no other agent provider is the active one
    core = sorted(core)
    # A core component published through Runtipi's reverse proxy (state `exposed`) still runs in a bridged container:
    # it keeps the shared network for Traefik and also joins wizard-core and every app's wizard-gw-<id>.
    core_bridge = [c for c in core if by_id[c]["state"] in ("isolated", "exposed")]
    gateway = {"core": core, "path": "network" if core_bridge else "loopback",
               "detail": ("the shell, Agent Service and agent run in containers: each app gets a wizard-gw-<id> network "
                          "with them" if core_bridge else
                          "the gateway runs on the server's network and calls apps on their published 127.0.0.1 port")}

    def resolve(to: str) -> tuple[list[str], str]:
        name = to.removeprefix("slot:")
        if not to.startswith("slot:") and name in by_id:
            return [name], ""
        if providers.get(name):
            cands = providers[name]
            if len(cands) == 1 or name in multi_slots:   # a multi slot (e.g. ai-connection) is every provider by design
                return list(cands), ""
            chosen = slot_choice.get(name)                # the owner's active app for this spot (apps.json slots)
            return [chosen if chosen in cands else sorted(cands)[0]], ""
        return [], "external" if name in external_slots else "not-installed"

    edges, seen = [], set()
    ok_grants = None if approved is None else {
        (str(g.get("from")), g.get("to")) for g in approved
        if isinstance(g, dict) and g.get("kind", "app") == "app" and isinstance(g.get("to"), str)}

    def grant_target(to: str) -> str:
        """The grant a declaration needs, named exactly as the App Manager names it (wizard-app-grants.ts targets()):
        an installed app id stays an app grant, anything else is a slot grant. An app grant and a slot grant of the
        same name are different grants."""
        return to if to.startswith("slot:") or to in by_id else f"slot:{to}"

    def add(frm: str, to: str, reason: str) -> None:
        if ok_grants is not None and reason in ("uses", "requires") and (frm, grant_target(to)) not in ok_grants:
            if (frm, to) not in seen:
                seen.add((frm, to))
                edges.append({"from": frm, "to": to, "provider": None, "reason": reason, "state": "not-approved"})
            return
        targets, state = resolve(to)
        if not targets:
            if (frm, to) not in seen:
                seen.add((frm, to))
                edges.append({"from": frm, "to": to, "provider": None, "reason": reason, "state": state})
            return
        for p in targets:
            if p == frm or (frm, p) in seen:
                continue
            seen.add((frm, p))
            edges.append({"from": frm, "to": to, "provider": p, "reason": reason, "state": ""})

    for a in apps:
        for u in a.get("uses") or []:
            add(a["id"], u, "uses")
        if a.get("router"):
            add(a["id"], "slot:connection-router", "requires")
    for g in grants or []:
        if (isinstance(g, dict) and g.get("kind", "app") == "app" and g.get("from") in by_id and isinstance(g.get("to"), str)
                and g.get("approved") != "install"):   # automatic grants follow their declaration (see above)
            add(g["from"], g["to"], "grant")

    nets: dict[str, dict] = {}

    def svc_members(a: dict) -> list[str]:
        return sorted(n for n, s in a["services"].items() if s.get("on_shared") and not s.get("network_mode"))

    def main_of(a: dict) -> str | None:
        mains = [n for n, s in a["services"].items() if s.get("main") and not s.get("network_mode")]
        return mains[0] if len(mains) == 1 else (svc_members(a) or [None])[0]

    def member(net: str, kind: str, app: dict, services: list[str], alias: bool, **labels) -> None:
        n = nets.setdefault(net, {"name": net, "kind": kind, "internal": kind != "app", "members": [],
                                  "labels": {LABEL: kind, **{f"org.wizard-os.{k}": v for k, v in labels.items()}}})
        main = main_of(app)
        for s in services:
            aliases = sorted({app["id"], s}) if alias and s == main else []
            n["members"].append({"app": app["id"], "service": s, "aliases": aliases})

    iso = {a["id"]: a for a in apps if a["state"] == "isolated"}
    # Apps exposed through Runtipi's reverse proxy keep the shared network, but still take part in links (and gateway
    # networks), so an isolated app that uses one, or that one uses, keeps its declared path.
    part = {a["id"]: a for a in apps if a["state"] in ("isolated", "exposed")}
    for a in iso.values():
        member(app_net(a["id"]), "app", a, svc_members(a), False, owner=a["id"])
    for e in edges:
        c, p = part.get(e["from"]), part.get(e["provider"] or "")
        if not e["provider"]:
            continue
        if not c or not p:
            consumer = by_id.get(e["from"]) or {}
            e["state"] = ("shared" if not on else "host" if consumer.get("state") == "host-network" else "unmanaged")
            continue
        if gateway["path"] == "network" and (c["id"] in core or p["id"] in core):
            e["state"], e["network"] = "gateway", (gw_net(p["id"]) if c["id"] in core else gw_net(c["id"]))
            if c["id"] in core and p["id"] in core:
                e["network"] = CORE_NET
            continue
        name = link_name(c["id"], p["id"])
        e["state"], e["network"] = "link", name
        member(name, "link", c, svc_members(c), False, consumer=c["id"], provider=p["id"])
        mp = main_of(p)
        member(name, "link", p, [mp] if mp else [], True, consumer=c["id"], provider=p["id"])
    if gateway["path"] == "network":
        cores = [part[c] for c in core_bridge]
        for c in cores:
            member(CORE_NET, "core", c, svc_members(c), True)
        for a in part.values():
            if a["id"] in core:
                continue
            net = gw_net(a["id"])
            member(net, "gateway", a, svc_members(a), True, owner=a["id"])
            for c in cores:
                member(net, "gateway", c, svc_members(c), True, owner=a["id"])
    # per app, per service: the networks it should be on (Docker names) with aliases
    for a in apps:
        a["want"] = {s: {} for s in svc_members(a)} if a["state"] in ("isolated", "exposed") else {}
    for n in nets.values():
        for m in n["members"]:
            a = by_id[m["app"]]
            if m["service"] in a.get("want", {}):
                cur = a["want"][m["service"]].setdefault(n["name"], [])
                a["want"][m["service"]][n["name"]] = sorted(set(cur) | set(m["aliases"]))
    return {"apps": apps, "networks": nets, "edges": edges, "gateway": gateway, "enabled": on}


def keeps_shared(a: dict, service: str) -> bool:
    """Whether one service of an app stays on Runtipi's shared network: a service published through the reverse proxy
    (Traefik reaches it there), or any shared service of an app wam leaves as it is (custom, host access, isolation
    off). Every other service of an isolated or exposed app is taken off it, service by service."""
    svc = (a.get("services") or {}).get(service) or {}
    if not svc.get("on_shared") or svc.get("network_mode"):
        return False   # host / container / service network modes join no network at all
    if a.get("state") == "isolated":
        return False
    if a.get("state") == "exposed":
        return bool(svc.get("exposed"))
    return True


def overlay_for(a: dict) -> dict:
    """The x-wizard-networks part of one isolated app's override: per service, its full network list (what Runtipi
    generated minus the shared network, plus its wizard networks), and the wizard networks as external."""
    services, top = {}, {}
    # An exposed app keeps the shared network only on its published service(s), for the reverse proxy; its other
    # services leave it like an isolated app's. With nothing to change, there is no override block.
    if a.get("state") == "exposed" and not any((a.get("want") or {}).values()) and all(
            keeps_shared(a, s) for s in (a.get("want") or {})):
        return {"services": {}, "networks": {}}
    for s, want in sorted((a.get("want") or {}).items()):
        nets = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in a["services"][s]["networks"].items()
                if k != SHARED or keeps_shared(a, s)}
        for k in nets:
            nets[k].pop("gw_priority", None)
        for net, aliases in sorted(want.items()):
            cfg = {"aliases": aliases} if aliases else {}
            if net.startswith(APP_PREFIX):
                cfg["gw_priority"] = 1     # the app's own bridge is its way out (link networks are internal)
            nets[net] = cfg
            top[net] = {"name": net, "external": True}
        services[s] = {k: (v or None) for k, v in nets.items()}
    return {"services": services, "networks": top}


# ---------------------------------------------------------------- the override file

def strip_overlay(doc: dict) -> dict:
    """Remove exactly what an earlier write_overlay added (recorded under MARK); keep everything else."""
    mark = doc.pop(MARK, None)
    if not isinstance(mark, dict):
        return doc
    svcs = doc.get("services") if isinstance(doc.get("services"), dict) else {}
    for name in mark.get("services") or []:
        s = svcs.get(name)
        if isinstance(s, dict):
            s.pop("networks", None)
            if not s:
                svcs.pop(name)
    if isinstance(doc.get("services"), dict) and not doc["services"]:
        doc.pop("services")
    nets = doc.get("networks") if isinstance(doc.get("networks"), dict) else {}
    for name in mark.get("networks") or []:
        nets.pop(name, None)
    if isinstance(doc.get("networks"), dict) and not doc["networks"]:
        doc.pop("networks")
    return doc


def write_overlay(path: Path, overlay: dict | None) -> bool:
    """Put (or with None, remove) the managed block in one app's override. Returns True when the file changed."""
    before = path.read_text() if path.is_file() else None
    doc = (load_yaml(before) or {}) if before else {}
    if not isinstance(doc, dict):
        raise NetworkError(f"{path}: expected a compose mapping")
    if overlay is not None and not overlay.get("services"):
        overlay = None
    if not overlay and MARK not in doc:
        return False    # nothing of ours in it: never rewrite another writer's file (alongside, Claude login, owner)
    doc = strip_overlay(doc)
    if overlay and overlay.get("services"):
        svcs = doc.setdefault("services", {})
        for name, nets in overlay["services"].items():
            s = svcs.setdefault(name, {})
            s["networks"] = _Tagged("!override", nets)
        doc.setdefault("networks", {}).update(overlay["networks"])
        doc[MARK] = {"services": sorted(overlay["services"]), "networks": sorted(overlay["networks"])}
    body = dump_yaml(doc) if doc else ""
    head = []   # keep the comment header other writers put on top (alongside, Claude login)
    for line in (before or "").splitlines():
        if not line.startswith("#"):
            break
        if line + "\n" != HEADER:
            head.append(line + "\n")
    text = "".join(head) + (HEADER if MARK in doc else "") + body if doc else ""
    if not doc:
        if before is not None:
            path.unlink()
            return True
        return False
    if text == before:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".wam-tmp")
    tmp.write_text(text)
    if path.is_file():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- Docker

def docker_cmd() -> list[str]:
    from .agent_link import _docker
    return _docker()


def _run(docker: list[str], *args, check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess:
    p = subprocess.run(docker + list(args), capture_output=True, text=True, timeout=timeout)
    if check and p.returncode:
        raise NetworkError(f"docker {' '.join(args[:3])}: {(p.stderr or p.stdout).strip()[:300]}")
    return p


def observe(docker: list[str], store: str = "wizardapps") -> dict:
    """Live state: every Docker network (name -> {labels, subnets, internal}) and every Runtipi app container
    (urn, service, running, networks -> aliases)."""
    p = _run(docker, "network", "ls", "-q")
    ids = p.stdout.split()
    networks = {}
    if ids:
        for n in json.loads(_run(docker, "network", "inspect", *ids).stdout or "[]"):
            networks[n["Name"]] = {"labels": n.get("Labels") or {}, "internal": bool(n.get("Internal")),
                                   "subnets": [c.get("Subnet") for c in (n.get("IPAM") or {}).get("Config") or [] if c.get("Subnet")],
                                   "containers": sorted((v or {}).get("Name", k) for k, v in (n.get("Containers") or {}).items())}
    p = _run(docker, "ps", "-aq", "--filter", "label=runtipi.appurn")
    containers = []
    if p.stdout.split():
        for c in json.loads(_run(docker, "inspect", *p.stdout.split()).stdout or "[]"):
            labels = (c.get("Config") or {}).get("Labels") or {}
            nets = ((c.get("NetworkSettings") or {}).get("Networks") or {})
            containers.append({
                "id": c["Id"][:12], "name": c.get("Name", "").lstrip("/"), "urn": labels.get("runtipi.appurn", ""),
                "service": labels.get("com.docker.compose.service", ""), "running": bool((c.get("State") or {}).get("Running")),
                "network_mode": (c.get("HostConfig") or {}).get("NetworkMode", ""),
                "ports": sorted({(b.get("HostIp") or "127.0.0.1", int(b["HostPort"]))
                                 for bs in ((c.get("NetworkSettings") or {}).get("Ports") or {}).values() or []
                                 for b in bs or [] if str(b.get("HostPort", "")).isdigit()}),
                "networks": {k: sorted(set((v or {}).get("Aliases") or []) | set((v or {}).get("DNSNames") or []))
                             for k, v in nets.items()}})
    return {"networks": networks, "containers": containers}


def ports_answer(ports, timeout: float = 3.0, tries: int = 5) -> bool:
    """Every published TCP port still accepts a connection (checked from the host, where the gateway calls)."""
    import socket
    for host, port in ports or []:
        host = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        for i in range(tries):
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    break
            except OSError:
                if i == tries - 1:
                    return False
                time.sleep(1)
    return True


def _free_subnet(used: list[str], pool_net: ipaddress.IPv4Network, taken: set) -> str:
    busy = []
    for s in list(used) + list(taken):
        try:
            busy.append(ipaddress.ip_network(s, strict=False))
        except ValueError:
            continue
    for cand in pool_net.subnets(new_prefix=PREFIXLEN):
        if not any(cand.overlaps(b) for b in busy):
            return str(cand)
    raise NetworkError(f"no free /{PREFIXLEN} left in {pool_net} for app networks (set network_pool in app-manager.json)")


def _bridged(container: dict) -> bool:
    """A container that can join networks (not host / none / another container's or service's namespace)."""
    nm = str(container.get("network_mode") or "")
    return nm not in ("host", "none") and not nm.startswith(("container:", "service:"))


def network_matches(planned: dict, live: dict | None) -> bool:
    """A reserved name is not identity: match the same ownership label and shape as reconciliation."""
    return bool(live and live.get("internal") == planned["internal"] and
                (live.get("labels") or {}).get(LABEL) == planned["kind"] and
                all((live.get("labels") or {}).get(k) == v for k, v in planned["labels"].items()))


def drift(p: dict, live: dict, store: str = "wizardapps") -> list[dict]:
    """Differences between the plan and running containers: one row per container that is off-plan."""
    rows = []
    shared_docker = os.environ.get("RUNTIPI_DOCKER_NETWORK") or SHARED_DOCKER
    for a in p["apps"]:
        urn = f"{a['id']}:{store}"
        for c in live["containers"]:
            if c["urn"] != urn or not c["running"]:
                continue
            have = {k for k in c["networks"] if ours(k) and
                    (k not in p["networks"] or k not in live["networks"] or network_matches(p["networks"][k], live["networks"][k]))}
            want = set((a.get("want") or {}).get(c["service"]) or {})
            on_shared = shared_docker in c["networks"]
            row = {"app": a["id"], "service": c["service"], "container": c["name"], "missing": sorted(want - have),
                   "extra": sorted(have - want), "on_shared": on_shared,
                   "should_share": keeps_shared(a, c["service"]) and _bridged(c)}
            bad_alias = [n for n, al in ((a.get("want") or {}).get(c["service"]) or {}).items()
                         if n in c["networks"] and not set(al) <= set(c["networks"][n])]
            row["alias_missing"] = bad_alias
            # the app's own compose networks (its private network, recipe networks) must never be lost
            nm = str(c.get("network_mode") or "")
            bridged = nm not in ("host", "none") and not nm.startswith(("container:", "service:"))
            row["lost"] = sorted(n for n in a["services"].get(c["service"], {}).get("docker_networks") or []
                                 if n not in c["networks"]) if bridged else []
            needs = bool(row["missing"] or row["extra"] or bad_alias or row["lost"] or
                         (row["should_share"] and not on_shared) or   # e.g. isolation turned off, an exposed service
                         (a["state"] in ("isolated", "exposed") and c["service"] in (a.get("want") or {}) and on_shared
                          and not keeps_shared(a, c["service"])))
            if needs:
                row["recreate"] = bool(row["lost"]) or (a["state"] == "isolated" and (on_shared or app_net(a["id"]) in row["missing"]))
                rows.append(row)
    return rows


# ---------------------------------------------------------------- the whole loop

def approved_grants(c, rt) -> list | None:
    """The App Manager's approved grants (wizard-grants.json, else its API), or None when neither can be read."""
    from . import registry
    got = registry.read_grants_file(Path(c.runtipi_root) / "state" / "wizard-grants.json")
    if got is not None:
        return got
    try:
        got = rt.req("GET", "/apps/wizard/grants")
        return got if isinstance(got, list) else None
    except Exception:  # noqa: BLE001 - an older App Manager: declarations alone
        return None


def _context(c, rt=None, extra=()):
    """(installed ids, grants, external slots, mode) for the planner, from the App Manager and wam's own files."""
    from . import alongside
    from .cli import STORE, installed_ids, read_grants
    rt = rt or c.runtipi()
    ids = installed_ids(rt)
    approved = grants = approved_grants(c, rt)
    if grants is None:
        grants = read_grants(c, rt)
    from . import slots
    ext = set(alongside.external_slots(c.home))
    defaults = slots.load_defaults()
    ext |= {(defaults.get(s_) or {}).get("app") for s_ in list(ext)} - {None}
    return rt, ids | set(extra), grants, ext, STORE, approved


def published_urns(rt) -> set[str]:
    """Unreadable or malformed lifecycle state authorizes no publication."""
    try:
        rows = rt.installed()
    except Exception:  # noqa: BLE001 - an unavailable owner record is not permission
        return set()
    if not isinstance(rows, (list, tuple)):
        return set()
    return {r["info"]["urn"] for r in rows if isinstance(r, dict) and isinstance(r.get("info"), dict)
            and isinstance(r["info"].get("urn"), str) and isinstance(r.get("app"), dict)
            and (r["app"].get("exposed") is True or r["app"].get("exposedLocal") is True)}


def build(c, rt=None, extra=(), on: bool | None = None, fresh=()) -> tuple[dict, str]:
    rt, ids, grants, ext, store, approved = _context(c, rt, extra)
    apps = read_apps(c.runtipi_root, store, ids, fresh=fresh)
    # Generated labels may predate the start gate or come from a malicious recipe. Only the App Manager's
    # owner-configured lifecycle state can publish an app; do not let the watcher rejoin it from labels alone.
    published = published_urns(rt)
    for a in apps:
        for service in a["services"].values():
            service["exposed"] = f"{a['id']}:{store}" in published and service.get("main", False) and not service.get("network_mode")
    from . import slots as _slots
    try:
        chosen = {k: v.get("default") for k, v in (json.loads((c.home / "apps.json").read_text()).get("slots") or {}).items()
                  if isinstance(v, dict)}
    except (OSError, ValueError):
        chosen = {}
    multi = {k for k, v in _slots.load_defaults().items() if (v or {}).get("multi")}
    return plan(apps, grants, ext, on=enabled(c.home) if on is None else on, slot_choice=chosen, multi_slots=multi,
                approved=approved), store


def reconcile(c, rt=None, apply: bool = True, restart: bool = True, extra=(), restart_fn=None, on: bool | None = None,
              fresh=()) -> dict:
    """Make Docker and the overrides match the declarations. Idempotent; safe to run any time.
    apply=False: report only. restart=False: never restart an app (live connect/disconnect only; apps still on
    the shared network are listed under pending_restart). `extra`: app ids about to be installed (their store
    recipe is planned so they come up isolated on their first start). Runs one at a time per box (the CLI, the
    `wam serve` watcher and the installer can all call it)."""
    if not apply:
        return _reconcile(c, rt, apply, restart, extra, restart_fn, on, fresh)
    import fcntl
    c.home.mkdir(parents=True, exist_ok=True)
    with open(c.home / "networks.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            return _reconcile(c, rt, apply, restart, extra, restart_fn, on, fresh)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _reconcile(c, rt, apply, restart, extra, restart_fn, on, fresh) -> dict:
    p, store = build(c, rt, extra, on=on, fresh=fresh) if (on is not None or fresh) else build(c, rt, extra)
    policy_error = None
    if apply:
        try:   # first: the App Manager enforces the same switch at every start, whoever starts the app
            write_policy(c, p["enabled"])
        except OSError as e:
            policy_error = f"{policy_path(c.runtipi_root)}: not written ({str(e)[:120]})"
    try:
        docker = docker_cmd()
    except (NetworkError, OSError, subprocess.SubprocessError) as e:
        return {"enabled": p["enabled"], "errors": [f"Docker unavailable: {str(e)[:160]}"] + ([policy_error] if policy_error else []),
                "unavailable": True}
    live = observe(docker, store)
    out = {"enabled": p["enabled"], "gateway": p["gateway"], "created": [], "removed": [], "overrides": [],
           "connected": [], "disconnected": [], "restarted": [], "pending_restart": [], "errors": []}
    if policy_error:
        out["errors"].append(policy_error)
    if not apply:
        out["drift"] = drift(p, live, store)
        out["plan"] = summary(p)
        return out
    # 1. networks the plan needs (with labels; subnets from our pool, never overlapping any existing network)
    used = [s for n in live["networks"].values() for s in n["subnets"]]
    the_pool, taken, rejected = pool(c.home), set(), set()
    for name, n in sorted(p["networks"].items()):
        have = live["networks"].get(name)
        if network_matches(n, have):
            continue
        if have:   # same name, wrong shape (or not ours): replace only if it is ours and empty
            if have["labels"].get(LABEL) and not have["containers"]:
                _run(docker, "network", "rm", name, check=False)
            else:
                out["errors"].append(f"{name}: a different network with this name exists; not touched")
                rejected.add(name)
                continue
        sub = _free_subnet(used, the_pool, taken)
        taken.add(sub)
        args = ["network", "create", "--driver", "bridge", "--subnet", sub]
        if n["internal"]:
            args.append("--internal")
        for k, v in sorted(n["labels"].items()):
            args += ["--label", f"{k}={v}"]
        _run(docker, *args, name)
        out["created"].append(name)
    # A planned name taken by a network we didn't make (or can't replace) is never joined: no override entry, no live
    # connect, and its members' declared connection stays down until the name is free (the error says which).
    for name in rejected:
        p["networks"].pop(name, None)
        for a in p["apps"]:
            for want in (a.get("want") or {}).values():
                want.pop(name, None)
        for e in p["edges"]:
            if e.get("network") == name:
                e["state"] = "collision"
    # 2. overrides: managed block for isolated apps, removed from every other app (and from uninstalled apps)
    planned = {a["id"]: a for a in p["apps"]}
    base = Path(c.runtipi_root) / "user-config" / store
    for a in p["apps"]:
        f = override_path(c.runtipi_root, store, a["id"])
        if a.get("custom") and not (f.is_file() and MARK in f.read_text()):
            continue   # custom and nothing of ours in its override: never touch it
        try:
            # custom apps (host access the owner allowed, hand-set networks, ...): only wam's own managed block is
            # removed, so a stale !override from when the app was isolated never fights its new settings
            if write_overlay(f, overlay_for(a) if a["state"] in ("isolated", "exposed") and not a.get("custom") else None):
                out["overrides"].append(a["id"])
        except (OSError, yaml.YAMLError, NetworkError) as e:
            out["errors"].append(f"{a['id']}: override not written ({str(e)[:160]})")
    if base.is_dir():
        for d in sorted(base.iterdir()):
            f = d / "docker-compose.yml"
            if d.name not in planned and f.is_file() and MARK in f.read_text():
                if write_overlay(f, None):
                    out["overrides"].append(d.name)
    # 3. running containers, live (no restart): join the planned networks first, then leave the ones no longer
    #    declared and, for isolated apps, the shared network. Docker moves the default route and keeps published
    #    ports; each published port is checked afterwards and the app is restarted only if one stopped answering.
    shared_docker = os.environ.get("RUNTIPI_DOCKER_NETWORK") or SHARED_DOCKER
    recreate = set()
    ports = {c_["name"]: c_.get("ports") or [] for c_ in live["containers"]}

    def connect(container: str, name: str, aliases) -> bool:
        args = ["network", "connect"]
        for al in aliases or []:
            args += ["--alias", al]
        # The app's own bridge must stay its default route once the shared network is gone (internal link and
        # gateway networks have no way out); Docker >= 28 takes the priority on connect, older Docker ignores it.
        prio = ["--gw-priority", "1"] if name.startswith(APP_PREFIX) or name == shared_docker else []
        r = _run(docker, *args, *prio, name, container, check=False)
        if r.returncode and prio and "gw-priority" in (r.stderr or ""):
            r = _run(docker, *args, name, container, check=False)
        if r.returncode and "already exists" not in (r.stderr or ""):
            out["errors"].append(f"connect {container} to {name}: {(r.stderr or '').strip()[:160]}")
            return False
        out["connected"].append(f"{container} -> {name}")
        return True

    def disconnect(container: str, name: str) -> bool:
        r = _run(docker, "network", "disconnect", "-f", name, container, check=False)
        if r.returncode:
            out["errors"].append(f"disconnect {container} from {name}: {(r.stderr or '').strip()[:160]}")
            return False
        out["disconnected"].append(f"{container} -x {name}")
        return True

    for row in drift(p, live, store):
        a = planned[row["app"]]
        want = (a.get("want") or {}).get(row["service"]) or {}
        ok = True
        for name in row["alias_missing"]:
            disconnect(row["container"], name)
        for name in row["missing"] + row["alias_missing"]:
            ok &= connect(row["container"], name, want.get(name))
        if row["should_share"] and not row["on_shared"]:
            ok &= connect(row["container"], shared_docker, [row["service"]])
        if not ok or row["lost"]:
            recreate.add(row["app"])   # a compose network the app needs is gone: Runtipi rebuilds it from the compose
            continue
        for name in row["extra"]:
            disconnect(row["container"], name)
        if (a["state"] in ("isolated", "exposed") and row["on_shared"] and row["service"] in (a.get("want") or {})
                and not keeps_shared(a, row["service"])):
            if disconnect(row["container"], shared_docker) and not ports_answer(ports.get(row["container"])):
                recreate.add(row["app"])
    # 4. recreate (restart through the App Manager, the path the UI uses) the apps that must leave or rejoin the
    #    shared network: Runtipi rebuilds them from the generated compose plus the override
    for app_id in sorted(recreate):
        if not restart or restart_fn is None:
            out["pending_restart"].append(app_id)
            continue
        try:
            res = restart_fn(app_id)
            if isinstance(res, dict) and res.get("error"):
                out["errors"].append(f"restart {app_id}: {res['error']}")
            else:
                out["restarted"].append(app_id)
        except (Exception, SystemExit) as e:  # noqa: BLE001 - reported per app
            out["errors"].append(f"restart {app_id}: {str(e)[:160]}")
    # 5. our networks nobody needs any more (an uninstalled app, a dropped declaration)
    live = observe(docker, store)
    for name, n in sorted(live["networks"].items()):
        if n["labels"].get(LABEL) and ours(name) and name not in p["networks"] and name not in rejected:
            for cname in n["containers"]:
                _run(docker, "network", "disconnect", "-f", name, cname, check=False)
            r = _run(docker, "network", "rm", name, check=False)
            if r.returncode:
                out["errors"].append(f"remove {name}: {(r.stderr or '').strip()[:160]}")
            else:
                out["removed"].append(name)
    out["drift"] = drift(p, live, store)
    out["plan"] = summary(p)
    _save(c.home, out)
    return out


def started_apps(docker: list[str], store: str = "wizardapps"):
    """Yield the id of every Wizard app whose container starts, as Docker reports it (blocks; ends with Docker)."""
    fmt = '{{index .Actor.Attributes "runtipi.appurn"}}'
    p = subprocess.Popen(docker + ["events", "--filter", "type=container", "--filter", "event=start",
                                   "--filter", "label=runtipi.appurn", "--format", fmt],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        for line in p.stdout or []:
            app_id, _, st = line.strip().partition(":")
            if st == store and ID.fullmatch(app_id):
                yield app_id
    finally:
        p.kill()


def watch(step, docker_fn=None, debounce: float = 3.0, idle: float = 600.0, stop=None, events=None) -> None:
    """`wam serve`: every start of a Wizard app, whoever caused it (the App Store views, Runtipi's own UI, a restart,
    an update, a reboot), is followed by a reconcile, so its declared links and gateway network appear and stale ones
    go. The App Manager already started it without the shared network (its start-time policy); this adds what the
    declarations allow. Between events it reconciles every `idle` seconds without restarting anything (grants approved
    in the UI). `step(restart: bool)` runs one reconcile; `events` (tests) replaces Docker's event stream."""
    import queue
    import threading
    q: queue.Queue = queue.Queue()

    def reader():
        while not (stop and stop.is_set()):
            try:
                for app_id in (events if events is not None else started_apps((docker_fn or docker_cmd)())):
                    q.put(app_id)
                if events is not None:
                    return
            except Exception:  # noqa: BLE001 - Docker restarting: try again
                pass
            time.sleep(10)

    threading.Thread(target=reader, name="wam-network-events", daemon=True).start()
    while not (stop and stop.is_set()):
        try:
            first = q.get(timeout=idle)
        except queue.Empty:
            step(False)
            continue
        if first is None:
            return
        deadline = time.time() + debounce   # one reconcile for a burst (an app's services start one by one)
        while time.time() < deadline:
            try:
                if q.get(timeout=max(0.05, deadline - time.time())) is None:
                    step(True)
                    return
            except queue.Empty:
                break
        step(True)


def _save(home: Path, res: dict) -> None:
    try:
        keep = {k: res.get(k) for k in ("enabled", "created", "removed", "overrides", "restarted", "pending_restart", "errors")}
        (home / "networks.json").write_text(json.dumps({"at": int(time.time()), **keep}, indent=2) + "\n")
    except OSError:
        pass


def summary(p: dict) -> dict:
    return {"networks": {n: sorted({f"{m['app']}/{m['service']}" for m in v["members"]}) for n, v in sorted(p["networks"].items())},
            "apps": {a["id"]: a["state"] for a in p["apps"]}}


def disable(c, restart_fn=None) -> dict:
    """Rollback: isolation off. Every managed override block is removed, apps go back to the shared network
    (restart), and wam's networks are deleted. `wam network on` (or a reconcile with isolation on) redoes it."""
    set_enabled(c.home, False)
    return reconcile(c, restart_fn=restart_fn)


# ---------------------------------------------------------------- the connections report (CLI, apps.json, UI)

def report(c, rt=None, p: dict | None = None, live: dict | None = None) -> dict:
    """Per app: what it can reach (and why), who reaches it, how the gateway reaches it, internet access, and the
    live check (actual Docker membership vs the declarations)."""
    from .cli import STORE
    if p is None:
        p, _ = build(c, rt)
    try:
        live = live if live is not None else observe(docker_cmd(), STORE)
        rows, live_ok = drift(p, live, STORE), True
    except (OSError, NetworkError, subprocess.SubprocessError, ValueError) as e:
        rows, live_ok, live = [], False, {"networks": {}, "containers": []}
        err = str(e)[:160]
    else:
        err = ""
    by_app: dict[str, list] = {}
    for r in rows:
        by_app.setdefault(r["app"], []).append(r)
    apps = []
    invalid_networks = {name for name, planned in p["networks"].items()
                        if name in live["networks"] and not network_matches(planned, live["networks"][name])}
    shared_docker = os.environ.get("RUNTIPI_DOCKER_NETWORK") or SHARED_DOCKER
    for a in p["apps"]:
        out_edges = [e for e in p["edges"] if e["from"] == a["id"]]
        in_edges = [e for e in p["edges"] if e.get("provider") == a["id"]]
        problems = [f"{name}: a different network with this name exists (wrong ownership or internal flag)"
                    for name in sorted(invalid_networks)
                    if any(m["app"] == a["id"] for m in p["networks"][name]["members"])]
        for r in by_app.get(a["id"], []):
            if r["on_shared"] and not r["should_share"] and a["state"] in ("isolated", "exposed"):
                problems.append(f"{r['service']} is still on the shared network (every app can reach it)")
            for n in r["missing"]:
                problems.append(f"{r['service']} is not on {n}")
            for n in r["extra"]:
                problems.append(f"{r['service']} is on {n}, which nothing declares")
            for n in r.get("alias_missing") or []:
                problems.append(f"{r['service']} answers on {n} without its name")
            for n in r.get("lost") or []:
                problems.append(f"{r['service']} lost its own network {n}")
        containers = [x for x in live["containers"] if x["urn"] == f"{a['id']}:{STORE}"]
        running = any(x["running"] for x in containers)
        if a["state"] == "isolated":
            internet = True                       # the app's own bridge (wizard-app-<id>) is not internal
        elif a["state"] in ("host-network",):
            internet = True
        else:
            internet = None if not containers else any(
                n in live["networks"] and not live["networks"][n]["internal"] for x in containers for n in x["networks"])
        if a["state"] == "host-network":
            gw = "is on the server's network"
        elif p["gateway"]["path"] == "network" and a["state"] in ("isolated", "exposed") and a["id"] not in p["gateway"]["core"]:
            gw = f"through {gw_net(a['id'])}"
        elif a["state"] == "isolated" or a["state"] == "shared":
            gw = "on its published 127.0.0.1 port" if p["gateway"]["path"] == "loopback" else "over the shared network"
        else:
            gw = "on its published 127.0.0.1 port"
        apps.append({
            "id": a["id"], "name": a.get("name") or a["id"], "state": a["state"], "why": a.get("why") or "",
            "running": running, "internet": internet, "gateway": gw, "host_access": a.get("host_access") or [],
            "can_reach": [{"to": e["provider"] or e["to"], "declared": e["to"], "reason": e["reason"], "state": e["state"],
                           "network": e.get("network")} for e in out_edges] +
                         # a link is one Docker network shared by exactly two apps: the provider can open connections
                         # back to the consumer too (Docker networks are two-way); shown so the view never understates it
                         [{"to": e["from"], "declared": None, "reason": "two-way link", "state": "peer", "network": e.get("network")}
                          for e in in_edges if e["state"] == "link"] +
                         # fresh boxes: an app shares its wizard-gw-<id> with the shell, Agent Service and agent, so it
                         # can open connections to them too (the gateway path, not another app)
                         [{"to": core, "declared": None, "reason": "gateway network", "state": "core", "network": gw_net(a["id"])}
                          for core in p["gateway"]["core"] if gw_net(a["id"]) in p["networks"] and core != a["id"]
                          and any(m["app"] == core for m in p["networks"][gw_net(a["id"])]["members"])
                          and not any(e["provider"] == core for e in out_edges)] +
                         # and the other way: a core component reaches every app whose gateway network it is on
                         [{"to": n["name"].removeprefix(GW_PREFIX), "declared": None, "reason": "gateway network",
                           "state": "gateway", "network": n["name"]}
                          for n in p["networks"].values() if n["kind"] == "gateway" and a["id"] in p["gateway"]["core"]
                          and any(m["app"] == a["id"] for m in n["members"])],
            "reached_by": [{"from": e["from"], "reason": e["reason"], "state": e["state"], "network": e.get("network")}
                           for e in in_edges] +
                          # fresh boxes: the shell, Agent Service and agent share this app's wizard-gw-<id> and can open
                          # connections to it (the gateway path)
                          [{"from": core, "reason": "gateway network", "state": "core", "network": gw_net(a["id"])}
                           for core in p["gateway"]["core"] if gw_net(a["id"]) in p["networks"] and core != a["id"]
                           and any(m["app"] == core for m in p["networks"][gw_net(a["id"])]["members"])
                           and not any(e["from"] == core for e in in_edges)] +
                          ([{"from": n["name"].removeprefix(GW_PREFIX), "reason": "gateway network", "state": "gateway",
                             "network": n["name"]}
                            for n in p["networks"].values() if n["kind"] == "gateway" and a["id"] in p["gateway"]["core"]
                            and any(m["app"] == a["id"] for m in n["members"])]),
            "drift": problems,
        })
    # Apps that keep Runtipi's shared network (isolation off, a published service, custom / host-access apps) can
    # all open connections to each other there, declared or not: list those peers both ways, so the report never
    # understates what an app can actually reach.
    sharing = sorted(a["id"] for a in p["apps"] if any(keeps_shared(a, s) for s in (a.get("services") or {})))
    for row in apps:
        if row["id"] in sharing:
            peers = [x for x in sharing if x != row["id"]]
            row["can_reach"] += [{"to": x, "declared": None, "reason": "shared network", "state": "shared-network",
                                  "network": shared_docker} for x in peers]
            row["reached_by"] += [{"from": x, "reason": "shared network", "state": "shared-network",
                                   "network": shared_docker} for x in peers]
    shared_members = sorted(n for n in (live["networks"].get(shared_docker) or {}).get("containers", []))
    res = {"schema": 1, "enabled": p["enabled"], "checked_at": int(time.time()), "live": live_ok,
           "gateway": p["gateway"], "apps": apps,
           "networks": [{"name": n, "kind": v["kind"], "internal": v["internal"],
                         "members": sorted({f"{m['app']}/{m['service']}" for m in v["members"]}),
                         "exists": n in live["networks"], "matches": network_matches(v, live["networks"].get(n))}
                        for n, v in sorted(p["networks"].items())],
           "shared_network": {"name": shared_docker, "containers": shared_members},
           "ok": live_ok and not invalid_networks and not any(x["drift"] for x in apps)
                 and all(network_matches(v, live["networks"].get(n)) for n, v in p["networks"].items())}
    if not live_ok:
        res["error"] = f"could not read Docker ({err})"
    return res


def describe(rep: dict) -> str:
    """`wam connections`: one block per app, plain words."""
    lines = [f"App isolation: {'on' if rep['enabled'] else 'OFF (every app can reach every app)'}; "
             f"gateway: {rep['gateway']['detail']}",
             f"Live check: {'matches the declarations' if rep['ok'] else 'DRIFT (see below)' if rep['live'] else rep.get('error')}", ""]
    word = {"link": "", "gateway": " (gateway network)", "external": " (outside Runtipi: not reachable from apps)",
            "host": " (from the server's network, on its published 127.0.0.1 port)",
            "peer": " (two-way: it uses this app over their link)",
            "core": " (Wizard core, over this app's gateway network)",
            "not-installed": " (not installed)", "unmanaged": " (not isolated: shared network)", "shared": " (shared network)",
            "not-approved": " (declared, but its grant is revoked or not approved yet: no network)",
            "collision": " (its network name is taken by another network: not connected, see `wam network reconcile`)",
            "shared-network": " (shared network: not declared, but reachable)"}
    for a in rep["apps"]:
        reach = [f"{e['to']}{word.get(e['state'], '')}" for e in a["can_reach"]]
        lines.append(f"{a['id']}  [{a['state']}{'' if a['running'] else ', stopped'}]")
        if a["why"]:
            lines.append(f"  note: {a['why']}")
        lines.append(f"  can reach: {', '.join(reach) or 'no other app'}")
        by = [b["from"] + (" (shared network)" if b["state"] == "shared-network" else "")
              for b in a["reached_by"] if b["state"] in ("link", "gateway", "core", "shared-network")]
        lines.append(f"  reached by: {', '.join(by) or 'no other app'}; agent gateway {a['gateway']}")
        lines.append(f"  internet: {'yes' if a['internet'] else 'no' if a['internet'] is False else 'unknown'}")
        for h in a["host_access"]:
            lines.append(f"  HOST ACCESS: {h}")
        for d in a["drift"]:
            lines.append(f"  DRIFT: {d}")
    return "\n".join(lines)
