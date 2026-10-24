# Mac launch source reconciliation

Trunk intent: retain approved main behavior, privacy boundaries and current contracts.
Mac/live intent: retain protected Mac recovery, app lifecycle, maintenance and live owner UX.

- `wam/alongside.py` hunk 0: disjoint-intent — Keep alongside setup mode and split-shell prefix.
- `wam/backup/restore.py` hunk 0: disjoint-intent — Keep incomplete-restore guard and managed-operation decorator.
- `wam/registry.py` hunk 0: disjoint-intent — Keep validated _entry projection; port trunk voice names into shared _entry separately.

No live deployment, image publish, release or guest rebuild is authorized by this source reconciliation.
