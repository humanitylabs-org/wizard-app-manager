"""Registry rewrites never leave an installed app without its SKILL.md, and `wizard link`'s collision check survives a
rewrite that runs at the same time.

Live (disposable KVM guest, 0.3.72 -> App Manager main de288a6c): the installer's `wam alongside` recreated the connection
apps; the App Manager's network watcher rewrote the registry after each start (rmtree of every skills/<app>/ folder, then
copy) while `wizard link` read the same folders, and the upgrade failed with
"wizard: [Errno 2] No such file or directory: '/root/.wizard-os/skills/files/SKILL.md'".
"""
import shutil
import threading
from pathlib import Path

from wam import builder_bundle, registry


def _apps(tmp_path: Path, ids, body="---\nname: {id}\n---\n{id} v{v}\n", v=1):
    out = []
    for app_id in ids:
        src = tmp_path / "src" / app_id / f"v{v}" / "SKILL.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(body.format(id=app_id, v=v))
        out.append({"id": app_id, "_skill_src": str(src)})
    return out


def _write(root, apps):
    registry.write({"apps": [dict(a) for a in apps]}, root)


def test_a_rewrite_never_removes_a_still_installed_apps_skill(tmp_path, monkeypatch):
    root = tmp_path / "home"
    sk = root / "skills"
    _write(root, _apps(tmp_path, ["files", "browser"]))
    seen_missing = []
    real_copy = shutil.copy2

    def copy_and_look(src, dst, *a, **kw):
        # what a concurrent reader would see at this moment of the rewrite
        seen_missing.extend(app for app in ("files", "browser") if not (sk / app / "SKILL.md").is_file())
        return real_copy(src, dst, *a, **kw)

    monkeypatch.setattr(registry.shutil, "copy2", copy_and_look)
    _write(root, _apps(tmp_path, ["files", "browser"], v=2))
    assert seen_missing == []
    assert (sk / "files" / "SKILL.md").read_text().endswith("files v2\n")
    assert sorted(p.name for p in (sk / "files").iterdir()) == ["SKILL.md"]   # no temp files left


def test_removed_apps_and_strays_go_routing_skills_stay(tmp_path):
    root = tmp_path / "home"
    sk = root / "skills"
    _write(root, _apps(tmp_path, ["files", "gone"]))
    (sk / "files" / "stray.txt").write_text("x")
    (sk / "loose.md").write_text("x")
    (sk / "wizard-job-files").mkdir()
    (sk / "wizard-job-files" / "SKILL.md").write_text("routing")
    (sk / registry.ROUTING_STAMP).write_text("stamp")
    _write(root, _apps(tmp_path, ["files"], v=2))
    assert sorted(p.name for p in sk.iterdir()) == sorted(["files", "wizard-job-files", registry.ROUTING_STAMP])
    assert sorted(p.name for p in (sk / "files").iterdir()) == ["SKILL.md"]


def test_two_writers_at_once_both_succeed(tmp_path):
    root = tmp_path / "home"
    sk = root / "skills"
    apps = _apps(tmp_path, [f"app{i}" for i in range(30)])
    errors = []

    def writer():
        try:
            for _ in range(15):
                _write(root, apps)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert all((sk / f"app{i}" / "SKILL.md").is_file() for i in range(30))
    assert sorted(p.name for p in root.iterdir() if p.name.endswith(".tmp")) == []


def test_collision_check_skips_a_skill_that_vanished_mid_scan(tmp_path, monkeypatch):
    hermes, osh = tmp_path / "hermes", tmp_path / "os"
    (hermes / "skills").mkdir(parents=True)
    sk = osh / "skills"
    (sk / "files").mkdir(parents=True)
    (sk / "files" / "SKILL.md").write_text("---\nname: files\n---\n")
    (sk / "browser").mkdir()
    (sk / "browser" / "SKILL.md").write_text("---\nname: browser\n---\n")
    real = Path.read_text

    def vanishing(self, *a, **kw):
        if self.parent.name == "files" and self.name == "SKILL.md":
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", vanishing)
    cfg = {"skills": {"external_dirs": [str(sk)]}}
    assert builder_bundle.check_collisions(hermes, osh, cfg) == [str(sk)]


def test_collision_check_still_refuses_a_real_wizard_builder(tmp_path):
    hermes, osh = tmp_path / "hermes", tmp_path / "os"
    (hermes / "skills" / "mine").mkdir(parents=True)
    (hermes / "skills" / "mine" / "SKILL.md").write_text("---\nname: wizard-builder\n---\n")
    try:
        builder_bundle.check_collisions(hermes, osh, {})
    except ValueError as e:
        assert "wizard-builder name collision" in str(e)
    else:
        raise AssertionError("a real wizard-builder collision must still be refused")
