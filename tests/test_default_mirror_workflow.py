"""Custody maintenance stays main-only, scoped and on the trusted external runner."""
from pathlib import Path
import yaml


def test_scoped_thales_schedule():
    text = (Path(__file__).resolve().parents[1] / '.github/workflows/default-mirrors.yml').read_text()
    workflow = yaml.safe_load(text)
    events = workflow.get('on', workflow.get(True))
    assert set(events) == {'schedule', 'workflow_dispatch'}
    job = workflow['jobs']['mirror']
    assert job['if'] == "github.ref == 'refs/heads/main'"
    assert job['runs-on'] == ['self-hosted', 'macOS', 'ARM64', 'wizard-app-manager']
    assert workflow['permissions'] == {'contents': 'write', 'packages': 'write'}
    assert 'secrets.GITHUB_TOKEN' in text
    assert 'secrets.DEFAULT_MIRROR_SSH_KEY' not in text
    assert 'if: always()' in text
    assert 'install_crane.py' in text
    assert 'go install' not in text
