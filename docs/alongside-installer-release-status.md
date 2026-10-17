# Alongside installer: source changes are not a new release

The public entry point is `https://aiwizards.com/os.sh` (`get.sh` from main).
Read-only customer preflight:

```sh
curl -fsSL https://aiwizards.com/os.sh | WIZARD_CHECK=1 bash
```

The default install still follows `releases/stable`, currently **0.2.2**. It does
not automatically install the latest main checkout. Its component source pins are:

- App Manager: `e39576a0e28e95b5eef996454a269288ba37e8c2`
- Agent Service: `eac2ea0fe8f81b2b1f8a1eb0217b7d3cfcd77764`
- UX: `e5399b258eaea8c25f4ed890f7e788dafebb3a6b`

These are manifest expectations, not a claim about an existing customer's running
containers. The installer records actual source/image readback in
`~/.wizard-os/release.json`; read that receipt and the container digests before
claiming an exact installation.

PR57's public entry point adds owner detection, explicit selection, fail-closed
handling of known but unverified installs, check mode, and a compatibility shim
for older installers' agent-initiated gateway restart. Multiple detected homes
require an explicit selector, including alongside reruns. An explicit owner/home
must resolve; existing alongside records cannot override it. Existing users are
not automatically granted passwordless sudo. The selected binary's directory is
put on PATH for older pinned components that do not support WIZARD_HERMES_BIN.

The new install.sh/CLI changes are available in source, but **not included in the
0.2.2 source pin**. Both old compatibility and new installer paths defer automatic
restart scheduling until the outer installer succeeds and writes a successful
release-verification result. Failed installs/verifications do not schedule one.
A direct install.sh invocation has no outer release verifier.

## Remaining release gate

Do not relabel 0.2.2, silently replace its source commit, or call a WAM_REF developer
override an exact release. Coordinate the pin-bootstrap work in
https://github.com/humanitylabs-org/wizard-app-manager/pull/56 before publishing a
new immutable manifest that includes these installer changes. That draft requires
real disposable fresh and alongside guest acceptance, including installed recipe
commits, actual image RepoDigests, Hermes preservation/unlink and the detached
restart surviving a real chat-driven installation. Root-owned and custom-home
installations need that end-to-end evidence too. Sandbox tests are not VM evidence.

Unknown layouts, inaccessible homes and unsupported service-unit syntax are not a
universal detection guarantee. Run preflight as the Hermes owner or administrator,
confirm the exact home, and stop rather than accept an unexpected fresh plan.
No production Hermes restart or live box update is required to review this PR.
