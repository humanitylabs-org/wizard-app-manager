# Owner edge (draft, not installed by default)

Both switches in `app-manager.json` default off (absence also means off):

- `owner_proof_enabled`: gates provisioning, edge serve, registry metadata and routing. **Do not enable until spec
  follow-up 2 (trusted stable-ID owner enrollment) is complete.**
- `owner_host_access_grants`: gates enhanced risk detection and grant enforcement in install/update/network allow.
  **Do not enable until spec follow-ups 2, 5, 6, 7, 8, 9 and 10 are complete.**

Off means main-compatible behaviour, not deny-all: the existing `--allow-host-access` path, stored allowances,
network policy and recipe mount handling remain unchanged, including Connection Router. The listener refuses
`grants_off`, creates no key, and no edge/provisioning/lifecycle/migration is activated by merging this code.

The following commands/routing description applies only after the corresponding switch is enabled.

Spec: wizard-os `spec/OWNER_PROOF.md` (Decision #106, proposed). Code: `wam/owner_edge.py`, `wam/owner_proof.py`.

Apps that declare `owner_proof` in `wizard.md` get passphrase-free owner access: the edge authenticates the owner
(tailscaled peer credentials, the owner's tailnet login, one of the owner's devices rather than the box itself) and
signs each request with the app's own key. `wam links` routes the app's Wizard path through the edge, and never to the
raw port while the edge is down.

## Commands

```
wam owner-proof status [--json]                 # declared apps, keys present, edge socket live, last owner activity
wam owner-proof provision <app> [--dry-run]     # mint keys, copy them for the app's container user, write the
                                                # x-wizard-owner-proof block of the user-config override
     [--adopt-proof FILE] [--adopt-wrap FILE]   # migration: keep existing keys (the remembered unlock keeps working)
wam owner-edge serve                            # the edge itself (run by the unit below)
```

`provision` restarts nothing: the app reads its keys on its next start (restart it through the usual gated path).

## User unit

Not installed by `install.sh` yet. On a box that opts in:

```
# ~/.config/systemd/user/wizard-owner-edge.service
[Unit]
Description=Wizard owner edge (signs owner requests for apps that declare owner_proof)
After=network-online.target

[Service]
WorkingDirectory=%h/wizard-app-manager
ExecStart=/usr/bin/env python3 -m wam owner-edge serve
UMask=0077
NoNewPrivileges=yes
Restart=on-failure

[Install]
WantedBy=default.target
```

Serve must be able to add `unix:` handlers (root, or the tailnet operator); `wam links` already retries
`tailscale serve` with `sudo -n`.

## Optional settings

`~/.wizard-os/owner-edge.json`:

```json
{"owner_logins": ["owner@example.com"], "allow_requests_from_this_box": false}
```

Without `owner_logins` the owner is the tailnet user who owns this node (a tagged node then has no owner and every
request is refused). The host-user-writable `allow_requests_from_this_box` option skips both local-box refusal and
device enrollment for local callers; every program on the box can act as owner. It defaults false and is unsupported
for rollout: follow-up 2 must remove/reject it and protect config custody before enabling the owner-device boundary.
The edge reads its switch only at startup; changing the file does not stop a running service. No service changes
are authorized or performed by this PR.
