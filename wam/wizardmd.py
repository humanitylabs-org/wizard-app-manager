"""wizard.md reader and checker.

Vendored from humanitylabs-org/wizard-os tools/check_wizard_md.py (the spec owner), plus body/passed/failures.
Keep the rules identical to the spec and to the index (wizard-app-index src/checker.js).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

TYPES = {"agent", "service", "view", "external"}
CATEGORIES = {"agent", "interface", "app", "system"}
BAD_CAPS = {"ALL", "SYS_ADMIN", "SYS_MODULE", "SYS_PTRACE", "SYS_RAWIO", "NET_ADMIN", "DAC_READ_SEARCH", "BPF", "PERFMON"}
HOST_PATH = re.compile(r"^/(etc|root|boot|proc|sys|dev|var/run|run|home|usr|lib|bin|sbin)(/|$)")
NEEDS = {"mic", "system-audio", "camera", "notifications", "files"}
DEFAULTS_FILE = Path(__file__).with_name("defaults.json")
SPECS = {0.1, 0.2}
SLOT = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
NPUB = re.compile(r"npub1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")


def front_matter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("no YAML front matter")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError("front matter is not a mapping")
    return data


def compose_risks(text: str) -> list[str]:
    """Host-level access a public, unreviewed app must never ask for (rule 12)."""
    try:
        doc = yaml.safe_load(text) or {}
    except Exception as e:  # noqa: BLE001
        return [f"compose does not parse: {e}"]
    risks = []
    for name, s in ((doc.get("services") or {}) if isinstance(doc, dict) else {}).items():
        if not isinstance(s, dict):
            continue
        if s.get("privileged"):
            risks.append(f"{name}: privileged")
        for k in ("network_mode", "pid", "ipc", "userns_mode", "uts"):
            if str(s.get(k, "")).lower() == "host":
                risks.append(f"{name}: {k}: host")
        for c in s.get("cap_add") or []:
            if str(c).upper().removeprefix("CAP_") in BAD_CAPS:
                risks.append(f"{name}: cap_add {c}")
        if s.get("devices"):
            risks.append(f"{name}: host devices")
        for o in s.get("security_opt") or []:
            if "unconfined" in str(o).lower():
                risks.append(f"{name}: security_opt {o}")
        for v in s.get("volumes") or []:
            src = v.split(":")[0] if isinstance(v, str) else str((v or {}).get("source", ""))
            if any(x in src for x in ("docker.sock", "containerd.sock", "podman.sock")):
                risks.append(f"{name}: mounts the container runtime socket")
            elif src == "/" or HOST_PATH.match(src):
                risks.append(f"{name}: mounts host path {src}")
    return risks


def known_dependencies() -> set[str]:
    """Offline vocabulary, not installed availability or permission to install.

    Read source defaults, not a box's selected release: a pinned box must still
    be able to check a newer app. Null placeholders are not known providers.
    Only explicit wizard.md capability snapshots count, never slot aliases or
    the candidate's own declarations.
    """
    known = set(NEEDS)
    for app in json.loads(DEFAULTS_FILE.read_text())["slots"].values():
        if app.get("app"):
            known.add(app["app"])
            known.update(app.get("provides") or [])
    return known


def check(repo: Path, github: str | None = None) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def rule(name: str, ok: bool, detail: str = "") -> None:
        out.append((name, bool(ok), detail))

    f = repo / "wizard.md"
    if not f.is_file():
        rule("wizard.md present", False, str(f))
        return out
    rule("wizard.md present", True)
    try:
        d = front_matter(f.read_text())
    except Exception as e:  # noqa: BLE001 - report as a failed rule
        rule("front matter parses", False, str(e))
        return out
    rule("front matter parses", True)
    rule("spec version known", d.get("wizard") in SPECS, str(d.get("wizard")))
    rule("id valid", bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}", str(d.get("id", "")))), str(d.get("id")))
    t = d.get("type")
    rule("type valid", t in TYPES, str(t))
    rule("version valid", bool(re.fullmatch(r"\d+\.\d+\.\d+([-+.][\w.]+)?", str(d.get("version", "")))), str(d.get("version")))
    s = str(d.get("summary", ""))
    rule("summary 1-140 chars", 0 < len(s) <= 140, f"{len(s)} chars")
    rule("license and source", bool(d.get("license")) and bool(d.get("source")))
    rt = d.get("runtime") or {}
    for key in ("config", "compose"):
        p = rt.get(key)
        rule(f"runtime.{key} exists", bool(p) and (repo / p).is_file(), str(p))
    cfg_path = repo / str(rt.get("config") or "")
    if rt.get("config") and cfg_path.is_file():
        import time as _t
        try:
            cfg = json.loads(cfg_path.read_text())
            now_ms = int(_t.time() * 1000)
            bad_ts = [k for k in ("created_at", "updated_at") if isinstance(cfg.get(k), (int, float)) and cfg[k] > now_ms]
            rule("runtipi config id matches", cfg.get("id") == d.get("id"), f"{cfg.get('id')} vs {d.get('id')}")
            rule("runtipi timestamps not in future", not bad_ts, ", ".join(bad_ts))
        except Exception as e:  # noqa: BLE001
            rule("runtipi config parses", False, str(e))
    if t in {"agent", "service", "external"}:
        m = d.get("mcp") or {}
        rule("mcp declared", bool(m.get("path")) and bool(m.get("port")), json.dumps(m))
    if t != "view":
        sk = d.get("skill")
        ok, detail = False, str(sk)
        if sk and (repo / sk).is_file():
            try:
                sfm = front_matter((repo / sk).read_text())
                ok = bool(sfm.get("name")) and bool(sfm.get("description"))
                detail = sfm.get("name", "")
            except Exception as e:  # noqa: BLE001
                detail = str(e)
        rule("skill ships with name+description", ok, detail)
    if t == "view":
        v = d.get("view") or {}
        rule("view declared", bool(v.get("path")) and bool(v.get("port")), json.dumps(v))
    known = known_dependencies()
    bad = [n for n in d.get("needs") or [] if n not in known and not str(n).startswith("credentials:")]
    rule("needs known", not bad, ", ".join(map(str, bad)))
    if d.get("provides") is not None:
        pv = d.get("provides")
        badp = [p for p in (pv if isinstance(pv, list) else [pv]) if not SLOT.fullmatch(str(p))]
        rule("provides are slot ids", isinstance(pv, list) and not badp, ", ".join(map(str, badp)))
    if d.get("category") is not None:
        rule("category is agent, interface, app or system", d.get("category") in CATEGORIES, str(d.get("category")))
    if d.get("icon") is not None:
        ic = repo / str(d.get("icon"))
        rule("icon exists (png/jpg)", ic.is_file() and ic.suffix.lower() in {".png", ".jpg", ".jpeg"}, str(d.get("icon")))
    comp = repo / str(rt.get("compose") or "")
    if rt.get("compose") and comp.is_file():
        risks = compose_risks(comp.read_text())
        rule("container asks for no host-level access", not risks, "; ".join(risks))
    if d.get("publisher") is not None:
        rule("publisher is an npub", bool(NPUB.fullmatch(str(d.get("publisher")))), str(d.get("publisher")))
    if github:
        r = subprocess.run(["gh", "api", f"repos/{github}/topics", "--jq", ".names"], capture_output=True, text=True)
        topics = set(re.findall(r'"([^"]+)"', r.stdout))
        rule("GitHub topic wizard-app (or wizard-ai during the transition)", bool(topics & {"wizard-app", "wizard-ai"}), r.stdout.strip() or r.stderr.strip())
    return out


def body(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.S)
    return (m.group(1) if m else text).strip()


def passed(results: list[tuple[str, bool, str]]) -> bool:
    return bool(results) and all(ok for _, ok, _ in results)


def failures(results: list[tuple[str, bool, str]]) -> list[str]:
    return [n for n, ok, _ in results if not ok]


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    gh = args[args.index("--github") + 1] if "--github" in args else None
    results = check(Path(args[0]), gh)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    passed = all(ok for _, ok, _ in results)
    print("COMPATIBLE" if passed else "NOT COMPATIBLE")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
