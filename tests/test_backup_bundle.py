"""The tar stream inside a backup: unsafe members and checksum problems are refused before anything outside staging
is written (docs/BACKUP.md, "Inside: a tar stream")."""
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wam.backup import bundle, crypto, fmt  # noqa: E402


def _ti(name, data=b"", kind=tarfile.REGTYPE, **kw):
    ti = tarfile.TarInfo(name)
    ti.type = kind
    ti.size = len(data) if kind == tarfile.REGTYPE else 0
    ti.mode = 0o600
    for k, v in kw.items():
        setattr(ti, k, v)
    return ti


def _tar(members, sums="auto", manifest=True) -> bytes:
    """Build a tar stream by hand: members = [(TarInfo, data)], with a matching checksums.json unless told."""
    buf = io.BytesIO()
    entries = {}
    with tarfile.open(fileobj=buf, mode="w|", format=tarfile.PAX_FORMAT) as t:
        if manifest:
            m = json.dumps({"format": 1}).encode()
            t.addfile(_ti("manifest.json", m), io.BytesIO(m))
            entries["manifest.json"] = {"sha256": hashlib.sha256(m).hexdigest(), "size": len(m)}
        for ti, data in members:
            t.addfile(ti, io.BytesIO(data) if ti.isreg() else None)
            if ti.isreg():
                entries[ti.name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
            elif ti.isdir():
                entries[ti.name] = {"type": "dir"}
        if sums == "auto":
            sums = {"entries": entries, "count": len(entries)}
        if sums is not None:
            s = json.dumps(sums).encode() if not isinstance(sums, bytes) else sums
            t.addfile(_ti("checksums.json", s), io.BytesIO(s))
    return buf.getvalue()


def _stage(tmp_path, data: bytes):
    stage, files = tmp_path / "stage", tmp_path / "media" / ".wizard-restore" / "x"
    sink = bundle.StageSink(stage, files)
    return bundle.scan(io.BytesIO(data), sink), stage


@pytest.fixture
def victim(tmp_path):
    v = tmp_path / "victim.txt"
    v.write_text("untouched")
    return v


def _assert_victim(victim):
    assert victim.read_text() == "untouched"
    assert not (victim.parent / "pwned").exists()


def test_good_stream_stages(tmp_path):
    data = _tar([(_ti("wizard-os", kind=tarfile.DIRTYPE), b""), (_ti("wizard-os/settings.json", b"{}"), b"{}"),
                 (_ti("files/a/b.txt", b"hi"), b"hi")])
    res, stage = _stage(tmp_path, data)
    assert (stage / "wizard-os" / "settings.json").read_text() == "{}"
    assert (tmp_path / "media" / ".wizard-restore" / "x" / "a" / "b.txt").read_text() == "hi"
    assert res["files"] == 3


@pytest.mark.parametrize("bad", [
    lambda v: (_ti(str(v.parent / "pwned"), b"x"), b"x"),                         # absolute path
    lambda v: (_ti("wizard-os/../../pwned", b"x"), b"x"),                          # parent path
    lambda v: (_ti("files/../../../pwned", b"x"), b"x"),
    lambda v: (_ti("wizard-os/link", kind=tarfile.SYMTYPE, linkname=str(v)), b""),  # symlink
    lambda v: (_ti("wizard-os/hard", kind=tarfile.LNKTYPE, linkname="wizard-os/settings.json"), b""),
    lambda v: (_ti("wizard-os/dev", kind=tarfile.CHRTYPE, devmajor=1, devminor=3), b""),
    lambda v: (_ti("wizard-os/blk", kind=tarfile.BLKTYPE, devmajor=8, devminor=0), b""),
    lambda v: (_ti("wizard-os/fifo", kind=tarfile.FIFOTYPE), b""),
    lambda v: (_ti("etc/passwd", b"x"), b"x"),                                     # outside the known roots
])
def test_unsafe_members_refused_before_any_write(tmp_path, victim, bad):
    data = _tar([(_ti("wizard-os/settings.json", b"{}"), b"{}"), bad(victim)])
    with pytest.raises(fmt.Damaged):
        _stage(tmp_path, data)
    _assert_victim(victim)


def test_symlink_then_nested_file_pivot_refused(tmp_path, victim):
    """A link to a folder outside staging followed by a file 'inside' it: the link is refused first, and even if a
    link already existed in staging the sink never follows it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    data = _tar([(_ti("files/pivot", kind=tarfile.SYMTYPE, linkname=str(outside)), b""),
                 (_ti("files/pivot/pwned", b"x"), b"x")])
    with pytest.raises(bundle.Unsafe):
        _stage(tmp_path, data)
    assert list(outside.iterdir()) == []
    # defence in depth: a pre-existing link in the staging folder is not followed either
    files_stage = tmp_path / "media2"
    files_stage.mkdir()
    (files_stage / "pivot").symlink_to(outside)
    sink = bundle.StageSink(tmp_path / "s2", files_stage)
    with pytest.raises(bundle.Unsafe):
        sink.write("files/pivot/pwned", io.BytesIO(b"x"), 0o600, 0)
    assert list(outside.iterdir()) == []
    _assert_victim(victim)


def test_checksum_mismatch_missing_and_extra_refused(tmp_path):
    good = [(_ti("wizard-os/a", b"one"), b"one"), (_ti("wizard-os/b", b"two"), b"two")]
    entries = {"manifest.json": None}
    data = _tar(good)
    res = bundle.scan(io.BytesIO(data))
    assert res["files"] == 3
    m = json.dumps({"format": 1}).encode()
    entries = {"manifest.json": {"sha256": hashlib.sha256(m).hexdigest(), "size": len(m)},
               "wizard-os/a": {"sha256": hashlib.sha256(b"one").hexdigest(), "size": 3},
               "wizard-os/b": {"sha256": hashlib.sha256(b"two").hexdigest(), "size": 3}}
    wrong = dict(entries, **{"wizard-os/b": {"sha256": hashlib.sha256(b"TWO").hexdigest(), "size": 3}})
    missing = {k: v for k, v in entries.items() if k != "wizard-os/b"}
    extra = dict(entries, **{"wizard-os/c": {"sha256": "0" * 64, "size": 1}})
    for sums, msg in ((wrong, "do not match"), (missing, "not listed"), (extra, "missing")):
        with pytest.raises(fmt.Damaged, match=msg):
            bundle.scan(io.BytesIO(_tar(good, {"entries": sums, "count": len(sums)})))
    with pytest.raises(fmt.Damaged, match="no checksums"):
        bundle.scan(io.BytesIO(_tar(good, None)))
    with pytest.raises(fmt.Damaged, match="manifest"):
        bundle.scan(io.BytesIO(_tar(good, manifest=False)))


def test_entries_after_checksums_refused():
    buf = io.BytesIO(_tar([(_ti("wizard-os/a", b"1"), b"1")]))
    raw = buf.getvalue().rstrip(b"\x00")
    extra = io.BytesIO()
    with tarfile.open(fileobj=extra, mode="w|") as t:
        t.addfile(_ti("wizard-os/late", b"x"), io.BytesIO(b"x"))
    blocks = len(raw) + (-len(raw) % 512)
    data = buf.getvalue()[:blocks] + extra.getvalue()
    with pytest.raises(fmt.Damaged):
        bundle.scan(io.BytesIO(data))


def test_unsafe_app_archive_refused(tmp_path, victim):
    inner = io.BytesIO()
    with tarfile.open(fileobj=inner, mode="w:gz") as t:
        t.addfile(_ti("./app-data/link", kind=tarfile.SYMTYPE, linkname="/etc"))
    raw = inner.getvalue()
    with pytest.raises(bundle.Unsafe):
        _stage(tmp_path, _tar([(_ti("apps/x/data.tar.gz", raw), raw)]))
    with pytest.raises(bundle.Unsafe):
        bundle.scan(io.BytesIO(_tar([(_ti("apps/x/data.tar.gz", raw), raw)])))
    _assert_victim(victim)


def test_writer_and_scan_round_trip_through_encryption(tmp_path):
    src = tmp_path / "tree"
    (src / "a" / "b").mkdir(parents=True)
    (src / "a" / "b" / "f.txt").write_text("hello")
    (src / "top.bin").write_bytes(b"\x00" * 5000)
    (src / "link").symlink_to(src / "top.bin")
    rk = crypto.new_recovery_key()
    buf = io.BytesIO()
    w = fmt.Writer(buf, fmt.Keys([rk]))
    bw = bundle.BundleWriter(w)
    bw.add_json("manifest.json", {"format": 1})
    bw.add_dir("files")
    bw.add_tree("files", src)
    bw.finish()
    w.close()
    assert bw.skipped == ["files/link"]
    r = fmt.open_reader(io.BytesIO(buf.getvalue()), fmt.Keys([rk]))
    res, stage = None, tmp_path / "stage"
    res = bundle.scan(r, bundle.StageSink(stage, tmp_path / "fs"))
    assert (tmp_path / "fs" / "a" / "b" / "f.txt").read_text() == "hello"
    assert res["files"] == 3


def test_app_archive_symlinks_follow_runtipis_rule():
    """Runtipi's restore accepts symlinks inside app-data/ that stay inside it; so does verify."""
    def arch(linkname, where="./app-data/db/current"):
        inner = io.BytesIO()
        with tarfile.open(fileobj=inner, mode="w:gz") as t:
            t.addfile(_ti("./app-data/db/v1", b"x"), io.BytesIO(b"x"))
            t.addfile(_ti(where, kind=tarfile.SYMTYPE, linkname=linkname))
        return inner.getvalue()
    ok = arch("v1")
    assert bundle.scan(io.BytesIO(_tar([(_ti("apps/x/data.tar.gz", ok), ok)])))["files"] == 2
    for bad in (arch("../../../etc"), arch("/etc/passwd"), arch("v1", "./app/link")):
        with pytest.raises(bundle.Unsafe):
            bundle.scan(io.BytesIO(_tar([(_ti("apps/x/data.tar.gz", bad), bad)])))
