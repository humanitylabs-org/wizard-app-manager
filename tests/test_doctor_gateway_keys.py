"""#387: `wam doctor` flags apps with tools that the Agent Service's gateway holds no grant key for, so a missing key
shows as a disconnected agent connection instead of tools silently vanishing. Read-only, always on (bug lane, no switch)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_doctor_updates import box  # noqa: F401,E402  (shared doctor fixture)
from wam import cli, gateway_keys  # noqa: E402

KEY = "ab" * 32
REG = {"apps": [{"id": "wizard-agent-service", "mcp": {"url": "http://127.0.0.1:1/mcp"}},
                {"id": "messenger", "mcp": {"url": "http://127.0.0.1:2/mcp"}},
                {"id": "notes", "mcp": {"url": "http://127.0.0.1:3/mcp"}},
                {"id": "wizard-ux"}]}


def service_home(box):
    """The Agent Service's WIZARD_OS_HOME inside its data mount (/state/wizard-os in the container)."""
    d = box.runtipi_root / "app-data" / "wizardapps" / "wizard-agent-service" / "data" / "wizard-os"
    (d / "agents").mkdir(parents=True)
    return d


def write_keys(d, outgoing):
    (d / "grants").mkdir(exist_ok=True)
    f = d / "grants" / "agent-service.json"
    f.write_text(json.dumps({"outgoing": outgoing}))
    f.chmod(0o600)
    return f


def test_service_data_folder_wins_over_a_legacy_home_agents_folder(box):
    """Codex on #492: with the service's data folder present but no agents/ in it yet, a legacy ~/.wizard-os/agents
    must not send the check to ~/.wizard-os/grants."""
    data = box.runtipi_root / "app-data" / "wizardapps" / "wizard-agent-service" / "data"
    data.mkdir(parents=True)
    (box.home / "agents").mkdir(parents=True, exist_ok=True)
    assert gateway_keys.path(box.home, box.runtipi_root) == data / "wizard-os" / "grants" / "agent-service.json"


def test_doctor_always_shows_the_row(box):
    """No feature switch: `wam doctor` flags a missing gateway key on every box (#387, bug lane)."""
    service_home(box)
    (box.home / "apps.json").write_text(json.dumps(REG))
    rows = [(ok, d) for n, ok, d in cli.doctor(box) if n == gateway_keys.NAME]
    assert rows and rows[0][0] is False and "messenger, notes" in rows[0][1]


def test_no_file_flags_every_app_with_tools(box):
    d = service_home(box)
    [(name, ok, detail)] = cli.doctor_gateway_keys(box, REG)
    assert name == gateway_keys.NAME and not ok
    assert "no gateway key file for messenger, notes" in detail
    assert "wizard-agent-service" not in detail.split(":")[0] and "wizard-ux" not in detail
    assert str(d / "grants" / "agent-service.json") in detail


def test_missing_and_malformed_keys_are_flagged_without_printing_keys(box):
    d = service_home(box)
    write_keys(d, {"messenger": KEY, "notes": "not-a-key"})
    [(_, ok, detail)] = cli.doctor_gateway_keys(box, REG)
    assert not ok and "no valid key for notes" in detail and "messenger," not in detail
    assert KEY not in detail and "not-a-key" not in detail


def app_rows(box, app_id, keys):
    """The app's own layer-2 file with a host:agent-service row per key."""
    f = box.runtipi_root / "app-data" / "wizardapps" / app_id / "wizard" / "grants.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"outgoing": {}, "enforce": True,
                             "incoming": [{"from": "host:agent-service", "sha256": gateway_keys.digest(k)} for k in keys]}))
    return f


def test_all_keys_present_passes(box):
    d = service_home(box)
    write_keys(d, {"messenger": KEY, "notes": "cd" * 32})
    app_rows(box, "messenger", [KEY])
    app_rows(box, "notes", ["cd" * 32])
    [(_, ok, detail)] = cli.doctor_gateway_keys(box, REG)
    assert ok and detail.startswith("2 apps")


def test_key_without_the_apps_row_is_flagged(box):
    """AI review on #387: a gateway key the app doesn't hold (no host:agent-service row) still leaves tools hidden."""
    d = service_home(box)
    write_keys(d, {"messenger": KEY, "notes": "cd" * 32})
    app_rows(box, "messenger", [KEY])
    app_rows(box, "notes", ["ef" * 32])
    [(_, ok, detail)] = cli.doctor_gateway_keys(box, REG)
    assert not ok and "host:agent-service row) for notes" in detail and "messenger" not in detail
    assert "cd" * 32 not in detail


def test_no_agent_service_or_no_tools_adds_no_row(box):
    assert cli.doctor_gateway_keys(box, {"apps": [{"id": "messenger", "mcp": {"url": "x"}}]}) == []
    assert cli.doctor_gateway_keys(box, {"apps": [{"id": "wizard-agent-service", "mcp": {"url": "x"}}]}) == []


def test_unreadable_file_is_read_through_the_runtipi_container(box, monkeypatch):
    d = service_home(box)
    f = write_keys(d, {})
    seen = []

    def rt_read(inner):
        seen.append(inner)
        return json.dumps({"outgoing": {"messenger": KEY, "notes": KEY}}).encode()

    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (_ for _ in ()).throw(PermissionError("0600 root")) if self == f else real(self))
    app_rows(box, "messenger", [KEY])
    app_rows(box, "notes", [KEY])
    row = gateway_keys.check(REG, box.home, box.runtipi_root, rt_read=rt_read)
    assert row and row[1] and seen == ["/data/app-data/wizardapps/wizard-agent-service/data/wizard-os/grants/agent-service.json"]


def test_invalid_json_fails(box):
    d = service_home(box)
    (d / "grants").mkdir()
    (d / "grants" / "agent-service.json").write_text("{")
    [(_, ok, detail)] = cli.doctor_gateway_keys(box, REG)
    assert not ok and "not valid JSON" in detail
