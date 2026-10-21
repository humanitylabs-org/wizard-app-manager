"""Install a Wizard app straight from a private GitHub repo, for people who can see it (the team, before it's public).

The public index only lists public repos. For a private one, `wam` does on this box what the App Manager does for an
indexed app: fetch the repo at its newest commit (with this box's GitHub login), run the same container-safety check,
and write the app into the Wizard store folder. Images are pulled on the host with the same login, so Runtipi finds
them locally. Nothing about a private app is sent to the index.

GitHub login, first found wins: $GITHUB_TOKEN, $GH_TOKEN, `gh auth token`. Private images also need the
`read:packages` scope (`gh auth refresh -s read:packages`).
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import wizardmd

CATEGORIES = ("agent", "interface", "app", "system")
IMAGE_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-([^}]*))?\}")


class PrivateError(RuntimeError):
    pass


def token() -> str:
    for k in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(k):
            return os.environ[k]
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _api(path: str, tok: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com{path}", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "User-Agent": "wizard-app-manager"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise PrivateError(f"GitHub {path} -> {e.code}") from None
    except OSError as e:
        raise PrivateError(f"GitHub unreachable: {e}") from None


def head_commit(repo: str, tok: str) -> str:
    return _api(f"/repos/{repo}/commits/HEAD", tok)["sha"]


def _git_env(tok: str) -> dict:
    """Pass the login as an HTTP header through git's env config, so it never shows in the process list."""
    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}


def fetch(repo: str, commit: str, dest: Path, tok: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    env = _git_env(tok)
    for args in (["init", "-q"], ["fetch", "-q", "--depth", "1", f"https://github.com/{repo}", commit], ["checkout", "-q", "FETCH_HEAD"]):
        r = subprocess.run(["git", "-c", "advice.detachedHead=false", *args], cwd=dest, env=env, capture_output=True, text=True, timeout=300)
        if r.returncode:
            raise PrivateError(f"git {args[0]} {repo}: {r.stderr.strip()[:200]}")


def images(compose: dict) -> list[str]:
    """Image names with ${VAR:-default} resolved to the default (what Runtipi will run unless the owner overrides)."""
    out = []
    for svc in (compose.get("services") or {}).values():
        img = IMAGE_VAR.sub(lambda m: m.group(1) or "", str((svc or {}).get("image") or "")).strip()
        if img and img not in out:
            out.append(img)
    return out


def pull(image: str, tok: str) -> None:
    """Pull on the host unless the image is already here. Uses a throwaway Docker login, never saved on the box."""
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    if subprocess.run(["docker", "pull", "-q", image], capture_output=True).returncode == 0:
        return
    if not image.startswith("ghcr.io/"):
        raise PrivateError(f"could not pull {image}")
    with tempfile.TemporaryDirectory() as cfg:
        env = {**os.environ, "DOCKER_CONFIG": cfg}
        login = subprocess.run(["docker", "login", "ghcr.io", "-u", "x-access-token", "--password-stdin"],
                               input=tok, env=env, capture_output=True, text=True)
        r = subprocess.run(["docker", "pull", "-q", image], env=env, capture_output=True, text=True) if login.returncode == 0 else login
    if r.returncode:
        raise PrivateError(f"could not pull {image}: this GitHub login can't read it. Run `gh auth refresh -s read:packages` "
                           f"(or set GITHUB_TOKEN with read:packages), or ask the owner to make the package public")


BOOTSTRAP_HEADER = '# WAM private-provider internal bootstrap\n'


def _bootstrap_config(rt, urn):
    config = rt.req('GET', f'/user-config/{urn}')
    text = config.get('dockerCompose') or ''
    if text.strip() and not text.startswith(BOOTSTRAP_HEADER):
        raise PrivateError('private provider bootstrap conflicts with existing user Compose override')
    if (config.get('appEnv') or '').strip():
        raise PrivateError('private provider bootstrap conflicts with existing user environment override')
    return text


def remove_sealed_bootstrap(rt, store, app_id):
    """Clear only our override through the daemon; never unlink root-owned files."""
    urn = f'{app_id}:{store}'
    _bootstrap_config(rt, urn)
    rt.req('PUT', f'/user-config/{urn}', {'dockerCompose': '', 'appEnv': ''})
    if _bootstrap_config(rt, urn).strip():
        raise PrivateError('private provider bootstrap removal readback differs')


def write_sealed_bootstrap(rt, store, app_id, compose):
    """Keep a provisioned local provider off Runtipi's implicit public network."""
    import textwrap
    mains = [(name, svc) for name, svc in compose.get('services', {}).items()
             if (svc.get('x-runtipi') or {}).get('is_main')]
    if len(mains) != 1:
        raise PrivateError('private provider requires exactly one main service')
    name, svc = mains[0]
    nets = svc.get('networks') or []
    if (not isinstance(nets, (list, dict)) or not nets or
            any(not (compose.get('networks', {}).get(n) or {}).get('internal') for n in nets)):
        raise PrivateError('private provider bootstrap networks must all be internal')
    urn = f'{app_id}:{store}'
    _bootstrap_config(rt, urn)
    body = yaml.safe_dump({name: {'networks': list(nets)}}, sort_keys=False)
    body = body.replace('networks:', 'networks: !override', 1)
    text = BOOTSTRAP_HEADER + 'services:\n' + textwrap.indent(body, '  ')
    rt.req('PUT', f'/user-config/{urn}', {'dockerCompose': text, 'appEnv': ''})
    # Runtipi writeTextFile appends one LF, including to an already LF-terminated
    # document. Accept only that exact delimiter, never YAML/tag reserialization.
    if _bootstrap_config(rt, urn) not in (text, text + '\n'):
        raise PrivateError('private provider bootstrap readback differs; refusing install')


def prepare(runtipi_root: Path, store: str, repo: str, tok: str | None = None) -> dict:
    """Fetch `repo` (owner/name) at its newest commit into the Wizard store folder and pull its images.
    Returns an index-like record: {id, repo, uses, private: True, install: {commit}}."""
    tok = tok or token()
    if not tok:
        raise PrivateError(f"{repo} isn't in the public index and this box has no GitHub login. Run `gh auth login` "
                           f"with an account that can see it, or set GITHUB_TOKEN")
    commit = head_commit(repo, tok)
    with tempfile.TemporaryDirectory() as tmpdir:
        src = Path(tmpdir) / "src"
        fetch(repo, commit, src, tok)
        wtext = (src / "wizard.md").read_text() if (src / "wizard.md").is_file() else ""
        if not wtext:
            raise PrivateError(f"{repo}: no wizard.md")
        d = wizardmd.front_matter(wtext)
        app_id = str(d.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", app_id):
            raise PrivateError(f"{repo}: bad id {app_id!r}")
        rt = d.get("runtime") or {}
        cfg_p, comp_p = src / str(rt.get("config", "")), src / str(rt.get("compose", ""))
        if not cfg_p.is_file() or not comp_p.is_file():
            raise PrivateError(f"{repo}: wizard.md runtime.config/compose missing")
        ctext = comp_p.read_text()
        risks = wizardmd.compose_risks(ctext)
        if risks:
            raise PrivateError(f"{repo} asks for host-level access: {'; '.join(risks)}")
        compose = yaml.safe_load(ctext) or {}
        for img in images(compose):
            pull(img, tok)

        dst = runtipi_root / "repos" / store / "apps" / app_id
        old_tipi = 0
        if (dst / "config.json").is_file():
            try:
                old_tipi = int(json.loads((dst / "config.json").read_text()).get("tipi_version") or 0)
            except (ValueError, json.JSONDecodeError):
                old_tipi = 0
        have = (dst / ".wizard-commit").read_text().strip() if (dst / ".wizard-commit").is_file() else ""
        rec = {"id": app_id, "repo": repo, "uses": d.get("uses") or [], "private": True, "install": {"commit": commit}}
        if have == commit:
            return rec

        cfg = json.loads(cfg_p.read_text())
        cfg["id"] = app_id
        cat = d.get("category")
        cfg["categories"] = [cat if cat in CATEGORIES else ("agent" if d.get("type") == "agent" else "app")]
        cfg["tipi_version"] = max(int(cfg.get("tipi_version") or 1), old_tipi + 1 if have else old_tipi)   # new commit = update
        cfg["short_desc"] = cfg.get("short_desc") or str(d.get("summary") or "")
        for svc in (compose.get("services") or {}).values():   # box-only by default, like the App Manager does
            xr = (svc or {}).get("x-runtipi") or {}
            if xr.get("is_main") and xr.get("internal_port") and not svc.get("ports"):
                svc["ports"] = [f"127.0.0.1:${{APP_PORT}}:{xr['internal_port']}"]

        # Runtipi otherwise adds its public shared network to every is_main service.
        # Preserve the declared internal bootstrap through a final Compose override.
        if (d.get('private_inference') or {}).get('provisioning_service'):
            from .runtipi import Runtipi
            write_sealed_bootstrap(Runtipi.from_install(runtipi_root), store, app_id, compose)


        tmp = dst.with_name(f"{app_id}.tmp-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "metadata").mkdir(parents=True)
        (tmp / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
        (tmp / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
        body = wizardmd.body(wtext)
        (tmp / "metadata" / "description.md").write_text(("" if body.startswith("# ") else f"# {d.get('name') or app_id}\n\n") + body + "\n")
        (tmp / "wizard.md").write_text(wtext)
        if isinstance(d.get("skill"), str) and (src / d["skill"]).is_file():
            shutil.copy2(src / d["skill"], tmp / "SKILL.md")
        for cand in (cfg_p.parent / "metadata" / "logo.jpg", src / str(d.get("icon") or "-"), cfg_p.parent / "logo.jpg"):
            if cand.is_file():
                shutil.copy2(cand, tmp / "metadata" / "logo.jpg")
                break
        if (cfg_p.parent / "data").is_dir():
            shutil.copytree(cfg_p.parent / "data", tmp / "data")
        (tmp / ".wizard-commit").write_text(commit + "\n")
        (tmp / ".wizard-source").write_text(json.dumps({"repo": repo, "git": f"https://github.com/{repo}", "private": True}) + "\n")
        shutil.rmtree(dst, ignore_errors=True)
        tmp.rename(dst)
    return rec
