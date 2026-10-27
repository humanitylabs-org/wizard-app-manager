# Slice 3 baseline handoff repair — debug evidence, not full acceptance

No merge, live deployment or heavy-job submission. PR remains draft / `do-not-merge`.

## Exact baseline failure

Main: `67141228f6f73516ab68ece1c4c72bf3f67aebe0`.
Original evidence: `forum:/srv/work/privsep/slice3/20261027T200005Z-staging/evidence/`.
`baseline-install.log` ended after successful root host preparation with another
administrator-approval request (exit 2). It never reached the ordinary install.

A short KVM debug guest used a child overlay of that stopped baseline disk; the
original evidence disk was not changed. Evidence:
`forum:/srv/work/privsep/slice3/20261027T211442Z-baseline-debug/`.
The actual `sudo -iu hermes` session reported:

| Main `named_host_ready()` prerequisite | Observed |
| --- | --- |
| `~/runtipi/.env` | present |
| `/run/user/1000/bus` | socket present |
| `docker info` | exit 0 |
| `sg docker -c 'docker info'` | exit 0 |
| `tailscaled` | active |
| `tailscale debug prefs` operator matches `$USER` | **false: `OperatorUser=None`, `USER=hermes`** |
| `tailscale status --json` | `BackendState=NeedsLogin` |

The failing condition is specifically main's required Tailscale operator check,
not missing Docker membership, user-bus startup, or the Runtipi environment.
Host prep's successful `tailscale set --operator=hermes` did not make the clean,
unsigned-in daemon report that operator. No tailnet credentials were supplied.

## Runner repair and explicit comparison limit

- Baseline bootstrap now explicitly selects documented `WIZARD_PRIVATE_ACCESS=none`
  (README, private-access alternatives). This is a real supported owner choice,
  not `WIZARD_APPROVED`, a patched main installer or a fabricated successful baseline.
- Candidate explicitly keeps `WIZARD_PRIVATE_ACCESS=tailscale`, so existing operator
  provisioning and real agent Tailscale-mutation denial remain acceptance checks.
  Using `none` on both would remove the daemon/operator under test from candidate.
- All other bootstrap inputs remain identical except source SHA: mode, account,
  Hermes home and wait setting. Both guests use the same pinned stock Hermes,
  clean Debian image and registry mirror. Private-access provisioning is therefore
  **not** an equal-input comparison; authenticated private links remain unproven.
- `receipt.json` now includes `install_private_access` and
  `install_input_difference`; both exact shell commands remain captured.
- A failed download cannot run a stale `/root/get.sh`: download and execution are
  joined with `&&`. Registry retries and baseline failure handling are unchanged.
  A failed baseline is still a failure; it is not silently waived.

REUSE: the documented private-access option, existing installer/VM/retry/doctor checks.
EXTEND: bootstrap command selection and machine-readable evidence of input differences.
NEW: focused regression tests only; no production code or UI change.

## Verified

- In the debug guest, re-ran the **unchanged exact main** root alongside installer
  with `WIZARD_PRIVATE_ACCESS=none`: exit **0**. Installed checkout readback:
  `67141228f6f73516ab68ece1c4c72bf3f67aebe0`. Doctor: **15 rows, 15 OK**.
- Main still reports unavailable default apps `wizard-troubleshooter` and
  `wizard-voice-service` (not in the public index; private install unavailable).
  These warnings are retained in `none-install.log`; exit 0 and doctor OK do not
  assert that every optional/default app installed.
- Debug steps `conditions`, `none-install`, `verify`: all exit 0.
  `receipt.json` has `stopped: true`, QEMU exit 0; its PID was absent on readback.
- New runner regressions before repair: **2 failed** (missing command helper and
  missing machine-readable input evidence). After repair, focused runner and
  alongside tests: **12 passed**. Node Open tests: **4 passed**.
  `git diff --check`: exit 0.

This is a repaired baseline continuation, not a fresh two-guest acceptance run.
The next immutable source export / job specification is staged separately for
coordinator admission. Full-suite and clean-guest rerun results remain pending;
no new candidate acceptance result is claimed here.
