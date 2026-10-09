"""wizard.md reader and checker.

Vendored from humanitylabs-org/wizard-os tools/check_wizard_md.py (the spec owner).
Keep the rules identical; change them in wizard-os first.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

TYPES = {"agent", "service", "view", "external"}
NEEDS = {"mic", "system-audio", "camera", "notifications", "files"}
SPECS = {0.1, 0.2}
SLOT = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
ID = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")


def front_matter(text: str) -> dict:
    import yaml

    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise ValueError("no YAML front matter")
    data = yaml.safe_load(m.group(1))
    if not isinstance(data, dict):
        raise ValueError("front matter is not a mapping")
    return data


def body(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.S)
    return (m.group(1) if m else text).strip()


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
    except Exception as e:  # noqa: BLE001
        rule("front matter parses", False, str(e))
        return out
    rule("front matter parses", True)
    rule("spec version known", d.get("wizard") in SPECS, str(d.get("wizard")))
    rule("id valid", bool(ID.fullmatch(str(d.get("id", "")))), str(d.get("id")))
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
    bad = [n for n in d.get("needs") or [] if n not in NEEDS and not str(n).startswith("credentials:")]
    rule("needs known", not bad, ", ".join(map(str, bad)))
    if d.get("provides") is not None:
        pv = d.get("provides")
        badp = [p for p in (pv if isinstance(pv, list) else [pv]) if not SLOT.fullmatch(str(p))]
        rule("provides are slot ids", isinstance(pv, list) and not badp, ", ".join(map(str, badp)))
    if github:
        r = subprocess.run(["gh", "api", f"repos/{github}/topics", "--jq", ".names"], capture_output=True, text=True)
        rule("GitHub topic wizard-ai", "wizard-ai" in r.stdout, r.stdout.strip() or r.stderr.strip())
    return out


def passed(results: list[tuple[str, bool, str]]) -> bool:
    return bool(results) and all(ok for _, ok, _ in results)


def failures(results: list[tuple[str, bool, str]]) -> list[str]:
    return [n for n, ok, _ in results if not ok]


if __name__ == "__main__":
    import sys

    res = check(Path(sys.argv[1] if len(sys.argv) > 1 else "."))
    for name, ok, detail in res:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    print("COMPATIBLE" if passed(res) else "NOT COMPATIBLE")
    sys.exit(0 if passed(res) else 1)
