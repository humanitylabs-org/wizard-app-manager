"""Real-host regression: files must not steal the not-yet-started host bridge port."""
from unittest.mock import Mock

import pytest

from wam import cli


class Installed(Exception):
    pass


@pytest.mark.parametrize('default,taken,shell,service,expected', [
    (8794, {8794, 8795, 8796}, 8796, 8797, 8798),
    (8797, set(), 8796, 8797, 8798),
    (18996, {18996}, 18997, 18998, 18999),
    (8794, set(), 8796, 8797, 8794),
])
def test_dependency_install_reserves_pending_alongside_ports(tmp_path, monkeypatch, default, taken, shell, service, expected):
    c = cli.Config()
    c.home = tmp_path
    al = {'shell': {'port': shell}, 'service': {'port': service, 'active': False}}
    rt = Mock()
    rt.app.return_value = {'info': {'id': 'files', 'port': default}}
    got = {}

    def installed(urn, form):
        got.update(form)
        raise Installed()

    rt.install.side_effect = installed
    monkeypatch.setattr(cli.Config, 'runtipi', lambda self: rt)
    monkeypatch.setattr(cli, 'ensure_store', lambda *a: None)
    monkeypatch.setattr(cli, 'installed_ids', lambda *a: set())
    monkeypatch.setattr(cli, 'resolve_any', lambda *a: {'id': 'files', 'uses': []})
    monkeypatch.setattr(cli, 'build_form', lambda *a, **k: ({}, []))
    monkeypatch.setattr(cli, 'trusted_origin_env', lambda *a: {})
    monkeypatch.setattr(cli, 'port_free', lambda p: p not in taken)
    monkeypatch.setattr(cli.alongside, 'load', lambda *a: al)
    monkeypatch.setattr(cli.alongside, 'external_slots', lambda *a: {})
    with pytest.raises(Installed):
        cli.install(c, 'files')
    assert got.get('port', default) == expected


def test_free_port_skips_reserved_even_without_listener(monkeypatch):
    monkeypatch.setattr(cli, 'port_free', lambda p: True)
    assert cli.free_port(8795, avoid={8795, 8796, 8797}) == 8798
