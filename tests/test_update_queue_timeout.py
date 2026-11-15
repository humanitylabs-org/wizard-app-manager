"""#711: a big app's update (Messenger) outlasted Runtipi's 5-minute queue timeout. The update was re-run up to 3
times, Runtipi recorded the app `stopped` on its old version, and wam started it while the update was still going.

The backend half (no RPC retries, 30 min for install/update/backup/restore, status sync leaves `updating` alone)
is tested in packages/backend/src/modules/queue. Here: wam waits longer than that, and never starts an app that
Runtipi still reports as `updating`.
"""
import re
from pathlib import Path

from wam import cli

ROOT = Path(__file__).resolve().parents[1]


def _rt(final):
    calls = {"start": 0, "waits": []}

    class RT:
        def app(self, urn):
            return {"app": {"status": "running", "version": "1"}}

        def update(self, urn, perform_backup=True):
            pass

        def wait_status(self, urn, want, timeout=900):
            calls["waits"].append(timeout)
            return final

        def start(self, urn):
            calls["start"] += 1

    return RT(), calls


def _update(tmp_path, monkeypatch, final):
    c = cli.Config()
    c.runtipi_root, c.home = tmp_path / "rt", tmp_path / "home"
    c.home.mkdir()
    rt, calls = _rt(final)
    monkeypatch.setattr(c, "runtipi", lambda: rt, raising=False)
    monkeypatch.setattr(cli.alongside, "load", lambda h: None)
    monkeypatch.setattr(cli, "host_access_risks", lambda *a, **k: [])
    monkeypatch.setattr(cli, "network_step", lambda *a, **k: {})
    monkeypatch.setattr(cli, "backfill_owner_login", lambda *a, **k: False)
    # 0.3.143 has no keep_trusted_hosts (#708) and no graceful-restart wrapper (#642): app_update is the whole update
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return cli.app_update(c, "messenger", backup=False), calls


def test_an_update_still_running_is_not_started_on_top(tmp_path, monkeypatch):
    out, calls = _update(tmp_path, monkeypatch, "updating")
    assert calls["start"] == 0
    assert out["status"] == "updating" and out.get("error")


def test_a_failed_update_of_a_running_app_still_starts_it_again(tmp_path, monkeypatch):
    out, calls = _update(tmp_path, monkeypatch, "stopped")
    assert calls["start"] == 1 and out["restarted"]


def test_wam_waits_longer_than_runtipi_gives_an_update(tmp_path, monkeypatch):
    _, calls = _update(tmp_path, monkeypatch, "running")
    src = (ROOT / "packages/backend/src/modules/queue/lifecycle-timeouts.ts").read_text()
    minutes = int(re.search(r"LONG_COMMAND_MINUTES = (\d+);", src).group(1))
    assert cli.LONG_COMMAND_MINUTES == minutes
    # the backend's long timeout plus one more period waiting for the late result
    assert calls["waits"][0] > 2 * minutes * 60


def test_the_wait_follows_a_higher_configured_queue_timeout(tmp_path, monkeypatch):
    (tmp_path / "rt").mkdir()
    (tmp_path / "rt" / ".env").write_text("QUEUE_TIMEOUT_IN_MINUTES=90\n")
    _, calls = _update(tmp_path, monkeypatch, "running")
    assert calls["waits"][0] > 2 * 90 * 60
