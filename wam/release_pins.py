"""Opt-in release source pins, shared by bootstrap and recursive dependency installs.

Only WIZARD_RELEASE_PINS activates this policy; a stale receipt never pins a developer run.
The manager image's index adapter also needs the same mapping (release-index.mjs), otherwise
it replaces a preloaded recipe with the live catalog revision during rt.install().
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

IMAGE_VARS = {"wizard-shell": "WIZARD_SHELL_IMAGE", "wizard-ux": "WIZARD_UX_IMAGE",
              "wizard-agent-service": "WIZARD_AGENT_SERVICE_IMAGE"}


def load(path=None):
    path = path or os.environ.get("WIZARD_RELEASE_PINS")
    if not path:
        return {}
    doc = json.loads(Path(path).read_text())
    if not isinstance(doc, dict):
        raise ValueError("release must be an object")
    defaults = doc.get("default_apps", {})
    if not isinstance(defaults, dict):
        raise ValueError("release default_apps must be an object")
    seen = set()
    for slot, row in defaults.items():
        if row is None:
            continue
        if not isinstance(row, dict):
            raise ValueError(f"release default app {slot}: expected object or null")
        # Older release manifests only pinned versions. Do not invent a source pin for those.
        if "commit" not in row and "repo" not in row:
            continue
        app, repo, commit = (row.get(k, "") for k in ("app", "repo", "commit"))
        if not isinstance(app, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", app) or app in seen:
            raise ValueError(f"release default app {slot}: invalid or duplicate app id")
        seen.add(app)
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo):
            raise ValueError(f"release default app {slot}: expected owner/repo")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"release default app {slot}: expected full commit")
    images = doc.get("images", {})
    if not isinstance(images, dict):
        raise ValueError("release images must be an object")
    required = ("wizard-app-manager", "wizard-ux", "wizard-agent-service") if (
        "wizard-ux" in images or "wizard-agent-service" in images) else ("wizard-app-manager", "wizard-shell")
    if any(name not in images for name in required):
        raise ValueError("release needs App Manager and shell/Agent Service image pins")
    for name, im in images.items():
        image_ref(im)  # all pins validated before any fetch, image pull or service change
    for var in IMAGE_VARS.values():
        if os.environ.get(var) and not re.fullmatch(r"[A-Za-z0-9._/:@-]+", os.environ[var]):
            raise ValueError(f"{var}: invalid image reference")
    return doc


def image_ref(im):
    if not isinstance(im, dict):
        raise ValueError("release image must be an object")
    ref, digest = im.get("ref", ""), im.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("release image needs sha256:<64 hex>")
    repo = re.sub(r"(@.*|:[^/:]*)$", "", str(ref))
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?(?:/[a-z0-9._-]+)+", repo):
        raise ValueError("release image needs an image repository")
    return f"{repo}@{digest}"


def resolve(ref):
    doc = load()
    for row in doc.get("default_apps", {}).values():
        if row and row.get("commit") and ref in (row["app"], row["repo"], "https://github.com/" + row["repo"]):
            app = row["app"]
            im = doc.get("images", {}).get(app)
            override = os.environ.get(IMAGE_VARS.get(app, ""))
            return {"id": app, "repo": row["repo"], "commit": row["commit"],
                    "image": override or (image_ref(im) if im else None)}
    return None


def defaults(stock):
    doc = load()
    if not doc:
        return stock
    # Keep slot labels/required flags, but the release determines which defaults exist.
    return {slot: {**stock.get(slot, {}), **(row or {}), "app": (row or {}).get("app")}
            for slot, row in doc.get("default_apps", {}).items()}


def configure_manager(path, image, pins):
    """Keep stock Compose tags and owner settings; add a private, read-only pin-index sidecar.

    It uses the SAME manager digest (Node is already in that image), no additional image,
    published port, Docker socket, credentials, or backend rebuild.
    """
    import yaml

    class Tagged:
        def __init__(self, tag, value):
            self.tag, self.value = tag, value

    class Loader(yaml.SafeLoader):
        pass

    class Dumper(yaml.SafeDumper):
        pass

    def tagged(loader, tag, node):
        make = {yaml.ScalarNode: loader.construct_scalar, yaml.SequenceNode: loader.construct_sequence,
                yaml.MappingNode: loader.construct_mapping}[type(node)]
        return Tagged(tag, make(node))

    def represent(dumper, value):
        node = dumper.represent_data(value.value)
        node.tag = value.tag
        return node

    Loader.add_multi_constructor("!", lambda loader, tag, node: tagged(loader, "!" + tag, node))
    Dumper.add_representer(Tagged, represent)
    path = Path(path)
    doc = yaml.load(path.read_text(), Loader=Loader) if path.is_file() else {}
    doc = doc or {}
    services = doc.setdefault("services", {})
    manager = services.setdefault("runtipi", {})
    manager.update(image=image, pull_policy="if_not_present")
    env: dict | list = manager.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) if "=" in item else (item, None) for item in env)
    if not isinstance(env, dict):
        raise ValueError("runtipi environment must be a mapping or list")
    endpoint = "http://wizard-release-index:7789"
    service = services.get("wizard-release-index")
    if service and not service.get("x-wizard-release-pins"):
        raise ValueError("wizard-release-index service already exists and is not owned by WAM")
    if pins:
        if env.get("WIZARD_INDEX_URL") not in (None, endpoint):
            raise ValueError("remove the custom runtipi WIZARD_INDEX_URL before applying release pins")
        env["WIZARD_INDEX_URL"] = endpoint
        services["wizard-release-index"] = {
            "x-wizard-release-pins": True, "image": image, "pull_policy": "if_not_present",
            "entrypoint": ["node", "/release-index.mjs"], "command": [],
            "restart": "unless-stopped", "read_only": True, "user": "1000:1000",
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "networks": ["tipi_main_network"],
            "volumes": [f"{Path(__file__).with_name('release-index.mjs').resolve()}:/release-index.mjs:ro",
                        f"{Path(pins).resolve()}:/release-pins.json:ro"],
            "healthcheck": {"test": ["CMD", "node", "-e",
                "fetch('http://127.0.0.1:7789/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"],
                "interval": "2s", "timeout": "5s", "retries": 30}}
    else:
        services.pop("wizard-release-index", None)
        if env.get("WIZARD_INDEX_URL") == endpoint:
            env.pop("WIZARD_INDEX_URL")
    if env:
        manager["environment"] = env
    else:
        manager.pop("environment", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(doc, Dumper=Dumper, sort_keys=False))


if __name__ == "__main__":
    import sys
    try:
        load()
        if len(sys.argv) > 1:
            configure_manager(sys.argv[1], sys.argv[2], os.environ.get("WIZARD_RELEASE_PINS"))
    except (ValueError, OSError, TypeError, KeyError) as e:
        sys.exit(f"invalid release pins: {e}")
