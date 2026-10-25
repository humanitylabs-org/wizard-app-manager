"""Agent import rules: what crosses machines in a Hermes home and what stays this box's own.

Adapted from try-wizard wizard-import-hermes ("Bring My Hermes into Wizard"), generalised for any agent home path,
for container agents and for the alongside host Hermes:
- the stock `hermes backup` zip is checked (no absolute or parent paths, has config.yaml) and filtered: per-machine
  files (bin/, logs/, runtime/, caches, locks, the Hermes code checkout, Wizard's own plugins) never cross;
- the same per-machine entries are never moved out of the box's agent home, so the restored agent keeps this box's
  Hermes install, keys and caches;
- this box's Wizard wiring wins over the imported config: `terminal`, `mcp_servers.wizard`, the wizard skill
  `external_dirs`, Wizard's plugins, `platforms.api_server`, and the Wizard-owned .env keys
  (API_SERVER_*, WIZARD_*, HERMES_DASHBOARD*);
- messaging platforms are set `enabled: false` (tokens kept) and every enabled cron job is paused with stock
  `cron.jobs.pause_job`, so the copy never answers or runs jobs alongside the original.
The scripts below run with stock Hermes' own Python, never a modified Hermes.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import zipfile
from .filesystem import Path
from . import filesystem as datafs

import yaml

MAX_BYTES = 256 << 30
# Per-machine or rebuilt-on-start entries of a Hermes home (top-level names, or nested paths).
KEEP_DIRS = ("bin", "logs", "runtime", "cache", "audio_cache", "image_cache", ".ssh-vm", "plugins/wizard-os",
             "plugins/wizard-product", "backups", "node_modules", "node", "hermes-agent", ".venv", "venv")
KEEP_RE = re.compile(r"(\.lock$|\.pid$|-wal$|-shm$|\.bak$|^state\.db\.|^\.update_check$|^models_dev_cache"
                     r"|^\.skills_prompt_snapshot\.json$|^gateway_state\.json$|(^|/)\.DS_Store$)")
WIZARD_ENV = re.compile(r"^\s*(export\s+)?(API_SERVER_[A-Z_]*|WIZARD_[A-Z0-9_]*|HERMES_DASHBOARD[A-Z_]*)\s*=")
WIZARD_PLUGINS = ("wizard-os", "wizard-product")

# Messaging platforms and the .env keys that switch them on (stock gateway/config_env.py). Used only to *read* what
# was on at export; switching off and on goes through the stock config at import/activate.
MESSAGING_ENV = {
    "telegram": ("TELEGRAM_BOT_TOKEN",), "discord": ("DISCORD_BOT_TOKEN",), "slack": ("SLACK_BOT_TOKEN",),
    "whatsapp": ("WHATSAPP_ENABLED",), "whatsapp_cloud": ("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "WHATSAPP_CLOUD_ACCESS_TOKEN"),
    "signal": ("SIGNAL_HTTP_URL",), "mattermost": ("MATTERMOST_TOKEN",), "matrix": ("MATRIX_ACCESS_TOKEN", "MATRIX_PASSWORD"),
    "email": ("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"), "sms": ("TWILIO_ACCOUNT_SID",),
    "dingtalk": ("DINGTALK_CLIENT_ID", "DINGTALK_CLIENT_SECRET"), "feishu": ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
    "wecom": ("WECOM_BOT_ID", "WECOM_SECRET"), "wecom_callback": ("WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"),
    "weixin": ("WEIXIN_TOKEN", "WEIXIN_ACCOUNT_ID"), "bluebubbles": ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD"),
    "qqbot": ("QQ_APP_ID", "QQ_CLIENT_SECRET"), "yuanbao": ("YUANBAO_APP_ID", "YUANBAO_APP_SECRET"),
    "relay": ("GATEWAY_RELAY_URL",),
}
NOT_MESSAGING = {"api_server", "local", "webhook", "msgraph_webhook"}


def keep_in_place(rel: str) -> bool:
    """True for per-machine entries of an agent home: never captured, never moved, never overwritten."""
    rel = rel.strip("/")
    for k in KEEP_DIRS:
        if rel == k or rel.startswith(k + "/"):
            return True
    return bool(KEEP_RE.search(rel))


def _keep_parent(rel: str) -> bool:
    return any(k.startswith(rel + "/") for k in KEEP_DIRS)


def root_files(root: Path) -> list[str]:
    """The Agent Service's own files beside the agent folders (`.agent-grants.json`, `.main-agent.json`,
    `.apps-seen.json`): plain top-level files of the agents root, without temporary or lock files."""
    root = Path(root)
    try:
        names = sorted(datafs.listdir(root))
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for n in names:
        p = Path(root) / n
        if p.is_symlink() or not p.is_file() or ".tmp" in n or KEEP_RE.search(n):
            continue
        out.append(n)
    return out


def movable_entries(home: Path) -> list[str]:
    """Relative paths to set aside so only per-machine entries stay: top-level entries, descending into a folder that
    holds a kept entry (plugins/ keeps plugins/wizard-os)."""
    out: list[str] = []

    def walk(d: Path, prefix: str) -> None:
        try:
            names = sorted(datafs.listdir(d))
        except FileNotFoundError:
            return
        for n in names:
            rel = f"{prefix}{n}"
            if keep_in_place(rel):
                continue
            if _keep_parent(rel) and (d / n).is_dir() and not (d / n).is_symlink():
                walk(d / n, rel + "/")
                continue
            out.append(rel)
    walk(Path(home), "")
    return out


# ---------------------------------------------------------------- the archive

class ImportRefused(ValueError):
    pass


def check_and_filter(src: Path, dst: Path) -> dict:
    """Copy a stock `hermes backup` zip to dst without per-machine entries, refusing unsafe names."""
    kept, skipped, total = 0, [], 0
    try:
        z = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ImportRefused("the agent archive is not a zip") from None
    with z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        firsts = {n.split("/", 1)[0] for n in names if "/" in n}
        prefix = (firsts.pop() + "/") if len(firsts) == 1 and firsts <= {".hermes", "hermes"} and \
            all("/" in n for n in names) else ""
        rels = [n[len(prefix):] for n in names]
        if "config.yaml" not in rels:
            raise ImportRefused("the agent archive isn't a Hermes backup (no config.yaml)")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as w:
            for name, rel in zip(names, rels):
                parts = rel.split("/")
                if not rel or rel.startswith("/") or ".." in parts or "\\" in rel or "\x00" in rel or "" in parts:
                    raise ImportRefused(f"unsafe path in the agent archive: {name[:120]}")
                info = z.getinfo(name)
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    skipped.append(rel)        # symlinks never cross machines
                    continue
                if keep_in_place(rel):
                    skipped.append(rel)
                    continue
                total += info.file_size
                if total > MAX_BYTES:
                    raise ImportRefused("the agent archive is implausibly large")
                with z.open(name) as r, w.open(rel, "w", force_zip64=True) as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                kept += 1
    return {"files": kept, "skipped": len(skipped), "skipped_examples": sorted({s.split("/")[0] for s in skipped})[:12]}


def read_zip_member(src: Path, rel: str) -> bytes | None:
    try:
        with zipfile.ZipFile(src) as z:
            for n in z.namelist():
                if n == rel or n.endswith("/" + rel) and n.count("/") == rel.count("/") + 1:
                    return z.read(n)
    except (zipfile.BadZipFile, OSError, KeyError):
        return None
    return None


def _env_map(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:]
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def active_record(cfg_text: bytes | None, env_text: bytes | None, jobs_text: bytes | None) -> dict:
    """What was on in one agent: messaging platforms (enabled, or switched on by credentials and not explicitly
    off) and cron jobs (enabled and not paused). Ids and names only, never values."""
    try:
        cfg = yaml.safe_load(cfg_text or b"") or {}
    except yaml.YAMLError:
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    env = _env_map((env_text or b"").decode("utf-8", "replace"))
    plats = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
    on = set()
    for name, block in plats.items():
        if name in NOT_MESSAGING or not isinstance(block, dict):
            continue
        if block.get("enabled") is True:
            on.add(name)
    for name, keys in MESSAGING_ENV.items():
        block = plats.get(name) if isinstance(plats.get(name), dict) else {}
        if block.get("enabled") is False:
            continue
        if any((env.get(k) or "").strip() for k in keys):
            on.add(name)
    jobs = []
    try:
        data = json.loads(jobs_text or b"{}")
        rows = data.get("jobs", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        for j in rows:
            if isinstance(j, dict) and j.get("id") and j.get("enabled", True) and j.get("state") != "paused" \
                    and not j.get("paused_at") and j.get("state") not in ("completed", "error"):
                jobs.append(str(j["id"]))
    except ValueError:
        pass
    return {"messaging": sorted(on), "cron": sorted(jobs)}


def active_from_zip(zip_path: Path) -> dict:
    return active_record(read_zip_member(zip_path, "config.yaml"), read_zip_member(zip_path, ".env"),
                         read_zip_member(zip_path, "cron/jobs.json"))


# ---------------------------------------------------------------- this box's wiring

def wizard_wiring(home: Path) -> dict:
    """What Wizard set in this box's agent home, to put back over the imported config."""
    try:
        cfg = yaml.safe_load((home / "config.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    plugins = (cfg.get("plugins") or {}).get("enabled") or [] if isinstance(cfg.get("plugins"), dict) else []
    skills = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    return {"terminal": cfg.get("terminal"),
            "mcp_wizard": (cfg.get("mcp_servers") or {}).get("wizard") if isinstance(cfg.get("mcp_servers"), dict) else None,
            "external_dirs": [d for d in (skills.get("external_dirs") or []) if "wizard" in str(d)],
            "plugins": [p for p in plugins if p in WIZARD_PLUGINS],
            "api_server": (cfg.get("platforms") or {}).get("api_server") if isinstance(cfg.get("platforms"), dict) else None,
            "present": bool(cfg)}


def wizard_env_lines(home: Path) -> list[str]:
    envp = home / ".env"
    try:
        return [ln for ln in envp.read_text().splitlines() if WIZARD_ENV.match(ln)]
    except OSError:
        return []


def env_merge(home: Path, wizard_lines: list[str]) -> list[str]:
    """Imported .env without Wizard-owned keys, then this box's Wizard lines. Returns the imported keys replaced
    (names only)."""
    envp = home / ".env"
    lines = envp.read_text().splitlines() if envp.is_file() else []
    replaced = sorted({ln.split("=", 1)[0].strip() for ln in lines if WIZARD_ENV.match(ln)})
    keep = [ln for ln in lines if not WIZARD_ENV.match(ln)]
    text = "\n".join(keep + ["", "# Wizard OS (this box's wiring, kept by wam backup import)"] + wizard_lines) + "\n"
    tmp = envp.with_name(".env.wam-import")
    with tmp.open("w") as f:
        f.write(text)
    tmp.chmod(0o600)
    datafs.rename(tmp, envp)
    return replaced


# ---------------------------------------------------------------- scripts run by stock Hermes' Python

POST_IMPORT = r'''
import json, sys
sys.path.insert(0, "/opt/hermes")
keep = json.load(sys.stdin)
from hermes_cli.config import read_raw_config, atomic_config_write, get_config_path
cfg = read_raw_config() or {}
report = {"disabled_messaging": [], "paused_jobs": []}
if keep.get("present"):
    if keep.get("terminal") is not None:
        cfg["terminal"] = keep["terminal"]
    if keep.get("mcp_wizard") is not None:
        if not isinstance(cfg.get("mcp_servers"), dict): cfg["mcp_servers"] = {}
        cfg["mcp_servers"]["wizard"] = keep["mcp_wizard"]
    skills = cfg.get("skills") if isinstance(cfg.get("skills"), dict) else {}
    mine = list(keep.get("external_dirs") or [])
    dirs = mine + [d for d in (skills.get("external_dirs") or []) if d not in mine and "wizard" not in str(d)]
    if dirs or "external_dirs" in skills:
        skills["external_dirs"] = dirs; cfg["skills"] = skills
    plugins = cfg.get("plugins") if isinstance(cfg.get("plugins"), dict) else {}
    enabled = [p for p in (plugins.get("enabled") or []) if p not in ("wizard-os", "wizard-product")]
    for p in keep.get("plugins") or []:
        if p not in enabled: enabled.append(p)
    if enabled or "enabled" in plugins:
        plugins["enabled"] = enabled; cfg["plugins"] = plugins
platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
if keep.get("present"):
    if keep.get("api_server") is not None:
        platforms["api_server"] = keep["api_server"]
    else:
        platforms.pop("api_server", None)
from gateway.config import Platform
try:
    from gateway.config_env import _ENV_ENABLE_CREDENTIALS as CREDS
except Exception:
    CREDS = {}
from hermes_cli.config import load_env
env = load_env() or {}
names = set(platforms) - {"api_server"}
with_creds = set()
for plat, keys in CREDS.items():
    if any(env.get(k) for k in keys):
        names.add(plat.value); with_creds.add(plat.value)
for name in sorted(names):
    if name in ("api_server", "local", "webhook", "msgraph_webhook"): continue
    try:
        Platform(name)
    except ValueError:
        continue
    block = platforms.get(name) if isinstance(platforms.get(name), dict) else {}
    if block.get("enabled") is False and name not in with_creds:
        continue
    if block.get("enabled") is not False:
        report["disabled_messaging"].append(name)
    block["enabled"] = False
    platforms[name] = block
cfg["platforms"] = platforms
atomic_config_write(get_config_path(), cfg)
from cron.jobs import load_jobs, pause_job
for job in load_jobs():
    if job.get("enabled", True) and job.get("state") != "paused":
        pause_job(job["id"], reason="Restored by wam backup: paused so it doesn't run twice. `wam backup activate` resumes it.")
        report["paused_jobs"].append(job["id"])
print(json.dumps(report))
'''

ACTIVATE = r'''
import json, sys
sys.path.insert(0, "/opt/hermes")
want = json.load(sys.stdin)
from hermes_cli.config import read_raw_config, atomic_config_write, get_config_path
cfg = read_raw_config() or {}
platforms = cfg.get("platforms") if isinstance(cfg.get("platforms"), dict) else {}
report = {"enabled_messaging": [], "resumed_jobs": [], "missing_jobs": []}
for name in want.get("messaging") or []:
    block = platforms.get(name) if isinstance(platforms.get(name), dict) else {}
    block["enabled"] = True
    platforms[name] = block
    report["enabled_messaging"].append(name)
cfg["platforms"] = platforms
atomic_config_write(get_config_path(), cfg)
from cron.jobs import load_jobs, resume_job
have = {j.get("id"): j for j in load_jobs()}
for jid in want.get("cron") or []:
    j = have.get(jid)
    if not j:
        report["missing_jobs"].append(jid); continue
    if j.get("enabled", True) and j.get("state") != "paused":
        continue
    resume_job(jid)
    report["resumed_jobs"].append(jid)
print(json.dumps(report))
'''


def last_json_line(text: str) -> dict:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    raise ValueError("stock Hermes printed no result")


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()
