# Existing-Hermes alongside foundation — 15 October 2026

This install branch uses the current merged Wizard App Manager source and already-published Wizard UX / Agent Service images. Its only runtime delta is selecting those three image digests in alongside mode. App-builder and app-installation feature branches are independent work; this handoff does not merge them or wait for them.

## Install next to your existing Hermes

Run from the normal sudo-capable Linux account that owns the running Hermes, with its usual HOME and HERMES_HOME. This route is for Ubuntu/Debian with user systemd available. A root-owned Hermes needs the existing root/user installation route checked first; do not run this command under a different account and assume it links the right agent.

```bash
curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/integration/alongside-foundation-20261015/get.sh -o /tmp/wizard-foundation-get.sh &&
env WIZARD_MODE=alongside WAM_REF=integration/alongside-foundation-20261015 bash /tmp/wizard-foundation-get.sh
```

The existing installer installs App Manager, selects the separate shell and Agent Service, and links the existing Hermes. Its normal supporting default apps still install. Hermes stays in place; `wizard link --restart` makes the reversible integration/configuration changes and restarts its gateway. Run while it is idle. Existing Tailscale settings are preserved; complete its sign-in prompt if this machine has not joined your tailnet.

This is a first alongside installation route. If this box already runs the older combined `wizard-shell`, use its existing `wam shell-split` procedure rather than installing a second backend beside it. On an existing split installation, `wam alongside` records new pins, but already-installed containers need `wam app update wizard-agent-service` and `wam app update wizard-ux` to adopt them.

## Exact foundation

- App Manager CLI/installer base: `2747df73fbdc062e3118f35786b73f62928b31c9` plus the image-selection-only change in this branch.
- App Manager container: `ghcr.io/humanitylabs-org/wizard-app-manager@sha256:8afef790d701d8bdd6728187f943c5d4de50cd09da5dc29f245f636af32c1117` (published `latest` resolved at preparation time; this image does not carry an OCI source-revision label).
- Wizard UX: source `263d7fb659ceaa1f790182dc50d07dd9986e6529`, image `ghcr.io/humanitylabs-org/wizard-ux@sha256:f662feab8a9eab66340a7c9426e4279ee1ee764615fd2ce5cfbdd048a4ee1bf0`.
- Agent Service: source `b4fa722d34ac70cb8a5d59658f0c151def3a17cc`, image `ghcr.io/humanitylabs-org/wizard-agent-service@sha256:6d3eae8ca20b41b1973fbd027efdc281aa59abea6deba96091e0a0243dd04f48`.

All three image manifests were read anonymously and include amd64 and arm64. UX and Service image revision labels match their source commits. Their exact-source publish / anonymous-pull workflows passed:

- https://github.com/humanitylabs-org/wizard-ux/actions/runs/38736086050
- https://github.com/humanitylabs-org/wizard-agent-service/actions/runs/38711023014

## Check the installed foundation

```bash
~/.local/bin/wam slots
~/.local/bin/wizard doctor
~/.local/bin/wam apps --json
```

The agent slot should be the external host Hermes; shell and Agent Service should be running. The actual loopback ports are in `~/.wizard-os/alongside.json`; default UX is `http://127.0.0.1:8796/wizard-os-home/`. Access this through the machine's existing private proxy, or forward that port over SSH from your laptop. Tailscale installation alone does not publish the loopback web port.

Final recipient acceptance: open Wizard, send a typed message, and receive a reply from the existing Hermes. This handoff verifies source/image selection; it does not claim that acceptance has already passed on the recipient's VPS.
