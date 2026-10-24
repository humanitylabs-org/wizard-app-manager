"""Managed Mac uses the App Manager API, never the unpublished queue port."""
import json
from unittest.mock import Mock

import pytest

from wam import cli


@pytest.mark.parametrize("argv,backup", [
    (["app", "update", "files"], True),
    (["app", "update", "files", "--no-backup"], False),
])
def test_files_cli_update_does_not_passthrough_to_runtipi_cli(monkeypatch, capsys, argv, backup):
    rt = Mock()
    rt.app.side_effect = [
        {"app": {"status": "running", "version": "1.0.0"}},
        {"app": {"status": "running", "version": "1.0.1"}},
    ]
    rt.wait_status.return_value = "running"
    monkeypatch.setattr(cli.Config, "runtipi", lambda self: rt)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "runtipi_cli", lambda *a: pytest.fail("unpublished queue transport used"))
    assert cli.main(argv) == 0
    rt.update.assert_called_once_with("files:wizardapps", perform_backup=backup)
    rt.start.assert_not_called()
    assert json.loads(capsys.readouterr().out)["status"] == "running"
