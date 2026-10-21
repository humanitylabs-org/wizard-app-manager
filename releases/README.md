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

## 0.3.16 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.16.json`. Byte-identical SHA-256: `969cba6e7f85f54564a06b47c0ca8a6bc6f438ac5c14b2be524a2bbb38192041`.
- 0.3.15 plus App Manager #110 (`e076a31`): the Claude connection app is a default on every box (new `claude-subscription` slot) and, in alongside mode, is wired to the host agent's own Claude login; Agent Service #60 (`1f3fa4f`): a signed-out provider is reported in `wizard_reconnect`; Wizard UX #200 (`45ef8f6`): 'Claude is signed out' panel with an Open/Get Claude button.
- New Agent Service and UX images (multi-arch, pinned by digest); the App Manager image is unchanged from 0.3.12.

## 0.3.17 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.17.json`. Byte-identical SHA-256: `ae450b3a7a4e98fa5fa10d640c54b3edc6df4585ea6264c94847de4a0cf5f2c9`.
- 0.3.16 plus box auto-updates: App Manager #83 (`bcdf461`): `wam updates` (Automatic by default, an explicit Ask me is kept, unpinned boxes never update unless forced) and install.sh runs `wam updates --ensure-timer` on every install and rerun; Wizard UX #211 (`7e929b9`): Settings → Updates.
- My skin: Agent Service #62 (`e7bfd6d`) serves the owner's own skin layer from Files (Skins/My skin.css); Wizard UX #212 (`c1656a9`) applies it on top of the picked skin.
- New Agent Service and UX images (multi-arch, pinned by digest); the App Manager image is unchanged from 0.3.12.

## 0.3.18 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.18.json`. Byte-identical SHA-256: `81065c20771b1531b1144beea6f959515b72050d08d09245a3ddf84e5cebcedd`.
- 0.3.17 plus App Manager #116 (`1068428`, fixes #113): `wam app update` and `wam alongside` reconcile the alongside image pin from the selected release (explicit developer overrides still win) before queuing a core update, so the Agent Service no longer stays on a stale image.
- Every other pin is identical to 0.3.17; the App Manager image is unchanged from 0.3.12.

## 0.3.19 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.19.json`. Byte-identical SHA-256: `9c903594b3f009b6a73a1fafa9e2b3bbffcc92a12ad5dfe0fa412a682759fae2`.
- 0.3.18 plus App Manager #118 (`c595834`): Store Get no longer fails with a bare "Install failed (502)" when the App Manager service can't run `docker info`; the architecture check falls back to `sudo -n docker`, then the machine's CPU, and unexpected errors answer with a plain reason.
- Every other pin is identical to 0.3.18; the App Manager image is unchanged from 0.3.12.

## 0.3.20 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.20.json`. Byte-identical SHA-256: `28c20be9958417598cdebbf8b16324d1f377ae1e167cf7d1f445b93386fd1795`.
- 0.3.19 plus App Manager #123 (`cb81d8f`, wizard-os#90): an update cut short by a reboot (or a killed installer) is marked `interrupted` with its stale install.lock released, so the Update button or the nightly Automatic check retries instead of reporting "installing" forever. Main also carries #122 (private provider metadata kept in apps.json).
- `secrets` default app moves to secret-drop `690b277` (engine image digest-pinned, same bytes); `install.note` now describes what both install modes actually pin and read back (wizard-os#27).
- Every other pin is identical to 0.3.19; the App Manager image is unchanged from 0.3.12.

## 0.3.21 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.21.json`. Byte-identical SHA-256: `a97a6c9acc38ae88712a66a355e16dff908557b12de01ac9ec3015c84c3e5a7a`.
- 0.3.20 plus Wizard UX main `7dbef20`: app badges (#226), app title-bar tools (#232, used by Morning Report 0.4.0), slim phone title bars and the Update burst on Dark/Light/Landscapes (#233), and fixes #234–#242.
- Every other pin is identical to 0.3.20; the App Manager and Agent Service images are unchanged.
