# Public Wizard OS release metadata

This directory publishes only the installer’s release metadata. The canonical Wizard OS specification repository remains private; no source checkout, credentials or owner configuration is mirrored here.

`get.sh` reads `stable` (or `latest` on a box whose owner chose the latest channel), then `<version>.json`. `WIZARD_RELEASE_BASE` still supports an explicitly chosen alternate mirror or local release directory.

## Channels (wizard-os DECISIONS #75)

- `latest`: every release as soon as it is cut. Mirror it with each new release JSON.
- `stable`: only releases promoted with wizard-os `tools/promote_stable.py`, i.e. 72 hours on latest plus a passed upgrade test, recorded in `promotions/<version>.json`. A fix or feature release with a passed upgrade test may skip the 72 hours (`fast_track` in its record, wizard-os DECISIONS #82 and #93); `wam/updates.py` applies the same gate as the tool. Mirror the record together with the pointer.

Never point `stable` at a release without its promotion record: `tests/test_release_channels.py` fails, and boxes on Automatic refuse to install it. 0.3.25 is grandfathered as the first stable.

## stable 0.3.61

Promoted under DECISIONS #93 (fast-track, classification **feature**; owner-authorized by Miguel, 26 Oct 2026: approved final wording verbatim, "ship it"). Stable 0.3.59 with only the shell pin changed: wizard-ux 325be039 → 87b35bcb1314961887c06da5f96ad10b50786fe6 (`release/stable-onboarding`; #567 Recommended fix: the orb never freezes in an iPhone Safari tab; #568: three-step guide "Step N of 3" with the owner's words, Hide and Install beats removed; image `sha256:ecaba88e568ff2b6d90c6ca19a5b9bba153fcbb2eb7a4ee1aca3073c9e9acdb6`, official dispatch run 40339463332). Every other pin is identical to 0.3.59. Installed upgrade 0.3.59 → 0.3.61 passed on demo-4 through `os.sh` with `WIZARD_STRICT=1` (mirror-head base disclosed): pinned, 0 failed checks, Hermes files unchanged, doctor 0 FAIL, live WebKit check 61/61. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.61.md (wizard-os #257 cut, #258 promotion). Rollback: point `stable` back at 0.3.59.

Release JSON SHA256: `b049d493c0eea548ce5a987a4cb05985912020b13c80765f4f0071233ebb6665`.
Promotion JSON SHA256: `ce5b9ff2811b108ccfc6d2ffb4744897dea9b5e54985d2c339a70c759174c076`.

## 0.3.63 provenance

Stable 0.3.61 with only the two AI connection apps changed: ChatGPT chatgpt-connection f87d5dd → 476d771 (0.2.4, engine `wizard-chatgpt:0.2.4@sha256:1c90102e819574b4c5e0c50f782b3f7cd0e998b53415407471bf59dabca27418`) and Claude claude-connection eb434ae → d0b59dd (0.2.6, engine `wizard-claude:0.2.6@sha256:5c7ca0b3b38803794aa3296f69bed11253a4136c26acded9f3298ed115580745`), both from wizard-app-store #32. Channel: on latest only (stable stays 0.3.61 until promotion). Canonical: wizard-os #259. SHA256 `11d78db80b6e42bc548a4783014b13b9bcfb88d0204865a9edbd681200f065f5`.

## 0.3.62 provenance

Latest continuity only: exactly the 0.3.60 runtime pins (only version, name and about differ), so Latest never trails Stable. It does not carry the 0.3.61 guide or fix; those reach main through wizard-ux #569. Channel: latest. Canonical: wizard-os #257 (file) and #258 (latest → 0.3.62). SHA256 `c6b9fde9bcd21fbd159ded564f59453b9f0bd8be925488238afef03df10ad057`.

## 0.3.61 provenance

Cut in wizard-os #257 (merge `d1793589`) and promoted to stable in #258 (merge `e094225d`). SHA256 `b049d493c0eea548ce5a987a4cb05985912020b13c80765f4f0071233ebb6665`.

## stable 0.3.59 (superseded by 0.3.61)

Promoted under DECISIONS #93 (fast-track, classification **feature**; owner-authorized by Miguel, 26 Oct 2026: "Ship it (not limited to just 2 though)"). Stable 0.3.57 plus any number of Claude subscriptions:
- **Claude app.** claude-connection 3b2d618 → eb434ae (0.2.5; engine `wizard-claude:0.2.5@sha256:4a8f91cfa6abc829d17af533f18d23932a131ca48e2e821e870db0f474a2436f`, wizard-app-store #31).
- **Shell.** wizard-ux ffa4c636 → 325be039 (#564: #537 + #558 cherry-picked on `release/stable-onboarding`; image `sha256:cd80f6f823cfc2fd74532fbad5d002eebf2b584cdf080ad30a944b97566db06d`, differs only in `core/chat-models.js`).
- **App Manager.** d926eec → e3f76a68 on `maintenance/stable-0.3.57` (#250 = #249 + #252: alongside host dashboards turn on the account broker and reload after the plugin refresh; image inputs unchanged).

Every other pin is identical to 0.3.57. The upgrade 0.3.57 → 0.3.59 passed at component level: the pinned App Manager steps (`claude_login.ensure`, `host_dashboard.ensure`) plus a Compose recreate of both Claude versions. The original sign-in stayed byte-identical, and rollback was verified. No full `os.sh` run happened on a real box. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.59.md (wizard-os #256). Rollback: point `stable` back at 0.3.57.

Release JSON SHA256: `27db303e23ac810692c4106152ba230bb33c7f97e8eaef4f73bd12c558993c96`.
Promotion JSON SHA256: `6d6a2c84c0151263179607ddaba9dfcd9aedd7c79580a4c37255bf3d7f11dc36`.

## 0.3.60 provenance

Latest 0.3.58 plus the same multi-account Claude change, so Latest never trails Stable:
- **Claude app.** claude-connection eb434ae.
- **App Manager.** 4879531820cec4680ab19ed0a47ee8f094421c8a on `maintenance/latest-0.3.58` (#251).
- **Shell.** Wizard UX main a0399680 (#558, includes #537; image `sha256:a4b018a26f62ee42b9a5399a52ba6e5371a5ce3f714a16e576ff183b25611223`). 16 other main UX PRs since e0900880 ride along; they are listed in its `about`.

Channel: latest. Canonical: wizard-os #255 (cut) and #256 (latest → 0.3.60). SHA256 `ab3258adb9fe60252550af96a9f62445a4768c0be5a3addca3c43c1e9c93f5d9`.

## 0.3.59 provenance

Cut in wizard-os #255 (merge `109c46b3`) and promoted to stable in #256. SHA256 `27db303e23ac810692c4106152ba230bb33c7f97e8eaef4f73bd12c558993c96`.

## stable 0.3.57 (superseded by 0.3.59)

Promoted under DECISIONS #93 (fast-track, classification **feature**; owner-requested and authorized by Miguel, 26 Oct 2026). It is stable 0.3.55 with only the wizard-ux pin changed: `components.wizard-ux`, `images.wizard-ux` and `default_apps.shell` move from 26c3b20 to ffa4c6366f9267e3ed5ca2fbc710ca9c8ad97686 (wizard-ux #548 on `release/stable-onboarding`), image `sha256:a667cd87f0446dce9e6117ff05adf741df47979d380a31c946cd400e11ed721c`. The phone tour ends with an optional Add to Home Screen beat, replacing the install text removed from os.md (#246). Every other pin is identical to 0.3.55. The upgrade 0.3.55 → 0.3.57 passed at component level, the same standard as 0.3.55. It used the pinned App Manager d926eec `write_shell_override` step with the release pins, a Compose recreate onto the new digest, and a browser matrix on the upgraded container (11/11), with rollback verified. No full `os.sh` installer run happened on a real box (see the evidence). Canonical evidence: wizard-os/docs/acceptance/promote-0.3.57.md (wizard-os #251). Rollback: point `stable` back at 0.3.55.

Release JSON SHA256: `69d54ea0cf9e2a5361eabedcfe6510071594868803b743cdf268e89dd05544db`.
Promotion JSON SHA256: `841d522aeb0132d2539d5e61493f7bd8d5f16e422d9d69a88b48bc48e10abc4e`.

## 0.3.58 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #250 (file) and PR #251 (cut onto latest), `releases/0.3.58.json`; byte-identical SHA-256 `e9de77dddc22dfe3301f210e4fa276bb5bafd6e80514d6322ce2b88ea5c9405f`.
- Latest continuity only: exactly the 0.3.56 runtime pins (only version, name and about differ), so Latest testers keep the 0.3.54 backup bundle while stable moves to 0.3.57. It does not carry the 0.3.57 Install beat.
- Channel: on latest only.

## 0.3.57 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #250, `releases/0.3.57.json`; byte-identical SHA-256 `69d54ea0cf9e2a5361eabedcfe6510071594868803b743cdf268e89dd05544db`.
- Stable 0.3.55 plus wizard-ux #548 (official maintenance publication run 40292759016). No trunk features ride along.
- Channel: promoted to stable 26 Oct 2026; see `## stable 0.3.57` above.

## stable 0.3.55 (superseded by 0.3.57)

Promoted under DECISIONS #82 (fast-track, classification fix). It is stable 0.3.53 with only the Troubleshooter pin changed: `components.wizard-troubleshooter` 4b1f0ef → c1b4c432fd7b162d23d42fb7d3d8fb049bee764e (wizard-troubleshooter #12 and #13, plus the #10 license). This is host tooling with no image, and every other pin is identical to 0.3.53. Upgrade 0.3.53 → 0.3.55 of the `wam troubleshooter` step passed in a disposable systemd box. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.55.md (wizard-os #249). Rollback: point `stable` back at 0.3.53.

Release JSON SHA256: `f00d8bbd4360acc28144e03dd3bcbcf3c518f026ccdb05cf88598d59aad49000`.
Promotion JSON SHA256: `7dd2fbedeb54949b388837ccfec91ea9c3d80eabb576b9e46f3ff8487b41428a`.

## 0.3.56 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #248 (file) and PR #249 (cut onto latest), `releases/0.3.56.json`; byte-identical SHA-256 `a9d449c424e653c63b905a0d31cb0cee886dce5c696fd4688f9e214a21e028c5`.
- Latest 0.3.54 plus the same Troubleshooter pin as stable 0.3.55 (c1b4c43). Every image and every other pin is identical to 0.3.54.
- Channel: on latest only. It keeps the 0.3.54 backup bundle, which still needs its own gates before promotion.

## 0.3.55 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #248, `releases/0.3.55.json`; byte-identical SHA-256 `f00d8bbd4360acc28144e03dd3bcbcf3c518f026ccdb05cf88598d59aad49000`.
- Stable 0.3.53 plus Troubleshooter c1b4c43. This adds the bounded owner-approved repair tools shared by Claude, ChatGPT and Tinfoil (#12), and lets Claude fall back to the agent's second Claude sign-in on a usage limit (#13). No trunk features ride along.
- Channel: promoted to stable 26 Oct 2026; see `## stable 0.3.55` above.

## stable 0.3.53 (superseded by 0.3.55)

Promoted under DECISIONS #82 (fast-track, classification fix: only the wizard-ux Stable onboarding fix vs stable 0.3.39) after a supported strict installer upgrade of the alongside demo from stable 0.3.39. Authorized by Miguel (@mistermizel), 26 Oct 2026. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.53.md (wizard-os #244). Rollback: point `stable` back at 0.3.39.

Release JSON SHA256: `53bcbd64c56967add1bf70634c6139e7e022d28ccda39f36da58748eba301bcb`.
Promotion JSON SHA256: `55dadebe6e5711dbd8f1b1fcc601ba4ea461c06c1e464eee53868c5e5adb816f`.

## 0.3.53 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #242, `releases/0.3.53.json`; byte-identical SHA-256 `53bcbd64c56967add1bf70634c6139e7e022d28ccda39f36da58748eba301bcb`.
- Corrected fix-only Stable onboarding candidate. Supersedes the unpromoted 0.3.51 candidate without rewriting its immutable manifest: UX #535 reserves native safe areas and gives absolute Done rows real WebKit/Chromium scroll reach. No unrelated Latest feature imported; all non-UX runtime pins are identical to Stable 0.3.39.
- Official hosted signed maintenance publication and anonymous amd64/arm64 pulls: UX run `40264433758`, source `26c3b20fa484b79cb1b1836bfd05064195659360`, digest `sha256:03a93684174475546f38bcabd246894cecbde09bc6c78e4ae1178ed0045d91d9`.
- At cut (superseded): Stable stayed 0.3.39 until new exact-candidate installed acceptance and separately reviewed promotion. Latest continuity will preserve the prior Latest bundle separately and never promote it as this repair.
- Channel now: promoted to stable 26 Oct 2026 after the installed 0.3.39 → 0.3.53 acceptance; see `## stable 0.3.53` above.

## 0.3.51 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #237, `releases/0.3.51.json`.
- Byte-identical SHA-256: `9f28c6104767a5812cd429a2f086bda657d3aa782850f72a4873632b180ce2cd`.
- Fix-only backport atop Stable 0.3.39. Only the UX component/image/default-shell source changes; no unrelated 0.3.50 features are promoted. One-time maintenance-source exception reviewed in wizard-os #236 and UX #530; runtime backport UX #531.
- Official hosted multi-architecture publish/sign/finalize/anonymous-pull run: `wizard-ux/actions/runs/40259576904`; exact maintenance source `c0bd9adc82f6d61cd156b9f2568249cff7d668ba`, digest `sha256:a36276aa5f63676c3217a24664831cac80e32c434adf3312b0fc66cb34ea9bd0`.
- Channel: Latest only at this cut. Stable stays 0.3.39 until installed upgrade/fresh acceptance and a separately reviewed promotion record. This mirror does not claim that acceptance has passed.

## 0.2.2 provenance

- Canonical source: `humanitylabs-org/wizard-os`, merge `8f54a471535a66afbdda3520dd6b6374541bb143`, `releases/0.2.2.json`.
- Byte-identical SHA-256: `8a0d3394c92e01984069647c8e6f2f7f21ddd5ef7a26c8e84daa18429d747c84`.
- Every image is pinned by immutable digest and verified for amd64 and arm64. Component commits and publication evidence are in the manifest.
- This snapshot includes split UX/Agent Service images and the App Manager installer fixes. It does not include later UI commits simply because they are newer.
- Alongside installation consumes exact split-image pins. Fresh installation still uses catalog defaults and reports drift where those differ. This metadata publication is not a claim that full fresh-machine acceptance has passed.

The canonical manifest’s private-URL caveat records its original publication boundary. This public mirror closes that metadata-availability gap; it does not expose the private repository or alter the selected image identities.

## stable 0.3.32 (promoted 22 Oct 2026)

- Canonical source: `humanitylabs-org/wizard-os` PR #151 (merge `9c369f4`), `releases/stable` and `releases/promotions/0.3.32.json`. Byte-identical SHA-256: `0.3.32.json` `b81fbc64d8a4a61a55239461380745b8d26df5f339eeb95b481b0b185ad2c099`, `promotions/0.3.32.json` `8134f8b4ce4a3ac3af0be9649c92d651054ff54c8684702849f5ec0a00dd17e4`.
- First fast-tracked fix promotion (wizard-os DECISIONS #82): the upgrade test went 0.3.25 → 0.3.32 on the demo box, evidence `docs/acceptance/promote-0.3.32.md`. Feature-like ride-alongs are listed in the record's `fast_track.notes`.
- Channel: `stable` and `latest` both 0.3.32. Rollback: point `stable` back at 0.3.25, whose grandfathered record is still valid.

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

## 0.3.33 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #154 (merge `b053eda`), `releases/0.3.33.json`. Byte-identical SHA-256: `ae75530ccc94323b701fbf34cbb93b1ca12e347f51ab450df1a936be6902aa3b`.
- 0.3.32 plus the Agent Service at official main `634fe96` (3c45698 + wizard-agent-service #90). The 0.3.32 shell calls the Setup API (`/wizard/v1/setup`) and `/wizard/v1/ui-preferences`, but its Agent Service pin `e7bfd6d` predates both, so first-run Setup never opened on a fresh box. #90 also lets the shell's own Serve host through the Setup host guard. Agent Service image `sha-634fe96…` (`sha256:51b19dd8…8277a`, amd64+arm64, Publish run 39723089025).
- Channel: on `latest` only. `stable` stays 0.3.32. The Agent Service ride-alongs include features (#68, #42, #72, #87), so this release soaks for 72h before promotion (DECISIONS #82).

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

## 0.3.34 provenance

- Canonical source: `humanitylabs-org/wizard-os` PR #187 (merge `b9e2f17`), `releases/0.3.34.json`. Byte-identical SHA-256: `86006b42f04e5cb55e70e46aae09c01661b2b4d8ac4b869cf3f486d0fdb7ddd9`.
- Current main for the private launch: Wizard UX `788affe`, Agent Service `65d33e1`, App Manager `0f8a58b` (image `db4325a`, image inputs unchanged). Default apps and Hermes pin unchanged from 0.3.33.
- Channel: on latest only.

## stable 0.3.34 (promoted 24 Oct 2026)

- Canonical source: `humanitylabs-org/wizard-os` PR #189 (branch `release/promote-0.3.34`, merge `5d7823f99ec8932b13163f7260a32be618956eb8`), `releases/stable` (SHA-256 `9bfdc0fda10c4841166b035a7c6d794879ec5bb2438c98a5cff8f897e5ccecad`) and `releases/promotions/0.3.34.json` (SHA-256 `b39449ac389001a96f8b47f90f4f0807f1b67a868bed0e8fb020ec750fbec2f3`).
- Fast-tracked feature promotion (DECISIONS #93, launch phase): upgrade 0.3.32 -> 0.3.34 passed on the demo box, evidence `docs/acceptance/promote-0.3.34.md`.
- Channel: `stable` and `latest` both 0.3.34. Rollback: point `stable` back at 0.3.32.

## 0.3.35 provenance

- Canonical source: `humanitylabs-org/wizard-os` `releases/0.3.35.json`, SHA-256 `5cb16a802b380129e7377622af8205843ad904a673909efea119ab9f098926f4`.
- 0.3.34 plus Agent Service `039eff5` (#141 phone setup on alongside boxes, #142 owner message shows). Channel: on latest.

## 0.3.36 provenance

- Canonical source: `humanitylabs-org/wizard-os` `releases/0.3.36.json`, SHA-256 `3f6727df061f5f092e273aa3c20fc65c99a1b86c87bb02f83735ce4a64c186ca`.
- 0.3.35 plus Agent Service `35b2463` (#143 owner message matching on real Hermes history). Channel: on latest.

## stable 0.3.36 (promoted 24 Oct 2026)

- Fast-tracked fix promotion: upgrade 0.3.34 -> 0.3.36 passed on the demo box, evidence `docs/acceptance/promote-0.3.36.md`. Rollback: point `stable` back at 0.3.34.

## 0.3.37 provenance

- Canonical source: `humanitylabs-org/wizard-os` `releases/0.3.37.json`, SHA-256 `4046b9ce04ef80663c644dce05526cc24fd677c5fa741158830a6641c55cb266`.
- 0.3.36 plus Wizard UX `78b3311` (#440) and App Manager `cfd13ec` (#194). Channel: on latest.

## 0.3.38 provenance

Canonical: wizard-os releases/0.3.38.json. SHA256: `999fc2a33e0534478e23172d899fd4aa8bc1281724815bb62c6c42004f0862bf`.
Channel: on latest only; stable remains 0.3.36 pending installed-demo upgrade verification.

## stable 0.3.38

Promoted under DECISIONS #93 after supported installed-demo upgrade from stable 0.3.36. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.38.md (wizard-os #197). Feature classification records shared-kit/My-skin/default-change ride-alongs.

Release JSON SHA256: `999fc2a33e0534478e23172d899fd4aa8bc1281724815bb62c6c42004f0862bf`.
Promotion JSON SHA256: `61927c23be97586486781c4b932b40351cd7c6bed637666f0c120f0bc072dd03`.

## 0.3.39 provenance

Canonical: wizard-os releases/0.3.39.json (wizard-os #203). SHA256: `6aa5201e633339f71b3854c13dff30189d0a03d57799854713c1455e19a60243`.
0.3.38 plus wizard-ux #456 (`c8433e3`): first-run tour without the Command beat and with per-step skip; UX image `sha256:57bb8e26154aeec5a1316497ab6e22570858a842b6e8ecda7890568ea594ee19`. Other pins unchanged.
Channel: on latest only; stable remains 0.3.38 pending installed-demo upgrade verification.

## stable 0.3.39

Promoted under DECISIONS #93 (classification feature: the default first-run tour changes) after a supported strict installer upgrade of the alongside demo from stable 0.3.38. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.39.md (wizard-os #204). Rollback: point `stable` back at 0.3.38.

Release JSON SHA256: `6aa5201e633339f71b3854c13dff30189d0a03d57799854713c1455e19a60243`.
Promotion JSON SHA256: `ca495855d7f5b917fac0eb20037df1523cf1aff974214ae9633fe26431914bbe`.

## 0.3.40 provenance

Canonical: wizard-os releases/0.3.40.json (wizard-os #209). SHA256: `947cb1e88bf758e8e4889f85a352e1d04f688fc3543f9e3e9c8dedc8eda7fd53`.
0.3.39 plus wizard-ux #482/#483 (`9fcf86c`): alongside first launch starts the first-message orientation once (phone tour, desktop composer guide), Setup stays manual, the desktop shell loads the tour; UX image `sha256:c369207da4392095a2be02c6c56c05f877b6bf079546c6816c375b11659b6c90`. Other pins unchanged.
Channel: on latest only; stable remains 0.3.39 pending installed-demo upgrade verification.

## 0.3.50 provenance

Canonical wizard-os PR #233. Channel: on latest only; stable remains 0.3.39.
Manifest SHA256: `918b984a4ba0bd78cb863ad559adf762752c60d43d058d5288d4e138ca13044f`. Complete 0.3.49 non-UX pins retained; compatible Android/onboarding/Spaces UX is immutable 03b07142. No passed runtime release gate or promotion implied.
