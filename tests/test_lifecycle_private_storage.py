"""Keep the lightweight real-filesystem lifecycle regression in normal pytest CI."""
import os
from pathlib import Path
import shutil
import subprocess


def test_private_storage_lifecycle_node_regressions():
    node = shutil.which("node")
    assert node, "Node 22.18+ is required for the lifecycle filesystem regression"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--experimental-strip-types", "--test", "tests/lifecycle-private-storage.test.mjs"],
        cwd=root,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
