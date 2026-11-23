"""wizard-app-manager#868: one dropped connection (curl exit 35, a TLS reset) failed a fresh install outright.

Every installer download now goes through `wizard_fetch` (into a file, tried again), and the third-party setup
scripts we run (Runtipi's, Docker's) get a curl config that makes their own downloads try again too.
"""
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading

import pytest

REPO = Path(__file__).resolve().parents[1]
COPIES = ["install.sh", "get.sh", "scripts/ensure-base-docker.sh", "scripts/prepare-named-user.sh",
          "scripts/setup-private-access.sh"]


def block(path, name):
    """The text of shell function <name> in <path>."""
    text = (REPO / path).read_text()
    m = re.search(r"^" + name + r"\(\) \{[^\n]*\n.*?^\}\n", text, re.M | re.S)
    assert m, f"{path} has no {name}()"
    return m.group(0)


def test_every_installer_has_the_same_fetch_and_no_unretried_download():
    fetch = {block(p, "wizard_fetch") for p in COPIES}
    assert len(fetch) == 1, "wizard_fetch differs between the installers"
    curlrc = {block(p, "retry_curlrc") for p in ["install.sh", "scripts/ensure-base-docker.sh",
                                                 "scripts/prepare-named-user.sh"]}
    assert len(curlrc) == 1
    for p in COPIES:
        for line in (REPO / p).read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            # the one-shot downloads of setup scripts / the installer itself: never a bare curl any more
            for host in ("setup.runtipi.io", "get.docker.com", "tailscale.com/install.sh", "repo_raw "):
                if host in line and "curl " in line and "echo" not in line and "printf" not in line:
                    pytest.fail(f"{p}: unretried download: {line.strip()}")


def fake_bin(tmp_path, curl_body):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/bash\n" + curl_body)
    (bin_dir / "curl").chmod(0o755)
    (bin_dir / "sleep").write_text('#!/bin/sh\necho "sleep $*" >> "$CALLS"\n')
    (bin_dir / "sleep").chmod(0o755)
    return bin_dir


def run_fetch(tmp_path, fails, tries=None):
    calls = tmp_path / "calls"
    bin_dir = fake_bin(tmp_path, f'''echo "curl $*" >> "$CALLS"
n=$(grep -c '^curl' "$CALLS")
if [ "$n" -le {fails} ]; then echo "curl: (35) TLS connect error" >&2; exit 35; fi
printf 'payload\\n' > "$4"
''')
    script = block("install.sh", "wizard_fetch") + 'set -euo pipefail\nwizard_fetch https://setup.runtipi.io "$OUT"\n'
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "OUT": str(tmp_path / "out")}
    if tries:
        env["WIZARD_FETCH_TRIES"] = str(tries)
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    return p, calls.read_text().splitlines()


def test_a_dropped_connection_is_tried_again(tmp_path):
    p, calls = run_fetch(tmp_path, fails=2)
    assert p.returncode == 0, p.stderr
    assert [c for c in calls if c.startswith("curl")] == ["curl -fsSL https://setup.runtipi.io -o " + str(tmp_path / "out")] * 3
    assert [c for c in calls if c.startswith("sleep")] == ["sleep 3", "sleep 6"]
    assert (tmp_path / "out").read_text() == "payload\n"
    assert "try 1 of 5" in p.stderr and "curl exit 35" in p.stderr


def test_it_gives_up_with_curls_exit_code_after_the_last_try(tmp_path):
    p, calls = run_fetch(tmp_path, fails=99, tries=3)
    assert p.returncode == 35
    assert len([c for c in calls if c.startswith("curl")]) == 3
    assert len([c for c in calls if c.startswith("sleep")]) == 2


class FlakyServer:
    """Resets the first <drops> connections (like the TLS reset in the issue), then serves a small file."""

    def __init__(self, drops):
        self.drops, self.seen = drops, 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.seen += 1
            c.recv(4096)
            if self.seen <= self.drops:
                c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
                c.close()   # RST: curl reports a failed receive, not an HTTP answer
                continue
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nok\n")
            c.close()


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_the_curl_config_makes_a_third_party_scripts_plain_curl_retry(tmp_path):
    """The Runtipi setup script downloads runtipi-cli with a plain `curl --location URL -o file`."""
    rc_dir = tmp_path / "rc"
    rc_dir.mkdir()
    script = block("install.sh", "retry_curlrc") + 'retry_curlrc "$1"\n'
    subprocess.run(["bash", "-c", script, "x", str(rc_dir)], check=True, timeout=20)
    config = (rc_dir / ".curlrc").read_text()
    assert "retry = 5" in config and "retry-connrefused" in config
    srv = FlakyServer(drops=1)
    url = f"http://127.0.0.1:{srv.port}/runtipi-cli.tar.gz"
    plain = subprocess.run(["curl", "-q", "--location", url, "-o", str(tmp_path / "a")], capture_output=True,
                           timeout=20)
    assert plain.returncode != 0, "without the config, one reset fails the download"
    if "retry-all-errors" not in config:
        pytest.skip("this curl is older than 7.71: only timeouts are retried")
    srv.seen, srv.drops = 0, 1
    p = subprocess.run(["curl", "--location", url, "-o", str(tmp_path / "b")], capture_output=True, text=True,
                       timeout=60, env={"PATH": "/usr/bin:/bin", "CURL_HOME": str(rc_dir), "HOME": str(tmp_path)})
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "b").read_text() == "ok\n"
    assert srv.seen == 2


@pytest.mark.parametrize("codes,want_rc,want_tries", [
    (["000", "429", "200"], 0, 3),     # a reset, then rate-limited, then the file
    (["503", "408", "200"], 0, 3),
    (["404"], 4, 1),                   # not published is an answer: never retried
    (["403"], 1, 1),
    (["000", "000", "000", "000"], 1, 4),
])
def test_release_metadata_is_tried_again_only_on_transient_answers(tmp_path, codes, want_rc, want_tries):
    calls = tmp_path / "calls"
    (tmp_path / "codes").write_text("\n".join(codes) + "\n")
    bin_dir = fake_bin(tmp_path, '''echo "curl $*" >> "$CALLS"
n=$(grep -c '^curl' "$CALLS")
code=$(sed -n "${n}p" "$CODES")
[ "$code" = 000 ] && exit 35
printf '%s' "$code"
''')
    script = ('set -euo pipefail\nRELEASE_BASE=https://raw.example.test/releases\n' + block("get.sh", "fetch_release")
              + 'rc=0; fetch_release stable "$OUT" || rc=$?; echo "rc=$rc"\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "CALLS": str(calls), "OUT": str(tmp_path / "out"),
           "CODES": str(tmp_path / "codes")}
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert f"rc={want_rc}" in p.stdout, p.stdout + p.stderr
    assert len([c for c in calls.read_text().splitlines() if c.startswith("curl")]) == want_tries
