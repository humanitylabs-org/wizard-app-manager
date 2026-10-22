"""DECISIONS #83: origin-first, immutable curator copies (no mutable mirror HEAD)."""
import re
import subprocess
import urllib.request

SOURCE_MIRROR = "https://github.com/humanitylabs-org/wizard-app-manager/releases/download/curator-source-mirrors"
BUNDLE_LIMIT_BYTES = 256 * 1024 * 1024
IMAGE_MIRROR = "ghcr.io/humanitylabs-org/wizard-app-manager"


def source_ref(repo, commit):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("mirror requires a repository and exact 40-hex commit")
    return f"refs/tags/curator/{repo}/{commit}"


def source_name(repo, commit):
    source_ref(repo, commit)  # validate before constructing a URL or filename
    return f"{repo.replace('/', '--')}--{commit}.bundle"


def source_url(repo, commit):
    return f"{SOURCE_MIRROR}/{source_name(repo, commit)}"


def download_bundle(repo, commit, dest, limit=BUNDLE_LIMIT_BYTES):
    # Anonymous public release download; no origin or account credential required.
    request = urllib.request.Request(source_url(repo, commit), headers={"User-Agent": "wizard-app-manager"})
    with urllib.request.urlopen(request, timeout=90) as response, open(dest, "wb") as out:
        total = 0
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("curator bundle exceeds size limit")
            out.write(chunk)


def image_ref(image):
    if image.startswith("ghcr.io/humanitylabs-org/"):
        return None
    match = re.fullmatch(r"[^\s@$]+@sha256:([0-9a-f]{64})", image)
    return f"{IMAGE_MIRROR}:curator-sha256-{match[1]}@sha256:{match[1]}" if match else None


def image_available(image):
    try:
        if subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=15).returncode == 0:
            return True
        return subprocess.run(["docker", "manifest", "inspect", image], capture_output=True, timeout=45).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_images(compose, probe=None):
    """Docker validates the same digest on pull. Never guess a digest for a tag.

    manifest inspect is registry-only: fresh sudo users need no Docker socket.
    Existing local images and successful origins never consult the mirror.
    """
    probe = probe or image_available
    for service in (compose.get("services") or {}).values():
        raw = service.get("image", "")
        image = raw
        if isinstance(image, str):
            image = re.sub(r"\$\{[A-Z_][A-Z_0-9]*:-([^}]+)\}", r"\1", image)
        mirror = image_ref(image) if isinstance(image, str) and not service.get("build") else None
        if mirror and not probe(image) and probe(mirror):
            # Preserve an owner's explicit environment override; replace only
            # the recipe's pinned default, never the whole ${VAR:-default}.
            service["image"] = raw.replace(image, mirror)
