# Alongside Service install repair — review required

This is isolated development source, not release acceptance. The failed Service
continuation preserved a built Service image but reached neither remaining
consumer case. Do not replay its partially installed overlay.

## Implemented source changes (not runtime verified)

- **Product:** `prepare-alongside.py` retains only four validated, literal image
  choices across its intentionally clean root-to-model-free-manager environment:
  `WAM_IMAGE`, `WIZARD_SHELL_IMAGE`, `WIZARD_UX_IMAGE`, and
  `WIZARD_AGENT_SERVICE_IMAGE`. Shell/Python/git/loader/daemon environment remains
  excluded. No explicit choice still selects the existing installer default.
  The actual failure was a product handoff bug: `get.sh` forwarded the image but
  the root preparation discarded it before `install.sh` selected its default.
- **Product:** pin the manager's `runtipi` and `user-config` directory descriptors
  before upstream first setup, refuse unsafe retained control directories and
  approval/lock endpoints, verify unchanged inodes afterward, and normalize
  only the two pinned directory modes before token generation or host approval
  sync. Existing installs cannot use first-setup normalization. App payloads are
  never chmodded. The final authority guard is unchanged.
- **QA:** cleanup uses full container IDs and the guest's actual Unix Docker
  API, not a Docker CLI diagnostic string. Remove only admitted Wizard/Runtipi
  project IDs, require endpoint-specific HTTP 404 plus a successful final empty
  inventory and unchanged engine identity, retain unowned containers, and export
  progress even after failure. The first removal failure is retained while
  later owned IDs still receive bounded cleanup.

The log proves the actual unsafe-parent refusal, but contains no directory stat
snapshot. Group-write from upstream setup is a supported causal hypothesis, not
an observed exact mode. New tests reproduce that setup mode change and cover
retained group-write, symlink, hardlink, foreign UID and replacement refusals.
The next admitted native run must record actual pre/post control metadata.

## Unresolved security contract: no private access versus approval custody

`prepare-named-user.sh` does not install Tailscale for explicit
`WIZARD_PRIVATE_ACCESS=none`. `prepare-alongside.py` nevertheless always installs
approval custody. This is not just a missing-command exception:

1. `install-approval-custody.py:apply` requires authoritative Serve state before
   any install/remove decision; arbitrary CLI failure must remain refusal.
2. It separately requires a loaded `tailscaled.service`.
3. `approval_custody.require_custody` requires the root-installed tailscaled
   dependency/drop-in; `inherited_listener` uses it for every worker activation.
4. Owner authorization is Tailscale-peer-authenticated on a separate HTTPS
   origin. Loopback identity headers, absent routes, and a client-chosen mode are
   not replacement authentication or evidence that persisted Serve is absent.

No custody guard or route-retirement behavior is changed in this repair.
The coordinator must choose and independently review one of these contracts
before a runnable continuation is sealed:

- **Keep the existing single custody contract:** install the official Tailscale
  package and daemon as an approval-custody prerequisite even when private app
  access is `none`; do not enroll, log in or publish a route. Prove real signed-out
  daemon Serve status behavior in a disposable guest, dependency ordering, and
  fail-closed owner actions. Package presence alone is not this proof. Existing
  missing/failed CLI and unknown Serve state must still refuse retirement.
- **Add an explicitly reviewed first-install absence state:** root proves no
  prior custody unit/alias/dependency, Tailscale unit/socket/persisted state, or
  origin/retirement record; retains PID-1 socket custody without an auth route;
  risk-raising owner actions remain unavailable. Root preparation must upgrade
  this state before future authenticated Serve publication. This changes
  installer, worker-custody and publication contracts and needs new causal
  absence/upgrade/crash/retirement cases, not an exception catch around `routes`.

Neither option is implemented or alleged proven. No unauthenticated local
approval, fake route output, generic missing-command fallback, or silent custody
skip is permitted.

## Smallest follow-on proof

After the contract decision and independent review, seal the exact new pushed
branch/commit and genuine bundle, then use fresh thin overlays of the original
stopped committed-storage guest. Bind and reuse the preserved Service archive;
never rebuild either already-attested image or reuse the failed live containers.
Re-attest the backend handle against the accepted archive, and inspect **every
named tested image** against its exact expected identity (backend, Service and
any selected UX pin), before and after the ordinary source installer.

Run only the affected/new installer and cleanup checks, plus new checks required
by the chosen custody contract, followed by:

1. `installed-compiled-backend-physical-service-consumer`
2. `stale-and-unavailable-mapping-before-update-restart`

Retain the unchanged 278 primitive tests, 3 causal controls, 12 committed-storage
runtime cases and 27 identity tests as explicitly inherited proof with their
original dependency and receipt hashes. The installer/control changes invalidate
only their affected old source assertions; those must run freshly. No storage
init, request, relocation or migration replay. Preserve root-controlled committed
metadata before/after, backing disk hashes/stats after stop, bounded cleanup,
and the original first failure. Capacity is a separate admission gate: preserve
the existing reserve and justify any same-campaign accounting from actual
allocated metadata. This document is not a native job specification.
