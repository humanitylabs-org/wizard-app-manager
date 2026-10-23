"""Install only the verified upstream Darwin/arm64 crane binary. No compiler/build."""
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import urllib.request

URL = 'https://github.com/google/go-containerregistry/releases/download/v0.20.6/go-containerregistry_Darwin_arm64.tar.gz'
SHA256 = 'ae30b28498de3e1ac51a37042eab46116ec70d86aa9d10431a1fbde35b9918b9'


def main(dest):
    with urllib.request.urlopen(URL, timeout=90) as response:
        data = response.read(64 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != SHA256:
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
