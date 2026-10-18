"""Scheduled backups: schedule set/show/off, systemd units, `run` to a fake S3 server and to a folder, read-back and
restore drill, prune to keep=N, status file, the one-run lock (docs/BACKUP.md, "Scheduled backups")."""
import contextlib
import hashlib
import io
import json
import re
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from backup_fakes import make_source, mode, write_tools  # noqa: E402
from wam.backup import capture, crypto, keys as keylib, schedule  # noqa: E402
from wam.backup import cli as bcli  # noqa: E402
from wam.backup.s3 import S3, S3Error  # noqa: E402

AUTH = re.compile(r"^AWS4-HMAC-SHA256 Credential=AKTEST/\d{8}/[a-z0-9-]+/s3/aws4_request, "
                  r"SignedHeaders=[a-z0-9;-]+, Signature=[0-9a-f]{64}$")


class FakeS3:
    """In-process S3: PUT, multipart (create / upload-part / complete / abort), GET with Range, HEAD,
    ListObjectsV2 and DELETE. Every request must carry a SigV4 Authorization header and a matching payload hash."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.log: list[tuple[str, str]] = []
        self.bad_auth = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self, body: bytes) -> bool:
                ok = bool(AUTH.match(self.headers.get("Authorization", ""))) and self.headers.get("x-amz-date")
                sha = self.headers.get("x-amz-content-sha256")
                ok = ok and sha == hashlib.sha256(body).hexdigest()
                if not ok:
                    outer.bad_auth += 1
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
                return bool(ok)

            def _split(self):
                u = urllib.parse.urlsplit(self.path)
                parts = urllib.parse.unquote(u.path).lstrip("/").split("/", 1)
                return parts[0], (parts[1] if len(parts) > 1 else ""), dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _body(self):
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def do_PUT(self):
                body = self._body()
                if not self._auth(body):
                    return
                bucket, key, q = self._split()
                if "uploadId" in q:
                    outer.uploads[q["uploadId"]][int(q["partNumber"])] = body
                    outer.log.append(("part", key))
                    return self._send(200, headers={"ETag": f'"{hashlib.md5(body).hexdigest()}"'})
                outer.objects[key] = body
                outer.log.append(("put", key))
                self._send(200, headers={"ETag": '"x"'})

            def do_POST(self):
                body = self._body()
                if not self._auth(body):
                    return
                bucket, key, q = self._split()
                if "uploads" in q:
                    uid = f"u{len(outer.uploads) + 1}"
                    outer.uploads[uid] = {}
                    outer.log.append(("create", key))
                    return self._send(200, f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId>"
                                           f"</InitiateMultipartUploadResult>".encode())
                parts = outer.uploads.pop(q["uploadId"])
                nums = [int(n) for n in re.findall(rb"<PartNumber>(\d+)</PartNumber>", body)]
                assert nums == sorted(parts), (nums, sorted(parts))
                outer.objects[key] = b"".join(parts[n] for n in nums)
                outer.log.append(("complete", key))
                self._send(200, b"<CompleteMultipartUploadResult/>")

            def do_HEAD(self):
                if not self._auth(b""):
                    return
                _, key, _ = self._split()
                if key not in outer.objects:
                    return self._send(404)
                self.send_response(200)
                self.send_header("Content-Length", str(len(outer.objects[key])))
                self.end_headers()

            def do_GET(self):
                if not self._auth(b""):
                    return
                bucket, key, q = self._split()
                if not key and q.get("list-type") == "2":
                    keys = sorted(k for k in outer.objects if k.startswith(q.get("prefix", "")))
                    start = int(q.get("continuation-token") or 0)
                    page, more = keys[start:start + 2], start + 2 < len(keys)   # tiny pages: exercise paging
                    xml = "".join(f"<Contents><Key>{k}</Key><Size>{len(outer.objects[k])}</Size></Contents>" for k in page)
                    xml = (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{xml}'
                           f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
                           + (f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else "")
                           + "</ListBucketResult>")
                    outer.log.append(("list", q.get("prefix", "")))
                    return self._send(200, xml.encode())
                if key not in outer.objects:
                    return self._send(404, b"<Error><Code>NoSuchKey</Code></Error>")
                data = outer.objects[key]
                rng = self.headers.get("Range")
                if rng:
                    a, b = re.fullmatch(r"bytes=(\d+)-(\d+)", rng).groups()
                    outer.log.append(("range", key))
                    return self._send(206, data[int(a):int(b) + 1])
                outer.log.append(("get", key))
                self._send(200, data)

            def do_DELETE(self):
                if not self._auth(b""):
                    return
                _, key, q = self._split()
                if "uploadId" in q:
                    outer.uploads.pop(q["uploadId"], None)
                else:
                    outer.objects.pop(key, None)
                    outer.log.append(("delete", key))
                self._send(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for k, v in {"HOME": tmp_path / "proc-home", "WIZARD_OS_HOME": tmp_path / "proc-home/.wizard-os",
                 "RUNTIPI_ROOT": tmp_path / "proc-home/runtipi", "HERMES_HOME": tmp_path / "proc-home/.hermes",
                 "TMPDIR": tmp_path}.items():
        monkeypatch.setenv(k, str(v))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")


@pytest.fixture
def s3():
    srv = FakeS3()
    yield srv
    srv.close()


@pytest.fixture
def box(tmp_path, monkeypatch):
    tools = write_tools(tmp_path / "tools")
    b = make_source(tmp_path, tools)
    (b.home / "backup.json").unlink()                      # start without a schedule
    keylib.store_key(b.home, crypto.new_recovery_key())
    b.env.update({"WIZARD_S3_ACCESS_KEY_ID": "AKTEST", "WIZARD_S3_SECRET_ACCESS_KEY": "s3-secret-value"})
    monkeypatch.setenv("WAM_BACKUP_S3_PART_SIZE", str(64 << 10))   # small parts: the fixture uploads in several
    n = {"i": 0}

    def name(bx, folder=None):
        n["i"] += 1
        return Path(folder or Path.cwd()) / f"oldbox-20261018-0300{n['i']:02d}.wizardbackup"
    monkeypatch.setattr(capture, "default_output", name)
    # big enough files that the bundle needs several 64 KiB parts
    (b.runtipi / "media" / "big.bin").write_bytes(bytes(range(256)) * 1200)
    return b


def _cli(bx, *argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = bcli.main([*argv, "--json"], box=bx)
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 1, buf.getvalue()
    return code, json.loads(lines[0])


def test_s3_client_basics(s3):
    c = S3(s3.url, "bucket", "AKTEST", "secret")
    import tempfile
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"x" * 300_000)
        f.flush()
        c.part_size = 100_000
        res = c.put_file("p/a.wizardbackup", Path(f.name))
        assert res["parts"] == 3 and s3.objects["p/a.wizardbackup"] == b"x" * 300_000
        c.part_size = 1 << 20
        assert c.put_file("p/b.wizardbackup", Path(f.name))["parts"] == 1
    assert c.head("p/a.wizardbackup")["bytes"] == 300_000
    assert c.get_range("p/a.wizardbackup", 10, 19) == b"x" * 10
    with c.open("p/b.wizardbackup") as body:
        assert len(body.read()) == 300_000
    assert [o["key"] for o in c.list("p/")] == ["p/a.wizardbackup", "p/b.wizardbackup"]
    c.delete("p/a.wizardbackup")
    assert "p/a.wizardbackup" not in s3.objects
    bad = S3(s3.url, "bucket", "WRONG", "secret")
    with pytest.raises(S3Error, match="403"):
        bad.head("p/b.wizardbackup")
    assert s3.bad_auth == 1


def test_schedule_set_show_off_and_units(box, s3):
    bx = box.box()
    code, res = _cli(bx, "schedule", "set", "--s3", f"{s3.url}/bucket", "--keep", "2", "--at", "04:15")
    assert code == 0 and res["enabled"] and res["keep"] == 2 and res["at"] == "04:15"
    assert res["target"]["prefix"] == f"wizard/{res['box_id']}/"
    cfg_text = (box.home / "backup.json").read_text()
    assert "s3-secret-value" not in cfg_text and "AKTEST" not in cfg_text
    sec = box.home / "secrets" / "backup-target.json"
    assert mode(sec) == 0o600 and json.loads(sec.read_text())["secret_access_key"] == "s3-secret-value"
    units = Path(box.env["HOME"]) / ".config/systemd/user"
    timer = (units / "wizard-backup.timer").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in timer and "Persistent=true" in timer
    assert "backup run --scheduled" in (units / "wizard-backup.service").read_text()
    assert ["daemon-reload"] in box.systemctl_calls and ["enable", "--now", "wizard-backup.timer"] in box.systemctl_calls
    code, res = _cli(bx, "schedule", "show")
    assert code == 0 and res["enabled"] and res["credentials_stored"] and res["key_stored"]
    assert "s3-secret-value" not in json.dumps(res)
    code, res = _cli(bx, "schedule", "off")
    assert code == 0 and res["enabled"] is False
    assert ["disable", "--now", "wizard-backup.timer"] in box.systemctl_calls
    code, res = _cli(bx, "run", "--scheduled")
    assert code == 0 and res["skipped"] == "scheduled backups are off"


def test_systemctl_is_never_run_in_a_test_env(box):
    from wam.backup.box import Box
    b = Box(env=box.env, rt=box.rt)          # no injected systemctl: the test-env guard must hold
    assert schedule.systemctl(b, ["daemon-reload"]) is False


def test_run_to_s3_uploads_verifies_prunes_and_records_status(box, s3):
    bx = box.box()
    _cli(bx, "schedule", "set", "--s3", f"{s3.url}/bucket/wizard/test-box", "--keep", "2")
    for _ in range(3):
        code, res = _cli(bx, "run", "--scheduled")
        assert code == 0 and res["ok"], res
    keys = sorted(k for k in s3.objects if k.startswith("wizard/test-box/"))
    assert keys == ["wizard/test-box/oldbox-20261018-030002.wizardbackup",
                    "wizard/test-box/oldbox-20261018-030003.wizardbackup"]     # newest kept
    kinds = [k for k, _ in s3.log]
    assert "create" in kinds and kinds.count("part") >= 6 and "complete" in kinds   # multipart was used
    assert "range" in kinds and "get" in kinds and "delete" in kinds and s3.bad_auth == 0
    st = json.loads((box.home / "backup-status.json").read_text())
    assert st["ok"] and st["drill"]["ok"] and st["object"] == keys[-1] and st["last_success_at"]
    assert st["pruned"] == ["wizard/test-box/oldbox-20261018-030001.wizardbackup"]
    assert st["bytes"] == len(s3.objects[keys[-1]])
    assert not list((box.home / "backup-work").glob("*.wizardbackup"))     # the local copy is removed
    code, res = _cli(bx, "status")
    assert res["last"]["ok"] and res["enabled"]


def test_restore_drill_catches_a_corrupted_upload(box, s3, monkeypatch):
    bx = box.box()
    _cli(bx, "schedule", "set", "--s3", f"{s3.url}/bucket/p", "--keep", "3")
    real = S3.open

    def corrupt(self, key):
        data = bytearray(s3.objects[key])
        data[len(data) // 2] ^= 1
        s3.objects[key] = bytes(data)
        return real(self, key)
    monkeypatch.setattr(S3, "open", corrupt)
    code, res = _cli(bx, "run")
    assert code == 1 and not res["ok"] and "damaged" in res["error"]
    st = json.loads((box.home / "backup-status.json").read_text())
    assert st["ok"] is False and st["error"] and st["last_success_at"] is None


def test_run_to_a_folder(box, tmp_path):
    bx = box.box()
    dest = tmp_path / "external-drive" / "wizard"
    code, res = _cli(bx, "schedule", "set", "--path", str(dest), "--keep", "2")
    assert code == 0
    for _ in range(3):
        code, res = _cli(bx, "run")
        assert code == 0 and res["ok"] and res["drill"]["ok"], res
    assert sorted(p.name for p in dest.iterdir()) == ["oldbox-20261018-030002.wizardbackup",
                                                      "oldbox-20261018-030003.wizardbackup"]
    assert all(mode(p) == 0o600 for p in dest.iterdir())


def test_second_concurrent_run_exits_cleanly(box, tmp_path):
    bx = box.box()
    _cli(bx, "schedule", "set", "--path", str(tmp_path / "dest"))
    with schedule.lock(bx):
        code, res = _cli(bx, "run")
        assert code == 0 and res["ok"] and "another backup" in res["skipped"]
        code, res = _cli(bx, "import", str(tmp_path / "nothing.wizardbackup"), "--yes")
        assert code == 1
    assert not (tmp_path / "dest").exists() or not list((tmp_path / "dest").iterdir())
    code, res = _cli(bx, "run")
    assert code == 0 and res["ok"] and "skipped" not in res


def test_schedule_set_refusals(box, s3, tmp_path):
    bx = box.box()
    code, res = _cli(bx, "schedule", "set")
    assert code == 1 and "--s3" in res["error"]
    code, res = _cli(bx, "schedule", "set", "--path", str(tmp_path / "x"), "--at", "25:00")
    assert code == 1 and "HH:MM" in res["error"]
    del box.env["WIZARD_S3_SECRET_ACCESS_KEY"]
    (box.home / "secrets" / "backup-target.json").unlink()
    code, res = _cli(box.box(), "schedule", "set", "--s3", f"{s3.url}/bucket")
    assert code == 1 and "credentials" in res["error"]
    (box.home / "secrets" / "backup-key.json").unlink()
    code, res = _cli(box.box(), "schedule", "set", "--path", str(tmp_path / "never"))
    assert code == 1 and "key new" in res["error"] and not (tmp_path / "never").exists()
