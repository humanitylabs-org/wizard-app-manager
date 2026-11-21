"""A source root Runtipi (root) owns: the pinned copy is written through the Runtipi container."""
import io
import subprocess
import tarfile

import pytest

from wam import packaging


@pytest.fixture(autouse=True)
def runtipi4_mount(monkeypatch):
    """Runtipi 4 mounts app-data at /app-data inside the runtipi container (#889)."""
    import wam.app_rename as ar
    monkeypatch.setattr(ar, "app_data_dir", lambda: "/app-data")


def test_container_path():
    assert packaging._container_path("/srv/runtipi/app-data/.wizard-source/wizardapps/food@abc") == \
        "/app-data/.wizard-source/wizardapps/food@abc"
    with pytest.raises(packaging.PackagingError):
        packaging._container_path("/tmp/elsewhere")


def test_container_path_follows_the_real_mount(monkeypatch):
    import wam.app_rename as ar
    monkeypatch.setattr(ar, "app_data_dir", lambda: "/data/app-data")   # an older layout that mounts it under /data
    assert packaging._container_path("/srv/runtipi/app-data/x/y") == "/data/app-data/x/y"


def test_container_path_without_app_data_folder_refuses(monkeypatch):
    import wam.app_rename as ar

    def missing():
        raise ar.RenameError("cannot find Runtipi's app-data folder inside the runtipi container")
    monkeypatch.setattr(ar, "app_data_dir", missing)
    with pytest.raises(packaging.PackagingError, match="app-data"):
        packaging._container_path("/srv/runtipi/app-data/x")


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
    assert argv[:4] == ["docker", "exec", "-i", "runtipi"] and argv[-1] == "/app-data/.wizard-source/wizardapps/food@abc"
    assert "./app/server.py" in names
