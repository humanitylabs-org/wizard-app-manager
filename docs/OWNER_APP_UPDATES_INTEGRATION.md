# Owner updater on the actual installed host

Base: `543431650de49764833126dc6edc2e8a24c32fc1`, independently identified from
`/proc/<MainPID>/cwd`, module argv and absent PYTHONPATH; production files hash-checked.

REUSE: reviewed app_updates/source_integrity, source packaging and timer linkage.
EXTEND: existing StoreHandler owner gate, CLI parser, update queue and release source holds.
NEW: update-only `update_policy` adapter with the pure reviewed host-policy/network
allowlist closure. There is no approval authority on this older installed host;
unknown or protected recipes require owner review and are refused without writing
an approval or admitting an update. No agent privilege, network mutation, approval
migration, final-token machinery or unrelated newer-main dependency is imported.

The existing owner transport remains authoritative: local root-peer proof,
Tailscale owner identity and POST origin/header gate are unchanged. This is not the
newer `owner.check` API. The adapter never substitutes for the backend final guard.

One source runs both host API and CLI. Existing wrapper environment/defaults remain
intact; deployment changes its `cd` source path only. No dual-runtime dispatcher.
Legacy command parser and unrelated function bodies are retained. Intentional
restrictions: raw `app update` cannot bypass source/recipe checks, explicit updates
preserve owner source edits, and automatic admission serializes against Manual.

Required CI remains required. A frozen-base preview PR is not main publication.
The obsolete r5 source-mismatch proof is retained, not relabelled successful.
Lifecycle `2378bb26` and shell `e01df78a` are unchanged; prior exact-source evidence
is reused only by subtree hashes. No live activation is authorized by this branch.
