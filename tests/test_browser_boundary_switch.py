"""Browser boundary switch (wizard-os 389 rehearsal findings).

1. The flag was process env only: any `wam` run without it (an agent, the installer, a timer) rewrote apps.json
   without browser rows while the shell, the Agent Service and the app gates stayed on. The box's persisted choice
   (`browser_boundary` in app-manager.json) now decides when the env says nothing; the env still wins when set.
2. Turning it off left every app's WAM-written gate env (flag, credential, gateway key, own hosts) in place, so the
   apps kept refusing everything while nothing fed them. Off now removes exactly those variables.
"""
import json

from wam import browser_admission as ba
from wam import cli
from tests.test_browser_gateway_keys import box, env_of, install  # noqa: F401  (fixture + helpers)


def test_env_wins_else_persisted_choice(tmp_path):
    home = tmp_path / 'wos'; home.mkdir()
    env = {'WIZARD_OS_HOME': str(home)}
    assert ba.enabled(env) is False                       # nothing anywhere: off
    (home / 'app-manager.json').write_text(json.dumps({'browser_boundary': True, 'private_apps': {}}))
    assert ba.enabled(env) is True                        # persisted choice, no env
    assert ba.enabled({**env, 'WIZARD_BROWSER_BOUNDARY': '0'}) is False   # explicit env wins
    (home / 'app-manager.json').write_text(json.dumps({'browser_boundary': 'yes'}))
    assert ba.enabled(env) is False                       # only a real true turns it on
    (home / 'app-manager.json').write_text('{not json')
    assert ba.enabled(env) is False                       # unreadable: off (and the projection says why elsewhere)
    assert ba.enabled({**env, 'WIZARD_BROWSER_BOUNDARY': '1'}) is True


def test_flagless_run_keeps_the_projection_when_the_box_chose_it(box, monkeypatch):
    box.rows.append(install(box.root))
    assert box.b(box.write()).get('gateway_key')
    monkeypatch.delenv('WIZARD_BROWSER_BOUNDARY')
    monkeypatch.setenv('WIZARD_OS_HOME', str(box.c.home))
    (box.c.home / 'app-manager.json').write_text(json.dumps({'browser_boundary': True}))
    reg = box.write()                                     # e.g. the installer's own `wam` run
    assert box.b(reg).get('gateway_key') and env_of(box.env())['WIZARD_BROWSER_BOUNDARY'] == '1'


def test_off_removes_only_the_gate_env(box, monkeypatch):
    box.rows.append(install(box.root))
    box.write()
    env = box.env()
    env.write_text(env.read_text() + 'OWNER_SETTING=kept\n')
    key_files = sorted(p.name for p in (box.c.home / 'browser-gateway-keys').iterdir())
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '0')
    reg = box.write()
    assert 'browser' not in next(a for a in reg['apps'] if a['id'] == 'transcriber')
    assert env_of(env) == {'OWNER_SETTING': 'kept'}
    assert sorted(p.name for p in (box.c.home / 'browser-gateway-keys').iterdir()) == key_files   # keys kept
    assert ba.unproject(box.root, 'wizardapps') == []     # idempotent
    monkeypatch.setenv('WIZARD_BROWSER_BOUNDARY', '1')
    box.write()                                           # on again: the same per-installation key comes back
    assert env_of(env)['WIZARD_GATEWAY_KEY'] == (box.c.home / 'browser-gateway-keys' / key_files[0]).read_text().strip()
