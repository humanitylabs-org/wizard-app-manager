"""Install a checksum-pinned upstream crane for the supported runner platforms."""
import hashlib
import io
from pathlib import Path
import platform
import sys
import tarfile
import urllib.request

RELEASES = {
    ('Darwin', 'arm64'): ('Darwin_arm64', 'ae30b28498de3e1ac51a37042eab46116ec70d86aa9d10431a1fbde35b9918b9'),
    ('Linux', 'x86_64'): ('Linux_x86_64', 'c1d593d01551f2c9a3df5ca0a0be4385a839bd9b86d4a76e18d7b17d16559127'),
}


def release(system=None, machine=None):
    key = (system or platform.system(), machine or platform.machine())
    if key not in RELEASES:
        raise ValueError(f'unsupported crane platform: {key}')
    asset, checksum = RELEASES[key]
    return f'https://github.com/google/go-containerregistry/releases/download/v0.20.6/go-containerregistry_{asset}.tar.gz', checksum


def main(dest):
    url, checksum = release()
    with urllib.request.urlopen(url, timeout=90) as response:
        data = response.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != checksum:
        raise ValueError('crane release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        member = archive.getmember('crane')
        if not member.isfile():
            raise ValueError('crane archive entry is not a regular file')
        root = Path(dest)
        root.mkdir(parents=True, exist_ok=True)
        (root / 'crane').write_bytes(archive.extractfile(member).read())
        (root / 'crane').chmod(0o755)


if __name__ == '__main__':
    main(sys.argv[1])
