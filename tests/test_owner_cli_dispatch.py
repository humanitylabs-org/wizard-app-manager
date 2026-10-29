"""Black-box finite command dispatch; synthetic runtimes and HOME only."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_only_new_command_moves_runtime_and_existing_cli_bytes_are_untouched(tmp_path):
    roots = {key: tmp_path / key for key in ('legacy_source', 'updates_source')}
    for key, root in roots.items():
        (root / 'wam').mkdir(parents=True)
        (root / 'wam/__init__.py').write_text('')
        (root / 'wam/cli.py').write_text('import json,sys;print(json.dumps(' + repr(key) + '));print(json.dumps(sys.argv[1:]))')
    cfg = tmp_path / 'dispatch.json'
    cfg.write_text(json.dumps({**{k: str(v) for k, v in roots.items()}, 'python': sys.executable}))
    script = Path(__file__).resolve().parents[1] / 'tools/owner-app-updates-cli-dispatch.py'
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    for args in (['app-updates'], ['app-updates', '--ask'], ['updates', '--ensure-timer'], ['app', 'update', 'demo'], ['apps', '--json']):
        r = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=True,
                           cwd=tmp_path, env={**os.environ, 'HOME': str(tmp_path), 'WAM_CLI_DISPATCH_CONFIG': str(cfg),
                                              'PYTHONDONTWRITEBYTECODE': '1'})
        marker, read_args = [json.loads(line) for line in r.stdout.splitlines()]
        assert marker == ('updates_source' if args[0] == 'app-updates' else 'legacy_source')
        assert read_args == args
    assert {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
