# Named-user acceptance — 2026-10-19

## Safety and infrastructure

- Existing root demo is not reset, migrated, reinstalled, or logged out.
- Initial host: Ubuntu 24.04, 2 vCPU, 7940 MiB RAM, 5699 MiB available, 84 GiB free disk. No /dev/kvm or nested virtualization CPU flag.
- Disposable QEMU TCG Ubuntu 24.04 guest: 2 emulated CPU, 3072 MiB RAM, sparse 32 GiB disk; 120% CPU quota, 3500 MiB memory cap, nice 10.
- User-mode NAT only; SSH forwarded on host **loopback** 127.0.0.1:22292. No bridge, host Tailscale change, or host Docker socket mount.
- Dedicated guest SSH key generated on demo, never copied from Miguel. No provider credentials copied.
- Guest `acceptance` user has password-required sudo, locked password, and no NOPASSWD rule. Root guest access is the explicitly approved administrator equivalent, not sudo bypass on the named account.
- Host artifacts: `/root/wizard-named-acceptance/`; tracked guest: `wizard-named-guest.service`.
- Hash-only baseline records root Hermes config, auth and .env and host Tailscale state; no transcript reads.

## Evidence still pending

Actual user preflight, sudo barrier, approved administrator installation, official stable recipe startup, owner preservation, API routing, resources, rerun and unlink remain pending. VM creation is not installation acceptance.
