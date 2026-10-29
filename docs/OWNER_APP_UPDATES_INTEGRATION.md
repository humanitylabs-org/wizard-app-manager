# Live-based owner update integration

REUSE: deployed host lifecycle, final recipe approvals, protected owner routes, existing update API and real-shell QA harness.
EXTEND: independent app-update policy, read-only status, per-app Manual admission, source integrity and the existing Settings update group.
NEW: no parallel lifecycle, policy store or skin system.

This is synthetic staging only, not deployment or owner acceptance. No inference, timer activation, owner setting write or service restart is authorized by this integration.

The current host API, pinned CLI, serving shell and lifecycle container have different bases. Keep them distinct; never swap in upstream main. Host API candidate retains all protected policy/owner modules, the existing final recipe approval call and owner-gated Store listener. It adds no bearer-token mutation route. Recipe-approval refusal is an owner-needed result, never a failed update or rollback.

The lifecycle container needs precisely the source-copy inventory/preservation and same-commit verification extension: its deployed source lacks both. The update request/backup API does not change. Do not rebuild any other service.

The pinned CLI is older than the protected serving API. Its runtime pin must not be silently fast-forwarded to the API tree: that imports unrelated changes. `tools/owner-app-updates-cli-dispatch.py` dispatches only `app-updates` to the new API runtime and preserves the pinned legacy runtime for every existing command. The parent supplies the explicit legacy/source/interpreter configuration after acceptance; the adapter only reads it. The synthetic black-box test exercises both selections without writing either source or configuration. No live wrapper or runtime pins are changed here.

The original repair-only review fixed three findings. Its additional legacy-Manual affordance finding is addressed by offering explicit one-shot verification for source-unverified candidates; actual verification still runs before any update and unknown/changed sources remain untouched.

Acceptance is exact-source focused Python/Node regressions, two-skin phone/desktop real-shell captures with intercepted synthetic data, synthetic host adapter tests, and source-preservation backend tests. Old broad backend evidence is not reused for a different deployed baseline. Browser fixture evidence is not a real owner install or physical-phone proof. Final promotion belongs to the parent, after fresh evidence and app parity.
