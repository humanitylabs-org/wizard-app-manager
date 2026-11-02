"""Regression proofs for real Store install failures (no production writes)."""
import json
from types import SimpleNamespace
import pytest
from wam import cli, private, links, store_install, store_selection, test_catalog

PIN = 'a' * 40


def test_required_origin_list_uses_owner_routes_only(tmp_path, monkeypatch):
    c = SimpleNamespace(home=tmp_path, secrets=tmp_path/'secrets')
    (tmp_path/'links.json').write_text(json.dumps({'host':'box.ts.net'}))
    monkeypatch.setattr(cli.alongside, 'load', lambda home:{'shell':{'port':8796}})
    monkeypatch.setattr(store_install, 'served_origins', lambda host:{'https://box.ts.net:8444','https://box.ts.net:8806','https://box.ts.net:7777'})
    monkeypatch.setattr(links, 'serve_status', lambda:{'Web':{
        'box.ts.net:8796':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}},
        'evil.ts.net:8796':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:8796'}}},
        'box.ts.net:2222':{'Handlers':{'/':{'Proxy':'http://127.0.0.1:2222'}}}},
        'AllowFunnel':{'box.ts.net:7777':True}})
    info={'form_fields':[{'type':'url','env_variable':'SAMPLE_ALLOWED_ORIGINS','required':True}]}
    form, missing=cli.build_form(c,'sample',info,{})
    assert not missing
    assert form['SAMPLE_ALLOWED_ORIGINS']=='https://box.ts.net:8444,https://box.ts.net:8796,https://box.ts.net:8806'
    assert cli.build_form(c,'sample',info,{'SAMPLE_ALLOWED_ORIGINS':'https://explicit.test'})[0]['SAMPLE_ALLOWED_ORIGINS']=='https://explicit.test'
    monkeypatch.setattr(links,'serve_status',lambda: (_ for _ in ()).throw(ValueError('unavailable')))
    assert cli.build_form(c,'sample',info,{})[1]
    info['form_fields'][0]['env_variable']='PROVIDER_URL'
    assert cli.build_form(c,'sample',info,{})[1]


def test_private_recipe_source_is_bound_to_repository(tmp_path, monkeypatch):
    def fetch(repo, commit, dst, tok):
        dst.mkdir(parents=True)
        (dst/'wizard.md').write_text('---\nid: sample\nname: Sample\nruntime:\n  config: config.json\n  compose: docker-compose.yml\n---\nSample\n')
        (dst/'config.json').write_text(json.dumps({'id':'sample','port':9000}))
        (dst/'docker-compose.yml').write_text('services:\n  sample:\n    image: example/sample:1\n')
    monkeypatch.setattr(private,'fetch',fetch)
    private.prepare(tmp_path,'wizardapps','org/sample',tok='test',source_pin={'id':'sample','repo':'org/sample','commit':PIN})
    cfg=json.loads((tmp_path/'repos/wizardapps/apps/sample/config.json').read_text())
    assert cfg['source']=='https://github.com/org/sample'


@pytest.mark.parametrize("data_path", [None, "custom", "custom/app-data", ".wizard-source/disk",
                                      "app-data/.wizard-source/disk"],
                         ids=["default", "custom", "custom-suffix", "ancestor-collision", "silent-wrong-path"])
@pytest.mark.parametrize("pkg", ["omitted", None, "source", "image", "build"],
                         ids=["omitted", "null", "source", "image", "build"])
def test_private_package_host_copy_and_container_build_context(tmp_path, monkeypatch, data_path, pkg):
    """Use real prepare/copy logic; a same-named host ancestor must not affect the container context."""
    from pathlib import Path
    import yaml
    from wam import mirrors, packaging

    root = tmp_path / "runtipi"
    root.mkdir()
    if data_path is not None:
        (root / ".env").write_text(f"RUNTIPI_APP_DATA_PATH={tmp_path / data_path}\n")
    host_data = root / "app-data" if data_path is None else tmp_path / data_path
    if data_path is not None and host_data.name != "app-data":
        host_data /= "app-data"
    host = host_data / ".wizard-source/wizardapps" / f"sample@{PIN[:12]}"
    service = {"build": {"context": "app", "dockerfile": "Dockerfile"}} if pkg == "build" else {"image": "example/sample:1"}
    if pkg in ("source", "build"):
        service["volumes"] = ["${APP_SOURCE_DIR}/app:/opt/app:ro"]

    def fetch(repo, commit, dst, tok):
        assert (repo, commit, tok) == ("org/sample", PIN, "test")
        dst.mkdir(parents=True)
        runtime = {"config": "config.json", "compose": "docker-compose.yml"}
        if pkg != "omitted":
            runtime["package"] = pkg
        (dst/'wizard.md').write_text('---\n' + yaml.safe_dump({"id": "sample", "name": "Sample", "runtime": runtime}) + '---\nSample\n')
        (dst/'config.json').write_text(json.dumps({'id':'sample','port':9000}))
        (dst/'docker-compose.yml').write_text(yaml.safe_dump({"services": {"sample": service}}))
        (dst/'.git').mkdir()
        (dst/'.git'/'HEAD').write_text('synthetic')
        (dst/'app').mkdir()
        (dst/'app'/'Dockerfile').write_text('FROM scratch\n')

    original_source_dir = packaging.source_dir
    def source_dir(app_data, *identity):
        if app_data == Path('/app-data'):
            assert pkg == 'build', 'non-build packages must not compute a container build root'
        return original_source_dir(app_data, *identity)

    monkeypatch.setattr(packaging, 'source_dir', source_dir)
    monkeypatch.setattr(private,'fetch',fetch)
    monkeypatch.setattr(mirrors, 'resolve_images', lambda compose: None)
    monkeypatch.setattr(private, 'pull', lambda *a: pytest.fail('no real image pull'))
    result = private.prepare(root,'wizardapps','org/sample',tok='test',source_pin={'id':'sample','repo':'org/sample','commit':PIN})
    assert result['install']['commit'] == PIN
    svc = yaml.safe_load((root/'repos/wizardapps/apps/sample/docker-compose.yml').read_text())['services']['sample']
    if pkg == 'build':
        assert svc['build'] == {'context': f'/app-data/.wizard-source/wizardapps/sample@{PIN[:12]}/app', 'dockerfile': 'Dockerfile'}
        assert svc['image'] == f'wizard-local/sample-sample:{PIN[:12]}'
        assert svc['pull_policy'] == 'build'
    else:
        assert 'build' not in svc
        assert svc['image'] == service['image']
    if pkg in ('source', 'build'):
        assert svc['volumes'] == ['${APP_SOURCE_DIR}/app:/opt/app:ro']
        assert (host/'app'/'Dockerfile').read_text() == 'FROM scratch\n'
        assert not (host/'.git').exists()
    else:
        assert not host.exists()


def test_incompatible_release_rejected_before_any_fetch(tmp_path, monkeypatch):
    entry={'id':'sample','repo':'org/sample','commit':PIN,'architectures':['arm64']}
    monkeypatch.setattr(test_catalog.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='x86_64\n',returncode=0))
    monkeypatch.setattr(private,'fetch',lambda *a:pytest.fail('no source download'))
    with pytest.raises(private.PrivateError,match='not this Wizard'):
        private.prepare(tmp_path,'wizardapps','org/sample',tok='test',release=entry)
    monkeypatch.setattr(test_catalog,'resolve',lambda app:entry)
    with pytest.raises(ValueError,match='not this Wizard'):
        store_selection.resolve(SimpleNamespace(),'sample',{'source':'owner-private','repo':'org/sample','commit':PIN,'operation':'install'})


def test_required_origin_list_with_empty_default_gets_owner_routes(tmp_path, monkeypatch):
    """Wisdom's recipe declares WISDOM_ORIGINS as required text with default ''. Runtipi treats '' as satisfied, so a
    fresh-box Store Get left it empty and Wisdom answered 421 'Unknown host' inside the shell. An empty default on a
    required origin list is no value: fill it from the owner's Serve mounts, keep explicit and non-empty values."""
    c = SimpleNamespace(home=tmp_path, secrets=tmp_path/'secrets')
    (tmp_path/'links.json').write_text(json.dumps({'host':'box.ts.net'}))
    monkeypatch.setattr(cli.alongside, 'load', lambda home: None)   # fresh box
    monkeypatch.setattr(store_install, 'served_origins', lambda host: {'https://box.ts.net:8796'})
    monkeypatch.setattr(links, 'serve_status', lambda: {'Web': {}})
    field = {'type':'text','label':'Trusted Wizard browser origin','env_variable':'WISDOM_ORIGINS','required':True,'default':''}
    info = {'form_fields':[field]}
    form, missing = cli.build_form(c,'wisdom',info,{})
    assert not missing
    assert form['WISDOM_ORIGINS'] == 'https://box.ts.net:8796'
    assert cli.build_form(c,'wisdom',info,{'WISDOM_ORIGINS':'https://explicit.test'})[0]['WISDOM_ORIGINS'] == 'https://explicit.test'
    # A real (non-empty) default stays the recipe's choice; optional and non-origin fields are untouched.
    assert 'WISDOM_ORIGINS' not in cli.build_form(c,'wisdom',{'form_fields':[dict(field, default='https://x.test')]},{})[0]
    assert 'WISDOM_ORIGINS' not in cli.build_form(c,'wisdom',{'form_fields':[dict(field, required=False)]},{})[0]
    assert 'WISDOM_TIMEZONE' not in cli.build_form(c,'wisdom',{'form_fields':[dict(field, env_variable='WISDOM_TIMEZONE')]},{})[0]
    # No owner mount known: the empty default still applies (Runtipi accepts it), nothing invented.
    monkeypatch.setattr(store_install, 'served_origins', lambda host: set())
    assert 'WISDOM_ORIGINS' not in cli.build_form(c,'wisdom',info,{})[0]
