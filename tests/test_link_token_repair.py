"""wizard-ux#974: the agent's `wizard` MCP gets 401 when the Hermes .env lost the box's gateway token or holds a
stale one. Behind the off-by-default feature switch wizard/link-token-repair, a repeat `wizard link` restores it and
`wizard doctor` names the mismatch (token values are never shown)."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from wam import builder_bundle as bb


@pytest.fixture
def homes(tmp_path, monkeypatch):
    osh, hh = tmp_path / 'os', tmp_path / 'hermes'
    hh.mkdir()
    (hh / 'config.yaml').write_text('# owner comment\nmodel: my-model\n')
    monkeypatch.setenv('WIZARD_OS_HOME', str(osh))
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'])
    return osh, hh


def load_cli(name):
    loader = importlib.machinery.SourceFileLoader(name, str(bb.SOURCE / 'wizard-cli/wizard'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def cli(homes, monkeypatch):
    """The CLI with switches stubbed; no Hermes needed (runs in CI, which has no ruamel)."""
    module = load_cli('wizard_test_token_repair_cli')
    switches = set()
    monkeypatch.setattr(module, 'switch_on', lambda tid: tid in switches)
    return module, switches


@pytest.fixture
def wizard(homes, monkeypatch):
    """Full link/unlink with Hermes' config editor (needs ruamel.yaml, like tests/test_builder_bundle.py)."""
    module = load_cli('wizard_test_token_repair')
    pytest.importorskip('ruamel.yaml')
    monkeypatch.setattr(module, 'editor_command', lambda home: [sys.executable, '-c', module._EDITOR])

    def run(home, *args, **kwargs):
        if args[:2] == ('plugins', 'install'):
            plugin = home / 'plugins' / module.PLUGIN_NAME
            plugin.mkdir(parents=True, exist_ok=True)
            (plugin / 'plugin.yaml').write_text('name: wizard-os\n')
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module, 'run_hermes', run)
    switches = set()
    monkeypatch.setattr(module, 'switch_on', lambda tid: tid in switches)
    return module, switches


def ns(hh):
    return SimpleNamespace(hermes_home=str(hh), restart=False, plugin_source=None, plugin_ref='pin', gateway=None)


def box_token(osh):
    return (osh / 'mcp-token').read_text().strip()


def lose_token(hh, w, value=None):
    """What a setup step that rewrites the Hermes .env does: drop the line, or leave a stale value; keep its own keys."""
    w.env_remove(hh / '.env', [w.TOKEN_ENV])
    w.env_append(hh / '.env', 'GOOGLE_SETUP_KEY', 'owner-value')
    if value:
        w.env_append(hh / '.env', w.TOKEN_ENV, value)


@pytest.mark.parametrize('stale', [None, 'stale-token-from-an-old-box'])
def test_switch_off_repeat_link_leaves_the_env_alone(homes, wizard, stale):
    osh, hh = homes
    w, _ = wizard
    w.cmd_link(ns(hh))
    lose_token(hh, w, stale)
    before = (hh / '.env').read_bytes()
    out = w.cmd_link(ns(hh))
    assert out['already_linked'] and 'token_repaired' not in out
    assert (hh / '.env').read_bytes() == before


@pytest.mark.parametrize('stale', [None, 'stale-token-from-an-old-box'])
def test_switch_on_repeat_link_restores_the_box_token(homes, wizard, stale):
    osh, hh = homes
    w, switches = wizard
    w.cmd_link(ns(hh))
    lose_token(hh, w, stale)
    switches.add(w.LINK_TOKEN_REPAIR)
    out = w.cmd_link(ns(hh))
    assert out['token_repaired'] is True and 'restart' in out['next']
    names = w.env_names(hh / '.env')
    assert names[w.TOKEN_ENV] == box_token(osh)
    assert names['GOOGLE_SETUP_KEY'] == 'owner-value'          # the setup step's own keys are kept
    assert (hh / '.env').read_text().count(w.TOKEN_ENV + '=') == 1
    assert box_token(osh) not in repr(out)                      # never shown
    # a further repeat finds nothing to repair
    assert 'token_repaired' not in w.cmd_link(ns(hh))
    # unlink still removes only what link added and keeps the owner's later edit
    w.cmd_unlink(ns(hh))
    names = w.env_names(hh / '.env')
    assert w.TOKEN_ENV not in names and names['GOOGLE_SETUP_KEY'] == 'owner-value'


def test_repair_never_mints_a_token_the_service_does_not_know(homes, wizard):
    osh, hh = homes
    w, switches = wizard
    w.cmd_link(ns(hh))
    lose_token(hh, w)
    (osh / 'mcp-token').unlink()
    switches.add(w.LINK_TOKEN_REPAIR)
    assert 'token_repaired' not in w.cmd_link(ns(hh))
    assert not (osh / 'mcp-token').exists() and w.TOKEN_ENV not in w.env_names(hh / '.env')


def doctor_rows(w, hh, monkeypatch):
    def offline(*a, **k):
        raise OSError('offline in tests')
    monkeypatch.setattr(w, '_http_json', offline)

    class NoGateway:
        def __init__(self, *a, **k):
            pass

        def open(self):
            raise w.Fail('tool_error', 'gateway not running in tests')
    monkeypatch.setattr(w, 'MCP', NoGateway)
    out = w.cmd_doctor(SimpleNamespace(hermes_home=str(hh), gate=False, url=None))
    return {r['check']: r for r in out['checks']}, out


def test_doctor_names_a_stale_token_only_with_the_switch_on(homes, wizard, monkeypatch):
    osh, hh = homes
    w, switches = wizard
    w.cmd_link(ns(hh))
    rows, _ = doctor_rows(w, hh, monkeypatch)
    assert 'gateway token matches this box' not in rows               # switch off: doctor unchanged
    switches.add(w.LINK_TOKEN_REPAIR)
    rows, _ = doctor_rows(w, hh, monkeypatch)
    assert rows['gateway token matches this box']['ok'] is True
    lose_token(hh, w, 'stale-token-from-an-old-box')
    rows, out = doctor_rows(w, hh, monkeypatch)
    row = rows['gateway token matches this box']
    assert row['ok'] is False and '401' in row['detail'] and 'wizard link --restart' in row['detail']
    assert box_token(osh) not in repr(out) and 'stale-token-from-an-old-box' not in repr(out)


def test_switch_on_is_off_without_wam(tmp_path, monkeypatch):
    module = load_cli('wizard_test_switch')
    monkeypatch.setenv('WIZARD_OS_HOME', str(tmp_path / 'os'))
    import wam.feature_switches as fs
    monkeypatch.setattr(fs, 'is_on', lambda tid, *a, **k: (_ for _ in ()).throw(OSError('no look.json')))
    assert module.switch_on(module.LINK_TOKEN_REPAIR) is False
    monkeypatch.setattr(fs, 'is_on', lambda tid, *a, **k: tid == module.LINK_TOKEN_REPAIR)
    assert module.switch_on(module.LINK_TOKEN_REPAIR) is True


def linked_box(w, osh, hh, env_token):
    """A box as `wizard link` leaves it, written by hand: the box token, the Hermes .env and the link record."""
    osh.mkdir(parents=True, exist_ok=True)
    (osh / 'mcp-token').write_text('box-token-' + 'x' * 32 + '\n')
    (hh / '.env').write_text('OWNER_KEY=kept\n' + (f'{w.TOKEN_ENV}={env_token}\n' if env_token else ''))
    rec = {'linked': True, 'state': 'linked', 'hermes_home': str(hh), 'env_added': [w.TOKEN_ENV],
           'known_env_shas': ['before-link'], 'notes': []}
    w.save_record(rec)
    return rec


@pytest.mark.parametrize('env_token', [None, 'stale-token-from-an-old-box'])
def test_repair_link_token_restores_only_the_token_line(homes, cli, env_token):
    osh, hh = homes
    w, _ = cli
    rec = linked_box(w, osh, hh, env_token)
    assert w.repair_link_token(hh, rec) is True
    assert w.env_names(hh / '.env') == {'OWNER_KEY': 'kept', w.TOKEN_ENV: box_token(osh)}
    saved = w.read_record()
    assert saved['known_env_shas'] == ['before-link'] and saved['env_added'] == [w.TOKEN_ENV]
    assert any('restored' in n for n in saved['notes']) and box_token(osh) not in repr(saved)
    assert w.repair_link_token(hh, saved) is False                       # nothing left to repair


def test_repair_link_token_without_box_token_changes_nothing(homes, cli):
    osh, hh = homes
    w, _ = cli
    rec = linked_box(w, osh, hh, None)
    (osh / 'mcp-token').unlink()
    before = (hh / '.env').read_bytes()
    assert w.repair_link_token(hh, rec) is False
    assert (hh / '.env').read_bytes() == before and not (osh / 'mcp-token').exists()


def test_doctor_token_row_without_hermes(homes, cli, monkeypatch):
    osh, hh = homes
    w, switches = cli
    linked_box(w, osh, hh, 'stale-token-from-an-old-box')
    rows, _ = doctor_rows(w, hh, monkeypatch)
    assert 'gateway token matches this box' not in rows
    switches.add(w.LINK_TOKEN_REPAIR)
    rows, out = doctor_rows(w, hh, monkeypatch)
    assert rows['gateway token matches this box']['ok'] is False
    assert box_token(osh) not in repr(out) and 'stale-token-from-an-old-box' not in repr(out)
    w.repair_link_token(hh, w.read_record())
    rows, _ = doctor_rows(w, hh, monkeypatch)
    assert rows['gateway token matches this box']['ok'] is True


def test_doctor_names_a_missing_hermes_token_with_the_remedy(homes, cli, monkeypatch):
    osh, hh = homes
    w, switches = cli
    linked_box(w, osh, hh, None)
    switches.add(w.LINK_TOKEN_REPAIR)
    rows, out = doctor_rows(w, hh, monkeypatch)
    row = rows['gateway token matches this box']
    assert row['ok'] is False and 'has no ' + w.TOKEN_ENV in row['detail']
    assert '401' in row['detail'] and 'wizard link --restart' in row['detail'] and box_token(osh) not in repr(out)
