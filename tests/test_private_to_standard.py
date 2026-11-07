"""An installed owner-private test build may move to the public Standard release of the same app id, but only
inside one GitHub owner and only as an explicit release change. Real prepare() against a local origin."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from wam import cli, private, store_selection as ss

TEMPLATE = Path(__file__).resolve().parents[1] / 'templates/hello-wizard'
APP = 'hello-wizard'
PUBLIC = 'acme/hello'
PRIVATE = 'acme/hello-history'
OLD = 'c' * 40


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


@pytest.fixture
def box(tmp_path, monkeypatch):
    src = tmp_path / 'origin'
    shutil.copytree(TEMPLATE, src)
    git(src, 'init', '-q')
    git(src, 'config', 'user.name', 'Test')
    git(src, 'config', 'user.email', 'test@example.invalid')
    git(src, 'add', '.')
    git(src, 'commit', '-qm', 'public release')
    commit = git(src, 'rev-parse', 'HEAD')
    bare = tmp_path / 'public.git'
    git(tmp_path, 'clone', '-q', '--bare', str(src), str(bare))
    config = tmp_path / 'gitconfig'
    config.write_text(f'[url "{bare.as_uri()}"]\n    insteadOf = https://github.com/{PUBLIC}\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setattr(private, 'token', lambda: '')
    monkeypatch.setattr(private, 'pull', lambda *a: pytest.fail('standard pins never pull'))
    entry = {'id': APP, 'repo': PUBLIC, 'commit': commit, 'install': {'commit': commit}, 'uses': []}
    monkeypatch.setattr(ss.index, 'resolve', lambda index, ref: dict(entry))
    c = SimpleNamespace(home=tmp_path / 'home', runtipi_root=tmp_path / 'runtipi', index='https://index.test',
                        runtipi=lambda: None)
    return c, commit


def install_old(c, identity):
    """Seed an installed copy plus its store-folder twin the way a previous Get left them."""
    for base in (c.runtipi_root / 'apps' / cli.STORE / APP, c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP):
        base.mkdir(parents=True)
        (base / '.wizard-source').write_text(json.dumps(identity))
        (base / '.wizard-commit').write_text(OLD + '\n')
        (base / '.wizard-selection').write_text(json.dumps({'repo': identity['repo'], 'commit': OLD}) + '\n')


PRIVATE_ID = {'repo': PRIVATE, 'git': f'https://github.com/{PRIVATE}', 'private': True, 'source': 'owner-private'}


def body(commit, **kw):
    return {'source': 'standard', 'repo': PUBLIC, 'commit': commit, 'operation': 'switch', **kw}


def finish_factory(c, calls):
    def finish(config, app, **kw):
        calls.append((app, kw))
        src = c.runtipi_root / 'repos' / cli.STORE / 'apps' / app
        dst = c.runtipi_root / 'apps' / cli.STORE / app
        shutil.rmtree(dst, ignore_errors=True)   # Runtipi replaces the installed folder with the store copy
        shutil.copytree(src, dst)
        return {'status': 'running'}
    return finish


@pytest.mark.parametrize('operation', ['switch', 'update'])
def test_owner_private_moves_to_standard_in_same_owner(box, monkeypatch, operation):
    c, commit = box
    install_old(c, PRIVATE_ID)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {APP})
    calls = []
    monkeypatch.setattr(cli, 'app_update', finish_factory(c, calls))
    monkeypatch.setattr(cli, 'write_registry', lambda c: None)
    sel = ss.resolve(c, APP, body(commit, operation=operation))
    assert ss.execute(c, APP, sel)['status'] == 'running'
    assert calls == [(APP, {})]   # native backup-first update, no uninstall
    for tree in (c.runtipi_root / 'apps' / cli.STORE / APP, c.runtipi_root / 'repos' / cli.STORE / 'apps' / APP):
        ident = json.loads((tree / '.wizard-source').read_text())
        assert (ident['repo'], ident['source']) == (PUBLIC, 'standard')
        assert (tree / '.wizard-commit').read_text().strip() == commit
        # the old owner-private pin must not shadow the new Standard pin
        assert json.loads((tree / '.wizard-selection').read_text()) == {'repo': PUBLIC, 'commit': commit}


@pytest.mark.parametrize('installed,selection,operation,reason', [
    ({**PRIVATE_ID, 'repo': 'other/hello-history'}, {}, 'switch', 'repository conflict'),          # other owner
    ({**PRIVATE_ID, 'repo': 'ACME/hello-history'}, {}, 'install', 'already installed'),             # install of installed id
    ({'repo': 'acme/other', 'git': 'x', 'private': True, 'source': 'standard'}, {}, 'switch', 'repository conflict'),  # standard -> other
    ({'repo': PRIVATE, 'source': 'link'}, {}, 'update', 'repository conflict'),                    # link
    ({'repo': PRIVATE}, {}, 'switch', 'repository conflict'),                                      # unknown source
    ({'repo': PRIVATE, 'private': True}, {}, 'switch', 'repository conflict'),                     # legacy untagged
    ({'repo': PRIVATE, 'source': 'alpha-test'}, {}, 'switch', 'repository conflict'),              # other source
])
def test_every_other_repository_change_is_still_refused(box, monkeypatch, installed, selection, operation, reason):
    c, commit = box
    install_old(c, installed)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {APP})
    monkeypatch.setattr(private, 'prepare', lambda *a, **k: pytest.fail('must not prepare'))
    with pytest.raises(ValueError, match=reason):
        ss.execute(c, APP, ss.resolve(c, APP, body(commit, operation=operation, **selection)))


def test_owner_comparison_is_case_insensitive_and_exact(box, monkeypatch):
    c, commit = box
    install_old(c, {**PRIVATE_ID, 'repo': 'ACME/hello-history'})
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {APP})
    monkeypatch.setattr(cli, 'app_update', finish_factory(c, []))
    monkeypatch.setattr(cli, 'write_registry', lambda c: None)
    ss.execute(c, APP, ss.resolve(c, APP, body(commit)))
    assert json.loads((c.runtipi_root / 'apps' / cli.STORE / APP / '.wizard-source').read_text())['repo'] == PUBLIC


def test_install_operation_never_changes_repository(box, monkeypatch):
    c, commit = box
    install_old(c, PRIVATE_ID)
    monkeypatch.setattr(cli, 'installed_ids', lambda rt: {APP})
    monkeypatch.setattr(private, 'prepare', lambda *a, **k: pytest.fail('must not prepare'))
    with pytest.raises(ValueError, match='already installed'):
        ss.execute(c, APP, ss.resolve(c, APP, body(commit, operation='install')))


def test_direct_helper_install_is_refused_even_if_reached(box):
    """Defence in depth: the helper itself says no for install, for a link target and for a prefix-only owner match."""
    sel = {'source': 'standard', 'repo': PUBLIC, 'operation': 'install'}
    assert not ss._private_to_standard(PRIVATE_ID, sel)
    assert not ss._private_to_standard(PRIVATE_ID, {**sel, 'operation': 'switch', 'source': 'link'})
    assert not ss._private_to_standard(PRIVATE_ID, {**sel, 'operation': 'switch', 'repo': 'acme-evil/hello'})
    assert not ss._private_to_standard({**PRIVATE_ID, 'repo': 'acme-evil/x'}, {**sel, 'operation': 'switch'})
    assert not ss._private_to_standard({'source': 'owner-private'}, {**sel, 'operation': 'switch'})
    assert ss._private_to_standard(PRIVATE_ID, {**sel, 'operation': 'switch'})
