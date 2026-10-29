#!/usr/bin/env python3
"""Audit COPY closure and emit a minimal context from an exact Git archive.

This deliberately supports only this repository's simple dockerignore grammar,
not all Docker syntax. Unknown syntax fails closed. No Docker/compiler executes;
the subsequent real Docker build remains the authoritative packaging check.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import shlex
import tarfile

NATIVE = 'packages/backend/native/bind-metadata.c'


def read(archive, name):
    stream = archive.extractfile(name)
    assert stream is not None, 'non-file context member: ' + str(name)
    return stream.read()


def rules(text):
    result = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        include = line.startswith('!')
        pattern = line[1:] if include else line
        assert re.fullmatch(r'[A-Za-z0-9_.*/-]+', pattern), 'unsupported dockerignore syntax'
        pattern = pattern.strip('/')
        assert pattern and '..' not in pattern.split('/'), 'unsafe ignore pattern'
        # Match a path or one of its ancestors, with root-relative Go globs.
        pieces = pattern.split('/')
        expression = ''
        for i, piece in enumerate(pieces):
            if piece == '**':
                expression += '(?:[^/]+/)*' if i < len(pieces) - 1 else '.*'
            else:
                assert '**' not in piece, 'unsupported embedded **'
                expression += re.escape(piece).replace(r'\*', '[^/]*')
                if i < len(pieces) - 1:
                    expression += '/'
        result.append((include, re.compile('^' + expression + '$')))
    return result


def included(name, patterns):
    parts = name.split('/')
    ancestors = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
    value = True
    for include, pattern in patterns:
        if any(pattern.fullmatch(p) for p in ancestors):
            value = include
    return value


def copy_sources(text):
    sources = []
    for line in text.splitlines():
        if not re.match(r'^COPY\s', line, re.I):
            continue
        assert not line.endswith('\\'), 'multiline COPY needs explicit audit'
        words = shlex.split(line)
        if any(w.startswith('--from=') for w in words[1:]):
            continue  # Stage outputs, not local context inputs.
        assert not words[1].startswith('--') and not words[1].startswith('['), 'unsupported COPY form'
        for word in words[1:-1]:
            name = word.removeprefix('./').rstrip('/')
            assert name and not any(c in name for c in '*?[]$') and '..' not in PurePosixPath(name).parts, 'unsupported COPY source'
            sources.append(name)
    assert NATIVE in sources, 'native COPY disappeared'
    return sources


def audit(archive_path, ignore_override=None):
    with tarfile.open(archive_path) as archive:
        members = {m.name.rstrip('/'): m for m in archive if not m.isdir()}
        assert all(m.isfile() for m in members.values()), 'context source links/special files need explicit audit'
        assert all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in members)
        raw = {n: read(archive, n) for n in members}
        ignore = raw['.dockerignore'].decode() if ignore_override is None else ignore_override
        patterns = rules(ignore)
        selected = {n: b for n, b in raw.items() if included(n, patterns)}
        assert NATIVE in selected, 'native source excluded by .dockerignore'
        assert selected[NATIVE] == raw[NATIVE] and selected[NATIVE], 'native bytes missing'
        copy_audit = []
        for source in copy_sources(raw['Dockerfile'].decode()):
            matches = sorted(n for n in selected if n == source or n.startswith(source + '/'))
            assert matches, 'COPY input missing from filtered context: ' + source
            copy_audit.append({'source': source, 'member_count': len(matches)})
        assert not any(n.startswith(('qa/', 'tests/', 'templates/', '.venv/')) for n in selected)
        assert {n for n in selected if n.startswith('wam/')} == {'wam/hermes-lock.json'}
        assert {n for n in selected if n.startswith('packages/backend/native/')} == {NATIVE}
        for forbidden in ('packages/backend/native/private.c', 'qa/private.json', 'tests/test_private.py', 'wam/private.py', '.env'):
            assert not included(forbidden, patterns), 'private fixture unexpectedly included: ' + forbidden
        # Docker always receives these control files, though COPY cannot use them.
        selected.update({n: raw[n] for n in ('Dockerfile', '.dockerignore')})
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w', format=tarfile.PAX_FORMAT) as output:
            for name, data in sorted(selected.items()):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = members[name].mode
                output.addfile(info, io.BytesIO(data))
        context = stream.getvalue()
        # Check the GENERATED context, not merely a prediction of membership.
        with tarfile.open(fileobj=io.BytesIO(context)) as generated:
            assert read(generated, NATIVE) == raw[NATIVE]
            assert set(generated.getnames()) == set(selected)
        proof = {'method': 'strict repository dockerignore subset; generated tar readback; real Docker build still required',
                 'source_archive_sha256': hashlib.sha256(Path(archive_path).read_bytes()).hexdigest(),
                 'dockerignore_sha256': hashlib.sha256(ignore.encode()).hexdigest(),
                 'context_sha256': hashlib.sha256(context).hexdigest(), 'member_count': len(selected),
                 'native_sha256': hashlib.sha256(raw[NATIVE]).hexdigest(), 'copy_inputs': copy_audit,
                 'members': {n: hashlib.sha256(b).hexdigest() for n, b in sorted(selected.items())}}
        return proof, context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--negative-control', action='store_true')
    args = parser.parse_args()
    proof, context = audit(args.archive)
    if args.negative_control:
        with tarfile.open(args.archive) as archive:
            ignore = read(archive, '.dockerignore').decode()
        old = ignore.replace('!'+NATIVE+'\n', '')
        assert old != ignore
        try:
            audit(args.archive, old)
        except AssertionError as error:
            assert str(error) == 'native source excluded by .dockerignore'
        else:
            raise AssertionError('old-ignore negative control did not fail')
        proof['old_ignore_negative_control_rejected'] = True
    if args.output:
        with args.output.open('xb') as output:
            output.write(context)
    print(json.dumps(proof, sort_keys=True))


if __name__ == '__main__':
    main()
