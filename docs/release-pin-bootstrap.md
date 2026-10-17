# Release-pin bootstrap candidate

This change is **not release 0.2.2 acceptance**. The published manifest still selects the older WAM source (`e39576a0e28e95b5eef996454a269288ba37e8c2`). Do not change that manifest or claim its exact-source check passed when testing this branch.

## Install behavior

- `get.sh` validates and exports `WIZARD_RELEASE_PINS` before running `install.sh`. The latter validates again before touching private access, Docker or services.
- Every default app with `repo` + full `commit` resolves from that source, including recursive dependencies. A removed public-index entry is not a fallback error. Legacy manifests that only pin versions retain their old resolution behavior.
- WAM reuses its checked local-recipe preparation and ordinary Runtipi install API. Core image digests replace the main service image **before its first install**. Public image pulls remain in Runtipi, not the CLI user's Docker session.
- The released manager image otherwise fetches the live index again during `rt.app()` / `rt.install()` and overwrites that source. A small, read-only `wizard-release-index` service gives that unchanged image the same pinned commit through its existing index API. It runs Node from the **same pinned manager image**: no rebuild, extra image, host port, Docker socket or credentials. Unpinned lookups still reach the public index. Startup waits for this local view before starting the manager.
- Stock Runtipi setup still happens first, followed by the pinned manager image. Alongside keeps its external Hermes slot and host-network/loopback overrides. Compose tags and owner settings survive the generated override update.
- The CLI wrapper and API service retain the active pin file. Running the unpinned installer removes the manager's pin-index override; `WAM_REF` remains explicitly unpinned and rejected with `WIZARD_STRICT=1`.
- `release.json` now checks installed source commits as well as core running image digests and app versions. Existing strict behavior is not relaxed.

## Focused checks

Run lightweight source checks from a clean checkout:

```sh
bash -n get.sh install.sh
node --check wam/release-index.mjs
uv run --with pytest --with pyyaml --no-project python -m pytest -q \
  tests/test_release_bootstrap.py tests/test_release_pins.py tests/test_release_split.py \
  tests/test_install_docker.py tests/test_private_aliases.py tests/test_private_catalog.py
```

These checks execute the real Python dependency/recipe path with fixture source and Runtipi, the real Node HTTP pin view with local upstream, and sandboxed installer/get scripts. They are **not** Docker/VM execution or running-image evidence.

## Candidate-only VM route

Use the authorized heavy-job runner on the assigned build host. The parent QA owner creates a **new disposable Ubuntu 24.04 VM**, with no owner-home mounts, and stages the exact pushed candidate checkout there. Do not reuse the failed release baseline disk or run this on a live installation.

Inside that guest, as a passwordless-sudo non-root user:

```sh
sudo apt-get update
sudo apt-get install -y git curl python3 python3-yaml
# CANDIDATE_SHA must be the exact pushed integration/e2e-022-bootstrap head under test.
git clone https://github.com/humanitylabs-org/wizard-app-manager.git "$HOME/wam-candidate"
git -C "$HOME/wam-candidate" checkout --detach "$CANDIDATE_SHA"
WIZARD_MODE=fresh python3 "$HOME/wam-candidate/scripts/qa-release-candidate.py" \
  --manifest "$HOME/wam-candidate/releases/0.2.2.json" \
  --evidence "$HOME/candidate-022-evidence"
```

The helper creates a **separate test-manifest copy** that changes only the WAM source selector to the clean, pushed candidate. All original image/default-app pins stay unchanged and it runs ordinary `get.sh` with `WIZARD_STRICT=1`. It records the byte-identical original manifest, its SHA-256, the explicit source exception and the installed checkout SHA. No release file in Git is edited.

After readback it preserves the strict candidate receipt in `candidate-release.json`, and writes the original source expectation back into normal `~/.wizard-os/release.json` with **`status: mismatch`, source check `ok: false`, `release_exact: false`**. A helper exit 0 means only that the explicit candidate experiment passed; it cannot certify the original release. `candidate-exception.json` and `original-release-readback.json` make this distinction machine-readable. A non-source strict drift still fails the helper.

For alongside use a separate disposable fixture guest with a known Hermes sentinel and `WIZARD_MODE=alongside`; record its before/after bytes and verify `wizard unlink` independently. Do not use the owner's Hermes as the test fixture.

The QA owner must still collect actual container RepoDigests, installed recipe commits, install outcome, complete default-app presence, alongside preservation, and subsequent UX/API acceptance. This patch does not certify those outcomes.
