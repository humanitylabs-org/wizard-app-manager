#!/bin/sh
# Run inside the isolated QA image with /opt/wizard-builder:ro and /work:rw.
set -eu
export PATH="$WIZARD_BUILDER_ROOT/current/bin:$PATH"
wam builder-bundle doctor
wam new container-proof --request 'Build a habit tracker'
wam check "$HOME/.wizard-os/dev/container-proof"
python3 -c 'from pathlib import Path; p=Path.home()/".wizard-os/dev/container-proof/web/index.html"; p.write_text(p.read_text().replace("</title>", " - edited</title>"))'
wam package-inline "$HOME/.wizard-os/dev/container-proof"
wam check "$HOME/.wizard-os/dev/container-proof"
python3 - <<'PY'
from pathlib import Path
import errno
p = Path('/opt/wizard-builder/current/system-skills/wizard-builder/SKILL.md')
assert p.is_file()
assert (p.parent / 'references/build.md').is_file()
try:
    p.write_text('must not write')
except OSError as e:
    assert e.errno in (errno.EROFS, errno.EACCES), e
else:
    raise AssertionError('bundle was writable')
assert ' - edited</title>' in (Path.home()/'.wizard-os/dev/container-proof/web/index.html').read_text()
print('PASS: non-root read-only bundle, references, create/check/edit/package-inline/check; no source checkout')
PY
