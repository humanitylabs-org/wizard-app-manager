# Slice 6 light checks

Executed locally against this implementation:

```
python -m pytest -q \
  tests/test_privilege_migration.py tests/test_privsep_slice6_runner.py \
  tests/test_privsep_alongside.py tests/test_privsep_host_policy.py \
  tests/test_privsep_installed.py tests/test_privsep_slice4.py \
  tests/test_privsep_slice5.py tests/test_privsep_slice5_runner.py
```

Result: **196 passed in 21.60s**. This includes real archive-copy filesystem
fixtures, a partially interrupted rollback restoring original data/token/unit
bytes, per-migration-step journal interruptions and resume, doctor-failure
ordering, container-owner preservation, privacy sentinels, regenerated auxiliary
unit syntax, extra-agent Service mount wiring, and a mocked-QEMU runner contract
that executes the real runner function through its guaranteed stop path.

`bash -n scripts/prepare-named-user.sh`, Python compilation, and `git diff --check`
also passed. The inherited separated Service fallback used a one-digit disabled
STT endpoint but rejected one-digit ports; the new Service wiring test exposed
that mismatch, and the fallback now passes bounded loopback-port validation.

These are NOT VM receipts. The clean legacy installation, migration, grant,
rollback, remigration and full pytest suite remain for the staged forum job.
No live deployment or live state change was performed. Stock Hermes was not
modified. The James-box inspection was limited to metadata and found explicit
preflight blockers documented separately.
