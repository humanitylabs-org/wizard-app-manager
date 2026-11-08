Based on the generic `wizard` CLI from humanitylabs-org/wizard-os `tools/` (upstream base recorded in SOURCE.json). WAM owns local stock-builder provisioning, discovery, safe plugin refresh and unlink changes. SOURCE.json keeps separate upstream and shipped hashes; retain the local changes when syncing upstream. `install.sh` installs this as `~/.local/bin/wizard`.

## Headless first contact and offline support

`wizard contact` probes the installed shell's loopback front door and its same-origin
`/wizard/v1/setup` API, then prints an SSH forward and browser URL. HTML alone is
not readiness: 0.3.32 pins an Agent Service without Setup, so contact reports
`contact_setup_unavailable` until the box is updated to a release with Setup support
(0.3.33 or later). The shell port already proxies the API; forwarding the standalone
Agent Service port would lose the shell. Run the SSH command **on your computer**,
then open the URL there.
Use `--ssh-host HOST --ssh-port PORT` for the address you connected with (required
when sudo strips SSH context; NAT discovery is only a hint). It retains normal SSH
host-key verification and opens no LAN listener. After connecting, use Setup for
phone access. A phone-only first-contact route is not claimed.

`wizard report > wizard-report.json` creates an offline, shareable JSON diagnostics
bundle. Its closed schema is `report.schema.json`. It projects version/digest
identities, fixed service states, update policy and bounded error-class counts;
it never includes raw journals, exception messages, URLs, credentials, app content,
environment or host/user names, and never uploads. A missing probe is `unavailable`.
Expected/observed pins are the installer's recorded readback, not a new Docker
inspect: the report intentionally does not query container configuration or auth
stores. Review before sharing. Tests: `python3 -m unittest tests/test_launch_audit_cli.py`.

## Gateway token repair (feature switch, off by default)

With the feature switch `wizard/link-token-repair` on (Settings, Try new features, For testers), a repeat
`wizard link` on an already-linked box puts this box's gateway token (`$WIZARD_OS_HOME/mcp-token`) back into the
Hermes `.env` when `WIZARD_MCP_TOKEN` is missing or stale, for example after a setup step rewrote the file. That
mismatch is what makes `hermes mcp test wizard` return `401 unauthorized` (wizard-ux#974). Only that one line
changes; other keys stay, `unlink` still removes only what link added, and it never mints a new token. `wizard
doctor` then also shows "gateway token matches this box", comparing short sha256 prefixes and never the values.
Restart Hermes afterwards (`wizard link --restart`). Tests: `tests/test_link_token_repair.py`.
