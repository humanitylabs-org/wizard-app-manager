"""Owner-chosen updates (wam/updates.py): check vs stable, Ask me (default) / Automatic, apply = installer rerun."""
import io
import json
import subprocess

import pytest

from wam import updates


@pytest.fixture(autouse=True)
def _no_cache():
    updates._latest.clear()
    yield
    updates._latest.clear()


def _base(tmp_path, stable, promoted=True):
    d = tmp_path / "rel"; d.mkdir(exist_ok=True)
    (d / "stable").write_text(stable + "\n")
    if promoted:                                   # Automatic installs only a stable release with a promotion record
        (d / "promotions").mkdir(exist_ok=True)
        (d / "promotions" / f"{stable}.json").write_text(json.dumps({
            "version": stable, "latest_since": "2026-10-01T00:00:00Z", "promoted_at": "2026-10-05T00:00:00Z",
            "upgrade_test": {"from": "0.0.1", "to": stable, "passed": True, "evidence": "https://example.test/run"}}))
    return d.as_uri()


def _pin(h, rel):
    h.mkdir(exist_ok=True)
    (h / "release.json").write_text(json.dumps({"release": rel, "status": "pinned"}))


def _ok_runner(calls):
    return lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", "")


def test_status_compares_installed_release_with_stable(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.8")
    assert updates.status(h, _base(tmp_path, "0.3.8"))["update_available"] is False
    _pin(h, "0.3.10")
    updates._latest.clear()
    assert updates.status(h, _base(tmp_path, "0.3.9"))["update_available"] is False   # numeric, not string order
    updates._latest.clear()
    st = updates.status(h, _base(tmp_path, "0.4.0"))
    assert st["update_available"] is True and st["unpinned"] is False and st["installed"] == "0.3.10"
    off = updates.status(h, (tmp_path / "missing").as_uri())
    assert off["checked"] is False and off["update_available"] is False


def test_unpinned_box_never_reports_or_applies_an_update_unless_forced(tmp_path):
    """Archimedes blocker: no release.json = updated by hand. Neither Update nor Automatic moves it to stable."""
    h = tmp_path / "wos"; h.mkdir()
    base = _base(tmp_path, "0.3.16")
    st = updates.status(h, base)
    assert st["unpinned"] is True and st["update_available"] is False and st["installed"] is None
    (h / "release.json").write_text(json.dumps({"status": "unpinned", "reason": "WAM_REF=main"}))   # get.sh's record
    assert updates.status(h, base)["unpinned"] is True
    calls = []
    res = updates.apply(h, base, _ok_runner(calls))
    assert res["started"] is False and "unpinned" in res["error"] and calls == []
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["started"] is False
    assert calls == []
    assert updates.apply(h, base, _ok_runner(calls), force=True)["started"] is True and len(calls) == 1


def test_latest_is_cached_about_ten_minutes(tmp_path, monkeypatch):
    clock = [1000.0]
    hits = []
    real = updates.urllib.request.urlopen
    monkeypatch.setattr(updates.urllib.request, "urlopen",
                        lambda url, timeout: hits.append(url) or real(url, timeout=timeout))
    base = _base(tmp_path, "0.3.16")
    now = lambda: clock[0]
    assert updates.latest(base, now=now) == "0.3.16"
    (tmp_path / "rel" / "stable").write_text("0.3.17\n")
    clock[0] += 599
    assert updates.latest(base, now=now) == "0.3.16" and len(hits) == 1             # served from the cache
    assert updates.latest(base, now=now, fresh=True) == "0.3.17" and len(hits) == 2  # apply/timer read it fresh
    clock[0] += 601
    assert updates.latest(base, now=now) == "0.3.17" and len(hits) == 3
    missing = (tmp_path / "nope").as_uri()
    assert updates.latest(missing, now=now) is None
    clock[0] += 61
    updates.latest(missing, now=now)
    assert len(hits) == 5                                                            # a miss retries after a minute


def test_mode_defaults_to_ask_me_and_an_explicit_automatic_is_kept(tmp_path):
    h, ud = tmp_path / "wos", tmp_path / "units"
    st = updates.status(h, _base(tmp_path, "0.3.16"))
    assert updates.mode(h) == "ask" and st["mode"] == "ask" and st["chosen"] is False
    # A box whose owner never chose gets no timer, and the installer records no choice for the owner.
    assert updates.ensure_timer(h, ud, systemctl=False) == {"mode": "ask", "timer": False}
    assert not (h / "updates.json").exists() and not (ud.exists() and list(ud.iterdir()))
    # A timer left by the old Automatic default does nothing for an owner who never chose.
    assert updates.apply_if_auto(h, _base(tmp_path, "0.3.17"), runner=lambda *a, **k: pytest.fail("ran"), is_busy=lambda: False).get("started") is not True
    updates.set_mode("auto", h, ud, systemctl=False)
    assert updates.mode(h) == "auto" and (ud / "wizard-os-update.timer").is_file()
    timer = (ud / "wizard-os-update.timer").read_text()
    assert "OnCalendar=*-*-* 03:30" in timer and "RandomizedDelaySec=1h" in timer and "Persistent=true" in timer
    assert "updates --apply-if-auto" in (ud / "wizard-os-update.service").read_text()
    # An installer rerun after the owner chose Automatic keeps Automatic and its timer.
    assert updates.ensure_timer(h, ud, systemctl=False) == {"mode": "auto", "timer": True}
    assert updates.status(h, _base(tmp_path, "0.3.16"))["chosen"] is True
    updates.set_mode("ask", h, ud, systemctl=False)
    assert updates.mode(h) == "ask" and not list(ud.iterdir())
    with pytest.raises(SystemExit):
        updates.set_mode("sometimes", h, ud, systemctl=False)
    (h / "updates.json").write_text("{not json")
    assert updates.mode(h) == "ask"


def test_apply_reruns_the_installer_only_when_newer(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.7")
    calls = []
    run = _ok_runner(calls)
    updates.set_mode("ask", h, tmp_path / "u", systemctl=False)
    assert updates.apply_if_auto(h, _base(tmp_path, "0.3.8"), run)["started"] is False and calls == []  # Ask me
    res = updates.apply(h, _base(tmp_path, "0.3.8"), run)
    assert res["started"] is True
    assert calls[-1] == ["bash", "-c", "curl -fsSL https://aiwizards.com/os.sh | WIZARD_RELEASE=0.3.8 bash -s start"]
    _pin(h, "0.3.8")
    assert updates.apply(h, _base(tmp_path, "0.3.8"), run)["started"] is False and len(calls) == 1
    _pin(h, "0.3.7")
    (h / "install-status.json").write_text(json.dumps({"state": "running"}))      # an update already running
    res = updates.apply(h, _base(tmp_path, "0.3.8"), run)
    assert res["started"] is False and res["installing"] is True and len(calls) == 1
    (h / "install-status.json").write_text(json.dumps({"state": "done"}))
    fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom")
    res = updates.apply(h, _base(tmp_path, "0.3.8"), fail)
    assert res["started"] is False and res["error"] == "boom"


def test_automatic_skips_while_the_agent_is_working_and_retries_next_window(tmp_path):
    h = tmp_path / "wos"
    _pin(h, "0.3.15")
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)                    # the owner chose Automatic
    base, calls = _base(tmp_path, "0.3.16"), []
    res = updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: True)
    assert res["started"] is False and res["skipped"] == "busy" and calls == []
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: None)["started"] is True  # unknown = go
    assert updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)["started"] is True


def test_busy_reads_the_agent_service_health_queue(monkeypatch):
    bodies = {}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(url, timeout):
        b = bodies["b"]
        if isinstance(b, Exception):
            raise b
        return R(json.dumps(b).encode())
    monkeypatch.setattr(updates.urllib.request, "urlopen", fake)
    for body, want in [({"queue": {"active": 1, "queued": 0}}, True), ({"queue": {"active": 0, "queued": 2}}, True),
                       ({"queue": {"active": 0, "queued": 0}}, False), ({"status": "ok"}, None),
                       (OSError("down"), None)]:
        bodies["b"] = body
        assert updates.busy("http://127.0.0.1:8798") is want
    assert updates.busy(None) is None


def test_service_origin_from_alongside_or_registry(tmp_path):
    h = tmp_path / "wos"; h.mkdir()
    assert updates.service_origin(h) is None
    (h / "apps.json").write_text(json.dumps({"apps": [{"id": "wizard-agent-service",
                                                       "health": "http://127.0.0.1:8797/wizard/health"}]}))
    assert updates.service_origin(h) == "http://127.0.0.1:8797"
    (h / "alongside.json").write_text(json.dumps({"service": {"port": 8798}}))
    assert updates.service_origin(h) == "http://127.0.0.1:8798"


def test_store_listener_updates_route(tmp_path, monkeypatch):
    """Settings → Updates goes the Store's path: tailscaled-only peer, owner login, shell Origin, X-Wizard-Store."""
    from wam import cli, store_install as si

    h = tmp_path / "wos"
    _pin(h, "0.3.15")
    (h / "links.json").write_text(json.dumps({"host": "box.ts.net"}))
    monkeypatch.setenv("WIZARD_OS_HOME", str(h))
    monkeypatch.setenv("WIZARD_RELEASE_BASE", _base(tmp_path, "0.3.16"))
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    monkeypatch.setattr(si, "owner_login", lambda: "owner@example.com")
    monkeypatch.setattr(si, "served_origins", lambda host: {"https://box.ts.net:8444"})
    calls = []
    monkeypatch.setattr(updates, "apply", lambda *a, **k: calls.append(k) or {"started": True})
    monkeypatch.setattr(updates, "set_mode", lambda m, *a, **k: calls.append(m) or {"mode": m, "chosen": True})

    class C:
        home = h
    sent = []

    def call(method, body=None, **hdr):
        hd = {"X-Forwarded-For": "100.64.0.9", "Tailscale-User-Login": "owner@example.com", "Origin": "https://box.ts.net:8444",
              "X-Wizard-Store": "1", "Content-Type": "application/json", **hdr}
        raw = json.dumps(body).encode() if body is not None else b""
        hd["Content-Length"] = str(len(raw))
        o = cli.StoreHandler.__new__(cli.StoreHandler)
        o.cfg, o.path, o.client_address = C(), "/_updates", ("127.0.0.1", 5555)
        o.headers = {k: v for k, v in hd.items() if v is not None}
        o.rfile = io.BytesIO(raw)
        o._json = lambda code, b: sent.append((code, b))
        o._go(method)
        return sent[-1]

    code, b = call("GET")
    assert code == 200 and b["installed"] == "0.3.15" and b["latest"] == "0.3.16" and b["update_available"] is True
    assert call("GET", **{"Tailscale-User-Login": "intruder@example.com"})[0] == 403
    assert call("POST", {"mode": "ask"}, Origin="https://evil.example")[0] == 403
    assert call("POST", {"mode": "ask"}, **{"X-Wizard-Store": None})[0] == 403
    assert call("POST", {"mode": "ask"})[0] == 200 and calls[-1] == "ask"
    assert call("POST", {"mode": "never"})[0] == 400
    assert call("POST", {"action": "apply", "force": True})[0] == 400    # the browser can never force
    assert call("POST", {"action": "apply"}) == (200, {"started": True}) and calls[-1] == {}
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 1000)
    assert call("GET")[0] == 403

    # App updates share exactly the same owner/origin/JSON admission, but not the OS mode.
    from wam import app_updates as au
    monkeypatch.setattr(si, "peer_uid", lambda *a, **k: 0)
    saved = {"app_mode": "auto", "chosen": False, "due": [], "skipped": []}
    monkeypatch.setattr(au, "status", lambda c: dict(saved))
    monkeypatch.setattr(au, "set_mode", lambda mode, home: saved.update(app_mode=mode, chosen=True))
    monkeypatch.setattr(au, "start_detached", lambda: {"started": True})
    assert call("GET")[1]["apps"]["chosen"] is False
    assert call("POST", {"app_mode": "ask"}, Origin="https://evil.example")[0] == 403
    assert saved["app_mode"] == "auto"
    assert call("POST", {"app_mode": "ask"}, **{"Tailscale-User-Login": "other"})[0] == 403
    assert call("POST", {"app_mode": "ask"}, **{"X-Wizard-Store": None})[0] == 403
    assert call("POST", {"app_mode": "never"})[0] == 400
    assert call("POST", {"app_mode": "ask", "force": True})[0] == 400
    assert call("POST", {"app_mode": "ask"})[1]["apps"] == {**saved, "app_mode": "ask", "chosen": True}
    assert calls[-1] == {}  # system action/mode untouched by the app choice
    assert call("POST", {"action": "apply-apps", "force": True})[0] == 400
    assert call("POST", {"action": "apply-apps"}) == (200, {"started": True})
    assert saved["app_mode"] == "ask"  # one manual run does not opt in
    assert call("POST", {"app_mode": "auto"})[1]["apps"]["app_mode"] == "auto"


def test_installer_enables_the_timer_on_every_install_and_rerun():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    api = text.index("systemctl --user restart wizard-app-manager-api.service")
    assert text.index('"$HOME/.local/bin/wam" updates --ensure-timer') > api


# ---- an update cut short by a reboot (wizard-os#90): never "installing" forever ----

def _dead_pid():
    p = subprocess.Popen(["true"]); p.wait()
    return p.pid


def _boot(tmp_path, value):
    f = tmp_path / "boot_id"; f.write_text(value + "\n")
    return f


def _running(h, **kw):
    h.mkdir(exist_ok=True)
    import time
    st = {"state": "running", "step": "starting", "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw}
    (h / "install-status.json").write_text(json.dumps(st))
    (h / "install.lock").write_text("")


def test_dead_runner_pid_is_interrupted_and_the_stale_lock_is_released(tmp_path):
    h = tmp_path / "wos"
    _running(h, pid=_dead_pid())
    assert updates.installing(h) is False
    st = json.loads((h / "install-status.json").read_text())
    assert st["state"] == "interrupted" and st["interrupted_step"] == "starting"
    assert st["interrupted_reason"] == "runner exited" and "Update now" in st["message"]
    assert "will be retried" not in st["message"]  # an unpinned / Ask-me box is never retried by itself
    assert not (h / "install.lock").exists()


def test_changed_boot_id_is_interrupted_even_if_the_pid_was_reused(tmp_path, monkeypatch):
    import os
    h = tmp_path / "wos"
    _running(h, pid=os.getpid(), boot_id="boot-before")          # pid alive (reused after the reboot)
    monkeypatch.setattr(updates, "BOOT_ID", _boot(tmp_path, "boot-after"))
    assert updates.install_state(h) == "interrupted"
    assert json.loads((h / "install-status.json").read_text())["interrupted_reason"] == "reboot"
    assert not (h / "install.lock").exists()


def test_status_from_before_boot_ids_started_before_this_boot_is_interrupted(tmp_path, monkeypatch):
    """The 0.3.17 box from the report: no boot_id recorded yet, started before the box last booted."""
    import os
    h = tmp_path / "wos"
    _running(h, pid=os.getpid(), started_at="2026-10-20T16:13:16Z")
    stat = tmp_path / "stat"; stat.write_text("cpu 1 2 3\nbtime 1792512809\n")   # 2026-10-20T16:13:29Z
    monkeypatch.setattr(updates, "PROC_STAT", stat)
    assert updates.install_state(h) == "interrupted"
    stat.write_text("btime 1792512700\n")                                          # booted before it started
    _running(h, pid=os.getpid(), started_at="2026-10-20T16:13:16Z")
    assert updates.install_state(h) == "running"


def test_live_runner_still_reports_installing(tmp_path, monkeypatch):
    import os
    h = tmp_path / "wos"
    monkeypatch.setattr(updates, "BOOT_ID", _boot(tmp_path, "same-boot"))
    _running(h, pid=os.getpid(), boot_id="same-boot")
    assert updates.installing(h) is True and updates.status(h, _base(tmp_path, "0.3.19"))["installing"] is True
    assert json.loads((h / "install-status.json").read_text())["state"] == "running"
    assert (h / "install.lock").exists()
    _running(h, boot_id="same-boot")                       # no pid recorded: no evidence the runner is gone
    assert updates.installing(h) is True


def test_a_held_lock_is_respected_even_with_a_dead_pid_or_changed_boot(tmp_path, monkeypatch):
    import fcntl
    h = tmp_path / "wos"
    monkeypatch.setattr(updates, "BOOT_ID", _boot(tmp_path, "boot-after"))
    for name in ("install.lock", "install.start.lock"):
        _running(h, pid=_dead_pid(), boot_id="boot-before")
        with open(h / name, "w") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert updates.installing(h) is True, name
            assert json.loads((h / "install-status.json").read_text())["state"] == "running"
            assert (h / "install.lock").exists()
        assert updates.installing(h) is False            # holder gone: now it heals


def test_apply_and_the_nightly_timer_retry_after_an_interrupted_update(tmp_path, monkeypatch):
    h = tmp_path / "wos"
    _pin(h, "0.3.17")
    updates.set_mode("auto", h, tmp_path / "u", systemctl=False)                    # the owner chose Automatic
    base, calls = _base(tmp_path, "0.3.19"), []
    monkeypatch.setattr(updates, "BOOT_ID", _boot(tmp_path, "boot-after"))
    _running(h, pid=_dead_pid(), boot_id="boot-before")
    st = updates.status(h, base)
    assert st["installing"] is False and st["interrupted"] is True and st["update_available"] is True
    res = updates.apply_if_auto(h, base, _ok_runner(calls), is_busy=lambda: False)     # the nightly timer
    assert res["started"] is True and len(calls) == 1
    _running(h, pid=_dead_pid(), boot_id="boot-before")
    assert updates.apply(h, base, _ok_runner(calls))["started"] is True and len(calls) == 2   # `wam updates --apply`


def test_finished_states_are_left_alone(tmp_path):
    h = tmp_path / "wos"; h.mkdir()
    for state in ("done", "failed", "needs_admin", "interrupted"):
        raw = json.dumps({"state": state, "pid": _dead_pid()})
        (h / "install-status.json").write_text(raw)
        assert updates.install_state(h) == state and (h / "install-status.json").read_text() == raw
    assert updates.install_state(tmp_path / "none") is None
