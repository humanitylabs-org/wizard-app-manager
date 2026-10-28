# Slice 3: main cannot complete the clean named-Hermes handoff

No merge, live deployment or heavy-job submission. PR remains draft / `do-not-merge`.

## Finding (supersedes the previous overlay-only repair)

Current main is `67141228f6f73516ab68ece1c4c72bf3f67aebe0`.
On a **clean Debian 12 cloud guest** with a working stock Hermes gateway owned by
an unprivileged `hermes` account, main's documented administrator `start` route
fails after successful host preparation. The exact failing condition in
`get.sh:named_host_ready()` (main line 692, condition at 693) is:

```bash
[ -f "$HOME/runtipi/.env" ] && [ -S "/run/user/$(id -u)/bus" ] || return 1
```

The **second test fails**: `/run/user/1000/bus` is absent. The Runtipi `.env`
**does exist**: main's root host-preparation helper creates it on first install.
The earlier inference that `.env` cannot exist on first install was incorrect.

The previous overlay-only result did not establish a clean-install repair.
Its bus was already present; suppressing the unsigned-in Tailscale operator gate
then allowed continuation. This cannot be used as clean baseline acceptance.

## Clean proof and documented owner routes

Each new debug run created a new qcow2 disk backed **directly by**
`/srv/work/images/debian-12-genericcloud-amd64.qcow2`, not a previous guest disk.
Each boot asserted `/home/hermes` was absent, installed the same stock Hermes
commit `74885f997bece88c1393bd174b25b32338336cb6`, verified API health and denied
`sudo -n true`, and then ran the unchanged main installer. No tailnet/model
credentials, approval bypass, production installer patch or sudo grant was used.

Earlier clean synchronous evidence:
`forum:/srv/work/privsep/slice3/20261027T212338Z-staging/evidence/baseline-install.log`
ends with successful root prep, another administrator request, and exit **2**.

New detached-route evidence (all under `forum:/srv/work/privsep/slice3/`):

- `20261027T214541Z-clean-baseline-debug/`: first `bash -s start` / `status`
  reproduction, terminal state `failed`, install exit **3**. The diagnostic shell
  step itself returned 1 because it asserted the initial `.env` hypothesis; its
  trace instead proved `.env` present and bus absent. VM stopped, QEMU exit 0.
- `20261027T214957Z-clean-baseline-debug/`: independent clean repeat; all diagnostic
  steps exit 0 and confirm bus absent, `.env` present, user manager active,
  `Linger=yes`. `dbus`, `dbus-daemon` and `dbus-user-session` are installed. Do not
  misreport the finding as a missing package or a stopped user manager.
- `20261027T215600Z-runner-clean-baseline-debug/`: actual new runner command/probe;
  installer fails identically. Its probe initially returned 2 for the extracted
  function because a multiline `bash -c` argument crossed `sudo -i`; classifier
  correctly rejected that mismatch. This probe transport was fixed to stdin.
- **`20261027T220027Z-runner-clean-baseline-debug/`**: independent clean proof using
  the repaired runner's actual bootstrap, VM, probe and classifier functions.
  `baseline-install.log`: exit **3**. `baseline-failure-probe.log`: exit **0**,
  `named_host_ready_exit=1`. `receipt.json` reports the exact documented reason,
  `baseline_failure_matches_documented=true`, no unexpected failures, and
  `candidate_passed=false` (candidate was deliberately not run in this debug).
  VM PID 2877239 stopped with QEMU exit 0. Started 22:00:27Z, finished 22:04:54Z.

The pinned equivalent of main's printed administrator command was run as root:

```bash
curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/67141228f6f73516ab68ece1c4c72bf3f67aebe0/get.sh -o /root/get.sh
WAM_REF=67141228f6f73516ab68ece1c4c72bf3f67aebe0 \
WIZARD_MODE=alongside WIZARD_HERMES_USER=hermes \
HERMES_HOME=/home/hermes/.hermes WIZARD_AGENT_SAFE=1 \
WIZARD_PRIVATE_ACCESS=none WIZARD_TAILSCALE_WAIT=1 \
bash -s start < /root/get.sh
bash -s status < /root/get.sh
```

Downloading first preserves exact source identity instead of following a mutable
public redirect. `status` was polled every 3 seconds with a finite bound. Its
successful invocation/start exit does **not** mean installation succeeded.
Actual terminal status is `failed`, `exit_code=3`, with:

```text
Administrator host preparation complete for hermes; no passwordless sudo was granted.
Administrator rights are needed once for: host preparation
[wizard-os] admin-command: curl -fsSL https://aiwizards.com/os.sh | sudo env WIZARD_HERMES_USER=hermes HERMES_HOME=/home/hermes/.hermes WIZARD_AGENT_SAFE=1 bash -s start
[wizard-os] owner-admin: The account hermes is not in this server's sudo group, so its password cannot approve this. An administrator of this server must run this once in their own terminal:
```

Unprivileged post-prep tests, from that final clean guest:

| Condition | Actual result |
| --- | --- |
| `~/runtipi/.env` | present |
| `/run/user/1000/bus` | **absent** |
| `docker info` / `sg docker -c 'docker info'` | both exit 0 |
| exact extracted main `named_host_ready` | exit 1 |
| `user@1000.service` / linger | active / yes |
| Hermes `sudo -n true` | denied |
| `~/wizard-app-manager` | absent; ordinary installation never reached |

`WIZARD_PRIVATE_ACCESS=none` is main's supported box-only option and removes the
unrelated unauthenticated operator condition. Both documented execution routes
still hit the same unconditional bus check. Main cannot complete this clean
scenario through either route tested. No claim is made that a manual systemd
repair could not make it work; that would not be an untouched-main owner install.
Why the installed user-bus socket was not activated is not further established.

## Runner contract

REUSE: clean-image VM lifecycle, exact pinned bootstrap, documented start/status,
registry-only retries, stock Hermes, doctor rows and candidate action/denial checks.
EXTEND: bounded detached completion polling and strict known-main-failure matching.
NEW: regression tests only; no production installer/runtime change.

- Both main and candidate document `start/status`, so both now use that identical
  route with `WIZARD_AGENT_SAFE=1`. Both still use the same stock Hermes/image.
- Baseline keeps `WIZARD_PRIVATE_ACCESS=none`; candidate keeps `tailscale` for real
  daemon/operator provisioning and mutation denial. This input difference is
  explicit in the receipt. Authenticated private links remain unproven.
- `candidate_passed` requires every named candidate install/action/denial check,
  all candidate doctor rows OK, and VM teardown. It is independent of whether main
  can install. A partially exercised candidate cannot pass.
- `baseline_status` is either `completed`, `cannot complete on main: <reason>`, or
  an unexpected failure. Expected failure requires **this exact main SHA**, exit
  3 and failed status/reason, the successful host-prep and owner-admin markers,
  plus every observed prerequisite value above. Network errors, timeouts, a
  different SHA, missing `.env`, inaccessible Docker or a different function exit
  are not waived.
- Overall `passed` requires candidate acceptance, a completed baseline or exactly
  matching documented baseline bug, existing full-suite policy, and teardown.
  The raw baseline install step retains exit 3: it is never rewritten as success.
- An uninstalled baseline has **no fabricated doctor rows**: comparison is marked
  unavailable and baseline rows are null; candidate doctor must pass on its own.

## Checks for this repair

- Focused runner + alongside regressions: **38 passed** (real pytest execution).
- Generated bootstrap/probe shell syntax and probe Python syntax are exercised in
  those tests, including success/failure/stopped/timeout terminal states, failed
  downloads, strict mismatch rejection and incomplete-candidate rejection.
- `git diff --check`: exit 0. Only this report, the runner and its focused test
  file changed; no production installer code changed.

The next full two-guest rerun is **staged only**, not submitted. Prior candidate
success does not verify the newly selected detached route; that remains pending
in the staged full rerun. This document records clean baseline proof, not a new
full acceptance pass.
