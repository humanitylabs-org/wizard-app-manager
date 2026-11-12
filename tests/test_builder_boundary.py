"""`wam check` warns when an app's page would stay blank in a sandboxed Wizard window (wizard-os issue 133)."""
from wam import builder

RULE = 'browser boundary: page files load in a sandboxed window'


def rules(root):
    return [r for r in builder.check_app(root)['rules'] if r['rule'] == RULE]


def page(root, head):
    (root / 'web' / 'index.html').write_text(f'<!doctype html><html><head>{head}</head><body></body></html>')


def test_the_template_loads_classic_scripts_and_gets_no_warning(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Habits')
    assert rules(root) == []
    assert builder.check_app(root)['ok']


def test_a_module_script_without_a_cors_answer_warns_but_never_fails(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Habits')
    page(root, '<script type="module" src="app.js"></script>')
    got = rules(root)
    assert len(got) == 1 and got[0]['severity'] == 'warning' and not got[0]['ok']
    assert 'web/index.html' in got[0]['detail'] and 'Origin: null' in got[0]['detail']
    assert builder.check_app(root)['ok']
    assert got[0] in builder.check_app(root)['warnings']


def test_a_crossorigin_stylesheet_or_script_warns_too(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Habits')
    for head in ('<link rel="stylesheet" crossorigin href="a.css">', "<script type='module' crossorigin src=x.js></script>",
                 '<script src="a.js" crossorigin="anonymous"></script>', '<SCRIPT TYPE=module src="a.js"></SCRIPT>'):
        page(root, head)
        assert len(rules(root)) == 1, head


def test_a_server_that_answers_cors_gets_no_warning(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Habits')
    page(root, '<script type="module" src="app.js"></script>')
    server = root / 'app' / 'server.py'
    server.write_text(server.read_text() + "\n# opaque page assets\nASSET_HEADERS = {'Access-Control-Allow-Origin': '*'}\n")
    assert rules(root) == []


def test_comments_tests_and_vendored_pages_do_not_count(tmp_path):
    root = builder.new_app('habit-tracker', tmp_path, 'Habits')
    page(root, '<!-- <script type="module" src="old.js"></script> --><script defer src="app.js"></script>')
    for d in ('tests', 'node_modules/x'):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / 'index.html').write_text('<script type="module" src="a.js"></script>')
    assert rules(root) == []
    # A CORS line only in a test file is no answer from the app's server.
    page(root, '<script type="module" src="app.js"></script>')
    (root / 'tests' / 'test_x.py').write_text("assert h['Access-Control-Allow-Origin'] == '*'\n")
    assert len(rules(root)) == 1
