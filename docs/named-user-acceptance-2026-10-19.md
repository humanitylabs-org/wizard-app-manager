# Named-user acceptance — 2026-10-19

## Safety and infrastructure

- Existing root demo is not reset, migrated, reinstalled, or logged out.
- Initial host: Ubuntu 24.04, 2 vCPU, 7940 MiB RAM, 5699 MiB available, 84 GiB free disk. No /dev/kvm or nested virtualization CPU flag.
- Disposable QEMU TCG Ubuntu 24.04 guest: 2 emulated CPU, 3072 MiB RAM, sparse 32 GiB disk. Initial 120%/3500 MiB cap hit translation-cache pressure; recreated only this disposable guest process with `tb-size=64`, 150% CPU, 3500 MiB RAM cap, nice 10. Current tracked unit: `wizard-named-guest-v2.service`. Root demo remained healthy.
- User-mode NAT only; SSH forwarded on host **loopback** 127.0.0.1:22292. No bridge, host Tailscale change, or host Docker socket mount.
- Dedicated guest SSH key generated on demo, never copied from Miguel. No provider credentials copied.
- Guest `acceptance` user has password-required sudo, locked password, and no NOPASSWD rule. Root guest access is the explicitly approved administrator equivalent, not sudo bypass on the named account.
- Host artifacts: `/root/wizard-named-acceptance/`; current tracked guest: `wizard-named-guest-v2.service` (initial guest unit stopped, disk retained).
- Hash-only baseline records root Hermes config, auth and .env and host Tailscale state; no transcript reads.

## Verified milestones

- Real public preflight as UID 1000 `acceptance` exited 0, selected `/home/acceptance/.hermes` and alongside mode, with Docker absent.
- Original public administrator attempt exited 2 solely because named owner lacked passwordless sudo. The named account's `sudo -n true` exited 1 before and after the new approval barrier. No sudoers grants were added.
- Fixed public `start` as the named user exits 2 before starting installation and prints one exact administrator command retaining `WIZARD_HERMES_USER=acceptance`, `HERMES_HOME=/home/acceptance/.hermes`, agent-safe mode and detached start.
- Owner files, memory and skill sentinels created under named UID; customized settings via supported Hermes CLI (`agent.max_turns=7`, `display.language=English`). Hash inventory: guest `/home/acceptance/acceptance-baseline.json`.
- Actual Hermes Python CLI executed one complete conversation through a deterministic local responder, returning `LOCAL_ROUTING_FIXTURE_OK — deterministic responder, not AI inference.` Session `20261019_064837_85f957`. The responder logs only path/model/stream metadata, not prompts. No real model inference or provider sign-in is claimed.
- Hermes prerequisite setup used official v0.22.2 source, installer stages and public PM APIs. Missing `libatomic1` was installed by the approved guest administrator. An interrupted npm entry prevented optional frontend products from building; Python CLI works, but this is **not TUI acceptance**. No Hermes source patch or copied credentials.
- WAM #70 merged as `2534a461186a61d41cb8ece8364d1c54e1f15939`; 385 unit/fixture tests pass, four new regressions fail on old main. Canonical wizard-os #61 and WAM mirror #71 merged 0.3.4. Online release validator passed; immutable official image/app pins retained from 0.3.3. Manifest SHA256 `7d9ab6c07ca0bbe70b223252fb529d58aede73f0b0b4f69b69bc914e5eb9e049`.
- Public os.sh equals merged main get.sh: SHA256 `c4168585d2d712eef5117eccf6efe015a7596db7bfac067a0fc2efc10f67be56`.
- Official public strict 0.3.4 detached administrator install started in the disposable guest, preserving selected named owner/home. Test bounds Tailscale waiting to 1 second: owner browser enrollment is intentionally not performed or simulated.

## Evidence still pending

Installed official recipes/RepoDigests, actual unprivileged progress projection, user bus/Docker readback, named-owner preservation after linking, live Hermes HTTP compact sessions and local-fixture chat, numeric Resources, rerun and unlink remain pending. A started unit is not installation acceptance. No private Wizard URL exists until real guest Tailscale enrollment by the owner; never reuse or log out the working root demo identity.
