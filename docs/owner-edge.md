# Owner edge (draft, not installed by default)

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
request is refused). `allow_requests_from_this_box` exists only for a box whose owner browses on the box itself; it
lets every program on the box act as the owner and stays off by default.
