# Public Wizard OS release metadata

This directory publishes only the installer’s release metadata. The canonical Wizard OS specification repository remains private; no source checkout, credentials or owner configuration is mirrored here.

`get.sh` reads `stable`, then `<version>.json`. `WIZARD_RELEASE_BASE` still supports an explicitly chosen alternate mirror or local release directory.

## 0.2.2 provenance

- Canonical source: `humanitylabs-org/wizard-os`, merge `8f54a471535a66afbdda3520dd6b6374541bb143`, `releases/0.2.2.json`.
- Byte-identical SHA-256: `8a0d3394c92e01984069647c8e6f2f7f21ddd5ef7a26c8e84daa18429d747c84`.
- Every image is pinned by immutable digest and verified for amd64 and arm64. Component commits and publication evidence are in the manifest.
- This snapshot includes split UX/Agent Service images and the App Manager installer fixes. It does not include later UI commits simply because they are newer.
- Alongside installation consumes exact split-image pins. Fresh installation still uses catalog defaults and reports drift where those differ. This metadata publication is not a claim that full fresh-machine acceptance has passed.

The canonical manifest’s private-URL caveat records its original publication boundary. This public mirror closes that metadata-availability gap; it does not expose the private repository or alter the selected image identities.

## 0.3.0 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #57, `releases/0.3.0.json`.
- Byte-identical SHA-256: `87c606848eb2e177ce30f3824e172ec776a4894a1d3ac0a3993710fd6deda644`.
- App Manager installer commit `a5b8fce` (agent-run `start`/`status`, Tailscale in every state, `os.md`). The App Manager image is the official `e39576a` build; its declared `image_inputs` are unchanged since, which wizard-os `tools/check_release.py` verifies.
- UX `a0d4425` (daily) and Agent Service `f7536b5` (main) are the newest merges whose public image publish had succeeded at selection. Every image is pinned by digest and pulls anonymously for amd64 and arm64.
- Default apps follow `wam/defaults.json`: the `ai-connection` slot (wizard-claude) replaces the retired AI Accounts default.
- This is source/image identity, not a fresh-box or alongside acceptance run.

Future releases copy only reviewed release JSONs and advance `stable` through a pull request. Preserve old versioned manifests byte-for-byte.

## 0.3.1 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.1.json`. Byte-identical SHA-256: `dc4ead2696d0f6f8a59a83577e4bcf197bacf980aa4b092ba18b38a495823983`.
- 0.3.0 plus App Manager #62 (`f7f52b4`): install.sh runs wam and the wizard CLI with a python3 that has PyYAML. The first live agent-run root install failed because the agent's bundled Python came first on PATH.
- Images and default apps are identical to 0.3.0.

## 0.3.6 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.6.json`. Byte-identical SHA-256: `5d509d49b2271aaac8a52891b9e991e3ee1f21772098e95c4179bb78fd3f4b4f`.
- 0.3.5 plus App Manager #74 (`6bac949`): detached administrator runners no longer fall through into another installation.
