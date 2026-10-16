"""Deterministic real stock-Hermes discovery + terminal proof, not inference.

Run with a Python carrying stock Hermes dependencies. Both paths are explicit;
never reads the operator profile or modifies the supplied Hermes source tree.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ap = argparse.ArgumentParser()
ap.add_argument('--hermes-source', required=True, type=Path)
ap.add_argument('--state-root', required=True, type=Path)
a = ap.parse_args()
root = a.state_root.resolve()
root.mkdir(parents=True, exist_ok=True)
home = root / 'home'
if home.exists():
    raise SystemExit('Use a new state-root: no existing Hermes profile is reused')
home.mkdir()
os.environ.update(HOME=str(home), HERMES_HOME=str(home / '.hermes'),
                  WIZARD_OS_HOME=str(home / '.wizard-os'), TMPDIR=str(root / 'tmp'),
                  PYTHONDONTWRITEBYTECODE='1')
Path(os.environ['TMPDIR']).mkdir()
sys.dont_write_bytecode = True
os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']
repo = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(repo))
from wam import builder_bundle as bb
receipt = bb.install(home / '.wizard-os')
bb.register(home / '.hermes', home / '.wizard-os')
sys.path.insert(0, str(a.hermes_source.resolve()))
from tools.skills_tool import skills_list, skill_view
listing = json.loads(skills_list())
assert 'wizard-builder' in json.dumps(listing), listing
loaded = json.loads(skill_view('wizard-builder'))
assert loaded.get('success') is True, loaded
reference = json.loads(skill_view('wizard-builder', file_path='references/build.md'))
assert reference.get('success') is True, reference
system_reference = json.loads(skill_view('wizard-builder', file_path='references/system-functions.md'))
assert system_reference.get('success') is True, system_reference
from tools.terminal_tool import terminal_tool
command = ('export PATH="${WIZARD_BUILDER_ROOT:-$HOME/.wizard-os/builder}/current/bin:$PATH"; '
           'wam builder-bundle doctor && wam new habit-tracker --request "Build a habit tracker" '
           '&& wam check "$HOME/.wizard-os/dev/habit-tracker"')
terminal = json.loads(terminal_tool(command, timeout=60, workdir=str(home)))
assert terminal.get('exit_code') == 0, terminal
source = home / '.wizard-os/dev/habit-tracker'
assert (source / 'web/app.js').is_file()
# Change real source through the same terminal route, then recheck it.
edit = 'from pathlib import Path; import re; p=Path.home()/".wizard-os/dev/habit-tracker/web/index.html"; p.write_text(re.sub(r"<title>.*?</title>", "<title>Daily Practice</title>", p.read_text()))'
command = ('python3 -c ' + shlex.quote(edit) + '; "$HOME/.wizard-os/bin/wam" check "$HOME/.wizard-os/dev/habit-tracker"')
modified = json.loads(terminal_tool(command, timeout=60, workdir=str(home)))
assert modified.get('exit_code') == 0, modified
assert '<title>Daily Practice</title>' in (source / 'web/index.html').read_text()
# The installed bundle must enforce the gate, not just discover its prose.
bad = source / 'web/duplicate-platform.js'
bad.write_text('navigator.share({text: "duplicate"}); new MediaRecorder(stream);')
negative = json.loads(terminal_tool('"$HOME/.wizard-os/bin/wam" check "$HOME/.wizard-os/dev/habit-tracker"', timeout=60, workdir=str(home)))
assert negative.get('exit_code') == 1, negative
assert 'system functions:' in negative.get('output', ''), negative
bad.unlink()
result = {'host': os.uname().nodename,
          'wam_sha': subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip(),
          'hermes_sha': subprocess.check_output(['git', '-C', str(a.hermes_source), 'rev-parse', 'HEAD'], text=True).strip(),
          'install': receipt, 'skills_list': listing, 'skill_loaded': loaded.get('name'),
          'reference_loaded': reference.get('success'), 'terminal_create_check': terminal,
          'terminal_modify_check': modified, 'terminal_gate_rejection': negative,
          'system_reference_loaded': system_reference.get('success'), 'inference': False,
          'operator_profile_used': False, 'shared_runtime_modified': False}
(root / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
