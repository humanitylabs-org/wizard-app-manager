"""Signed releases, warn-only (wizard-app-manager#565, step A of wizard-os#416).

Today a box trusts a release only because it came over HTTPS. This module checks, before an update uses any pin:

- a detached signature for the release file (`releases/<version>.json.sig`) and for its promotion record
  (`releases/promotions/<version>.json.sig`), made with `ssh-keygen -Y sign -n wizardos-release` (Ed25519);
- the sha256 of the installer the update downloads (`https://aiwizards.com/os.sh`, which serves get.sh) against the
  hashes the release file names: `"installer": {"os.sh": "<sha256>", "get.sh": "<sha256>"}`.

Why `ssh-keygen -Y` and not minisign: OpenSSH (8.0 or newer) is already on every box, so nothing new is installed,
and its signatures are plain Ed25519. The trusted keys ship INSIDE this checkout (`wam/release_signers`, the OpenSSH
allowed-signers format, one line per key so a key can be changed later by adding the new one first); they are never
fetched from where the releases come from.

The switch is `WIZARD_VERIFY_RELEASES` (environment, wins) or `"verify_releases"` in ~/.wizard-os/updates.json
(`wam updates --verify-releases off|warn`): `off` (default) changes nothing at all; `warn` checks, logs a missing or
bad signature to release-signature.log, records it (`release-signature.json`, `wam updates` status `signature`, and the quick
update's install-status.json / release.json) and the update carries on. `enforce` (refuse, change nothing) is
reserved for the next step: until ENFORCE_AVAILABLE is turned on it behaves like `warn` and says so.

States: `signature`: ok | missing | bad (with a `reason`); `installer`: ok | mismatch | unnamed (the release names
no installer hash) | unchecked.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ENV = "WIZARD_VERIFY_RELEASES"
SETTING = "verify_releases"
MODES = ("off", "warn", "enforce")
DEFAULT = "off"
ENFORCE_AVAILABLE = False        # step A ships off and warn only; enforce comes with the signed releases
NAMESPACE = "wizardos-release"   # ssh-keygen -Y sign -n wizardos-release
PRINCIPAL = "wizardos-release"   # the name every allowed key carries in release_signers
SIGNERS = Path(__file__).with_name("release_signers")
RECORD = "release-signature.json"
INSTALLER_NAMES = ("os.sh", "get.sh")
CACHE_SECONDS = 600
_cache: dict[tuple, tuple[float, dict]] = {}


def _home(home: Path | None) -> Path:
    return Path(home or os.environ.get("WIZARD_OS_HOME", Path.home() / ".wizard-os")).expanduser()


def requested(home: Path | None = None) -> str:
    """The mode asked for: the environment wins, then updates.json; anything unknown is off."""
    v = os.environ.get(ENV)
    if v is None:
        try:
            v = json.loads((_home(home) / "updates.json").read_text()).get(SETTING)
        except (OSError, ValueError, AttributeError):
            v = None
    v = str(v or "").strip().lower()
    return v if v in MODES else DEFAULT


def mode(home: Path | None = None) -> str:
    """The mode that runs: off | warn | enforce (enforce only once ENFORCE_AVAILABLE)."""
    m = requested(home)
    return "warn" if m == "enforce" and not ENFORCE_AVAILABLE else m


def on(home: Path | None = None) -> bool:
    return mode(home) != "off"


def signers(path: Path | None = None) -> list[str]:
    """The allowed-signers lines that carry a key (comments and blanks dropped)."""
    try:
        text = Path(path or SIGNERS).read_text()
    except OSError:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _ssh_keygen() -> str | None:
    return shutil.which("ssh-keygen")


def verify(data: bytes, sig: bytes | None, keys: Path | None = None) -> dict:
    """Check a detached signature over <data>: {"signature": ok|missing|bad, "reason": ...}."""
    if not sig:
        return {"signature": "missing", "reason": "no signature published"}
    keygen = _ssh_keygen()
    if not keygen:
        return {"signature": "bad", "reason": "ssh-keygen not found; cannot check signatures"}
    keys = Path(keys or SIGNERS)
    with tempfile.TemporaryDirectory(prefix="wam-sig-") as d:
        s = Path(d) / "file.sig"
        s.write_bytes(sig)
        def run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([keygen, "-Y", *args, "-n", NAMESPACE, "-s", str(s)], input=data,
                                  capture_output=True, timeout=30)
        if signers(keys):
            try:
                if run("verify", "-f", str(keys), "-I", PRINCIPAL).returncode == 0:
                    return {"signature": "ok"}
            except (OSError, subprocess.SubprocessError) as e:
                return {"signature": "bad", "reason": f"ssh-keygen failed: {type(e).__name__}"}
        try:   # a valid signature by a key that is not on the list is still not ok: say which it is
            valid = run("check-novalidate").returncode == 0
        except (OSError, subprocess.SubprocessError):
            valid = False
    if not valid:
        return {"signature": "bad", "reason": "signature does not match the file"}
    if not signers(keys):
        return {"signature": "bad", "reason": "no trusted release key in this App Manager checkout"}
    return {"signature": "bad", "reason": "signed by a key that is not on the trusted list"}


def _get(url: str, limit: int, timeout: float) -> bytes | None:
    """The bytes at <url>, or None when it is not there (404 / no such file / unreachable)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read(limit)
    except (OSError, ValueError):
        return None


def _base(base: str | None) -> str:
    from .updates import RELEASE_BASE
    return (base or os.environ.get("WIZARD_RELEASE_BASE") or RELEASE_BASE).rstrip("/")


def check_url(url: str, data: bytes | None = None, keys: Path | None = None, timeout: float = 10) -> dict:
    """Fetch <url> (unless its bytes are given) and <url>.sig and verify. Cached CACHE_SECONDS (files are immutable)."""
    key = (url, hashlib.sha256(data).hexdigest() if data is not None else None, str(keys or SIGNERS))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return dict(hit[1])
    if data is None:
        data = _get(url, 2 * 1024 * 1024, timeout)
    if data is None:
        res = {"signature": "missing", "reason": f"could not read {url.rsplit('/', 1)[-1]}"}
    else:
        res = verify(data, _get(url + ".sig", 64 * 1024, timeout), keys)
    _cache[key] = (time.monotonic(), res)
    return dict(res)


def check_release(version: str, base: str | None = None, data: bytes | None = None, keys: Path | None = None) -> dict:
    return check_url(f"{_base(base)}/{version}.json", data, keys)


def check_promotion(version: str, base: str | None = None, data: bytes | None = None,
                    keys: Path | None = None) -> dict:
    return check_url(f"{_base(base)}/promotions/{version}.json", data, keys)


def installer_hashes(doc) -> list[str]:
    """The installer sha256s a release file names (`installer.os.sh` / `installer.get.sh`), lowercase hex only."""
    inst = doc.get("installer") if isinstance(doc, dict) else None
    if not isinstance(inst, dict):
        return []
    out = []
    for name in INSTALLER_NAMES:
        v = str(inst.get(name) or "").strip().lower()
        if len(v) == 64 and all(c in "0123456789abcdef" for c in v) and v not in out:
            out.append(v)
    return out


def installer_state(sha: str | None, expect: list[str]) -> str:
    if not sha:
        return "unchecked"
    if not expect:
        return "unnamed"
    return "ok" if sha.lower() in expect else "mismatch"


def refuses(rec: dict | None, m: str | None = None) -> str | None:
    """Why enforce refuses this update (None = go on). Only in enforce mode; warn and off never refuse."""
    if not rec or (m or rec.get("mode")) != "enforce":
        return None
    if rec.get("signature") != "ok":
        return f"release signature {rec.get('signature')}: {rec.get('reason', '')}".strip(": ")
    if (rec.get("promotion") or {}).get("signature") not in (None, "ok"):
        return f"promotion record signature {rec['promotion']['signature']}"
    if rec.get("installer") == "mismatch":
        return "the downloaded installer does not match the release's installer hash"
    return None


LOG = "release-signature.log"   # its own file: get.sh empties install.log when the installer starts


def log(home: Path | None, msg: str) -> None:
    h = _home(home)
    try:
        h.mkdir(parents=True, exist_ok=True)
        with open(h / LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} [signature] {msg}\n")
    except OSError:
        pass


def _json(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _put(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".sig-tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(tmp, path.stat().st_mode & 0o777)
    except OSError:
        pass
    tmp.replace(path)


def stamp(home: Path | None) -> None:
    """Carry the last check into what the installer wrote once it has finished: release.json gets `signature` when it
    records the release that was checked, install-status.json gets the summary when that run (started after the
    check) is no longer running. get.sh writes both from scratch, so this runs after it (every `wam updates` status
    with the switch on); the installer's own files are never touched while it runs."""
    rec = read(home)
    if not rec or not rec.get("release"):
        return
    h = _home(home)
    try:
        receipt = _json(h / "release.json")
        if receipt and receipt.get("release") == rec["release"] and receipt.get("signature") != rec.get("signature"):
            _put(h / "release.json", {**receipt, "signature": rec.get("signature")})
        st = _json(h / "install-status.json")
        if (st and st.get("state") not in (None, "running") and "signature" not in st
                and str(st.get("started_at") or "") >= str(rec.get("checked_at") or "~")):
            _put(h / "install-status.json", {**st, "signature": summary(rec)})
    except OSError:
        pass


def read(home: Path | None = None) -> dict | None:
    try:
        d = json.loads((_home(home) / RECORD).read_text())
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def write(home: Path | None, rec: dict) -> dict:
    h = _home(home)
    h.mkdir(parents=True, exist_ok=True)
    rec = {**rec, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    tmp = h / (RECORD + ".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(h / RECORD)
    return rec


def before_update(home: Path | None, version: str, base: str | None = None, keys: Path | None = None) -> dict | None:
    """Check the target release before an update uses its pins. None when the switch is off (nothing happens).

    Returns the record (also written to release-signature.json): mode, release, signature, reason, installer
    hashes the release names (`expect`) and, when this release has one, its promotion record's signature."""
    m = mode(home)
    if m == "off":
        return None
    from . import quick_update
    doc = quick_update.release_doc(version, base)
    raw = doc[1] if doc else None
    res = check_release(version, base, raw, keys) if raw is not None else \
        {"signature": "missing", "reason": f"could not read {version}.json"}
    rec = {"mode": m, "requested": requested(home), "release": version, **res,
           "expect": installer_hashes(doc[0]) if doc else [], "installer": "unchecked"}
    prom = _get(f"{_base(base)}/promotions/{version}.json", 65536, 10)
    if prom is not None:
        rec["promotion"] = check_promotion(version, base, prom, keys)
    rec = write(home, rec)
    if rec["signature"] != "ok":
        log(home, f"release {version}: signature {rec['signature']} ({rec.get('reason', '')}); "
                  + ("refused (enforce)" if m == "enforce" else "carrying on (warn-only)"))
    if (rec.get("promotion") or {}).get("signature") not in (None, "ok"):
        log(home, f"promotion record {version}: signature {rec['promotion']['signature']} "
                  f"({rec['promotion'].get('reason', '')})")
    if not rec["expect"]:
        log(home, f"release {version} names no installer hash; the installer is not checked")
    return rec


def download(url: str, attempts: int = 4, timeout: float = 120) -> bytes | None:
    """The installer's bytes, fetched with curl exactly as the unchecked path does (`curl -fsSL --retry 4`; the
    installer host refuses Python's default User-Agent with 403), or None when that fails."""
    curl = shutil.which("curl")
    if curl:
        with tempfile.TemporaryDirectory(prefix="wam-installer-") as d:
            out = Path(d) / "os.sh"
            try:
                r = subprocess.run([curl, "-fsSL", "--retry", str(attempts), "--retry-delay", "2", "-o", str(out), url],
                                   capture_output=True, timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                return None
            return out.read_bytes() if r.returncode == 0 and out.is_file() else None
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})   # no curl: same request, same agent string
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(16 * 1024 * 1024)
        except (OSError, ValueError):
            if i + 1 < attempts:
                time.sleep(2)
    return None


def checked_installer(home: Path | None, rec: dict, env: str, url: str | None = None) -> tuple[str | None, dict]:
    """Download the installer HERE, hash it and compare with the release's hashes, then return the `bash -c` text that
    runs exactly that file (`<env>bash <file> start`; get.sh's detached start copies itself, so get.sh is not
    downloaded a second time). The hash is measured by this process, never read from the installer's own output.

    warn: a mismatch is logged and recorded and the installer still runs. enforce: a mismatch returns no script.
    A failed download returns no script either (as before, a failed download never looks like a started update)."""
    from .updates import INSTALLER
    data = download(url or INSTALLER)
    if data is None:
        rec = write(home, {**rec, "installer": "unchecked", "installer_sha256": None,
                           "installer_error": f"could not download the installer ({url or INSTALLER})"})
        return None, rec
    sha = hashlib.sha256(data).hexdigest()
    state = installer_state(sha, rec.get("expect") or [])
    rec = {**rec, "installer": state, "installer_sha256": sha}
    rec.pop("installer_error", None)
    if state == "mismatch":
        log(home, f"installer sha256 {sha} is not one the release {rec.get('release')} names; "
                  + ("refused (enforce)" if rec.get("mode") == "enforce" else "carrying on (warn-only)"))
        if rec.get("mode") == "enforce":
            return None, write(home, {**rec, "installer_error": "the installer does not match the release's hash"})
    fd, path = tempfile.mkstemp(prefix="wizard-os-installer.", dir=os.environ.get("TMPDIR") or None)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    q = shlex.quote(path)
    return f"trap 'rm -f {q}' EXIT; {env}bash {q} start", write(home, rec)


def summary(rec: dict | None) -> dict | None:
    """The short block `wam updates` shows (and install-status.json / release.json carry on a quick update)."""
    if not rec:
        return None
    out = {k: rec[k] for k in ("mode", "release", "signature", "reason", "installer", "checked_at") if k in rec}
    if rec.get("promotion"):
        out["promotion"] = rec["promotion"].get("signature")
    return out


def status(home: Path | None, version: str | None, base: str | None = None, keys: Path | None = None) -> dict | None:
    """Dry run for `wam updates`: the channel release's signature state, or None when the switch is off."""
    m = mode(home)
    if m == "off":
        return None
    stamp(home)
    out: dict = {"mode": m}
    if requested(home) != m:
        out["requested"] = requested(home)
        out["note"] = "enforce is not available yet; warning only"
    if version:
        out.update({"release": version, **check_release(version, base, keys=keys)})
    last = read(home)
    if last:
        out["last_update"] = summary(last)
    return out
