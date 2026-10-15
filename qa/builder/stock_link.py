"""Real stock CLI install/refresh/unlink proof in a disposable home (no model)."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument('--hermes-source', required=True, type=Path)
ap.add_argument('--state-root', required=True, type=Path)
ap.add_argument('--plugin-ref', required=True)
a = ap.parse_args()
root = a.state_root.resolve()
if root.exists():
    raise SystemExit('Use a new state-root')
root.mkdir(parents=True)
home, hh, osh = root / 'home', root / 'home/.hermes', root / 'home/.wizard-os'
hh.mkdir(parents=True)
config = '# owner config\nmodel: preserved-owner-model\n'
(hh / 'config.yaml').write_text(config)
(root / 'bin').mkdir()
shim = root / 'bin/hermes'
shim.write_text(f'#!{sys.executable}\nimport sys\nsys.dont_write_bytecode = True\nsys.path.insert(0, {str(a.hermes_source.resolve())!r})\nfrom hermes_cli.main import main\nmain()\n')
shim.chmod(0o755)
repo = Path(__file__).resolve().parents[2]
env = {**os.environ, 'HOME': str(home), 'HERMES_HOME': str(hh), 'WIZARD_OS_HOME': str(osh),
       'PATH': str(root / 'bin') + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH'],
       'PYTHONDONTWRITEBYTECODE': '1'}
results = []
for verb in ('link', 'link', 'unlink'):
    cmd = [sys.executable, str(repo / 'wizard-cli/wizard'), verb, '--hermes-home', str(hh)]
    if verb == 'link':
        cmd += ['--plugin-ref', a.plugin_ref]
    p = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=180)
    results.append({'command': cmd, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr})
    (root / 'receipt.json').write_text(json.dumps(results, indent=2) + '\n')
    assert p.returncode == 0, results[-1]
    if verb == 'link':
        plugin = hh / 'plugins/wizard-os'
        assert any('wizard-builder' in p.read_text() for p in plugin.rglob('*.py'))
assert (hh / 'config.yaml').read_text() == config
assert (osh / 'system-skills/wizard-builder/SKILL.md').is_file()
print('PASS: real stock CLI link, pinned routing plugin install, refresh, byte-identical unlink; OS bundle retained')
print(root / 'receipt.json')
