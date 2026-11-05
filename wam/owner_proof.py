"""Owner proof for app pages: the platform tells an app "this request is the box owner", per request, signed.

An app that wants passphrase-free owner access (Messenger first) declares it in wizard.md:

    owner_proof: hmac-v1            # shorthand, or the long form:
    owner_proof:
      scheme: hmac-v1
      wrap_key: true                # also mint a second, independent key the app may use to wrap data at rest
      activity: [/api/drafts]       # extra request paths that count as "the owner has this app on screen"
      strip: [X-Messenger-Owner-Proof]   # app-specific legacy headers a browser must never be able to send

The App Manager then:
  1. mints a random 32-byte proof key per app (and the optional wrap key), kept outside the app's data volume;
  2. mounts them read-only into the app's main service at /run/wizard-owner/ and names them in the environment
     (WIZARD_OWNER_PROOF_KEY_FILE, WIZARD_OWNER_WRAP_KEY_FILE, WIZARD_OWNER_PROOF_APP), in a block of the stock
     Runtipi user-config override marked `x-wizard-owner-proof` (only that block is ever rewritten or removed);
  3. routes the app's Wizard path through the owner edge (owner_edge.py), which signs each request only AFTER it
     authenticated the owner (tailscaled peer credentials + Serve identity), never for a bearer/agent caller.

Signature (spec: wizard-os spec/OWNER_PROOF.md):
    value   = "<unix seconds>.<48 hex nonce>.<hex HMAC-SHA256(key, message)>"
    message = "wizard-app-owner-v1\\n<app id>\\n<seconds>\\n<nonce>\\n<METHOD>\\n<request target as the app receives it>"
The app checks the value within +-30 s, rejects a reused nonce, and compares in constant time.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

SCHEME = "hmac-v1"
HEADER = "X-Wizard-App-Owner-Proof"
DOMAIN = "wizard-app-owner-v1"
WINDOW_S = 30
MARK = "x-wizard-owner-proof"
MOUNT = "/run/wizard-owner"
KEY_BYTES = 32
_PATH = re.compile(r"^/[A-Za-z0-9._~/-]{0,200}$")
_HEADER_NAME = re.compile(r"^X-[A-Za-z0-9-]{1,62}$", re.I)
# A browser-supplied header the edge must drop can never be one the request needs to work.
_NEVER_STRIP = {"host", "origin", "cookie", "content-type", "content-length", "accept", "authorization"}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def parse(w: dict) -> dict | None:
    """The normalized `owner_proof` declaration of one wizard.md front matter, None when absent.
    Raises ValueError for a malformed one (the checker reports it; the App Manager then treats the app as not
    declaring owner proof, so the app keeps its own sign-in)."""
    raw = w.get("owner_proof") if isinstance(w, dict) else None
    if raw is None or raw is False:
        return None
    if isinstance(raw, str):
        raw = {"scheme": raw}
    if not isinstance(raw, dict):
        raise ValueError("owner_proof must be a scheme name or a mapping")
    unknown = set(raw) - {"scheme", "wrap_key", "activity", "strip"}
    if unknown:
        raise ValueError(f"owner_proof: unknown keys {sorted(unknown)}")
    if raw.get("scheme") != SCHEME:
        raise ValueError(f"owner_proof.scheme must be {SCHEME!r}")
    wrap = raw.get("wrap_key", False)
    if not isinstance(wrap, bool):
        raise ValueError("owner_proof.wrap_key must be true or false")
    activity = raw.get("activity") or []
    if not isinstance(activity, list) or len(activity) > 8 or not all(isinstance(p, str) and _PATH.fullmatch(p)
                                                                      for p in activity):
        raise ValueError("owner_proof.activity must be up to 8 absolute paths")
    strip = raw.get("strip") or []
    if not isinstance(strip, list) or len(strip) > 8 or not all(
            isinstance(h, str) and _HEADER_NAME.fullmatch(h) and h.lower() not in _NEVER_STRIP for h in strip):
        raise ValueError("owner_proof.strip must be up to 8 X- header names")
    return {"scheme": SCHEME, "wrap_key": wrap, "activity": list(activity), "strip": [h.lower() for h in strip]}


# ---------------------------------------------------------------- signing (the edge) and the reference verifier

def message(app_id: str, timestamp: str, nonce: str, method: str, target: str) -> bytes:
    return "\n".join((DOMAIN, app_id, timestamp, nonce, method, target)).encode()


def sign(key: bytes, app_id: str, method: str, target: str, *, now: float | None = None,
         nonce: str | None = None) -> str:
    """Call only after the caller was authenticated as the owner."""
    timestamp = str(int(time.time() if now is None else now))
    nonce = nonce or secrets.token_hex(24)
    digest = hmac.new(key, message(app_id, timestamp, nonce, method, target), hashlib.sha256).hexdigest()
    return f"{timestamp}.{nonce}.{digest}"


def verify(value: str, key: bytes, app_id: str, method: str, target: str, *, now: float | None = None) -> str:
    """Reference verifier (apps re-implement it in their own language). Returns the nonce, which the caller must
    remember for 2 x WINDOW_S and refuse when seen again. Raises ValueError on any failure."""
    try:
        timestamp, nonce, digest = value.split(".")
    except (AttributeError, ValueError):
        raise ValueError("malformed") from None
    if not timestamp.isdigit() or not re.fullmatch(r"[0-9a-f]{48}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("malformed")
    if abs((time.time() if now is None else now) - int(timestamp)) > WINDOW_S:
        raise ValueError("expired")
    expected = hmac.new(key, message(app_id, timestamp, nonce, method, target), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise ValueError("bad signature")
    return nonce


# ---------------------------------------------------------------- keys

def key_dir(home: Path, app_id: str) -> Path:
    if not _ID.fullmatch(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    return Path(home) / "owner-proof" / app_id


def edge_key_path(home: Path, app_id: str) -> Path:
    """The edge's copy of the proof key: host user, 0600, in a 0700 folder."""
    return key_dir(home, app_id) / "edge.key"


def app_secret_dir(home: Path, app_id: str) -> Path:
    """The app's copy (proof.key, optional wrap.key), owned by the app's container user, mounted read-only."""
    return key_dir(home, app_id) / "app"


def read_private_key(path: Path) -> bytes:
    """32 bytes from a private regular file (no symlink, no group/other bits). Raises ValueError otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        raw = os.read(fd, KEY_BYTES + 1)
    finally:
        os.close(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or len(raw) != KEY_BYTES:
        raise ValueError(f"{path}: not a private {KEY_BYTES}-byte key")
    return raw


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"{path}: must be a folder owned by this user")
    os.chmod(path, 0o700)


def publish_key_once(path: Path, raw: bytes) -> bool:
    """Publish `raw` at `path` only if nothing is there yet, atomically: the key is written to a private temporary
    file and hard-linked into place, and link(2) fails if the name exists, so two callers racing to create the same
    key can never replace each other's (`os.replace` would). True when this call published it, False when someone
    else already had."""
    tmp = path.with_name(f".key-{secrets.token_hex(6)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def ensure_edge_key(home: Path, app_id: str, adopt: Path | None = None) -> bool:
    """Create the edge key once (or adopt an existing private key file, e.g. Messenger's hand-built ingress key, so
    the app keeps its remembered unlock). Never replaces an existing key, also when two callers provision at the
    same moment: the loser validates the winner's key (a different key than the one it was asked to adopt is
    refused) and returns False. Returns True when this call wrote the key."""
    d = key_dir(home, app_id)
    _private_dir(d.parent)
    _private_dir(d)
    path = edge_key_path(home, app_id)
    wanted = read_private_key(adopt) if adopt is not None else None
    if not path.exists() and publish_key_once(path, wanted if wanted is not None else secrets.token_bytes(KEY_BYTES)):
        return True
    current = read_private_key(path)   # the key that is there now, whoever wrote it
    if wanted is not None and not hmac.compare_digest(current, wanted):
        raise ValueError(f"{app_id}: a different owner proof key already exists; refusing to replace it")
    return False


_SAFE = re.compile(r"^[A-Za-z0-9._/@:+-]+$")


def provision_app_copy(home: Path, app_id: str, opts: dict, *, image: str, user: str = "1000:1000",
                       adopt_wrap: Path | None = None, run=subprocess.run, docker: list[str] | None = None) -> None:
    """Give the app its own copy of the keys, owned by its container user (rootless Docker maps that user to a
    subordinate uid the host user cannot write directly). A throwaway container of the app's own image does it,
    as alongside.adopt_media does. The wrap key is created once and never replaced (losing it costs the owner a
    one-time passphrase unlock); `adopt_wrap` copies an existing one in instead (migration)."""
    if not re.fullmatch(r"\d+:\d+", user) or not image or not _SAFE.fullmatch(image):
        raise ValueError("invalid image or user")
    src, dst = key_dir(home, app_id), app_secret_dir(home, app_id)
    for p in (src, dst) + ((adopt_wrap,) if adopt_wrap else ()):
        if not _SAFE.fullmatch(str(p)):
            raise ValueError(f"path {str(p)!r} has characters a command can't carry safely")
    dst.mkdir(mode=0o700, exist_ok=True)
    mounts = ["-v", f"{src}/edge.key:/src/edge.key:ro", "-v", f"{dst}:/dst"]
    script = ["set -e", "umask 077", "cp /src/edge.key /dst/proof.key"]
    if opts.get("wrap_key"):
        if adopt_wrap:
            mounts += ["-v", f"{adopt_wrap}:/old/wrap.key:ro"]
            script.append("if [ ! -s /dst/wrap.key ]; then cp /old/wrap.key /dst/wrap.key; fi")
        script.append("if [ ! -s /dst/wrap.key ]; then head -c 32 /dev/urandom > /dst/wrap.key; fi")
        script.append('[ "$(wc -c < /dst/wrap.key)" -eq 32 ]')
        script.append("! cmp -s /dst/proof.key /dst/wrap.key")
    script += [f"chown -R {user} /dst", "chmod 700 /dst", "chmod 600 /dst/*.key"]
    r = run([*(docker or ["docker"]), "run", "--rm", "--pull", "missing", "--network", "none", "--user", "0:0",
             "--entrypoint", "sh", *mounts, image, "-c", " && ".join(script)],
            capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError(f"could not give {app_id} its owner proof keys: {(r.stderr or '').strip()[:200]}")


# ---------------------------------------------------------------- the override block

def overlay(home: Path, app_id: str, service: str, opts: dict) -> dict:
    """What the App Manager adds to the app's main service: the read-only key mount and three variables."""
    env = {"WIZARD_OWNER_PROOF_KEY_FILE": f"{MOUNT}/proof.key", "WIZARD_OWNER_PROOF_APP": app_id}
    if opts.get("wrap_key"):
        env["WIZARD_OWNER_WRAP_KEY_FILE"] = f"{MOUNT}/wrap.key"
    return {"service": service, "environment": env, "volumes": [f"{app_secret_dir(home, app_id)}:{MOUNT}:ro"]}


def _strip(doc: dict) -> dict:
    """Remove exactly what an earlier write_overlay added (recorded under MARK); keep the owner's own lines."""
    mark = doc.pop(MARK, None)
    if not isinstance(mark, dict):
        return doc
    svcs = doc.get("services") if isinstance(doc.get("services"), dict) else {}
    s = svcs.get(mark.get("service"))
    if isinstance(s, dict):
        env = s.get("environment")
        if isinstance(env, dict):
            for k in mark.get("environment") or []:
                env.pop(k, None)
            if not env:
                s.pop("environment")
        vols = s.get("volumes")
        if isinstance(vols, list):
            s["volumes"] = [v for v in vols if v not in (mark.get("volumes") or [])]
            if not s["volumes"]:
                s.pop("volumes")
        if not s:
            svcs.pop(mark.get("service"))
    if isinstance(doc.get("services"), dict) and not doc["services"]:
        doc.pop("services")
    return doc


def write_overlay(path: Path, block: dict | None) -> bool:
    """Put (or with None remove) the owner-proof block in one app's user-config override. Refuses to take over a
    variable or mount the owner already set by hand (e.g. a hand-built trusted-owner block): migrate those first.
    Returns True when the file changed."""
    from .network import load_yaml, dump_yaml
    before = path.read_text() if path.is_file() else None
    doc = (load_yaml(before) or {}) if before else {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a compose mapping")
    if block is None and MARK not in doc:
        return False
    doc = _strip(doc)
    if block:
        svc = doc.setdefault("services", {}).setdefault(block["service"], {})
        env = svc.get("environment")
        if env is None:
            env = svc["environment"] = {}
        if not isinstance(env, dict):
            raise ValueError(f"{path}: {block['service']}.environment must be a mapping to add owner proof")
        clash = [k for k in block["environment"] if k in env and env[k] != block["environment"][k]]
        clash += [k for k in env if k.startswith("MESSENGER_OWNER_") or k == "MESSENGER_TRUST_WIZARD_OWNER"]
        vols = svc.setdefault("volumes", [])
        clash += [v for v in vols if isinstance(v, str) and v.split(":")[1:2] == [MOUNT]]
        if clash:
            raise ValueError(f"{path}: hand-written owner settings present ({', '.join(map(str, clash))}); "
                             "migrate them before the App Manager manages owner proof")
        env.update(block["environment"])
        vols.extend(v for v in block["volumes"] if v not in vols)
        doc[MARK] = {"service": block["service"], "environment": sorted(block["environment"]),
                     "volumes": list(block["volumes"])}
    head = []   # keep the leading comment header exactly as other writers left it
    for line in (before or "").splitlines():
        if not line.startswith("#"):
            break
        head.append(line + "\n")
    text = "".join(head) + dump_yaml(doc) if doc else ""
    if text == (before or ""):
        return False
    if not doc:
        path.unlink(missing_ok=True)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".wam-tmp")
    tmp.write_text(text)
    if path.is_file():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    tmp.replace(path)
    return True


# ---------------------------------------------------------------- what links.py and the edge need

def socket_dir(home: Path) -> Path:
    return Path(home) / "owner-edge"


def socket_path(home: Path, app_id: str) -> Path:
    if not _ID.fullmatch(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    return socket_dir(home) / f"{app_id}.sock"


def declared(reg: dict) -> dict[str, dict]:
    """App id -> owner_proof options, for installed apps whose registry entry carries a valid declaration."""
    return {a["id"]: a["owner_proof"] for a in reg.get("apps") or []
            if isinstance(a, dict) and isinstance(a.get("owner_proof"), dict) and a.get("id")}


def edge_target(home: Path, app_id: str) -> str | None:
    """The Serve target for an owner-proof app when its edge socket is live, else None."""
    p = socket_path(home, app_id)
    try:
        return f"unix:{p}" if stat.S_ISSOCK(p.lstat().st_mode) else None
    except OSError:
        return None


# ---------------------------------------------------------------- provisioning one installed app (CLI / lifecycle)

_DEFAULT = re.compile(r"^\$\{[A-Z0-9_]+:-([^}]+)\}$")


def main_service(recipe: dict) -> tuple[str, str, str]:
    """(service name, image, user) of the recipe's main service (`x-runtipi.is_main`)."""
    for name, svc in (recipe.get("services") or {}).items():
        svc = svc if isinstance(svc, dict) else {}
        if (svc.get("x-runtipi") or {}).get("is_main") is True:
            image = str(svc.get("image") or "")
            m = _DEFAULT.match(image)
            return name, (m.group(1) if m else image), str(svc.get("user") or "1000:1000")
    raise ValueError("the recipe has no main service")


def provision(c, app_id: str, *, store: str = "wizardapps", adopt_proof: Path | None = None,
              adopt_wrap: Path | None = None, image: str | None = None, dry_run: bool = False,
              run=subprocess.run) -> dict:
    """Mint (or adopt) the keys of one installed owner-proof app and write its override block. The app picks the
    keys up on its next start; nothing is restarted here."""
    from .network import load_yaml, override_path
    root = Path(c.runtipi_root)
    d = root / "apps" / store / app_id
    from .registry import _front
    opts = parse(_front((d / "wizard.md").read_text()))
    if not opts:
        raise ValueError(f"{app_id} does not declare owner_proof in its installed wizard.md")
    service, recipe_image, user = main_service(load_yaml((d / "docker-compose.yml").read_text()) or {})
    plan = {"app": app_id, "service": service, "user": user, "image": image or recipe_image, "options": opts,
            "edge_key": str(edge_key_path(c.home, app_id)), "app_keys": str(app_secret_dir(c.home, app_id)),
            "override": str(override_path(root, store, app_id))}
    if dry_run:
        return {**plan, "dry_run": True}
    plan["edge_key_written"] = ensure_edge_key(c.home, app_id, adopt=adopt_proof)
    provision_app_copy(c.home, app_id, opts, image=plan["image"], user=user, adopt_wrap=adopt_wrap, run=run)
    plan["override_changed"] = write_overlay(override_path(root, store, app_id), overlay(c.home, app_id, service, opts))
    return plan


def status(c, reg: dict) -> list[dict]:
    out = []
    for app_id, opts in sorted(declared(reg).items()):
        act = Path(c.home) / "owner-activity" / app_id
        out.append({"app": app_id, "options": opts, "edge_key": edge_key_path(c.home, app_id).is_file(),
                    "app_keys": app_secret_dir(c.home, app_id).is_dir(), "edge": edge_target(c.home, app_id),
                    "owner_active_at": int(act.stat().st_mtime) if act.exists() else None})
    return out
