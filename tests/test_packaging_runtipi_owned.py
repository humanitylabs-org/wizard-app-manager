"""A source root Runtipi (root) owns: the pinned copy is written through the Runtipi container."""
import io
import subprocess
import tarfile

import pytest

from wam import packaging


def test_container_path():
    assert packaging._container_path("/home/u/runtipi/app-data/.wizard-source/wizardapps/food@abc") == \
        "/data/app-data/.wizard-source/wizardapps/food@abc"
    with pytest.raises(packaging.PackagingError):
        packaging._container_path("/tmp/elsewhere")


def test_root_owned_source_root_copies_through_runtipi(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "app").mkdir(parents=True)
    (src / "app" / "server.py").write_text("print(1)\n")
    target = tmp_path / "runtipi" / "app-data" / ".wizard-source" / "wizardapps" / "food@abc"
    real = packaging.copy_source
    calls = []

    def copy(a, b, *k):
        if str(b).startswith(str(target)):
            raise PermissionError(13, "Permission denied")
        return real(a, b, *k)

    def run(argv, stdin=None, **k):
        names = tarfile.open(fileobj=io.BytesIO(stdin.read())).getnames()
        calls.append((argv, names))
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(packaging, "copy_source", copy)
    monkeypatch.setattr(subprocess, "run", run)
    import wam.agent_link as al
    monkeypatch.setattr(al, "_docker", lambda: ["docker"])
    compose = {"services": {"food": {"image": "python:3.12-alpine"}}}
    assert packaging.prepare_package("source", compose, src, target, "food", "abc") is True
    argv, names = calls[0]
    assert argv[:4] == ["docker", "exec", "-i", "runtipi"] and argv[-1] == "/data/app-data/.wizard-source/wizardapps/food@abc"
    assert "./app/server.py" in names
