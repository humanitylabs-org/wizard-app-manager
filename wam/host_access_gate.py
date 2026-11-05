"""Accepting an app with host access is an owner-device action no agent can trigger (spec/OWNER_PROOF.md, amendment 2).

Host access = what escapes app isolation: privileged mode, the Docker/containerd/podman socket, host bind mounts
(including /home), host networking and other host namespaces, host devices, risky `cap_add`, networks the app does
not own (wam/network.py host_access()). Before this gate `wam install --allow-host-access`, `wam app update
--allow-host-access` and `wam network allow` accepted it from whoever could run `wam`, and agents can: the agent
container has the `wam` command, and the Agent Service installs through wam in-process.

Now the flag alone never accepts anything. Acceptance needs a GRANT, and a grant is written only by the App Manager's
owner listener (cli.StoreHandler `POST /_host-access/<app>`) after it authenticated the request as the owner's own
enrolled device (Tailscale identity, not this box, same origin, no bearer, owner_devices.Enrollment). A grant is:
  - MACed with a host-only key (`<wizard-os>/owner-proof/host-access.key`, 0600, never mounted into an app or agent
    container, the same custody as the owner edge's key);
  - bound to one app, to what it is for (install / update / allow), to the exact set of risks the owner was shown and
    to the hash of the recipe they were read from (a changed recipe or image needs a new grant);
  - single use, and valid for ten minutes.
An allowance the owner already gave (app-manager.json `host_access_allowed`) is unchanged and still honoured.
Limit (as for the edge key): a process running as the host user can read the key; agents run in containers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from . import owner_proof as op

TTL_S = 600
DOMAIN = "wizard-host-access-v1"


def key_path(home: Path) -> Path:
    return Path(home) / "owner-proof" / "host-access.key"


def grants_dir(home: Path) -> Path:
    return Path(home) / "owner-proof" / "host-access-grants"


def enabled(home: Path) -> bool:
    """Grants are OFF until the owner (or the rollout) sets `owner_host_access_grants: true` in app-manager.json, a
    host file. While off, the listener issues nothing and no key is created: the endpoint is inert (spec: grants must
    not be turned on before the enrollment record is trustworthy and the grant UI is hardened)."""
    from . import network
    return network._cfg(home).get("owner_host_access_grants") is True


def _key(home: Path, create: bool) -> bytes:
    path = key_path(home)
    if create:
        op._private_dir(path.parent.parent)
        op._private_dir(path.parent)
        if not path.exists():
            op.publish_key_once(path, secrets.token_bytes(op.KEY_BYTES))
    return op.read_private_key(path)


def _digest(risks, kind: str = "", recipe: str = "") -> str:
    return hashlib.sha256("\n".join([kind, recipe, *sorted(str(r) for r in risks)]).encode()).hexdigest()


def _mac(key: bytes, app_id: str, risks_digest: str, exp: int, nonce: str) -> str:
    return hmac.new(key, f"{DOMAIN}\n{app_id}\n{risks_digest}\n{exp}\n{nonce}".encode(), hashlib.sha256).hexdigest()


def _path(home: Path, app_id: str) -> Path:
    if not op._ID.fullmatch(app_id):
        raise ValueError(f"invalid app id {app_id!r}")
    return grants_dir(home) / f"{app_id}.json"


def refusal(app_id: str, risks, what: str = "install") -> str:
    shown = "\n  - ".join(risks) if risks else "(none listed)"
    head = (f"REFUSED: the update of {app_id} asks for access that escapes app isolation" if what == "update"
            else f"REFUSED: {app_id} asks for access that escapes app isolation")
    return (f"{head}:\n  - {shown}\nIt could reach every app and the server itself. Accepting that is an owner "
            f"action, done from the owner's own device in Wizard (Store -> {app_id} -> Allow host access); no agent "
            f"or command line can do it, and the --allow-host-access flag alone accepts nothing. Ask the owner to "
            f"allow it on their device, then run the {what} again.")


def issue(home: Path, app_id: str, risks, kind: str = "", recipe: str = "", *, now: float | None = None) -> dict:
    """Write a grant. ONLY the owner listener calls this, after authenticating the owner's enrolled device."""
    if not risks:
        raise ValueError("nothing to accept: the app asks for no host access")
    if not enabled(home):
        raise PermissionError("host-access grants are not turned on")
    key = _key(home, create=True)
    exp = int((time.time() if now is None else now)) + TTL_S
    nonce, digest = secrets.token_hex(12), _digest(risks, kind, recipe)
    d = grants_dir(home)
    op._private_dir(d)
    path = _path(home, app_id)
    tmp = path.with_name(f".grant-{secrets.token_hex(6)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"app": app_id, "risks": digest, "exp": exp, "nonce": nonce,
                   "mac": _mac(key, app_id, digest, exp, nonce)}, f)
    os.replace(tmp, path)
    return {"app": app_id, "expires": exp}


def _check(path: Path, home: Path, app_id: str, risks, kind: str, recipe: str, now: float | None) -> bool:
    try:
        key = _key(home, create=False)
        got = json.loads(path.read_text())
        exp, nonce, digest = int(got["exp"]), str(got["nonce"]), str(got["risks"])
        return bool(risks and got.get("app") == app_id and digest == _digest(risks, kind, recipe)
                    and (time.time() if now is None else now) <= exp
                    and hmac.compare_digest(str(got["mac"]), _mac(key, app_id, digest, exp, nonce)))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def valid(home: Path, app_id: str, risks, kind: str = "", recipe: str = "", *, now: float | None = None) -> bool:
    """Is there an unexpired grant, signed by this box, for exactly this app, purpose, risks and recipe?"""
    try:
        path = _path(home, app_id)
    except ValueError:
        return False
    return _check(path, home, app_id, risks, kind, recipe, now)


def consume(home: Path, app_id: str, risks, kind: str = "", recipe: str = "", *, now: float | None = None) -> bool:
    """valid() and, if so, use the grant up (single use). The grant is first renamed away, so exactly one caller can
    take it and a grant re-issued meanwhile is never the one that gets used up by a stale check."""
    if not valid(home, app_id, risks, kind, recipe, now=now):     # a wrong try (purpose, recipe) never burns the grant
        return False
    path = _path(home, app_id)
    taken = path.with_name(f".used-{secrets.token_hex(6)}")
    try:
        os.rename(path, taken)
    except OSError:
        return False       # none, or another caller took it first
    try:
        return _check(taken, home, app_id, risks, kind, recipe, now)
    finally:
        try:
            os.unlink(taken)
        except FileNotFoundError:
            pass
