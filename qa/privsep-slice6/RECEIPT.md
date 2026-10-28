# Slice 6 rehearsal: legacy box migration, rollback, remigration

## Clean-VM rehearsal at ec4ae3b7 (forum KVM, 28 Oct 2026): PASS

Evidence: forum `/srv/work/privsep/slice6/20261028T064858Z-ec4ae3b7` (receipt.json). Legacy box built from main 67141228
(user-bus workaround only in setup), apps installed with data canaries and a grandfathered host mount. Migration moved
it to the separated layout; all previously running apps came back running with data intact; the agent used the thin
client, had no Docker/sudo beyond the logged one-time migration grant; doctor passed. Rollback restored the legacy
layout; migrating again succeeded. Full pytest 1721, only the pre-existing `test_chats_views` seal failure. VM stopped.
Not proven here: authenticated tailnet link resolution (needs an owner-controlled rehearsal).
