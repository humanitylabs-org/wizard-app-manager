
## Clean-VM acceptance at d3a04248 (forum KVM, 28 Oct 2026): PASS

Evidence: forum `/srv/work/privsep/slice5/20261028T011709Z-acceptance` (receipt.json). Unprivileged `hermes` on clean Debian 12:
before grant `sudo -n true` refused and the helper refuses any extra argument; owner turns developer mode on and grants
access, `hermes` gets sudo (logged) while Runtipi and the API stay `User=wizard-system`; agent token cannot grant
itself; after the owner-chosen short expiry sudo is refused again and status is inactive; `developer off` revokes.
Doctor passed. Full pytest 1633, only the pre-existing `test_chats_views` seal failure. VM stopped.
