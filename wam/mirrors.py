"""DECISIONS #83: origin-first, immutable curator copies (no mutable mirror HEAD)."""
import re
import subprocess

SOURCE_MIRROR = "https://github.com/humanitylabs-org/wizard-app-manager.git"
IMAGE_MIRROR = "ghcr.io/humanitylabs-org/wizard-app-manager"


def source_ref(repo, commit):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("mirror requires a repository and exact 40-hex commit")
    return f"refs/tags/curator/{repo}/{commit}"


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
