# Factory install completion and optional connection recovery

REUSE: existing Runtipi app-event queue, command consumer, status manager, share-grant validation, and CLI bootstrap. No shell/UI component or Hermes core changes.
EXTEND: command-owned install completion and host-wide lifecycle serialization; narrowly degrade Hermes' stock optional Claude login share.
NEW: regression tests only; no new production subsystem.

## Evidence boundary

The c4bd221 factory trial logged Claude install/down at 20:56:30, a missing installed row and browser allocation of the same subnet by 20:56:42, then the same failure on retry. Hermes' subsequent start explicitly failed with `Granted share unavailable: wizard-claude/claude-login`. The older trial on the same WAM commit passed.

This does **not** establish a 15-minute CLI timeout as the original cause: the failure appeared in about twelve seconds. The old install handler swallowed the worker error when deleting its row. The next trial must establish the actual pre-up failure from the newly retained runtime error. Registry denials for other apps are not assigned as Claude's cause.

## Changes and invariants

- CLI install waits for an observed terminal result, without a short wall-clock timeout; transport errors are unknown, not missing. The outer factory job remains bounded. `--no-wait` stays explicit.
- Only the command consumer, after command completion, records install success or logs failure and removes the row. A failed/expired RPC reply alone cannot delete a live install.
- App-event workers and the command mutex serialize full lifecycle operations across apps, including allocation, down/up, and completion. Duplicate install requests while installing are refused.
- Status sync cannot invent completion for an old `installing` row from absent containers. A truly interrupted operation remains pending rather than becoming falsely successful: investigate/recover the manager before retrying; do not remove an active row based on elapsed time.
- Only `hermes-agent`'s `wizard-claude/claude-login` mount at `/claude` degrades when unavailable. It warns, strips previously injected mounts, leaves consent intact, creates no credential fallback, and restores the mount on a later restart after provider repair. Other missing shares and invalid present providers fail closed.

## Regression proof

`tests/test_install_completion.py`, strengthened `test_install_stops_waiting_when_the_app_manager_drops_a_failed_install`, `install-completion.test.ts`, and `wizard-storage.test.ts` cover slow completion, observation errors, RPC expiry, duplicate admission, old status sync, cross-app serialization, actual failure logging/removal, optional login recovery, and required-share refusal.

Tests run on Thales. A new factory diagnostic is still required before promotion. No merge, tag, feed mutation, publication, release guest rebuild, or Hermes core modification is authorized by this change.
