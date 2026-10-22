Based on the generic `wizard` CLI from humanitylabs-org/wizard-os `tools/` (upstream base recorded in SOURCE.json). WAM owns local stock-builder provisioning, discovery, safe plugin refresh and unlink changes. SOURCE.json keeps separate upstream and shipped hashes; retain the local changes when syncing upstream. `install.sh` installs this as `~/.local/bin/wizard`.

## Headless first contact and offline support

`wizard contact` probes the installed shell's loopback view and prints an SSH forward
and browser URL. Run the SSH command **on your computer**, then open the URL there.
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
