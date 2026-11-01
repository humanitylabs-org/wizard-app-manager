"""`wam standard`: names every way a box differs from what the Store ships."""
import json
from pathlib import Path

import pytest

from wam import standard

PIN = "ghcr.io/o/app:1@sha256:" + "a" * 64


def app(root: Path, app_id: str, recipe: str, override: str | None = None, source: dict | None = None,
        commit: str = "c" * 40) -> None:
    d = root / "apps/wizardapps" / app_id
    d.mkdir(parents=True)
    (d / "docker-compose.yml").write_text(recipe)
    (d / ".wizard-source").write_text(json.dumps(source or {"repo": f"o/{app_id}", "source": "standard"}))
    (d / ".wizard-commit").write_text(commit + "\n")
    if override is not None:
        u = root / "user-config/wizardapps" / app_id
        u.mkdir(parents=True)
        (u / "docker-compose.yml").write_text(override)


def states(root, home=None, listed=None):
    rows = [standard.check_app(root, d.name, set(), listed) for d in sorted((root / "apps/wizardapps").iterdir())]
    return {r["id"]: r for r in rows}


def test_standard_app_and_wam_owned_overrides_pass(tmp_path):
    app(tmp_path, "plain", f"services:\n  plain:\n    image: {PIN}\n")
    app(tmp_path, "networked", f"services:\n  networked:\n    image: {PIN}\n",
        "services:\n  networked:\n    networks: !override\n      wizard-app-networked: {}\n")
    app(tmp_path, "pinned", "services:\n  pinned:\n    image: ${X:-ghcr.io/o/p:1}\n",
        f"services:\n  pinned:\n    image: {PIN}\n    pull_policy: missing\n    networks: !reset []\n")
    r = states(tmp_path)
    assert {k: v["state"] for k, v in r.items()} == {"networked": "standard", "pinned": "standard", "plain": "standard"}


def test_private_images_overrides_and_box_only_services_are_named(tmp_path):
    app(tmp_path, "local", "services:\n  local:\n    image: ${I:-local-build:1}\n    pull_policy: never\n")
    app(tmp_path, "byid", "services:\n  byid:\n    image: sha256:" + "b" * 64 + "\n")
    app(tmp_path, "patched", f"services:\n  patched:\n    image: {PIN}\n",
        "services:\n  patched:\n    command: [python, -c, 'print(1)']\n  extra:\n    image: x:1\n")
    app(tmp_path, "swapped", f"services:\n  swapped:\n    image: {PIN}\n", "services:\n  swapped:\n    image: mine:2\n")
    r = states(tmp_path)
    assert all(v["state"] == "custom" for v in r.values())
    assert any("replaces command" in x for x in r["patched"]["reasons"])
    assert any("only in this box's override" in x for x in r["patched"]["reasons"])
    assert any("instead of the Store image" in x for x in r["swapped"]["reasons"])


def test_index_decides_origin_and_flags_unpublished(tmp_path):
    app(tmp_path, "legacy", f"services:\n  legacy:\n    image: {PIN}\n", source={"repo": "o/legacy", "private": True})
    app(tmp_path, "mine", f"services:\n  mine:\n    image: {PIN}\n", source={"repo": "o/mine", "private": True})
    r = states(tmp_path, listed={"o/legacy": "d" * 40})
    assert r["legacy"]["state"] == "standard" and "newer release" in r["legacy"]["notes"][0]
    assert r["mine"]["state"] == "unpublished"
    offline = states(tmp_path)   # no index: a private-labelled install is reported, never silently passed
    assert offline["mine"]["state"] == "custom"


@pytest.mark.parametrize("source", ["owner-private", "link", "alpha-test"])
def test_index_preserves_explicit_nonstandard_source(tmp_path, source):
    app(tmp_path, "demo", f"services:\n  demo:\n    image: {PIN}\n",
        source={"repo": "O/Demo", "source": source})
    result = standard.check_app(tmp_path, "demo", listed={"o/demo": "c" * 40})
    assert result["state"] == "custom"
    assert f"installed from a {source} source, not the Store listing" in result["reasons"]


@pytest.mark.parametrize("source", ["owner-private", "link", "alpha-test"])
def test_unlisted_own_app_stays_allowed(tmp_path, source):
    app(tmp_path, "demo", f"services:\n  demo:\n    image: {PIN}\n",
        source={"repo": "o/demo", "source": source})
    result = standard.check_app(tmp_path, "demo", listed={})
    assert result["state"] == "unpublished"


@pytest.mark.parametrize("image", ["ghcr.io/o/app:1", PIN, "${APP_IMAGE:-ghcr.io/o/app:1}"])
def test_override_never_policy_inherits_recipe_image(tmp_path, image):
    app(tmp_path, "demo", f"services:\n  demo:\n    image: {image}\n",
        "services:\n  demo:\n    pull_policy: never\n")
    result = standard.check_app(tmp_path, "demo")
    assert result["state"] == "custom"
    resolved = standard._resolve(image)
    assert f"demo: override runs {resolved[:60]} instead of the Store image" in result["reasons"]


@pytest.mark.parametrize("policy", ["missing", "always"])
def test_policy_only_pullable_override_stays_allowed(tmp_path, policy):
    app(tmp_path, "demo", f"services:\n  demo:\n    image: {PIN}\n",
        f"services:\n  demo:\n    pull_policy: {policy}\n")
    assert standard.check_app(tmp_path, "demo")["state"] == "standard"


def test_alongside_wiring_and_digest_pin_stay_allowed(tmp_path):
    app(tmp_path, "demo", "services:\n  demo:\n    image: ghcr.io/o/app:1\n",
        f"""services:
  demo:
    image: {PIN}
    pull_policy: missing
    user: '1000:1000'
    group_add: ['1000']
    network_mode: host
    networks: !reset []
    ports: !reset []
    environment:
      WIZARD_HOST: 127.0.0.1
      WIZARD_SETUP_MODE: alongside
    volumes:
      - /synthetic/media:/media
    healthcheck:
      test: ['CMD', 'true']
""")
    assert standard.check_app(tmp_path, "demo")["state"] == "standard"


def test_my_release_shell_is_personal(tmp_path):
    app(tmp_path, "wizard-ux", f"services:\n  wizard-ux:\n    image: {PIN}\n", "services:\n  wizard-ux:\n    image: wizard-ux:mine-1\n")
    (tmp_path / "home/bin").mkdir(parents=True)
    (tmp_path / "home/bin/my-release").write_text("")
    (tmp_path / "home/my-release.status").write_text("OK")
    res = standard.check(tmp_path, tmp_path / "home")
    assert res["apps"][0]["state"] == "personal" and res["personal"] == 1 and res["custom"] == 0
