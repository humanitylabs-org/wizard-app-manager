#!/usr/bin/env python3
"""Wizard style ratchet. Read source only; never import or execute app code.

Canonical implementation: wizard-os/style/check_style.py. Mirrors are distribution
artifacts. Existing violations warn; additions and changed kit copies fail with
--base. --strict checks every line (new apps). --warn is an inventory only.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess

EXTENSIONS = {'.css', '.html', '.htm', '.js', '.mjs', '.jsx', '.tsx', '.ts', '.vue', '.svelte', '.svg', '.py'}
SKIP = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'tests', 'test', 'fixtures', 'tools', 'docs', 'qa', 'skills', 'skill', 'content', 'data', 'android'}
RULES = {
    'literal-colour': re.compile(r'(?i)(?<![\w/#])#(?:[\da-f]{8}|[\da-f]{6}|[\da-f]{4}|[\da-f]{3})\b|\b(?:rgba?|hsla?)\s*\('),
    'literal-font': re.compile(r'(?i)font-family\s*:\s*(?!\s*var\(--wz-)[^;}\n]+'),
    'cyclic-token': re.compile(r'(?P<token>--wz-[\w-]+)\s*:[^;}\n]*var\(\s*(?P=token)\s*[,)]'),
    'px-type': re.compile(r'(?i)font-size\s*:\s*[^;}\n]*?\b\d*\.?\d+px|\bfont\s*:\s*[^;}\n]*?\b\d*\.?\d+px'),
}
KIT_NAMES = {'wizard-ui.css': 'ui.css', 'wizard-tokens.css': 'tokens.css', 'wizard-sheet.js': 'sheet.js'}
# Skin packages and owner tweaks *are* skin definitions (raw colours, fonts, px type by
# nature). Their literal values stay visible as warnings but never fail CI; structural
# rules (cyclic tokens, kit drift, symlinks) still fail there. Prefixes come from the
# canonical policy's "skin_layers" list so the scope is reviewed, never a per-app glob.
SKIN_LITERAL_RULES = {'literal-colour', 'literal-font', 'px-type'}
# Committed generated third-party bundles (policy "generated_vendor", e.g. web/vendor/)
# are build output: a bundler renames identifiers file-wide, so library theme colours
# would count as changed lines. They warn on every content rule, but only while the
# repo's own CI rebuilds them and fails on drift (a `git diff --exit-code` step naming
# the file or a parent folder), read from the base commit as well as the PR so a PR
# can never grant itself the skip. Kit drift and symlinks still fail there.
RUN_PREFIX = re.compile(r'^\s*(?:-\s+)?(?:run:\s*)?')
GUIDE = 'https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.DEVNULL)


def changed_lines(root, base):
    # An invalid or unavailable base is an error, never a warn-only fallback.
    git(root, 'rev-parse', '--verify', base + '^{commit}')
    diff = git(root, 'diff', '--no-ext-diff', '--no-renames', '--unified=0', base, '--', '.')
    changed, path = {}, None
    for line in diff.splitlines():
        if line.startswith('+++ b/'):
            path = line[6:]; changed.setdefault(path, set())
        elif path and line.startswith('@@'):
            m = re.search(r'\+(\d+)(?:,(\d+))?', line)
            if not m: raise ValueError("Unrecognised git diff hunk")
            start, count = int(m[1]), int(m[2] or 1)
            changed[path].update(range(start, start + count))
    # Include untracked source in local builder use.
    for path in git(root, 'ls-files', '--others', '--exclude-standard').splitlines():
        p = root / path
        if p.is_file(): changed[path] = set(range(1, len(p.read_bytes().splitlines()) + 1))
    return changed


def drift_checked_paths(texts):
    """Paths a workflow drift-checks with `git diff --exit-code|--quiet [--] <path>...`.

    Only executable commands count: YAML comments, quoted text (`echo "git diff ..."`)
    and step names are ignored; shell line continuations are joined first.
    """
    paths = set()
    for text in texts:
        for line in re.sub(r'\\\r?\n', ' ', text).splitlines():
            line = RUN_PREFIX.sub('', line, count=1)
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            try:
                toks = list(lex)
            except ValueError:
                continue
            seg = []
            for tok in toks + [';']:
                if tok and set(tok) <= set('&|;()'):
                    if seg[:2] == ['git', 'diff'] and ({'--exit-code', '--quiet'} & set(seg)):
                        for arg in seg[2:]:
                            arg = arg.rstrip('/')
                            if arg.startswith('./'): arg = arg[2:]
                            if arg and not arg.startswith('-') and not any(c in arg for c in '$*?`{}'):
                                paths.add(arg)
                    seg = []
                else:
                    seg.append(tok)
    return paths


def workflow_texts(root, ref=None):
    if ref is None:
        wf = root / '.github' / 'workflows'
        return [p.read_text(errors='replace') for p in sorted(wf.glob('*')) if p.suffix in ('.yml', '.yaml') and p.is_file()] if wf.is_dir() else []
    try:
        names = git(root, 'ls-tree', '--name-only', ref + ':.github/workflows').split()
    except subprocess.CalledProcessError:
        return []
    return [git(root, 'show', f'{ref}:.github/workflows/{n}') for n in names if n.endswith(('.yml', '.yaml'))]


def covered(rel, paths):
    return any(rel == p or rel.startswith(p + '/') for p in paths)


def scan(root, policy, *, base=None, strict=False):
    root = Path(root).resolve()
    changed = changed_lines(root, base) if base else {}
    exceptions = policy.get('exceptions', [])
    if any(not e.get('reason', '').strip() or not e.get('sha256') or not e.get('path') for e in exceptions):
        raise ValueError('Every exception needs an exact path, SHA256 and reason')
    skin_layers = tuple(policy.get('skin_layers', []))
    if any(not isinstance(x, str) or not x.endswith('/') or x.startswith(('/', '.')) or '*' in x for x in skin_layers):
        raise ValueError('skin_layers entries must be relative directory prefixes ending in /')
    vendor_layers = tuple(policy.get('generated_vendor', []))
    if any(not isinstance(x, str) or not x.endswith('/') or x.startswith(('/', '.')) or '*' in x for x in vendor_layers):
        raise ValueError('generated_vendor entries must be relative directory prefixes ending in /')
    drift = []
    if vendor_layers:
        drift = [drift_checked_paths(workflow_texts(root))]
        if base: drift.append(drift_checked_paths(workflow_texts(root, base)))
    findings = []
    for p in sorted(root.rglob('*')):
        rel = p.relative_to(root).as_posix()
        if any(part in SKIP for part in p.relative_to(root).parts) or p.suffix.lower() not in EXTENSIONS: continue
        if p.name.startswith(('test_', 'test-')) or '.test.' in p.name or '.spec.' in p.name: continue
        if p.is_symlink():
            findings.append(dict(path=rel, line=1, rule='symlink', error=strict or rel in changed)); continue
        if not p.is_file(): continue
        data = p.read_bytes(); digest = hashlib.sha256(data).hexdigest()
        if any(e['path'] == rel and e['sha256'] == digest for e in exceptions): continue
        approved = policy.get('assets', {}).get(p.name)
        if approved and digest == approved['sha256']: continue
        if approved or p.name in KIT_NAMES:
            findings.append(dict(path=rel, line=1, rule='kit-drift', error=strict or rel in changed)); continue
        # Strip comments while retaining line numbers; quoted CSS/inline HTML in JS/Python is intentionally scanned.
        text = data.decode('utf-8', errors='replace')
        text = re.sub(r'/\*[\s\S]*?\*/|<!--[\s\S]*?-->', lambda m: '\n' * m[0].count('\n'), text)
        for rule, pattern in RULES.items():
            for m in pattern.finditer(text):
                if rule == 'literal-colour' and m[0].startswith('#'):
                    before = text[max(0, m.start() - 160):m.start()]
                    after = text[m.end():m.end() + 60]
                    # Hex-like DOM IDs / fragment links are not colour values.
                    if (re.search(r'''(?:querySelector(?:All)?|closest|matches|\$)\(\s*['"]$''', before)
                            or re.search(r'''href\s*=\s*['"]$''', before)
                            or re.match(r'\s*(?:\{|::?[\w-]|\[)', after)):
                        continue
                line = text.count('\n', 0, m.start()) + 1
                if drift and rel.startswith(vendor_layers) and all(covered(rel, d) for d in drift):
                    findings.append(dict(path=rel, line=line, rule=rule, error=False, generated=True)); continue
                if rule in SKIN_LITERAL_RULES and rel.startswith(skin_layers):
                    findings.append(dict(path=rel, line=line, rule=rule, error=False, skin_layer=True)); continue
                findings.append(dict(path=rel, line=line, rule=rule, error=strict or line in changed.get(rel, set())))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('root', type=Path, nargs='?', default=Path.cwd())
    ap.add_argument('--policy', type=Path, default=Path(__file__).with_name('style-policy.json'))
    modes = ap.add_mutually_exclusive_group()
    modes.add_argument('--base'); modes.add_argument('--strict', action='store_true'); modes.add_argument('--warn', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if not (args.base or args.strict or args.warn): ap.error('Choose --base REF, --strict (new apps), or --warn (inventory)')
    try:
        rows = scan(args.root, json.loads(args.policy.read_text()), base=args.base, strict=args.strict)
    except (OSError, ValueError, subprocess.CalledProcessError) as e:
        ap.exit(2, 'Style check could not run: ' + str(e) + '\n')
    errors = [r for r in rows if r['error']]
    if args.json: print(json.dumps(rows, indent=2))
    else:
        for r in errors: print(f"ERROR {r['path']}:{r['line']}: {r['rule']}")
        print(f"Wizard style: {len(errors)} errors; {len(rows) - len(errors)} existing warnings. {GUIDE}")
    raise SystemExit(bool(errors))


if __name__ == '__main__': main()
