# Alongside root observability: evidence and remaining acceptance

Demo was inspected read-only. No installation, app update or service restart was performed.
Only session counts/schema and the new Wizard test conversation were inspected.

## Root causes

- Stable 0.3.1 selects UX a0d4425 and Agent Service f7536b5 independently. Demo's legacy
  `/wizard/hermes/api/sessions` returns the new test thread `wizard_1792383298899_ed3b89983950`
  (two messages), while the same route with `?view=list` returns `not_found`.
  The shipped UI requests this newer compact-list contract. The absence of a session DB mount is
  **not** proof that active threads require sharing history: both service revisions have an HTTP reader.
  Keep `share_sessions=false` and `share_history=false` unless the owner explicitly opts in.
- `/api/system/load` through the shell returns 404. Runtipi's authenticated API already returns real
  numeric metrics: CPU 1.3456291467708617, RAM 25%, disk 13%, capacity 8 GB / 96 GB at observation.
  The UX resource adapter was a bespoke deployment sidecar, not installed by the public installer.
- Only the Claude connection app is installed. No ChatGPT app/dashboard bridge or dashboard secrets
  file exists. The isolated Agent Service cannot read the owner's ChatGPT auth file by design, so its
  legacy host-file reader reports signed out. This is missing plumbing, not evidence of failed login.
  Claude's unavailable/signed-out status may be correct; do not fabricate a quota.

## Fix shape

- The `wam.resources` implementation is an exact six-number GET projection through the existing host WAM
  API. `wam links` publishes only `/api/system/load` on the shell's existing private Tailscale origin,
  reads back the exact route, refuses conflicting owner routes and Funnel, and never publishes the
  App Manager API root or credential files. Browser headers are not forwarded to Runtipi.
- Multi-provider bootstrap ensures the selected default is present even when another provider app
  already fills that slot. A new release can select ChatGPT without removing an existing Claude app.
  The existing host-dashboard bridge provisions the ChatGPT app with host dashboard Basic auth;
  the official wizard-os plugin supplies read-only usage from Hermes. No token files go to Agent Service.
- The release must pair a compact-list-capable official service with its official UX and select the
  current ChatGPT connection recipe. Code merged without a new stable manifest does not deliver this.

## Official component inventory (selection evidence, not deployment)

- UX daily `accf01cb895eabf7b4aaff79d5549a87f268c174`:
  `ghcr.io/humanitylabs-org/wizard-ux@sha256:09e2aa2a105b1408aac736f89c8441208c5f160ea58a7650424723ba8ea63aad`
  published successfully: https://github.com/humanitylabs-org/wizard-ux/actions/runs/39198124128
- Service main `1012536512b8ccae647f2e04712a2ba71dd9f7a8`:
  `ghcr.io/humanitylabs-org/wizard-agent-service@sha256:7c641ed8674d915e8867442195eac3ef3ad193604f527d7114fb5276df69b62b`
  published successfully: https://github.com/humanitylabs-org/wizard-agent-service/actions/runs/39191450093
- Both indexes fetched anonymously and advertise linux/amd64 and linux/arm64.
- ChatGPT recipe main `f87d5ddc449535418ee92ebeb24aaec36cc58058`, version 0.1.4:
  `ghcr.io/humanitylabs-org/wizard-chatgpt:0.1.4@sha256:0cb8fb715f8f06b2225b7dd93d22d84fe58d4ef218586a9f4c8009afc90462c2`.
  Public recipe: https://github.com/oscarhenrycollins/chatgpt-connection

## Verification and gates

- WAM tests exercise actual HTTP payload, secret/nonnumeric stripping, generic error states,
  cross-site and unsafe-method denial, additive provider bootstrap, and exact route readback/conflicts.
- Service latest-source compact-list contract tests: 12 passed (HTTP proxy plus list view/window tests).
- A separate no-history-mount contract smoke returns one synthetic thread through both legacy and compact
  APIs on the latest service. It fails on the installed f7536b5 service with a real `404 not_found` on
  the compact API. Resource HTTP and additive-provider regressions both fail on old WAM 4bdd7b3.
- Chromium loads the unchanged official UX resource module against the real WAM HTTP projection:
  numeric widget, capacity dialog, Back to Home, and absence of the injected synthetic secret all pass.
- Tailscale route tests here are deterministic **simulations**, not a real-node onboarding matrix.
- Required user-run acceptance after release: rerun the public installer via the original Hermes TUI;
  do not repair demo by hand. Reopen Wizard, confirm the existing new test thread appears and opens,
  send a new test turn and verify it remains after reload; open Resources and verify numeric meters.
  ChatGPT's app must report connected with real usage or a truthful provider error. Claude may stay
  signed out. Verify root baseline preservation, rerun/unlink and named-user/no-sudo separately.
- No universal readiness or rollout claim is made. The demo remains on its user-installed snapshot.
