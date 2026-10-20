"""One-time owner approval link for a named Hermes owner whose sudo needs a password (no terminal, agent-run).

The real get.sh runs detached on the fake box from test_get_sh_any_user. A local fake relay speaks the
www.aiwizards.com/os-approve box API (create / poll once / result / cancel); the test plays the owner's browser by
encrypting the password to the box's public key with RSA-OAEP-SHA256 and the request-bound label, exactly like the
real page. `sudo` is a stub that checks the password it is fed on stdin (-S) and then runs the root step, which is
a fake host preparation fetched through a stubbed curl. Cases: no root needed, approved, wrong password then a
fresh link, expired link, relay down, not a sudoer (group and sudoers), tries used up. Each asserts the password
never reaches a file, the log, the status or a command line.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from test_get_sh_any_user import Box, _local_wam

PASSWORD = "correct horse 7!"  # a test fixture value, not a real credential
LABEL = "wizard-os-approve/v1:"


class Relay:
    """In-memory stand-in for the relay's box API (same states as src/approve.js)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.lock = threading.Lock()
        relay = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                code, out = relay.handle(self.path.rsplit("/", 1)[-1], body)
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/os-approve"

    def handle(self, op, b):
        with self.lock:
            if op == "create":
                self.rows[b["id"]] = {**b, "state": "pending", "ct": None}
                return 201, {"id": b["id"], "ttl": 1800}
            row = self.rows.get(b.get("id"))
            if not row or hashlib.sha256(b.get("pickup", "").encode()).hexdigest() != row["pickup_hash"]:
                return 404, {"error": "not found"}
            if op == "poll":
                if row["state"] == "submitted":
                    ct, row["ct"], row["state"] = row["ct"], None, "picked_up"
                    return 200, {"state": "submitted", "ct": ct}
                return 200, {"state": row["state"]}
            if op == "result":
                row["state"] = b["outcome"]
                return 200, {"state": b["outcome"]}
            if op == "cancel":
                row["state"] = "cancelled"
                return 200, {"state": "cancelled"}
        return 404, {}

    def submit(self, cap: str, password: str, tmp: Path) -> None:
        """The owner's browser: encrypt to the box key (label bound to the id) and post only ciphertext."""
        rid = hashlib.sha256(cap.encode()).hexdigest()
        row = self.rows[rid]
        der = tmp / f"pub-{rid[:8]}.der"
        der.write_bytes(base64.b64decode(row["pubkey"]))
        ct = subprocess.run(
            ["openssl", "pkeyutl", "-encrypt", "-pubin", "-keyform", "DER", "-inkey", str(der),
             "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256",
             "-pkeyopt", "rsa_oaep_label:" + (LABEL + rid).encode().hex()],
            input=password.encode(), capture_output=True, check=True).stdout
        with self.lock:
            row["ct"], row["state"] = base64.b64encode(ct).decode(), "submitted"

    def expire(self, cap: str) -> None:
        with self.lock:
            self.rows[hashlib.sha256(cap.encode()).hexdigest()]["state"] = "expired"


# sudo: `-n true` always needs a password; `-S` reads it from stdin and then runs the command after `--`.
SUDO = r"""#!/bin/bash
if [ "$1" = -n ]; then echo "sudo: a password is required" >&2; exit 1; fi
[ "$1" = -S ] || { echo "sudo $*" >> "$FAKE_LOG"; exit 1; }
IFS= read -r pw || pw=""
while [ $# -gt 0 ] && [ "$1" != -- ]; do shift; done; shift
if [ -n "${FAKE_NOT_SUDOER:-}" ]; then echo "$USER is not in the sudoers file." >&2; exit 1; fi
if [ "$pw" != "$FAKE_SUDO_PASS" ]; then echo "Sorry, try again." >&2; echo "sudo: 1 incorrect password attempt" >&2; exit 1; fi
echo "sudo-S ok: $*" >> "$FAKE_LOG"
exec env SUDO_FAKE_ROOT=1 "$@"
"""
# curl: the root step's official get.sh is a fake host preparation; everything else is recorded.
CURL = r"""#!/bin/bash
echo "curl $*" >> "$FAKE_LOG"
for a; do :; done
case "$a" in *prep-get.sh) cat "$FAKE_PREP" ;; esac
exit 0
"""
PREP = r"""echo "PREP owner=$WIZARD_HERMES_USER only=$WIZARD_HOST_PREP_ONLY root=$SUDO_FAKE_ROOT" >> "$FAKE_LOG"
echo "==> Preparing the server"
mkdir -p "$FAKE_OWNER_HOME/runtipi" && echo X=1 > "$FAKE_OWNER_HOME/runtipi/.env"
echo "Administrator host preparation finished; continuing as $WIZARD_HERMES_USER."
exit ${FAKE_PREP_RC:-0}
"""


@pytest.fixture
def relay():
    r = Relay()
    yield r
    r.srv.shutdown()


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    for name, text in (("sudo", SUDO), ("curl", CURL)):
        (b.bin / name).write_text(text); (b.bin / name).chmod(0o755)
    (b.bin / "systemd-run").write_text(
        "#!/bin/bash\necho \"systemd-run $*\" >> \"$FAKE_LOG\"\n"
        "while [ $# -gt 0 ] && [ \"$1\" != /bin/bash ]; do shift; done\n"
        "[ $# -gt 0 ] || exit 0\nsetsid nohup \"$@\" </dev/null >/dev/null 2>&1 &\nexit 0\n")
    (b.bin / "systemd-run").chmod(0o755)
    (tmp_path / "prep.sh").write_text(PREP)
    b.add_user("dana", 1000)
    b.hermes("dana")
    return b


def _env(box: Box, tmp: Path, relay_url: str, **extra) -> dict:
    src = _local_wam(tmp, "echo '==> Installing the App Manager'\necho \"installed as $(id -un)\"\n")
    e = {"WAM_REPO": str(src), "WAM_REF": "main", "WAM_DIR": str(tmp / "wam"), "WIZARD_APPROVE_URL": relay_url,
         "WIZARD_APPROVE_INTERVAL": "0.1", "WIZARD_APPROVE_DOWN_AFTER": "1", "WIZARD_GET_SH_URL": "file:///prep-get.sh",
         "FAKE_PREP": str(tmp / "prep.sh"), "FAKE_OWNER_HOME": str(box.users["dana"][1]), "FAKE_GROUPS": "sudo",
         "FAKE_SUDO_PASS": PASSWORD}
    e.update(extra)
    return e


def _status(box: Box, env: dict) -> list[str]:
    return box.run("dana", "status", **env).stdout.splitlines()


def _wait_for(box: Box, env: dict, pred, timeout: float = 40) -> list[str]:
    end = time.time() + timeout
    out: list[str] = []
    while time.time() < end:
        out = _status(box, env)
        if pred(out):
            return out
        time.sleep(0.2)
    raise AssertionError("status never matched:\n" + "\n".join(out) + "\n--- log ---\n" +
                         (box.users["dana"][1] / ".wizard-os/install.log").read_text(errors="replace")[-3000:])


def _link(out: list[str], relay: Relay) -> str:
    i = next(n for n, l in enumerate(out) if l.startswith("Owner action: Open this link and enter your server password once"))
    url = out[i + 1]
    assert url.startswith(relay.url + "#")
    return url.split("#", 1)[1]


def _no_secret_anywhere(box: Box) -> None:
    for f in list(box.tmp.rglob("*")):
        if f.is_file() and f.name not in {"test_owner_approval.py"}:
            try:
                assert PASSWORD.encode() not in f.read_bytes(), f"password leaked into {f}"
            except PermissionError:
                pass


def test_approved_link_runs_only_the_root_step_then_finishes_as_owner(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url)
    p = box.run("dana", "start", **env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "sudo env" not in p.stderr, "the agent no longer gets the terminal command first"
    out = _wait_for(box, env, lambda o: any(l.startswith("Owner action: Open this link") for l in o))
    assert out[0] == "Wizard OS install: needs_owner"
    assert "Owner action: Open this link and enter your server password once to approve the Wizard OS install:" in out
    cap = _link(out, relay)
    row = relay.rows[hashlib.sha256(cap.encode()).hexdigest()]
    assert row["user"] == "dana" and "runtipi" in row["needs"]
    relay.submit(cap, PASSWORD, tmp_path)
    out = _wait_for(box, env, lambda o: o and o[0] in ("Wizard OS install: done", "Wizard OS install: failed",
                                                       "Wizard OS install: needs_admin"))
    assert out[0] == "Wizard OS install: done", "\n".join(out)
    log = box.log.read_text()
    assert "PREP owner=dana only=1 root=1" in log
    sudo_calls = [l for l in log.splitlines() if l.startswith("sudo-S ok:")]
    assert len(sudo_calls) == 1 and "prep-get.sh" in sudo_calls[0]
    ilog = (box.users["dana"][1] / ".wizard-os/install.log").read_text()
    assert "installed as dana" in ilog and "Administrator host preparation" in ilog
    assert row["state"] == "approved" and row["ct"] is None, "the relay keeps no ciphertext after pickup"
    st = json.loads((box.users["dana"][1] / ".wizard-os/install-status.json").read_text())
    assert not st.get("approval_url")
    _no_secret_anywhere(box)


def test_wrong_password_shows_a_fresh_link_then_approves(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url)
    assert box.run("dana", "start", **env).returncode == 0
    first = _link(_wait_for(box, env, lambda o: any(l.startswith("Owner action: Open this link") for l in o)), relay)
    relay.submit(first, "not the password", tmp_path)
    out = _wait_for(box, env, lambda o: any(l.startswith(relay.url + "#") and l.split("#")[1] != first for l in o))
    assert "Note: That password was not accepted; a fresh approval link is shown." in out
    second = _link(out, relay)
    assert relay.rows[hashlib.sha256(first.encode()).hexdigest()]["state"] == "wrong_password"
    relay.submit(second, PASSWORD, tmp_path)
    out = _wait_for(box, env, lambda o: o and o[0] in ("Wizard OS install: done", "Wizard OS install: failed",
                                                       "Wizard OS install: needs_admin"))
    assert out[0] == "Wizard OS install: done", "\n".join(out)
    assert sum(l.startswith("sudo-S ok:") for l in box.log.read_text().splitlines()) == 1
    _no_secret_anywhere(box)


def test_expired_link_is_replaced_by_a_fresh_one(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url)
    assert box.run("dana", "start", **env).returncode == 0
    first = _link(_wait_for(box, env, lambda o: any(l.startswith("Owner action: Open this link") for l in o)), relay)
    relay.expire(first)
    out = _wait_for(box, env, lambda o: any(l.startswith(relay.url + "#") and l.split("#")[1] != first for l in o))
    assert "Note: The approval link expired; a fresh one is shown." in out
    relay.submit(_link(out, relay), PASSWORD, tmp_path)
    out = _wait_for(box, env, lambda o: o and o[0] in ("Wizard OS install: done", "Wizard OS install: failed"))
    assert out[0] == "Wizard OS install: done"


def _closed_port_url() -> str:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return f"http://127.0.0.1:{port}/os-approve"


def _expect_terminal_fallback(box: Box, env: dict, why: str) -> list[str]:
    out = _wait_for(box, env, lambda o: o and o[0] in ("Wizard OS install: needs_admin", "Wizard OS install: done",
                                                       "Wizard OS install: failed"))
    assert out[0] == "Wizard OS install: needs_admin", "\n".join(out)
    assert why in out[1]
    cmd = out[2]
    assert cmd.startswith("curl -fsSL https://aiwizards.com/os.sh | sudo env WIZARD_HERMES_USER=dana HERMES_HOME=")
    assert cmd.endswith("WIZARD_AGENT_SAFE=1 bash -s start")
    assert not any(l.startswith("sudo-S ok:") for l in box.log.read_text().splitlines())
    return out


def test_relay_down_falls_back_to_the_terminal_command(box, tmp_path):
    env = _env(box, tmp_path, _closed_port_url())
    assert box.run("dana", "start", **env).returncode == 0
    _expect_terminal_fallback(box, env, "the approval link is unavailable right now")


def test_tries_used_up_fall_back_to_the_terminal_command(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url, WIZARD_APPROVE_TRIES="2")
    assert box.run("dana", "start", **env).returncode == 0
    first = _link(_wait_for(box, env, lambda o: any(l.startswith("Owner action: Open this link") for l in o)), relay)
    relay.submit(first, "nope 1", tmp_path)
    out = _wait_for(box, env, lambda o: any(l.startswith(relay.url + "#") and l.split("#")[1] != first for l in o))
    relay.submit(_link(out, relay), "nope 2", tmp_path)
    _expect_terminal_fallback(box, env, "not completed after 2 tries")


def test_sudoers_refusal_says_an_administrator_must_run_the_command(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url, FAKE_NOT_SUDOER="1")
    assert box.run("dana", "start", **env).returncode == 0
    cap = _link(_wait_for(box, env, lambda o: any(l.startswith("Owner action: Open this link") for l in o)), relay)
    relay.submit(cap, PASSWORD, tmp_path)
    out = _expect_terminal_fallback(box, env, "is not allowed to use sudo")
    assert "An administrator of this server must run this once" in out[1]
    assert relay.rows[hashlib.sha256(cap.encode()).hexdigest()]["state"] == "not_sudoer"
    _no_secret_anywhere(box)


def test_account_outside_the_sudo_group_gets_the_administrator_command_at_once(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url, FAKE_GROUPS="")
    p = box.run("dana", "start", **env)
    assert p.returncode == 2
    assert "not in this server's sudo group" in p.stderr and "An administrator of this server must run this once" in p.stderr
    assert "sudo env WIZARD_HERMES_USER=dana" in p.stderr
    assert relay.rows == {} and not (box.users["dana"][1] / ".wizard-os").exists()


def test_no_root_needed_starts_without_any_approval(box, relay, tmp_path):
    home = box.users["dana"][1]
    (home / "runtipi").mkdir()
    (home / "runtipi/.env").write_text("X=1\n")
    bus = tmp_path / "bus"
    s = socket.socket(socket.AF_UNIX); s.bind(str(bus))
    try:
        src = (box.tmp / "get-ready.sh")
        src.write_text(Path(__file__).resolve().parents[1].joinpath("get.sh").read_text()
                       .replace('"/run/user/$(id -u)/bus"', f'"{bus}"'))
        env = _env(box, tmp_path, relay.url)
        pre = box.run("dana", "--check", script=src, **env)
        assert "needs root once:   no (Docker, the runtime, linger and Tailscale operator are already in place)" in pre.stdout
        assert box.run("dana", "start", script=src, **env).returncode == 0
        out = _wait_for(box, env, lambda o: o and o[0] in ("Wizard OS install: done", "Wizard OS install: failed",
                                                           "Wizard OS install: needs_admin", "Wizard OS install: needs_owner"))
        assert out[0] == "Wizard OS install: done", "\n".join(out)
        assert relay.rows == {} and "sudo-S" not in box.log.read_text()
    finally:
        s.close()


def test_preflight_names_exactly_the_pieces_that_need_root(box, relay, tmp_path):
    env = _env(box, tmp_path, relay.url)
    pre = box.run("dana", "--check", **env)
    line = next(l for l in pre.stdout.splitlines() if l.startswith("needs root once:"))
    assert line == "needs root once:   runtipi (the agent's start shows the owner a one-time approval link)"
    assert not (box.users["dana"][1] / ".wizard-os").exists()
