# Slice 5 smoke evidence (not clean-install acceptance)

Implementation is on the existing do-not-merge privilege-separation branch; no
main merge, deployment, stock Hermes modification or owner-box write occurred.
The full clean-install + full-pytest gate is staged separately, not claimed here.

## Real Debian 12 helper debug guest

Forum evidence directory:
`/srv/work/privsep/slice5/20261028T005621Z-debug/`

Source tested: `f4b70c80`. The fixed root helper and grant/revoke state operations
are unchanged since that smoke; later changes harden source-launch dispatch and
the finite-sudo audit, covered by the regression run below. The short KVM guest used a clean
Debian 12 base with `sudo`, Python and the real systemd host, a non-admin `hermes`
user and a separate `wizard-system` user. It did **not** install Runtipi or stock
Hermes, so it is not the full alongside acceptance run.

Real execution: all eight debug steps returned 0; `receipt.json` has
`passed:true`, the guest process was stopped in `finally`, and a separate
`kill -0` readback confirmed it no longer existed.

`real-helper.log` SHA256:
`6cda635860b89c715d69dd1b608dbb09fed6ad9bcb714ce48a149e555817fcb3`

Verified against actual guest files/processes:

- Hermes `sudo -n true` refused before grant.
- Developer-off grant refused and every helper action refused extra arguments.
- Root-owner source selection + short grant created a valid root-owned sudoers
  include; `visudo -c` passed, Hermes sudo succeeded and had `COMMAND=` in the
  dedicated sudo logfile.
- A new sudoers include appeared in drift; the full-access failed/red doctor tuple
  appeared; root-entered settings remained readable at wizard-system's uid.
- Without invoking the status helper to expire it, the **actual root timer**
  removed the policy. Hermes sudo failed afterwards.
- Regrant succeeded; developer-off removed the policy immediately and sudo failed.

The committed clean-guest runner additionally requires stock Hermes bootstrap,
real Runtipi/manager ownership, thin-agent HTTP refusal, actual doctor API severity
and the full pytest baseline comparison.

## Local regression checks

`uv run --with pytest --with pyyaml -- python -m pytest -q
 tests/test_privsep*.py tests/test_owner_approval.py tests/test_fresh*.py
 tests/test_updates.py tests/test_doctor_updates.py`

**274 passed in 55.14 seconds.** This includes the exact fixed-helper sudo audit,
owner/forged-header tests, finite helper arguments, real candidate `visudo` syntax,
expiry, drift, one-time migration and developer-off tests, plus runner preflight
and receipt acceptance tests.

A broader local backup subset was attempted but is **not** a passing result:
first it hit the control-plane scratch path's AF_UNIX length limit; with a shorter
scratch basetemp it progressed without a failure before the bounded 240-second
command deadline. Full-suite results must come from the staged worker job, whose
only allowed failure is the existing public-index seal test.

## Confirmation-page visual check

The actual `developer_approval.respond` HTML and actual `approve.css` were rendered
in a browser at 430×1000 in dark and light color schemes. These are **visual
fixtures**, with an explicit example active-grant status (not live authority or a
claimed server grant). Screenshots: `screenshots/developer-dark.png`,
`screenshots/developer-light.png`, `screenshots/access-dark.png`. Both reviewed;
page content width equalled viewport width (430 px), all five forms were present,
and the narrow-screen expiry input, full-access confirmation and revoke button
were readable. There are no floating/translucent text panels.

Shell-wide persistent banner, Settings entry and red Health presentation remain
Wizard UX/Agent Service follow-ups; this slice exposes the API contract and the
protected App Manager confirmation page only.
