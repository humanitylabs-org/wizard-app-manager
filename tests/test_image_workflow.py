"""Execute the workflow's publish Bash against fake Docker, never a registry."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load((REPO / ".github/workflows/image.yml").read_text(), Loader=yaml.BaseLoader)
PUBLISH_SCRIPT = WORKFLOW["jobs"]["publish"]["steps"][-1]["run"]


def test_workflow_publication_guards_and_native_runners():
    build = WORKFLOW["jobs"]["build"]
    publish = WORKFLOW["jobs"]["publish"]
    assert {x["platform"]: x["runner"] for x in build["strategy"]["matrix"]["include"]} == {
        "linux/amd64": "ubuntu-latest", "linux/arm64": "ubuntu-24.04-arm"}
    assert build["strategy"]["fail-fast"] == "false"
    assert build["timeout-minutes"] == "40"
    assert publish["timeout-minutes"] == "10"
    assert publish["needs"] == "build"
    assert publish["if"] == "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    assert WORKFLOW["env"]["PUBLISH"] == "${{ github.event_name != 'pull_request' && github.ref == 'refs/heads/main' }}"
    image_build = next(s for s in build["steps"] if s.get("id") == "build")
    assert "|| 'type=cacheonly'" in image_build["with"]["outputs"]
    for job in (build, publish):
        assert job["steps"][0]["with"]["fetch-depth"] == "0"
    assert "RUN timeout 600 bun install --frozen-lockfile\n" in (REPO / "Dockerfile").read_text()


@pytest.mark.parametrize("digests,platforms,inspect_failure,success", [
    (["a" * 64, "b" * 64], [("linux", "amd64"), ("linux", "arm64"), ("unknown", "unknown")], False, True),
    (["a" * 64, "b" * 64], [("linux", "amd64")], False, False),
    (["a" * 64, "b" * 64], [("linux", "amd64"), ("windows", "arm64")], False, False),
    (["a" * 64, "b" * 64], [("linux", "amd64"), ("linux", "arm64")], True, False),
    (["a" * 64], [], False, False),
    ([], [], False, False),
    (["invalid", "b" * 64], [], False, False),
])
def test_publish_checks_version_before_changing_latest(tmp_path, digests, platforms, inspect_failure, success):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    directory = tmp_path / "digests"
    directory.mkdir()
    for digest in digests:
        (directory / digest).touch()
    calls = tmp_path / "calls.jsonl"
    manifest = {"manifests": [{"platform": {"os": os_, "architecture": arch}} for os_, arch in platforms]}
    docker = bin_dir / "docker"
    docker.write_text(f'''#!{sys.executable}
import json, os, sys
with open(os.environ['CALLS'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[3] == 'inspect':
    if os.environ['FAIL_INSPECT'] == '1':
        raise SystemExit(1)
    print(os.environ['MANIFEST'])
''')
    docker.chmod(0o755)
    env = {"PATH": str(bin_dir), "RUNNER_TEMP": str(tmp_path), "IMAGE": "example.invalid/wam",
           "VERSION": "v-test", "CALLS": str(calls), "MANIFEST": json.dumps(manifest),
           "FAIL_INSPECT": str(int(inspect_failure))}
    result = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", PUBLISH_SCRIPT],
                            cwd=directory, env=env, capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) == success, result.stderr
    log = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    latest_create = [c for c in log if c[:3] == ["buildx", "imagetools", "create"] and "example.invalid/wam:latest" in c]
    if success:
        assert log == [
            ["buildx", "imagetools", "create", "-t", "example.invalid/wam:v-test",
             "example.invalid/wam@sha256:" + "a" * 64, "example.invalid/wam@sha256:" + "b" * 64],
            ["buildx", "imagetools", "inspect", "--raw", "example.invalid/wam:v-test"],
            ["buildx", "imagetools", "create", "-t", "example.invalid/wam:latest", "example.invalid/wam:v-test"],
            ["buildx", "imagetools", "inspect", "--raw", "example.invalid/wam:latest"],
        ]
    else:
        assert latest_create == []
