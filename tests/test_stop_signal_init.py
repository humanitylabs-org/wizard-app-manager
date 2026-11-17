"""wizard-app-manager#738: containers WAM writes must stop on `docker stop`, not after Docker's timeout.

Python or Node as a container's first process (PID 1) ignores SIGTERM unless it handles it, so Docker waited out its
stop timeout and killed it on every restart, update and reboot. Docker's init (`init: true`) passes the signal on.
"""
import json
from pathlib import Path

import yaml

from wam import builder
from wam.builder_assets import package_inline
from wam.release_pins import configure_manager

REPO = Path(__file__).resolve().parents[1]


def test_packaged_inline_app_runs_python_under_docker_init(tmp_path):
    root = builder.new_app('to-dos', tmp_path, 'a to-do list', name='To-dos', kind='list')
    package_inline(root)
    service = yaml.safe_load((root / 'runtipi/docker-compose.yml').read_text())['services']['to-dos']
    assert service['command'][0] == 'python'
    assert service['init'] is True


def test_hello_wizard_template_runs_python_under_docker_init():
    service = yaml.safe_load((REPO / 'templates/hello-wizard/runtipi/docker-compose.yml').read_text())['services']['hello-wizard']
    assert service['command'][0] == 'python'
    assert service['init'] is True


def test_release_index_runs_node_under_docker_init(tmp_path):
    pins = tmp_path / 'pins.json'
    pins.write_text((REPO / 'releases/0.2.2.json').read_text())
    path = tmp_path / 'compose.yml'
    configure_manager(path, 'example/manager:pinned', str(pins))
    service = yaml.safe_load(path.read_text())['services']['wizard-release-index']
    assert service['entrypoint'][0] == 'node'
    assert service['init'] is True
