"""QA-only, exact Bun 1.3.0 saved-output inventory (not installed API proof)."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MIB = 1024**2
# Observed outputs of the source-bound 3116f500 candidate-backend-bundle step.
# Filenames are NOT a *.node exemption: the saved archive, JS references and
# lock-bound package source maps must independently agree with this inventory.
SAVED_BUNDLE = {
    'main.js': (6855279, 'c2eeccd5b47ff5cdada61050d11b9f8f19e6f3dfc770f805b510a3e7b1b66173'),
    'main.js.map': (20939273, '677ac827d5f4d5688d039f59c2da7a59c2e764d224941757083ccaa7dcd340c2'),
    'cpufeatures-e8x7s2f1.node': (60552, '264e4d7eba6ff421a4078b9272848b27f1803879aee448bc2b13ae93f352efab'),
    'sshcrypto-a3khcfhj.node': (83264, '616ce07eaa7e936b114efeb64256aaf487934f9be515e34cd46753b399ae5f59'),
}
ADDONS = {
    'cpufeatures-e8x7s2f1.node': ('cpu-features', '0.0.10', 'lib/index.js', "require('../build/Release/cpufeatures.node')",
        'sha512-9IkYqtX3YHPCzoVg1Py+o9057a3i0fp7S530UWokCSaFVTc7CwXPRiOjRjBQQ18ZCNafx78YfnG+HALxtVmOGA=='),
    'sshcrypto-a3khcfhj.node': ('ssh2', '1.17.0', 'lib/protocol/crypto.js', "require('./crypto/build/Release/sshcrypto.node')",
        'sha512-wPldCk3asibAjQ/kziWQQt1Wh3PgDFpC0XpwclzKcdT1vql6KeYxf5LIt4nlFkUeR8WuphYMKqUA56X4rjbfgQ=='),
}


def regular_bytes(path, cap):
    """No links, special files, hardlinks, sparse/oversize reads or stat/open races."""
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1, 'bundle requires regular single-link file'
    assert 0 < metadata.st_size <= cap, 'bundle file size bound'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        assert (actual.st_dev, actual.st_ino, actual.st_size) == (metadata.st_dev, metadata.st_ino, metadata.st_size), 'bundle open identity'
        data = stream.read(cap+1)
        after = os.fstat(stream.fileno())
    assert len(data) == metadata.st_size and len(data) <= cap, 'bundle bounded complete read'
    assert (after.st_size, after.st_mtime_ns, after.st_ctime_ns) == (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns), 'bundle mutated during read'
    return data


def validate_bundle(dist, source, *, saved=False, installed=None):
    assert stat.S_ISDIR(dist.lstat().st_mode), 'bundle root must be real directory'
    files = list(dist.iterdir())
    assert {p.name for p in files} == set(SAVED_BUNDLE), 'unexpected or missing bundle inventory'
    blobs = {p.name: regular_bytes(p, 64*MIB if p.suffix != '.node' else 4*MIB) for p in files}
    assert sum(map(len, blobs.values())) <= 512*MIB, 'bundle total bound'
    inventory = {name: {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in blobs.items()}
    if saved:
        assert {n: (v['bytes'], v['sha256']) for n, v in inventory.items()} == SAVED_BUNDLE, 'saved bundle bytes drift'
    js = blobs['main.js'].decode('utf-8')
    assert len(js) > 1024 and js.count('//# sourceMappingURL=main.js.map') == 1, 'bundle sourcemap reference'
    references = re.findall(r'[\"\'](\./[^\"\'\n]*\.node)[\"\']', js)
    assert sorted(references) == sorted('./'+name for name in ADDONS), 'bundle native references must exactly match emitted addons'
    source_map = json.loads(blobs['main.js.map'])
    assert source_map['version'] == 3 and len(source_map['sources']) == len(source_map['sourcesContent']), 'bundle sourcemap shape'
    lock = (source/'bun.lock').read_text()
    build = (source/'packages/backend/build.ts').read_text()
    assert "target: 'node'" in build and 'sourcemap: true' in build and "outdir: './dist'" in build, 'pinned Bun build contract'
    provenance = {}
    for name, (package, version, relative, binding, integrity) in ADDONS.items():
        # ELF64, little endian, ET_DYN, EM_X86_64: syntax-checking JS alone
        # would neither verify these bytes nor prove the loader's references.
        data = blobs[name]
        assert data[:6] == b'\x7fELF\x02\x01' and data[16:20] == b'\x03\x00\x3e\x00', 'native output ELF architecture/type'
        line = next(line for line in lock.splitlines() if line.startswith('    "'+package+'": ['))
        assert '"'+package+'@'+version+'"' in line and '"'+integrity+'"' in line, 'native package lock pin'
        mapped = '../../../node_modules/.bun/'+package+'@'+version+'/node_modules/'+package+'/'+relative
        assert source_map['sources'].count(mapped) == 1, 'native package source-map identity'
        content = source_map['sourcesContent'][source_map['sources'].index(mapped)]
        assert binding in content, 'native package binding inclusion'
        if installed is not None:
            physical = installed/'node_modules/.bun'/str(package+'@'+version)/'node_modules'/package
            assert json.loads((physical/'package.json').read_text())['version'] == version, 'installed package version'
            assert (physical/relative).read_text() == content, 'frozen package output differs from saved source map'
        provenance[name] = {'package': package, 'version': version, 'integrity': integrity,
            'source_map_path': mapped, 'source_sha256': hashlib.sha256(content.encode()).hexdigest(),
            'original_require': binding, 'emitted_reference': './'+name, 'elf64_x86_64_shared': True,
            'frozen_install_source_compared': installed is not None}
    return {'files': inventory, 'native_provenance': provenance,
        'saved_bytes_pinned': saved, 'bun_version': '1.3.0', 'build_ts_sha256': hashlib.sha256(build.encode()).hexdigest(),
        'bun_lock_sha256': hashlib.sha256(lock.encode()).hexdigest(), 'classification': 'saved backend bundle only; not API/image acceptance'}


def failure_record(exc, phase):
    """Trusted code locations and fixed reason codes, never exception payloads."""
    reasons = {AssertionError: 'assertion_failed', RuntimeError: 'runtime_failed',
               OSError: 'os_failed', ValueError: 'invalid_data', KeyError: 'missing_field'}
    kind = next((t for t in reasons if isinstance(exc, t)), None)
    frames = []
    tb = exc.__traceback__
    trusted = {'manager-seal-docker.py', 'manager_seal_bundle.py', 'manager_seal_continuation.py'}
    scripts = Path(__file__).resolve().parent
    while tb:
        file = Path(tb.tb_frame.f_code.co_filename).resolve()
        if file.parent == scripts and file.name in trusted:
            frames.append({'file': file.name, 'function': tb.tb_frame.f_code.co_name, 'line': tb.tb_lineno})
        tb = tb.tb_next
    return {'phase': phase, 'reason': reasons.get(kind, 'unexpected_exception'),
            'exception_type': kind.__name__ if kind else 'Exception', 'frames': frames[-8:]}
