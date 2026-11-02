"""Recipes that start declaring WIZARD_OWNER_LOGINS after install (Files, Tinfoil) must get the box owner's tailnet
login on update too: install is otherwise the only place WAM writes it, so existing boxes kept admitting any
tailnet login (wizard-os #386)."""
from types import SimpleNamespace

from wam import cli, store_install

OWNER = "owner@example.com"
DECLARES = "services:\n  files:\n    environment:\n      WIZARD_OWNER_LOGINS: ${WIZARD_OWNER_LOGINS:-}\n"


def box(tmp_path, recipe=DECLARES, env=None, where="repos"):
    rt = tmp_path / "runtipi"
    comp = (rt / "repos" / cli.STORE / "apps/files" if where == "repos" else rt / "apps" / cli.STORE / "files") / "docker-compose.yml"
    comp.parent.mkdir(parents=True)
    comp.write_text(recipe)
    if env is not None:
        p = rt / "user-config" / cli.STORE / "files" / "app.env"
        p.parent.mkdir(parents=True)
        p.write_text(env)
    return SimpleNamespace(home=tmp_path / "home", runtipi_root=rt)


def app_env(c):
    return (c.runtipi_root / "user-config" / cli.STORE / "files" / "app.env")


def test_missing_owner_login_is_written_and_other_values_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(store_install, "owner_login", lambda: OWNER)
    c = box(tmp_path, env="FILES_PASSWORD=keep-me-12chars\nWIZARD_OWNER_LOGINS=\n")
    assert cli.backfill_owner_login(c, "files") is True
    lines = app_env(c).read_text().splitlines()
    assert "FILES_PASSWORD=keep-me-12chars" in lines and f"WIZARD_OWNER_LOGINS={OWNER}" in lines
    assert len([l for l in lines if l.startswith("WIZARD_OWNER_LOGINS=")]) == 1
    assert app_env(c).stat().st_mode & 0o777 == 0o600
    c2 = box(tmp_path / "fresh", env=None, where="apps")        # no app.env yet, recipe only in apps/
    assert cli.backfill_owner_login(c2, "files") is True
    assert app_env(c2).read_text() == f"WIZARD_OWNER_LOGINS={OWNER}\n"


def test_quoted_empty_owner_login_counts_as_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(store_install, "owner_login", lambda: OWNER)
    for i, empty in enumerate(('WIZARD_OWNER_LOGINS=""', "WIZARD_OWNER_LOGINS=''", "WIZARD_OWNER_LOGINS= ")):
        c = box(tmp_path / str(i), env=f"A=1\n{empty}\n")
        assert cli.backfill_owner_login(c, "files") is True
        assert app_env(c).read_text() == f"A=1\nWIZARD_OWNER_LOGINS={OWNER}\n"


def test_never_replaces_an_owner_login_or_touches_other_recipes(tmp_path, monkeypatch):
    monkeypatch.setattr(store_install, "owner_login", lambda: OWNER)
    c = box(tmp_path, env="WIZARD_OWNER_LOGINS=chosen@example.com\n")
    assert cli.backfill_owner_login(c, "files") is False
    assert app_env(c).read_text() == "WIZARD_OWNER_LOGINS=chosen@example.com\n"
    c2 = box(tmp_path / "other", recipe="services:\n  files:\n    image: x\n", env="A=1\n")
    assert cli.backfill_owner_login(c2, "files") is False
    assert app_env(c2).read_text() == "A=1\n"


def test_unknown_owner_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(store_install, "owner_login", lambda: "")
    c = box(tmp_path, env="A=1\n")
    assert cli.backfill_owner_login(c, "files") is False
    assert app_env(c).read_text() == "A=1\n"


def test_app_update_writes_it_before_the_update_is_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(store_install, "owner_login", lambda: OWNER)
    c = box(tmp_path, env="A=1\n")
    seen = []

    class RT:
        def app(self, urn):
            return {"app": {"status": "running", "version": "fixture"}}

        def update(self, urn, perform_backup=True):
            seen.append(app_env(c).read_text())

        def wait_status(self, *a):
            return "running"

    rt = RT()
    c.runtipi = lambda: rt
    monkeypatch.setattr(cli.alongside, "load", lambda home: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {"ok": True, "errors": []})
    monkeypatch.setattr(cli.time, "sleep", lambda *a: None)
    cli.app_update(c, "files", backup=False)
    assert seen == [f"A=1\nWIZARD_OWNER_LOGINS={OWNER}\n"]


def test_flag_first_and_bulk_update_forms_backfill_too(tmp_path, monkeypatch):
    """`wam app update --no-backup files` and `wam app update --all` go straight to runtipi-cli (Codex P1)."""
    import subprocess
    from wam import hermes_lock
    monkeypatch.setattr(store_install, "owner_login", lambda: OWNER)
    monkeypatch.setattr(hermes_lock, "authorize", lambda: None)   # --all also passes the Hermes pause gate
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, cwd=None: calls.append(argv) or 0)
    for i, args in enumerate((["app", "update", "--no-backup", "files"], ["app", "update", "--all"])):
        c = box(tmp_path / str(i), env="A=1\n")
        (c.runtipi_root / "runtipi-cli").write_text("")
        (c.runtipi_root / "apps" / cli.STORE / "files").mkdir(parents=True)   # installed; recipe update in repos/
        other = c.runtipi_root / "apps" / cli.STORE / "other"
        other.mkdir(parents=True)
        (other / "docker-compose.yml").write_text("services:\n  other:\n    image: x\n")
        assert cli.runtipi_cli(c.runtipi_root, args) == 0
        assert app_env(c).read_text() == f"A=1\nWIZARD_OWNER_LOGINS={OWNER}\n"
        assert not (c.runtipi_root / "user-config" / cli.STORE / "other" / "app.env").exists()
    assert len(calls) == 2
