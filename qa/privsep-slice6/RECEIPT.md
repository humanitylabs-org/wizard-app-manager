# Slice 6 rehearsal: legacy box migration, rollback, remigration

## Historical clean-VM rehearsal at ec4ae3b7 (forum KVM, 28 Oct 2026): PASS

**Superseded as acceptance of the repaired security candidate.** This run predates
credential invalidation and the removal of automatic migration sudo. Its unconditional
rollback after completed migration is no longer valid: snapshot rollback is allowed
only before credential password-change intent; afterwards recovery is forward-only.
See `PRIVSEP-HANDOFF.md` for the verified credential milestone and the staged, not-run
`qa/privsep-compat/` replacement. Preserve this historical evidence; do not relabel it green at a newer SHA.

Evidence: forum `/srv/work/privsep/slice6/20261028T064858Z-ec4ae3b7` (receipt.json). Legacy box built from main 67141228
(user-bus workaround only in setup), apps installed with data canaries and a grandfathered host mount. Migration moved
it to the separated layout; all previously running apps came back running with data intact; the agent used the thin
client, had no Docker/sudo beyond the logged one-time migration grant; doctor passed. Rollback restored the legacy
layout; migrating again succeeded. Full pytest 1721, only the pre-existing `test_chats_views` seal failure. VM stopped.
Not proven here: authenticated tailnet link resolution (needs an owner-controlled rehearsal).
