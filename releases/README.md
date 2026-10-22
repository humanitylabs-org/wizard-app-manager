# Public Wizard OS release metadata

This directory publishes only the installer’s release metadata. The canonical Wizard OS specification repository remains private; no source checkout, credentials or owner configuration is mirrored here.

`get.sh` reads `stable` (or `latest` on a box whose owner chose the latest channel), then `<version>.json`. `WIZARD_RELEASE_BASE` still supports an explicitly chosen alternate mirror or local release directory.

## Channels (wizard-os DECISIONS #75)

- `latest`: every release as soon as it is cut. Mirror it with each new release JSON.
- `stable`: only releases promoted with wizard-os `tools/promote_stable.py`, i.e. 72 hours on latest plus a passed upgrade test, recorded in `promotions/<version>.json`. A fix-only release with a passed upgrade test may skip the 72 hours (`fast_track` in its record, wizard-os DECISIONS #82); `wam/updates.py` applies the same gate as the tool. Mirror the record together with the pointer.

Never point `stable` at a release without its promotion record: `tests/test_release_channels.py` fails, and boxes on Automatic refuse to install it. 0.3.25 is grandfathered as the first stable.

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

## 0.3.22 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.22.json`. Byte-identical SHA-256: `4f13a86f4e2eb94c67121ca0a9c7688215c7af70c787b63295f92bf0ed0312ab`.
- 0.3.21 plus App Manager #130 (`0b2a702`, wizard-os#93): every install and rerun sets up the working Troubleshooter (native user service at `/troubleshoot/` on the owner's shell addresses) from the new pinned component `wizard-troubleshooter` `c434bbe`. The owner gate is the box's own tailnet login and the guardian is report-only. Main also carries #128 (routing skills kept on registry rewrite).
- Every other pin is identical to 0.3.21. The App Manager, Wizard UX and Agent Service images are unchanged.

## 0.3.23 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.23.json`. Byte-identical SHA-256: `8ab124cf9a9073c9dcdeec0aa9a8592fdfc1f99d1ef2105b9015ec0baacd9f9f`.
- 0.3.22 plus wizard-troubleshooter #9 (`4b1f0ef`, wizard-os#93): the Troubleshooter's Sign in and Open … app links now open the connection app this box really has. The address comes from apps.json, or the app's App Store page when it isn't installed. Before, they went to a not_found page. A box without Claude Code now gets plain guidance instead of a dead Claude button. First snapshot with the required `hermes` pin (carried over, v2026.9.24).
- Every other pin is identical to 0.3.22. The App Manager, Wizard UX and Agent Service images are unchanged.

## 0.3.24 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.24.json`. Byte-identical SHA-256: `766e30a7c644f7d5098076c23850e2bfa6cbc10433a618115d22fdb22286bec1`.
- 0.3.23 plus App Manager #135 (`5005f6b`, wizard-os#87): a fresh install no longer fails before Runtipi exists when an earlier install left a root-owned `/tmp/runtipi-setup.sh` (curl exit 23). The Runtipi setup now downloads to a private temp dir. Main also carries #133 (owner screens through the shared view projection).
- Every other pin is identical to 0.3.23. The App Manager, Wizard UX and Agent Service images are unchanged.

## 0.3.25 provenance

- Canonical source: `humanitylabs-org/wizard-os`, `releases/0.3.25.json`. Byte-identical SHA-256: `9fbbde5313e44a3973317e8bdd9f49653d906560747a2882616c8d6d48de5689`.
- 0.3.24 plus App Manager #137 (`40e59d8`, wizard-os#87): a fresh install on a box whose new `wizard` user is not uid 1000 no longer fails at the end with Errno 13 on the stock Hermes `config.yaml`. The builder step reads and replaces it through the installer's passwordless sudo, keeping the agent's owner and mode.
- Every other pin is identical to 0.3.24. The App Manager, Wizard UX and Agent Service images are unchanged.

## 0.3.26 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #120 (merge `58577b9`), `releases/0.3.26.json`. Byte-identical SHA-256: `d586005ba25b0f8c04e878349517f8efc71a248a48796e6c6677e0350b5cca47`.
- 0.3.25 plus App Manager #143 (`07b7d84`, wizard-os#87): the App Store works on fresh boxes. `wam store-data` (run by install.sh after `wam links`) starts the read-only Store data adapter at the shell's commit and mounts `/wizard-os-home/store-data` and the owner-gated `/wizard-os-home/store-install` on the fresh shell's Tailscale Serve ports. Also carries #138 (updates default to Ask me) and #141 (stable/latest channels).
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.26 has a promotion record. Every image pin is identical to 0.3.25.

## 0.3.27 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #121, `releases/0.3.27.json`. Byte-identical SHA-256: `e8aefc91031be867f6cd1ada7258d971aeafcc9589e5d1a4b4879a9be0859a09`.
- 0.3.26 plus wizard-ux #268 (`00ffd23`): Settings → Updates has a Stable (recommended) / Latest (every release, for testers) channel picker over the App Manager's channel API (#141); on Latest with Automatic it says Automatic installs stable only. Wizard UX image `sha256:94d0eaaf…` (amd64+arm64).
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.27 has a promotion record. Every other pin is identical to 0.3.26.

## 0.3.32 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #128 (merge `a52f374`), `releases/0.3.32.json`. Byte-identical SHA-256: `b81fbc64d8a4a61a55239461380745b8d26df5f339eeb95b481b0b185ad2c099`.
- 0.3.31 plus App Manager #153 (`66d06b4`): apps whose wizard.md declares `runtime.package: source` run from the pinned commit (APP_SOURCE_DIR), so Wisdom installs from the Store on the official python:3.12-slim with no private app image. The App Manager image moves to the official build from `66d06b4` (`v4.10.2-wizard.415-66d06b4`, `sha256:a3c821aa…91a5d`, amd64+arm64, image run 39636054008) because #153 changes the backend.
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.32 has a promotion record.

## 0.3.31 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #127 (merge `46e9b4a`), `releases/0.3.31.json`. Byte-identical SHA-256: `df03d6cc926d4952c52c42d282f8451f6b7c9cf67825243d439df59da71c2cac`.
- 0.3.30 plus App Manager #155 (`7a3eaa1`): a Store Get fills a required origin list whose default is empty (Wisdom's `WISDOM_ORIGINS`), so the app no longer answers 421 'Unknown host' in the shell. Wizard UX image moves to official main `a79e28a` (`sha256:c60920e7…acfebec`, amd64+arm64, Publish run 39630570798) with #282 (fresh image ships data.js/summaries.js placeholders), plus #272, #280, #281. The App Manager image is unchanged because its image inputs are unchanged since 8d6e7bd.
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.31 has a promotion record.

## 0.3.30 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #126 (merge `a9925fc`), `releases/0.3.30.json`. Byte-identical SHA-256: `b85c61d09258f2b2957751c30c4f6017327ef2bbf48b2d192f57ddc1ee101be9`.
- 0.3.29 plus wizard-ux #273 and App Manager #152 (`d779843`, wizard-os#87): right after a Store Get the Store shows Open without a reload. The Wizard UX image moves to official main `dc9028c` (`sha256:1df6791b…a506be`, amd64+arm64, Publish run 39619521621). It also carries #256, #267, #269–#271, #274–#277 and #279. The App Manager image is unchanged because its image inputs are unchanged since 8d6e7bd.
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.30 has a promotion record.

## 0.3.29 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #124 (merge `cbd5fb7`), `releases/0.3.29.json`. Byte-identical SHA-256: `fc708e29602b8dba5579d402f99776a4d10bbc3ffb49faabf20c075809cce6c8`.
- 0.3.28 plus App Manager #150 (`6a39dfa`, wizard-os#87): on fresh boxes, apps installed from the Store show Open and open in a Wizard window. `wam links` (installer and Store Get) also serves every installed app page on a same-origin path on each owner shell Serve port (`/<id>/` stripped, or the recipe's declared `view.path`), never replacing a different route, and points apps.json at it. Also carries #148 (portable work paths) and #149 (tests only). Host tooling only: no image input changed.
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.29 has a promotion record. Every other pin is identical to 0.3.28.

## 0.3.28 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #122 (merge `261f554`), `releases/0.3.28.json`. Byte-identical SHA-256: `0a1c633798b839e5498c4e3b3254afff0656055c7196b48541ebcb240a27ca17`.
- 0.3.27 plus App Manager #146 (`b44c109`, wizard-os#87): on fresh boxes an app installed from the Store opens. `WIZARD_TRUSTED_HOSTS/ORIGINS` now name the shell origin and the app's own private address; before, they were empty without alongside.json and the app answered 403 "Unknown host". Also carries #140 (additive Chats slot, no default app). The App Manager image moves to the official `v4.10.2-wizard.391-8d6e7bd` (`sha256:197c4b57…`, amd64+arm64); its image inputs are unchanged up to b44c109.
- Channel: on `latest` only. `stable` stays 0.3.25 until 0.3.28 has a promotion record. Every other pin is identical to 0.3.27.
