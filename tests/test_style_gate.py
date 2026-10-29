"""Builder and CI share one canonical style scanner, not two policies."""
import json
import subprocess
from pathlib import Path
from wam import builder


def test_generated_app_is_strict_and_template_is_clean(tmp_path):
    app = builder.new_app('strict-style-fixture', tmp_path)
    assert builder.style_rows(app) == []
    css = app / 'web/styles.css'
    css.write_text(css.read_text() + '\n.bad { color: #fff; font-size: 12px; font-family: Arial; }\n')
    rows = builder.style_rows(app)
    assert any(r['severity'] == 'error' and '3 findings' in r['detail'] for r in rows)
    assert not builder.check_app(app)['ok']


def test_legacy_import_warns_but_new_changed_lines_fail(tmp_path):
    app = tmp_path / 'legacy'; app.mkdir()
    css = app / 'app.css'; css.write_text('a{color:#fff}\nb{color:var(--wz-text)}\n')
    assert builder.style_rows(app)[0]['severity'] == 'warning'
    def git(*args): subprocess.run(['git','-C',str(app),*args],check=True,capture_output=True)
    git('init'); git('config','user.name','Test'); git('config','user.email','fixture@example.invalid')
    git('add','.'); git('commit','-m','Baseline')
    css.write_text('a{color:#fff}\nb{font-size:12px}\n')
    rows = builder.style_rows(app)
    assert {r['severity'] for r in rows} == {'warning','error'}


def test_generated_kit_drift_is_an_error(tmp_path):
    app = builder.new_app('drift-style-fixture', tmp_path)
    css = app / 'web/wizard-ui.css'; css.write_text(css.read_text()+'\n/* independent edit */\n')
    rows = builder.style_rows(app)
    assert any(r['severity']=='error' and 'kit-drift' in r['detail'] for r in rows)


def test_installed_checker_ships_its_policy():
    root = Path(__file__).resolve().parents[1]
    assert '"style-policy.json"' in (root / 'pyproject.toml').read_text()
    assert json.loads((root / 'wam/style-policy.json').read_text())['assets']
