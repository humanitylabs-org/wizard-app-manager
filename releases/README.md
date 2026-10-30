# Public Wizard OS release metadata

This directory publishes only the installer’s release metadata. The canonical Wizard OS specification repository remains private; no source checkout, credentials or owner configuration is mirrored here.

`get.sh` reads `stable` (or `latest` on a box whose owner chose the latest channel), then `<version>.json`. `WIZARD_RELEASE_BASE` still supports an explicitly chosen alternate mirror or local release directory.

## Channels (wizard-os DECISIONS #75)

- `latest`: every release as soon as it is cut. Mirror it with each new release JSON.
- `stable`: only releases promoted with wizard-os `tools/promote_stable.py`, i.e. 72 hours on latest plus a passed upgrade test, recorded in `promotions/<version>.json`. A fix or feature release with a passed upgrade test may skip the 72 hours (`fast_track` in its record, wizard-os DECISIONS #82 and #93); `wam/updates.py` applies the same gate as the tool. Mirror the record together with the pointer.

Never point `stable` at a release without its promotion record: `tests/test_release_channels.py` fails, and boxes on Automatic refuse to install it. 0.3.25 is grandfathered as the first stable.

## stable 0.3.63

Promoted under DECISIONS #82/#93 (fast-track, classification **fix**). Stable 0.3.61 with only the two AI connection apps changed: ChatGPT chatgpt-connection f87d5dd → 476d771 (0.2.4: the same accounts page as Claude, sign-in through the agent, "Device code sign-in for Codex" named up front) and Claude claude-connection eb434ae → d0b59dd (0.2.6: no Claude Code fallback on alongside boxes, one row per account); both report an unreachable agent with Retry and answer `/api/connection` with `unreachable` instead of an empty inventory (wizard-app-store #32). Installed upgrade 0.3.61 → 0.3.63 passed on the demo box through `os.sh` with `WIZARD_STRICT=1`: pinned, 0 failed checks, Hermes files unchanged, doctor 0 FAIL, Home shows ChatGPT with usage. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.63.md (wizard-os #259 cut, #261 promotion). Rollback: point `stable` back at 0.3.61.

Release JSON SHA256: `11d78db80b6e42bc548a4783014b13b9bcfb88d0204865a9edbd681200f065f5`.
Promotion JSON SHA256: `80eb7f616dd25b5a2da0548d3284f55d2379a7f06082e1e6f34aea06bddc1534`.

## 0.3.64 provenance

Latest continuity: 0.3.62 (the 0.3.60 runtime pins) plus the same two AI connection app pins as Stable 0.3.63, so Latest never trails Stable. Channel: latest. Canonical: wizard-os #261. SHA256 `0c19f7bd3436fe7c8c00738c8e8cea09af2db62964f2638c05f97c88aea9e8a4`.

## stable 0.3.61 (superseded by 0.3.63)

Promoted under DECISIONS #93 (fast-track, classification **feature**; owner-authorized by Miguel, 26 Oct 2026: approved final wording verbatim, "ship it"). Stable 0.3.59 with only the shell pin changed: wizard-ux 325be039 → 87b35bcb1314961887c06da5f96ad10b50786fe6 (`release/stable-onboarding`; #567 Recommended fix: the orb never freezes in an iPhone Safari tab; #568: three-step guide "Step N of 3" with the owner's words, Hide and Install beats removed; image `sha256:ecaba88e568ff2b6d90c6ca19a5b9bba153fcbb2eb7a4ee1aca3073c9e9acdb6`, official dispatch run 40339463332). Every other pin is identical to 0.3.59. Installed upgrade 0.3.59 → 0.3.61 passed on demo-4 through `os.sh` with `WIZARD_STRICT=1` (mirror-head base disclosed): pinned, 0 failed checks, Hermes files unchanged, doctor 0 FAIL, live WebKit check 61/61. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.61.md (wizard-os #257 cut, #258 promotion). Rollback: point `stable` back at 0.3.59.

Release JSON SHA256: `b049d493c0eea548ce5a987a4cb05985912020b13c80765f4f0071233ebb6665`.
Promotion JSON SHA256: `ce5b9ff2811b108ccfc6d2ffb4744897dea9b5e54985d2c339a70c759174c076`.

## 0.3.63 provenance

Stable 0.3.61 with only the two AI connection apps changed: ChatGPT chatgpt-connection f87d5dd → 476d771 (0.2.4, engine `wizard-chatgpt:0.2.4@sha256:1c90102e819574b4c5e0c50f782b3f7cd0e998b53415407471bf59dabca27418`) and Claude claude-connection eb434ae → d0b59dd (0.2.6, engine `wizard-claude:0.2.6@sha256:5c7ca0b3b38803794aa3296f69bed11253a4136c26acded9f3298ed115580745`), both from wizard-app-store #32. Channel: stable since wizard-os #261. Canonical: wizard-os #259. SHA256 `11d78db80b6e42bc548a4783014b13b9bcfb88d0204865a9edbd681200f065f5`.

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

## 0.3.65 provenance

Canonical: wizard-os releases/0.3.65.json (wizard-os #262). SHA256: `4407ed7378988ad2d7ae55450ef52bbfca1f7678632264baa4dc30767cdf6fa0`.
Stable 0.3.63 plus wizard-app-manager #258 on the Stable maintenance branch (#259, `8607981`): installer reruns recreate connection apps on an address/login change or stale running container, HTTPS-off owner step, approved Tailscale login waited for, official-installer Hermes routes to `hermes update`. Other pins unchanged.
Channel: on latest only; stable remains 0.3.63 pending the upgrade test.

## stable 0.3.65

Promoted (fast-tracked fix, DECISIONS #82/#93) after a passed 0.3.63 -> 0.3.65 upgrade test with WIZARD_STRICT=1 on a disposable guest. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.65.md (wizard-os #263). Rollback: point `stable` back at 0.3.63.

Release JSON SHA256: `4407ed7378988ad2d7ae55450ef52bbfca1f7678632264baa4dc30767cdf6fa0`.
Promotion JSON SHA256: `b66b41d3fcf982410a70c2ff31efba8589cf11a6be87ea32508a63acbc40f50b`.

## 0.3.66 provenance

Canonical: wizard-os releases/0.3.66.json (wizard-os #263). SHA256: `68605bbc445ec12a38f6350a304e91b17773a3bdb289d10dcebebac2d34ab8b7`.
Latest continuity: 0.3.64 plus wizard-app-manager #258 on the Latest maintenance branch (#260, `da2f01d`). Other pins unchanged.
Channel: on latest only.

## 0.3.68 provenance

Canonical: wizard-os releases/0.3.68.json (wizard-os #267). SHA256: `29649334a5685cf85ed23f509c78ccdf1235dc55bd85f1e986c7a144eb4e9b04`.
0.3.67 plus Agent Service main `b1f8d2c1` (#191, #195, #196 emailHash, #203 identity marker), Wizard UX main `d53b2b24` (#573 model picker refresh), Claude 0.2.8 (`43f6a412`) and ChatGPT 0.2.6 (`f73b8487`). WAM, Troubleshooter, Hermes and other default apps unchanged.
Channel: on latest only; stable remains 0.3.65 pending the upgrade tests.

## stable 0.3.68

Promoted (fast-tracked fix, DECISIONS #82/#93) after passed 0.3.65 -> 0.3.68 and 0.3.61 -> 0.3.68 upgrade tests with WIZARD_STRICT=1 on disposable guests. Canonical evidence: wizard-os/docs/acceptance/promote-0.3.68.md (wizard-os #270). Rollback: point `stable` back at 0.3.65.

Release JSON SHA256: `29649334a5685cf85ed23f509c78ccdf1235dc55bd85f1e986c7a144eb4e9b04`.
Promotion JSON SHA256: `0984d62ca0b54ac0e4dab86e26df9b2dee7d2f93ddd89783f29eb8a3f21d3474`.

## 0.3.70 provenance

Canonical: wizard-os releases/0.3.70.json (wizard-os #270). SHA256: `1488db56ff5f4fa1f83105f18700d5f172b926934d201e933533a84f144699b4`.
0.3.69 (WAM `08b54f65`, #267) with 0.3.68's Agent Service `b1f8d2c1`, Wizard UX `d53b2b24`, Claude 0.2.8 and ChatGPT 0.2.6.
Channel: on latest only.

## 0.3.71 provenance

Canonical: wizard-os releases/0.3.71.json (wizard-os #287). SHA256: `7f854dbbe2cb15129615f3a2fd3ed1632a46d749219013f890a093fe0812a850`.
0.3.70 with Notifications 2.29.0-wizard10 (`oscarhenrycollins/notifications` `cdce9f18`, #277); compose images unchanged from 2.29.0-wizard2. Everything else exactly 0.3.70.
Channel: on latest only.

## 0.3.72 provenance

Canonical: wizard-os releases/0.3.72.json (wizard-os #292). SHA256: `0bf3563ed9d27b24f479819678d982cff036d17c32b246cc8602f1833257c588`.
0.3.71 with Wizard UX main `970378b2` (wizard-ux #670: pinned chats stay the same on every device; image `sha256:d1b00210…`, push run 40628699290). Everything else exactly 0.3.71.
Channel: on latest only.

## stable 0.3.72

Canonical: wizard-os #293 (promotion), cut in wizard-os #292. `releases/stable` → 0.3.72 (sha256 `d8c646206043137d6ef9e2a12e9f1de591b37cb026b94f4476c101db0f3338ab`); `promotions/0.3.72.json` sha256 `3911f6babcf670f2d43df22145926e35720c369d1efb0436a4751045ecfc44a7`.
Fast-tracked under DECISIONS #93 as a **feature** (owner-requested pins fix wizard-ux #670, plus ride-alongs: Notifications 2.29.0-wizard10, Wizard UX main features, App Manager 08b54f65). Installed upgrade 0.3.68 → 0.3.72 passed: wizard-os docs/acceptance/promote-0.3.72.md.

## 0.3.73 provenance

Canonical: wizard-os releases/0.3.73.json (release train). SHA256: `79ee11458bed4df743fda84a0484afc021440f5861043383233e30c9d52a511b`.
Latest 0.3.72 with Wizard UX 970378b2 -> db361987, Agent Service b1f8d2c1 -> 3773ca0c. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#674 Recommended fix: Spaces and subject order keep every device's edit; humanitylabs-org/wizard-ux#675 Recommended fix (performance): Settings says 'Takes about 2 minutes' when only the interfa; humanitylabs-org/wizard-ux#676 Recommended fix: an unreachable Wizard on Tailscale says what to check instead of 'No inte; humanitylabs-org/wizard-agent-service#205 Recommended fix: serve live run events for model-locked turns (events were 404 for every W; humanitylabs-org/wizard-agent-service#206 Recommended fix: archive window honours the Auto-archive choice; humanitylabs-org/wizard-agent-service#214 Recommended fix: owners choose Summary or Concise replies; humanitylabs-org/wizard-agent-service#219 Recommended fix: bring live fixes onto main: dismissed errors stay dismissed, faster Chats; humanitylabs-org/wizard-agent-service#221 Recommended fix: apps list carries declared type and category; humanitylabs-org/wizard-agent-service#237 Recommended fix: every picture or file an agent sends shows in chats; humanitylabs-org/wizard-agent-service#238 Recommended fix: shared lists keep every device's edit (item operations on ui-preferences); humanitylabs-org/wizard-agent-service#243 Recommended fix: Setup stops saying a phone is connected once the last phone leaves the ta. Stable stays 0.3.72 until the installed upgrade test passes.
Channel: on latest only.

## stable 0.3.73

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/303 (release train). `releases/stable` → 0.3.73; `promotions/0.3.73.json` sha256 `915e97a34a50a70d4e6d1e3fb38dfedcf3d751a40c02a86eb487b14a2e58747b`.
Fast-tracked as **feature**. Installed upgrade 0.3.72 → 0.3.73 passed: wizard-os docs/acceptance/promote-0.3.73.md.

## 0.3.74 provenance

Canonical: wizard-os releases/0.3.74.json (wizard-os #299, merge b2c7858d). SHA256: `dea8615dcf89453cc9c0b25fcfa144c2994dc0f464ab5f8397babce1651cbe21`.
0.3.73 with App Manager main `79272034` (#304 quick interface updates; #311/#312/#320 fixes; everything merged on main since 0.3.67's line, including #305/#307 automatic app updates, app connections, #184, #214); image `v4.10.2-wizard.778-d9e1644` (`sha256:53007625…`, run 40672973160, image inputs unchanged d9e16446..79272034). Everything else exactly 0.3.73.
Channel: on latest only.

## stable 0.3.74

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/313 (promotion), cut in wizard-os #299. `releases/stable` → 0.3.74; `promotions/0.3.74.json` sha256 `1ca71a372fc2148a22837b02adf224bf951ab034e9924160436472e83b361c10`.
Fast-tracked as **feature** (DECISIONS #93): App Manager main 79272034 (quick interface updates #304, fixes #311/#312/#320, App Manager main features ride along). Installed upgrade 0.3.73 → 0.3.74 passed: wizard-os docs/acceptance/promote-0.3.74.md.

## 0.3.75 provenance

Canonical: wizard-os releases/0.3.75.json (release train). SHA256: `4667a48cf1dbec17a2b98c13589290bc144f94dcc200e090497cec01c49f879e`.
Latest 0.3.74 with Wizard UX db361987 -> 91218c83, Agent Service 3773ca0c -> 7269a990. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#416 Recommended fix: Media viewer: long screenshots scroll; humanitylabs-org/wizard-ux#447 Recommended fix: Dictation inserts at the cursor instead of a copy card; humanitylabs-org/wizard-ux#458 Recommended fix: Store's My apps link opens in place instead of reloading Wizard; humanitylabs-org/wizard-ux#487 Recommended fix: Photos backup runs on Wi-Fi when the tailnet VPN is on; humanitylabs-org/wizard-ux#586 Recommended fix: Retry saved first messages after subscription sign-in recovers; humanitylabs-org/wizard-ux#587 Recommended fix: Show saved messages waiting for AI sign-in consistently; humanitylabs-org/wizard-ux#589 Recommended fix: Keep first-launch app discovery from recursively rebuilding the shell; humanitylabs-org/wizard-ux#605 Recommended fix: Share to Wizard picks New chat or one chat, quotes forwarded words, and s; humanitylabs-org/wizard-ux#635 Recommended fix: show times in the phone's own timezone; humanitylabs-org/wizard-ux#636 Recommended fix: every picked image rides an orb send made while it uploads; humanitylabs-org/wizard-ux#641 Recommended fix: Share to Wizard sheet looks like the rest of the OS; humanitylabs-org/wizard-ux#650 Recommended fix: Messenger recovers its own take after a reload and orphaned capture start; humanitylabs-org/wizard-ux#652 Recommended fix: tapping outside a sheet, menu or modal acts as Back across apps and shell; humanitylabs-org/wizard-ux#655 Recommended fix: keep Ready asks visible while helpers continue; humanitylabs-org/wizard-ux#658 Recommended fix: overlays with text are fully opaque in every skin; humanitylabs-org/wizard-ux#679 Recommended fix: status pills get their own colours in every skin; humanitylabs-org/wizard-ux#680 Recommended fix: the app kit follows its own text and touch-size rule; humanitylabs-org/wizard-ux#682 Recommended fix: apps open at once; setup check no longer holds the window; humanitylabs-org/wizard-ux#684 Recommended fix: show notifications when Ready has no chats; humanitylabs-org/wizard-ux#689 Recommended fix: keep saved-message retries out of the connection warning; humanitylabs-org/wizard-ux#690 Recommended fix: keep model picker above tall drafts and keyboard; humanitylabs-org/wizard-ux#691 Recommended fix: Setup and Settings use the shared app style contract; humanitylabs-org/wizard-ux#692 Recommended fix: keep agent replies on their own model and retain their threads; humanitylabs-org/wizard-ux#693 Recommended fix: Keep repeated Back presses inside Wizard at Home; humanitylabs-org/wizard-ux#694 Recommended fix: refresh the model picker on every open without repainting unchanged choic; humanitylabs-org/wizard-ux#696 Recommended fix: a running reply never shows its interim line twice; humanitylabs-org/wizard-ux#704 Recommended fix: Use current-route wording for voice health help; humanitylabs-org/wizard-ux#709 Recommended fix: a chat is in one Space; filed chats read Move to Space…; humanitylabs-org/wizard-ux#710 Recommended fix: Settings rows stay flat lines over the shared kit controls; humanitylabs-org/wizard-ux#713 Recommended fix: owners see Apps, never Wizard apps, on the agent page, Apps sheet and sha; humanitylabs-org/wizard-ux#715 Recommended fix: Screen chat QA fixture uses invented text, not a real team conversation; humanitylabs-org/wizard-ux#720 Recommended fix: show Claude extra enabled when its cap is unavailable; humanitylabs-org/wizard-ux#721 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#723 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#725 Recommended fix: shared-app install links open in the Store again; humanitylabs-org/wizard-ux#726 Recommended fix: launcher shows long one-word app names whole; humanitylabs-org/wizard-ux#728 Recommended fix: Ready Priority ranks every status and shows its groups; humanitylabs-org/wizard-agent-service#155 Recommended fix: owner CLI importer includes python-dotenv; humanitylabs-org/wizard-agent-service#183 Recommended fix: threads know MEDIA sends PDFs and other files up to 256 MB; humanitylabs-org/wizard-agent-service#197 Recommended fix: import older Hermes homes without shared-file or credential leaks; humanitylabs-org/wizard-agent-service#228 Recommended fix: let open owner asks stay Ready during background work; humanitylabs-org/wizard-agent-service#235 Recommended fix: chat pictures and files stay in the owner's browser, and edited files sti; humanitylabs-org/wizard-agent-service#241 Recommended fix: a sent file is a snapshot, so a later rewrite never changes earlier chat ; humanitylabs-org/wizard-agent-service#245 Recommended fix: pointer skills send the agent through the app's own skill, with end-to-en; humanitylabs-org/wizard-agent-service#250 Recommended fix: app workflow changes stay in released apps; humanitylabs-org/wizard-agent-service#254 Recommended fix: a chat is in one Space at a time; humanitylabs-org/wizard-agent-service#256 Recommended fix: Secret scan passes again (the round-10 synthetic placeholder is not a key; humanitylabs-org/wizard-agent-service#257 Recommended fix: the app-skill guard stops blocking reads and verification of installed sk; humanitylabs-org/wizard-agent-service#260 Recommended fix: scheduled notifications use the job's own agent and survive late admissio; humanitylabs-org/wizard-agent-service#263 Recommended fix: Markdown support is a fact, not a formatting push; humanitylabs-org/wizard-agent-service#264 Recommended fix: Secret scan passes again (shared-vault staging test's placeholder PEM is . Stable stays 0.3.74 until the installed upgrade test passes.
Channel: on latest only.

## 0.3.76 provenance

Canonical: wizard-os releases/0.3.76.json (wizard-os #319, merge 4ee0f8af). SHA256: `8f0f4345b784180bd6079decbd7f7578d14ae94366281d819fee657bdb5952b5`.
Stable 0.3.74 with exactly two pins moved: Wizard UX db361987 -> 25f28781 (`release/stable-onboarding`: db361987 + the app picker long-name fix, wizard-ux #726/#730 via #729; image `sha256:bac45843…`, run 40835930467) and Troubleshooter c1b4c43 -> 3e56f66 (host tooling: James's one-tap screen following the selected skin, sign-in reconnect path, reply probes off by default, Tinfoil check fix).

## 0.3.77 provenance

Canonical: wizard-os releases/0.3.77.json (wizard-os #319). SHA256: `20c10e1c8a2ac9832861b4d8bd10e6e37b3e544e0e451d72aee49d5377abd1fb`.
Latest 0.3.75 with only the Troubleshooter pin moved to 3e56f66 (same as 0.3.76), so Latest never trails Stable.
Channel: latest.

## stable 0.3.76

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/320 (promotion), cut in wizard-os #319. `releases/stable` → 0.3.76; `promotions/0.3.76.json` sha256 `2124babafb5e48ccb63e5729638824c57f633278e808bb60e3c0585f13221afc`.
Fast-tracked as **fix** (owner request, Miguel 29 Oct). Installed upgrade 0.3.74 → 0.3.76 passed: wizard-os docs/acceptance/promote-0.3.76.md.

## 0.3.78 provenance

Canonical: wizard-os releases/0.3.78.json (release train). SHA256: `9f0dff039f5aa72d5af800b3201d2ed2ba2d25f5a9456c09b55f4f0be0b10e40`.
Latest 0.3.77 with Wizard UX 91218c83 -> 7c92caef. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#416 Recommended fix: Media viewer: long screenshots scroll; humanitylabs-org/wizard-ux#447 Recommended fix: Dictation inserts at the cursor instead of a copy card; humanitylabs-org/wizard-ux#458 Recommended fix: Store's My apps link opens in place instead of reloading Wizard; humanitylabs-org/wizard-ux#487 Recommended fix: Photos backup runs on Wi-Fi when the tailnet VPN is on; humanitylabs-org/wizard-ux#586 Recommended fix: Retry saved first messages after subscription sign-in recovers; humanitylabs-org/wizard-ux#587 Recommended fix: Show saved messages waiting for AI sign-in consistently; humanitylabs-org/wizard-ux#589 Recommended fix: Keep first-launch app discovery from recursively rebuilding the shell; humanitylabs-org/wizard-ux#605 Recommended fix: Share to Wizard picks New chat or one chat, quotes forwarded words, and s; humanitylabs-org/wizard-ux#635 Recommended fix: show times in the phone's own timezone; humanitylabs-org/wizard-ux#636 Recommended fix: every picked image rides an orb send made while it uploads; humanitylabs-org/wizard-ux#641 Recommended fix: Share to Wizard sheet looks like the rest of the OS; humanitylabs-org/wizard-ux#650 Recommended fix: Messenger recovers its own take after a reload and orphaned capture start; humanitylabs-org/wizard-ux#652 Recommended fix: tapping outside a sheet, menu or modal acts as Back across apps and shell; humanitylabs-org/wizard-ux#655 Recommended fix: keep Ready asks visible while helpers continue; humanitylabs-org/wizard-ux#658 Recommended fix: overlays with text are fully opaque in every skin; humanitylabs-org/wizard-ux#679 Recommended fix: status pills get their own colours in every skin; humanitylabs-org/wizard-ux#680 Recommended fix: the app kit follows its own text and touch-size rule; humanitylabs-org/wizard-ux#682 Recommended fix: apps open at once; setup check no longer holds the window; humanitylabs-org/wizard-ux#684 Recommended fix: show notifications when Ready has no chats; humanitylabs-org/wizard-ux#689 Recommended fix: keep saved-message retries out of the connection warning; humanitylabs-org/wizard-ux#690 Recommended fix: keep model picker above tall drafts and keyboard; humanitylabs-org/wizard-ux#691 Recommended fix: Setup and Settings use the shared app style contract; humanitylabs-org/wizard-ux#692 Recommended fix: keep agent replies on their own model and retain their threads; humanitylabs-org/wizard-ux#693 Recommended fix: Keep repeated Back presses inside Wizard at Home; humanitylabs-org/wizard-ux#694 Recommended fix: refresh the model picker on every open without repainting unchanged choic; humanitylabs-org/wizard-ux#696 Recommended fix: a running reply never shows its interim line twice; humanitylabs-org/wizard-ux#704 Recommended fix: Use current-route wording for voice health help; humanitylabs-org/wizard-ux#709 Recommended fix: a chat is in one Space; filed chats read Move to Space…; humanitylabs-org/wizard-ux#710 Recommended fix: Settings rows stay flat lines over the shared kit controls; humanitylabs-org/wizard-ux#713 Recommended fix: owners see Apps, never Wizard apps, on the agent page, Apps sheet and sha; humanitylabs-org/wizard-ux#715 Recommended fix: Screen chat QA fixture uses invented text, not a real team conversation; humanitylabs-org/wizard-ux#720 Recommended fix: show Claude extra enabled when its cap is unavailable; humanitylabs-org/wizard-ux#721 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#723 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#725 Recommended fix: shared-app install links open in the Store again; humanitylabs-org/wizard-ux#726 Recommended fix: launcher shows long one-word app names whole; humanitylabs-org/wizard-ux#728 Recommended fix: Ready Priority ranks every status and shows its groups; humanitylabs-org/wizard-ux#718 Recommended fix: live replies resume after a dropped connection; humanitylabs-org/wizard-ux#719 Recommended fix: Wizard opens offline and shows saved thread copies; humanitylabs-org/wizard-ux#727 Recommended fix: one offline pop-up with three checks (Internet · Tailscale · Wizard); humanitylabs-org/wizard-ux#730 Recommended fix: launcher long-name size from the type token, with a regression test; humanitylabs-org/wizard-agent-service#155 Recommended fix: owner CLI importer includes python-dotenv; humanitylabs-org/wizard-agent-service#183 Recommended fix: threads know MEDIA sends PDFs and other files up to 256 MB; humanitylabs-org/wizard-agent-service#197 Recommended fix: import older Hermes homes without shared-file or credential leaks; humanitylabs-org/wizard-agent-service#228 Recommended fix: let open owner asks stay Ready during background work; humanitylabs-org/wizard-agent-service#235 Recommended fix: chat pictures and files stay in the owner's browser, and edited files sti; humanitylabs-org/wizard-agent-service#241 Recommended fix: a sent file is a snapshot, so a later rewrite never changes earlier chat ; humanitylabs-org/wizard-agent-service#245 Recommended fix: pointer skills send the agent through the app's own skill, with end-to-en; humanitylabs-org/wizard-agent-service#250 Recommended fix: app workflow changes stay in released apps; humanitylabs-org/wizard-agent-service#254 Recommended fix: a chat is in one Space at a time; humanitylabs-org/wizard-agent-service#256 Recommended fix: Secret scan passes again (the round-10 synthetic placeholder is not a key; humanitylabs-org/wizard-agent-service#257 Recommended fix: the app-skill guard stops blocking reads and verification of installed sk; humanitylabs-org/wizard-agent-service#260 Recommended fix: scheduled notifications use the job's own agent and survive late admissio; humanitylabs-org/wizard-agent-service#263 Recommended fix: Markdown support is a fact, not a formatting push; humanitylabs-org/wizard-agent-service#264 Recommended fix: Secret scan passes again (shared-vault staging test's placeholder PEM is . Stable stays 0.3.76 until the installed upgrade test passes.
Channel: on latest only.

## 0.3.79 provenance

Canonical: wizard-os releases/0.3.79.json (release train). SHA256: `08123e1b0059f015e55beff9d9f0df15e1b3b1b6a8f70ed97309ba393b5dfaa8`.
Latest 0.3.78 with Wizard UX 7c92caef -> 2e316e77, Agent Service 7269a990 -> 24f3cd0a. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#416 Recommended fix: Media viewer: long screenshots scroll; humanitylabs-org/wizard-ux#447 Recommended fix: Dictation inserts at the cursor instead of a copy card; humanitylabs-org/wizard-ux#458 Recommended fix: Store's My apps link opens in place instead of reloading Wizard; humanitylabs-org/wizard-ux#487 Recommended fix: Photos backup runs on Wi-Fi when the tailnet VPN is on; humanitylabs-org/wizard-ux#586 Recommended fix: Retry saved first messages after subscription sign-in recovers; humanitylabs-org/wizard-ux#587 Recommended fix: Show saved messages waiting for AI sign-in consistently; humanitylabs-org/wizard-ux#589 Recommended fix: Keep first-launch app discovery from recursively rebuilding the shell; humanitylabs-org/wizard-ux#605 Recommended fix: Share to Wizard picks New chat or one chat, quotes forwarded words, and s; humanitylabs-org/wizard-ux#635 Recommended fix: show times in the phone's own timezone; humanitylabs-org/wizard-ux#636 Recommended fix: every picked image rides an orb send made while it uploads; humanitylabs-org/wizard-ux#641 Recommended fix: Share to Wizard sheet looks like the rest of the OS; humanitylabs-org/wizard-ux#650 Recommended fix: Messenger recovers its own take after a reload and orphaned capture start; humanitylabs-org/wizard-ux#652 Recommended fix: tapping outside a sheet, menu or modal acts as Back across apps and shell; humanitylabs-org/wizard-ux#655 Recommended fix: keep Ready asks visible while helpers continue; humanitylabs-org/wizard-ux#658 Recommended fix: overlays with text are fully opaque in every skin; humanitylabs-org/wizard-ux#679 Recommended fix: status pills get their own colours in every skin; humanitylabs-org/wizard-ux#680 Recommended fix: the app kit follows its own text and touch-size rule; humanitylabs-org/wizard-ux#682 Recommended fix: apps open at once; setup check no longer holds the window; humanitylabs-org/wizard-ux#684 Recommended fix: show notifications when Ready has no chats; humanitylabs-org/wizard-ux#689 Recommended fix: keep saved-message retries out of the connection warning; humanitylabs-org/wizard-ux#690 Recommended fix: keep model picker above tall drafts and keyboard; humanitylabs-org/wizard-ux#691 Recommended fix: Setup and Settings use the shared app style contract; humanitylabs-org/wizard-ux#692 Recommended fix: keep agent replies on their own model and retain their threads; humanitylabs-org/wizard-ux#693 Recommended fix: Keep repeated Back presses inside Wizard at Home; humanitylabs-org/wizard-ux#694 Recommended fix: refresh the model picker on every open without repainting unchanged choic; humanitylabs-org/wizard-ux#696 Recommended fix: a running reply never shows its interim line twice; humanitylabs-org/wizard-ux#704 Recommended fix: Use current-route wording for voice health help; humanitylabs-org/wizard-ux#709 Recommended fix: a chat is in one Space; filed chats read Move to Space…; humanitylabs-org/wizard-ux#710 Recommended fix: Settings rows stay flat lines over the shared kit controls; humanitylabs-org/wizard-ux#713 Recommended fix: owners see Apps, never Wizard apps, on the agent page, Apps sheet and sha; humanitylabs-org/wizard-ux#715 Recommended fix: Screen chat QA fixture uses invented text, not a real team conversation; humanitylabs-org/wizard-ux#718 Recommended fix: live replies resume after a dropped connection; humanitylabs-org/wizard-ux#719 Recommended fix: Wizard opens offline and shows saved thread copies; humanitylabs-org/wizard-ux#720 Recommended fix: show Claude extra enabled when its cap is unavailable; humanitylabs-org/wizard-ux#721 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#723 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#725 Recommended fix: shared-app install links open in the Store again; humanitylabs-org/wizard-ux#726 Recommended fix: launcher shows long one-word app names whole; humanitylabs-org/wizard-ux#727 Recommended fix: one offline pop-up with three checks (Internet · Tailscale · Wizard); humanitylabs-org/wizard-ux#728 Recommended fix: Ready Priority ranks every status and shows its groups; humanitylabs-org/wizard-ux#730 Recommended fix: launcher long-name size from the type token, with a regression test; humanitylabs-org/wizard-ux#734 Recommended fix: working chats no longer flash into Ready while their history loads; humanitylabs-org/wizard-agent-service#155 Recommended fix: owner CLI importer includes python-dotenv; humanitylabs-org/wizard-agent-service#183 Recommended fix: threads know MEDIA sends PDFs and other files up to 256 MB; humanitylabs-org/wizard-agent-service#197 Recommended fix: import older Hermes homes without shared-file or credential leaks; humanitylabs-org/wizard-agent-service#228 Recommended fix: let open owner asks stay Ready during background work; humanitylabs-org/wizard-agent-service#235 Recommended fix: chat pictures and files stay in the owner's browser, and edited files sti; humanitylabs-org/wizard-agent-service#241 Recommended fix: a sent file is a snapshot, so a later rewrite never changes earlier chat ; humanitylabs-org/wizard-agent-service#245 Recommended fix: pointer skills send the agent through the app's own skill, with end-to-en; humanitylabs-org/wizard-agent-service#250 Recommended fix: app workflow changes stay in released apps; humanitylabs-org/wizard-agent-service#254 Recommended fix: a chat is in one Space at a time; humanitylabs-org/wizard-agent-service#256 Recommended fix: Secret scan passes again (the round-10 synthetic placeholder is not a key; humanitylabs-org/wizard-agent-service#257 Recommended fix: the app-skill guard stops blocking reads and verification of installed sk; humanitylabs-org/wizard-agent-service#260 Recommended fix: scheduled notifications use the job's own agent and survive late admissio; humanitylabs-org/wizard-agent-service#263 Recommended fix: Markdown support is a fact, not a formatting push; humanitylabs-org/wizard-agent-service#264 Recommended fix: Secret scan passes again (shared-vault staging test's placeholder PEM is ; humanitylabs-org/wizard-agent-service#266 Recommended fix: a finished run leaves Working at once. Stable stays 0.3.76 until the installed upgrade test passes.
Channel: on latest only.

## 0.3.80 provenance

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/323. SHA256: `5162b9ad74105d6a565120f5540ce2558e962d3a7dca9fb6bcfed17eac446c54`.
0.3.79 plus the first-run Welcome (wizard-ux #733, feature), App Manager c52f260f (#333 docker-group fix, #321, #331 fixes; #323/#328 Connections), Claude 0.2.10 and ChatGPT 0.2.8 (sign in first, no nickname field). Stable stays 0.3.76 until the 72h soak and an upgrade test.
Channel: on latest only.

## 0.3.81 provenance

Canonical: wizard-os releases/0.3.81.json (release train). SHA256: `143abe4af3648e37ac1f62c75d72ae2eec030e55b7b0f3d9e5f003c882283173`.
Latest 0.3.80 with Wizard UX fe269416 -> bc3c1408. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#416 Recommended fix: Media viewer: long screenshots scroll; humanitylabs-org/wizard-ux#447 Recommended fix: Dictation inserts at the cursor instead of a copy card; humanitylabs-org/wizard-ux#458 Recommended fix: Store's My apps link opens in place instead of reloading Wizard; humanitylabs-org/wizard-ux#487 Recommended fix: Photos backup runs on Wi-Fi when the tailnet VPN is on; humanitylabs-org/wizard-ux#586 Recommended fix: Retry saved first messages after subscription sign-in recovers; humanitylabs-org/wizard-ux#587 Recommended fix: Show saved messages waiting for AI sign-in consistently; humanitylabs-org/wizard-ux#589 Recommended fix: Keep first-launch app discovery from recursively rebuilding the shell; humanitylabs-org/wizard-ux#605 Recommended fix: Share to Wizard picks New chat or one chat, quotes forwarded words, and s; humanitylabs-org/wizard-ux#635 Recommended fix: show times in the phone's own timezone; humanitylabs-org/wizard-ux#636 Recommended fix: every picked image rides an orb send made while it uploads; humanitylabs-org/wizard-ux#641 Recommended fix: Share to Wizard sheet looks like the rest of the OS; humanitylabs-org/wizard-ux#650 Recommended fix: Messenger recovers its own take after a reload and orphaned capture start; humanitylabs-org/wizard-ux#652 Recommended fix: tapping outside a sheet, menu or modal acts as Back across apps and shell; humanitylabs-org/wizard-ux#655 Recommended fix: keep Ready asks visible while helpers continue; humanitylabs-org/wizard-ux#658 Recommended fix: overlays with text are fully opaque in every skin; humanitylabs-org/wizard-ux#679 Recommended fix: status pills get their own colours in every skin; humanitylabs-org/wizard-ux#680 Recommended fix: the app kit follows its own text and touch-size rule; humanitylabs-org/wizard-ux#682 Recommended fix: apps open at once; setup check no longer holds the window; humanitylabs-org/wizard-ux#684 Recommended fix: show notifications when Ready has no chats; humanitylabs-org/wizard-ux#689 Recommended fix: keep saved-message retries out of the connection warning; humanitylabs-org/wizard-ux#690 Recommended fix: keep model picker above tall drafts and keyboard; humanitylabs-org/wizard-ux#691 Recommended fix: Setup and Settings use the shared app style contract; humanitylabs-org/wizard-ux#692 Recommended fix: keep agent replies on their own model and retain their threads; humanitylabs-org/wizard-ux#693 Recommended fix: Keep repeated Back presses inside Wizard at Home; humanitylabs-org/wizard-ux#694 Recommended fix: refresh the model picker on every open without repainting unchanged choic; humanitylabs-org/wizard-ux#696 Recommended fix: a running reply never shows its interim line twice; humanitylabs-org/wizard-ux#704 Recommended fix: Use current-route wording for voice health help; humanitylabs-org/wizard-ux#709 Recommended fix: a chat is in one Space; filed chats read Move to Space…; humanitylabs-org/wizard-ux#710 Recommended fix: Settings rows stay flat lines over the shared kit controls; humanitylabs-org/wizard-ux#713 Recommended fix: owners see Apps, never Wizard apps, on the agent page, Apps sheet and sha; humanitylabs-org/wizard-ux#715 Recommended fix: Screen chat QA fixture uses invented text, not a real team conversation; humanitylabs-org/wizard-ux#718 Recommended fix: live replies resume after a dropped connection; humanitylabs-org/wizard-ux#719 Recommended fix: Wizard opens offline and shows saved thread copies; humanitylabs-org/wizard-ux#720 Recommended fix: show Claude extra enabled when its cap is unavailable; humanitylabs-org/wizard-ux#721 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#723 Recommended fix: COMPONENTS.md back under its 14,000-byte budget (npm test red on main); humanitylabs-org/wizard-ux#725 Recommended fix: shared-app install links open in the Store again; humanitylabs-org/wizard-ux#726 Recommended fix: launcher shows long one-word app names whole; humanitylabs-org/wizard-ux#727 Recommended fix: one offline pop-up with three checks (Internet · Tailscale · Wizard); humanitylabs-org/wizard-ux#728 Recommended fix: Ready Priority ranks every status and shows its groups; humanitylabs-org/wizard-ux#730 Recommended fix: launcher long-name size from the type token, with a regression test; humanitylabs-org/wizard-ux#734 Recommended fix: working chats no longer flash into Ready while their history loads; humanitylabs-org/wizard-ux#736 Recommended fix: protect every standalone app below Android status bar; humanitylabs-org/wizard-agent-service#155 Recommended fix: owner CLI importer includes python-dotenv; humanitylabs-org/wizard-agent-service#183 Recommended fix: threads know MEDIA sends PDFs and other files up to 256 MB; humanitylabs-org/wizard-agent-service#197 Recommended fix: import older Hermes homes without shared-file or credential leaks; humanitylabs-org/wizard-agent-service#228 Recommended fix: let open owner asks stay Ready during background work; humanitylabs-org/wizard-agent-service#235 Recommended fix: chat pictures and files stay in the owner's browser, and edited files sti; humanitylabs-org/wizard-agent-service#241 Recommended fix: a sent file is a snapshot, so a later rewrite never changes earlier chat ; humanitylabs-org/wizard-agent-service#245 Recommended fix: pointer skills send the agent through the app's own skill, with end-to-en; humanitylabs-org/wizard-agent-service#250 Recommended fix: app workflow changes stay in released apps; humanitylabs-org/wizard-agent-service#254 Recommended fix: a chat is in one Space at a time; humanitylabs-org/wizard-agent-service#256 Recommended fix: Secret scan passes again (the round-10 synthetic placeholder is not a key; humanitylabs-org/wizard-agent-service#257 Recommended fix: the app-skill guard stops blocking reads and verification of installed sk; humanitylabs-org/wizard-agent-service#260 Recommended fix: scheduled notifications use the job's own agent and survive late admissio; humanitylabs-org/wizard-agent-service#263 Recommended fix: Markdown support is a fact, not a formatting push; humanitylabs-org/wizard-agent-service#264 Recommended fix: Secret scan passes again (shared-vault staging test's placeholder PEM is ; humanitylabs-org/wizard-agent-service#266 Recommended fix: a finished run leaves Working at once; humanitylabs-org/wizard-app-manager#321 Recommended fix: app updates fill newly required settings first (or ask the owner); humanitylabs-org/wizard-app-manager#323 Recommended fix: Connections works on desktop: the phone's list on every screen, two colum; humanitylabs-org/wizard-app-manager#333 Recommended fix: wam picks up the docker group when the user manager predates it. Stable stays 0.3.76 until the installed upgrade test passes.
Channel: on latest only.

## stable 0.3.81

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/326 (release train). `releases/stable` → 0.3.81; `promotions/0.3.81.json` sha256 `8b68d2b3fa684545d4147507d6293f0f750814f4aefccad6b135ddc887d515ba`.
Fast-tracked as **feature**. Installed upgrade 0.3.76 → 0.3.81 passed: wizard-os docs/acceptance/promote-0.3.81.md.

## 0.3.82 provenance

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/327 and #328 (notes). SHA256: `160895672760666f737975078f3959d1e9dcc5dae02fc07e99f08c743d305f49`.
0.3.81 plus Recommended fixes from a fresh-install rehearsal: App Manager #341, Wizard UX #743, Claude 0.2.11 / ChatGPT 0.2.9 (wizard-app-store #43).
Channel: latest; the release train promotes after the upgrade test.

## 0.3.83 provenance

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/330. SHA256: `b8f6b93739180b6eb817e43a1d9899c8df98370bd3fe7f05e07a8352606165b2`.
0.3.82 plus the Recommended fix App Manager #345 (a fresh install's agent gets the wizard gateway).
Channel: latest; the release train promotes after the upgrade test.

## 0.3.84 provenance

Canonical: wizard-os releases/0.3.84.json (release train). SHA256: `24122c3310acdf25b9f0172763cad39d5180f872d683d52883e2be41c7dd90b9`.
Latest 0.3.83 with Wizard UX 0a23c082 -> 53d209c5. Owner-approved same-day Stable (DECISIONS #93 feature fast-track): humanitylabs-org/wizard-ux#668 Home & Settings for everyone (owner-approved: Miguel, 29 Oct 2026, 'stable today'). Stable stays 0.3.83 until the installed upgrade test passes.
Channel: on latest only.

## stable 0.3.84

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/334. `releases/stable` → 0.3.84; `promotions/0.3.84.json` sha256 `48eece5b9a407d3cddb5a185913ea06e4714c8ca79bed111a364f6635328eabc`.
Fast-tracked as **feature** (owner-approved). Installed upgrade 0.3.83 → 0.3.84 passed: wizard-os docs/acceptance/promote-0.3.84.md.

## 0.3.85 provenance

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/333. SHA256: `73a059e3bcc34f15cc78333881228a437240bf3d3c217dd90eae00535417845b`.
0.3.84 plus the Recommended fix App Manager #349 (upgrading boxes keep their apps connected; new boxes start with apps kept apart). Evidence: wizard-os docs/acceptance/release-0.3.85.md (installed upgrade 0.3.83 -> 0.3.84 -> 0.3.85 on a disposable KVM guest).
Channel: on latest only. Stable stays 0.3.84 (promoted separately by the release train, wizard-os #334); 0.3.85 waits for James.

## 0.3.86 provenance

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/335. SHA256: `e47f3d9aac054be5b34cb648e18aa38ee7328dea10383e86660eb7d662404a7d`.
0.3.85 plus App Manager #350 (a fresh install's agent can use the Claude sign-in; new image v4.10.2-wizard.897-016a8cb) and wizard-app-store #44 (Claude sign-in is just a link and a code box: Claude 0.2.12, ChatGPT 0.2.10).
Channel: latest only; Stable waits for James on #349.

## stable 0.3.86

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/337. `releases/stable` → 0.3.86; `promotions/0.3.86.json` sha256 `1c97ddf50f773ceee10f6ce7596cfff4ac08a0660fd9679b41170488f44a0fa0`.
Fast-tracked as **feature** on the owner's decision (Miguel, 30 Oct 2026: "If you're shipping a fix, ship it straight to stable"), overruling the James hold on #349. Installed upgrade 0.3.84 → 0.3.86 passed: wizard-os docs/acceptance/promote-0.3.86.md.

## 0.3.87 provenance

Canonical: wizard-os releases/0.3.87.json (release train). SHA256: `c3d29d3e35081bd1db1a46d293afa6fea3322f00129cafc87b130ebd4ee11b8e`.
Latest 0.3.86 with Wizard UX 53d209c5 -> bc665b42, Agent Service 24f3cd0a -> 44248704. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#712 Recommended fix: the Mac window buttons no longer cover the desktop bar; humanitylabs-org/wizard-ux#745 Recommended fix: keep the mobile reload banner above Chats; humanitylabs-org/wizard-ux#749 fix(landscapes): raise light Chats preview contrast; humanitylabs-org/wizard-ux#750 Recommended fix: Welcome and Settings › Look say "color", not "colour"; humanitylabs-org/wizard-agent-service#272 Recommended fix: refuse history too new for the approved runtime. Stable stays 0.3.86 until the installed upgrade test passes.
Channel: on latest only.

## 0.3.88 provenance

Canonical: wizard-os releases/0.3.88.json (release train). SHA256: `0515ff7605f8d0599c90dac4b1297268a47a582891b14abaa4ed71c87ce1c178`.
Latest 0.3.87 with Wizard UX bc665b42 -> 7f7a9dd2, Agent Service 44248704 -> 1f78352d. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#712 Recommended fix: the Mac window buttons no longer cover the desktop bar; humanitylabs-org/wizard-ux#745 Recommended fix: keep the mobile reload banner above Chats; humanitylabs-org/wizard-ux#749 fix(landscapes): raise light Chats preview contrast; humanitylabs-org/wizard-ux#750 Recommended fix: Welcome and Settings › Look say "color", not "colour"; humanitylabs-org/wizard-ux#746 fix(chats): align accent controls with active palette; humanitylabs-org/wizard-ux#747 fix(signal): improve Chats list text contrast; humanitylabs-org/wizard-ux#748 fix(sepia): raise Chats status label contrast; humanitylabs-org/wizard-ux#752 Recommended fix: keep multiline drafts editable; humanitylabs-org/wizard-ux#753 Recommended fix: a closed desk no longer comes back from another device; humanitylabs-org/wizard-ux#754 Recommended fix: Escape clears desktop Chats search before navigation; humanitylabs-org/wizard-agent-service#272 Recommended fix: refuse history too new for the approved runtime; humanitylabs-org/wizard-agent-service#274 Recommended fix: a finished queued run leaves Working at once; humanitylabs-org/wizard-agent-service#275 Recommended fix: account changes can be on for one provider (WIZARD_ACCOUNT_CHANGES=chatgp. Stable stays 0.3.86 until the installed upgrade test passes.
Channel: on latest only.

## stable 0.3.88

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/340 (release train). `releases/stable` → 0.3.88; `promotions/0.3.88.json` sha256 `f0460fcb5fbf7d8fdb82296acca9f26f8438ccae59b4aa5e8afaf72700ccbfb8`.
Fast-tracked as **fix**. Installed upgrade 0.3.86 → 0.3.88 passed: wizard-os docs/acceptance/promote-0.3.88.md.

## 0.3.89 provenance

Canonical: wizard-os releases/0.3.89.json (humanitylabs-org/wizard-os#341). SHA256: `b52b309ea28a2d1a351312f25b86301a612d2ad7878d69259abfb1e545dd1a9c`.
0.3.88 plus App Manager #357 (a fresh link installs the wizard-os plugin so ChatGPT sign-in works; image unchanged, v4.10.2-wizard.897-016a8cb), ChatGPT 0.2.12 and Claude 0.2.14 (fresh-Wizard ChatGPT model, Opus default after Claude sign-in). Agent Service 1f78352d and Wizard UX 7f7a9dd2 unchanged from 0.3.88.
Channel: on latest; Stable stays 0.3.88 until the installed upgrade test passes (owner rule: fixes then ship straight to stable).

## stable 0.3.89

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/342. `releases/stable` → 0.3.89; `promotions/0.3.89.json` sha256 `aecad83472cd7ccacb57a016b8c050191d24888caa4e2d8fe48012fc2422a206`.
Fast-tracked as **fix** on the owner's rule (Miguel, 30 Oct 2026: "If you're shipping a fix, ship it straight to stable"). Installed upgrade 0.3.88 → 0.3.89 passed: wizard-os docs/acceptance/promote-0.3.89.md. Fresh install of 0.3.89 on a clean KVM guest passed (27 checks / 0 failed, wizard-os plugin installed and enabled): wizard-os docs/acceptance/fresh-0.3.89.md (humanitylabs-org/wizard-os#343).
Rollback: point `releases/stable` back to 0.3.88.

## 0.3.90 provenance

Canonical: wizard-os releases/0.3.90.json (humanitylabs-org/wizard-os#344). SHA256: `222679a91c7500a346c59bb7a4184897f1729d4d7a3699c3effe019e576fa3d2`.
0.3.89 plus App Manager #364 (apps starting, restarting or updating, or a restarting shell, keep their same-origin Wizard path; Messenger no longer opens blank) with #362/#363 (Store removes an installed app); image unchanged, v4.10.2-wizard.897-016a8cb. Everything else unchanged from 0.3.89.
Channel: on latest; Stable stays 0.3.89 until the installed upgrade test passes (owner rule: fixes then ship straight to stable).

## stable 0.3.90

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/347. `releases/stable` → 0.3.90; `promotions/0.3.90.json` sha256 `b35c72fcf2fa1488d134cd35f0a5106bda829d581b6fc34add462b763a66145f`.
Fast-tracked as **fix** on Miguel’s rule (30 Oct 2026: “If you’re shipping a fix, ship it straight to stable”). Installed upgrade 0.3.89 → 0.3.90 passed: wizard-os docs/acceptance/promote-0.3.90.md (#346), 27 checks / 0 failed. Latest remains 0.3.90 on the mirror until the subsequent 0.3.91 mirror is published.
Rollback: point `releases/stable` back to 0.3.89.

## 0.3.91 provenance

Canonical: wizard-os releases/0.3.91.json (humanitylabs-org/wizard-os#345, release train). SHA256: `4c0a1b8e8cfe64485745c172dbc42808849d2c2ba357b4d4310451797709e8bb`.
0.3.90 with Wizard UX 7f7a9dd2 -> 8b4a5b80 (wizard-ux#757 Store can remove an installed app; #758 model picker stays clear of draft text).
Channel: on latest only (superseded by 0.3.92). Supersedes the conflicting mirror PR #366.

## 0.3.92 provenance

Canonical: wizard-os releases/0.3.92.json (humanitylabs-org/wizard-os#348). SHA256: `c267ab1812f52563537511769f2fe4af7bcec7296ed99869e124d0f18da79a26`.
0.3.91 with App Manager 29c4b831 -> 23a71b5d (#368: an app behind an owner gateway, e.g. Messenger, keeps its Wizard path; a stale Serve read is re-checked). Image unchanged, v4.10.2-wizard.897-016a8cb.
Channel: on latest; Stable stays 0.3.90 until the installed upgrade test passes (owner rule: fixes then ship straight to stable).

## 0.3.93 provenance

Canonical: wizard-os releases/0.3.93.json (humanitylabs-org/wizard-os#349). SHA256: `76766fd438c248d5316072350777636eaeaae3f8fc699711401d6593c27d3ce2`.
0.3.92 plus Wizard UX #755 (every skin in one list in Settings and onboarding; tweaks one list, off by default, labelled by author, quick button per author; Notifications in Chats and Machines on Home are ordinary settings); image sha-96f111f0. Everything else unchanged from 0.3.92.
Channel: on latest; Stable stays 0.3.90 until the installed upgrade test passes.

## 0.3.94 provenance

Canonical: wizard-os releases/0.3.94.json (release train). SHA256: `40530065c763b7f5cde063a28292c2d8220237aaba940df8d0d49afe45b2d2ec`.
Latest 0.3.93 with Wizard UX 96f111f0 -> 7991db03, Agent Service 1f78352d -> 9233c199. Cut by the release train for merged Recommended fixes: humanitylabs-org/wizard-ux#757 Recommended fix: Store can remove an installed app; humanitylabs-org/wizard-ux#758 Recommended fix: model picker stays clear of draft text; humanitylabs-org/wizard-ux#761 Recommended fix: the Welcome never switches skin on its own; picking sets only the skin; humanitylabs-org/wizard-ux#762 Recommended fix: model switch recovers when ChatGPT models briefly drop out; humanitylabs-org/wizard-ux#763 Recommended fix: a greyed model or stopped AI reading opens its app at Reconnect; humanitylabs-org/wizard-ux#764 Recommended fix: title-bar tools get search, QR and settings icons and an attention dot; humanitylabs-org/wizard-agent-service#281 Recommended fix: show and change which subscription the agent uses first; humanitylabs-org/wizard-agent-service#282 Recommended fix: an idle subscription's expired sign-in renews instead of blanking its usa; humanitylabs-org/wizard-agent-service#283 Recommended fix: Use first is not offered where stock Hermes cannot apply it; humanitylabs-org/wizard-app-manager#368 Recommended fix: keep an app behind an owner gateway on its Wizard path. Stable stays 0.3.90 until the installed upgrade test passes.
Channel: on latest only.

## stable 0.3.94

Canonical: https://github.com/humanitylabs-org/wizard-os/pull/351 (release train). `releases/stable` → 0.3.94; `promotions/0.3.94.json` sha256 `f3a3d17caa09dfd5371dbc53527b238fade6ef60858e50b99d0e1287d6bf56fe`.
Fast-tracked as **feature**. Installed upgrade 0.3.90 → 0.3.94 passed: wizard-os docs/acceptance/promote-0.3.94.md.
