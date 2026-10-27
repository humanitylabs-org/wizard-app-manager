"""wam runs stock `hermes backup` with two narrow replacements (one-step SQLite copy, vanished files are not errors)."""
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

from wam.backup.box import HERMES_BACKUP_PATCH, HERMES_MAIN, hermes_backup_argv


def _fake_hermes(tmp: Path) -> Path:
    pkg = tmp / "hermes_cli"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "backup_sqlite.py").write_text("def _safe_copy_db(src, dst, *, timeout_seconds=10.0):\n    return 'stock'\n")
    (pkg / "backup.py").write_text(textwrap.dedent("""
        from hermes_cli.backup_sqlite import _safe_copy_db
        def _write_zip_entries(zf, files, out, *, on_db_failure, on_error, on_progress, track_bytes):
            on_error('gone.md', FileNotFoundError('gone'))
            on_error('denied.md', PermissionError('denied'))
            return 0
    """))
    (pkg / "main.py").write_text(textwrap.dedent("""
        import sys
        def main():
            import hermes_cli.backup as hb, sqlite3
            src, dst = sys.argv[2], sys.argv[3]
            ok = hb._safe_copy_db(src, dst)
            errs = []
            hb._write_zip_entries(None, [], None, on_db_failure=None, on_error=lambda r, e: errs.append(r),
                                  on_progress=None, track_bytes=True)
            n = sqlite3.connect(dst).execute('select count(*) from t').fetchone()[0]
            print(ok, errs, n)
            return 0
    """))
    return pkg


def test_patch_copies_db_in_one_step_and_ignores_vanished_files(tmp_path):
    _fake_hermes(tmp_path)
    src, dst = tmp_path / "state.db", tmp_path / "copy.db"
    c = sqlite3.connect(src)
    c.execute("pragma journal_mode=wal")
    c.execute("create table t(x)")
    c.executemany("insert into t values (?)", [(i,) for i in range(5000)])
    c.commit()
    code = "import sys\nsys.path.insert(0, %r)\n" % str(tmp_path) + HERMES_BACKUP_PATCH + HERMES_MAIN
    out = subprocess.run([sys.executable, "-c", code, "backup", str(src), str(dst)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True ['denied.md'] 5000"
    assert (dst.stat().st_mode & 0o777) == 0o600


def test_launcher_shapes(tmp_path):
    sh = tmp_path / "hermes"
    sh.write_text("#!/bin/sh\nexec /opt/py/bin/python3 -I -c 'import sys\nfrom hermes_cli.main import main\n"
                  "sys.exit(main())\n' \"$@\"\n")
    argv = hermes_backup_argv(sh)
    assert argv[:3] == ["/opt/py/bin/python3", "-I", "-c"] and "_one_step_copy" in argv[3]
    assert argv[3].index("_one_step_copy") < argv[3].index("from hermes_cli.main import main")
    py = tmp_path / "hermes2"
    py.write_text("#!/venv/bin/python3\nimport sys\nfrom hermes_cli.main import main\nsys.exit(main())\n")
    assert hermes_backup_argv(py)[:2] == ["/venv/bin/python3", "-c"]
    wrap = tmp_path / "hermes-wrapper"
    wrap.write_text(f'#!/bin/sh\nexec {sh} "$@"\n')
    assert hermes_backup_argv(wrap)[:2] == ["/opt/py/bin/python3", "-I"]
    odd = tmp_path / "hermes3"
    odd.write_text("#!/bin/sh\necho hi\n")
    assert hermes_backup_argv(odd) is None


def test_rootless_docker_found_without_path_or_docker_host(tmp_path):
    """A systemd unit has neither ~/bin on PATH nor DOCKER_HOST: the installer's rootless Docker is still found."""
    import socket
    from wam.backup import dockercli
    home, run = tmp_path / "home", tmp_path / "run"
    (home / "bin").mkdir(parents=True)
    run.mkdir()
    fake = home / "bin" / "docker"
    fake.write_text("#!/bin/sh\n[ \"$1\" = --host ] && exit 0\nexit 1\n")
    fake.chmod(0o755)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(run / "docker.sock"))
    try:
        dockercli._cached.clear()
        got = dockercli.docker({"PATH": "/nonexistent", "HOME": str(home), "XDG_RUNTIME_DIR": str(run)})
        assert got == [str(fake), "--host", f"unix://{run / 'docker.sock'}"]
    finally:
        s.close()
        dockercli._cached.clear()
