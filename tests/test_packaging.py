import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wam import packaging  # noqa: E402

SHA = "b" * 40


@pytest.fixture
def checkout(tmp_path):
    src = tmp_path / "checkout"
    (src / ".git").mkdir(parents=True)
    (src / ".git" / "HEAD").write_text("ref")
    (src / "app").mkdir()
    (src / "app" / "server.py").write_text("print(1)")
    (src / "app" / "Dockerfile").write_text("FROM x")
    return src


def target(tmp_path):
    return packaging.source_dir(tmp_path / "app-data", "wizardapps", "demo", SHA)


def test_source_copies_pinned_code_without_git(tmp_path, checkout):
    compose = {"services": {"demo": {"image": "python@sha256:1"}}}
    assert packaging.prepare_package("source", compose, checkout, target(tmp_path), "demo", SHA)
    assert (target(tmp_path) / "app" / "server.py").read_text() == "print(1)"
    assert not (target(tmp_path) / ".git").exists()
    assert target(tmp_path).name == f"demo@{SHA[:12]}"


def test_legacy_and_image_copy_nothing(tmp_path, checkout):
    c = {"services": {"demo": {"image": "x"}}}
    assert not packaging.prepare_package(None, c, checkout, target(tmp_path), "demo", SHA)
    assert not packaging.prepare_package("image", c, checkout, target(tmp_path), "demo", SHA)
    assert not target(tmp_path).exists()


def test_build_points_at_copy_with_commit_image(tmp_path, checkout):
    c = {"services": {"web": {"build": {"context": "app"}}}}
    packaging.prepare_package("build", c, checkout, target(tmp_path), "demo", SHA)
    assert c["services"]["web"] == {"build": {"context": str(target(tmp_path) / "app")},
                                    "image": f"wizard-local/demo-web:{SHA[:12]}", "pull_policy": "build"}


@pytest.mark.parametrize("pkg,compose,msg", [
    ("source", {"services": {"d": {"image": "x", "build": "."}}}, "remove build:"),
    ("image", {"services": {"d": {"image": "x", "build": "."}}}, "remove build:"),
    ("build", {"services": {"d": {"image": "x"}}}, "need a build"),
    ("docker", {"services": {}}, "source, build or image"),
    ("build", {"services": {"d": {"build": {"context": "/etc"}}}}, "inside the repo"),
    ("build", {"services": {"d": {"build": {"context": ".."}}}}, "inside the repo"),
    ("build", {"services": {"d": {"build": {"context": "app", "dockerfile": "../../x"}}}}, "inside the context"),
])
@pytest.mark.parametrize("build_root", [None, f"/app-data/.wizard-source/wizardapps/demo@{SHA[:12]}"])
def test_wrong_shapes_rejected(tmp_path, checkout, pkg, compose, msg, build_root):
    with pytest.raises(packaging.PackagingError, match=msg):
        packaging.prepare_package(pkg, compose, checkout, target(tmp_path), "demo", SHA, build_root=build_root)


def test_symlinks_and_size_cap(tmp_path, checkout):
    os.symlink("/etc/passwd", checkout / "app" / "leak")
    with pytest.raises(packaging.PackagingError, match="outside the app"):
        packaging.copy_source(checkout, tmp_path / "out")
    (checkout / "app" / "leak").unlink()
    os.symlink("server.py", checkout / "app" / "alias.py")
    packaging.copy_source(checkout, tmp_path / "ok")
    assert os.readlink(tmp_path / "ok" / "app" / "alias.py") == "server.py"
    with pytest.raises(packaging.PackagingError, match="larger than"):
        packaging.copy_source(checkout, tmp_path / "big", limit=4)


def test_app_data_root_follows_env(tmp_path):
    assert packaging.app_data_root(tmp_path) == tmp_path / "app-data"
    (tmp_path / ".env").write_text("RUNTIPI_APP_DATA_PATH=/srv/data/app-data\n")
    assert packaging.app_data_root(tmp_path) == Path("/srv/data/app-data")


def test_prune_keeps_current_and_other_apps(tmp_path):
    root = tmp_path / "app-data"
    for n in ("demo@aaaaaaaaaaaa", f"demo@{SHA[:12]}", "other@aaaaaaaaaaaa"):
        (root / ".wizard-source" / "wizardapps" / n).mkdir(parents=True)
    packaging.prune(root, "wizardapps", "demo", f"demo@{SHA[:12]}")
    assert sorted(p.name for p in (root / ".wizard-source" / "wizardapps").iterdir()) == [f"demo@{SHA[:12]}", "other@aaaaaaaaaaaa"]


@pytest.mark.parametrize("host_root", ["ordinary", "custom/app-data", ".wizard-source/disk/app-data",
                                      "app-data/.wizard-source/disk/app-data"])
def test_build_context_uses_the_runtipi_view_when_given(tmp_path, checkout, host_root):
    c = {"services": {"web": {"build": {"context": "app", "dockerfile": "Dockerfile"}}, "root": {"build": "."}}}
    host = packaging.source_dir(tmp_path / host_root, "wizardapps", "demo", SHA)
    root = str(packaging.source_dir(Path("/app-data"), "wizardapps", "demo", SHA))
    packaging.prepare_package("build", c, checkout, host, "demo", SHA, build_root=root)
    assert c["services"]["web"]["build"] == {"context": f"{root}/app", "dockerfile": "Dockerfile"}
    assert c["services"]["root"]["build"] == {"context": root}
    assert (host / "app" / "server.py").read_text() == "print(1)"
    assert not (host / ".git").exists()
