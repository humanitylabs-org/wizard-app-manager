# Signed-out custody prerequisite — staging only

REUSE: committed-storage read-only fence and KVM SSH/stop transport. EXTEND:
new isolated prerequisites-only runner. No production files or security behavior
change. Do not merge this QA branch or install it on a real machine.

The parent selected the unchanged single custody contract: official Tailscale
and its real system daemon are approval-custody prerequisites even with private
app access `none`. `none` still grants no private app access. This proof does NOT
implement that production installer change. Independent review and native
results precede any such implementation.

## Exact finite scope

One new isolated VM boot, using two thin COW disks backed ONLY by the original
stopped, committed synthetic 851 guest. Never boot or clean up the partial 474
continuation guest. Never invoke the storage main, Transaction, relocation,
migration, request creation, full app installer, Docker CLI, backend/Service
build/load, consumer checks, login, enrollment, `tailscale up`, an auth key,
operator assignment, Serve setup/removal, or host network/systemd mutation.

The six expected guest checks, in order:

1. `synthetic-fresh-prerequisites`: complete extracted source hashes/modes/root
   ownership; absent Tailscale/auth-state/custody units; listener baseline; new
   synthetic manager account absent. Any unexpected preexisting state refuses.
2. `official-package-real-daemon`: official Debian bookworm signed apt repository,
   official package installation, systemd start, package version, key/binary
   hashes and actual `/proc/PID/exe` matching the installed daemon.
3. `signedout-status-and-serve-inspectable`: actual `status --json`,
   `serve status --json`, and `debug prefs`; exact NeedsLogin, no user/tailnet/IP,
   no operator/advertised routes/tags; genuine Serve JSON must be `{}`. Unknown
   output, null/non-object/invalid JSON, command failure or nonempty Serve is a
   diagnostic refusal, NEVER an empty-state fallback. Stop before custody if
   inspection is unavailable; do not force later checks.
4. `exact-installer-socket-dependency`: root invokes the unmodified f18 installer
   and custody module from the full source archive, installing its exact socket,
   worker and daemon dependency units. Record loaded dependency/order/listener
   facts plus root-owned unit digests. No manually reconstructed custody units.
5. `actual-worker-unauthenticated-owner-refusal`: a fixed header-free loopback GET
   and POST trigger the exact generated systemd worker (`python -m wam serve`).
   Both must return exact 403 owner-only responses; no owner pin, intent,
   approval origin, developer config or Runtipi operator may appear. No fake
   CLI, positive identity headers, patched handler or positive auth mock.
6. `signedout-no-publication-readback`: genuine status/Serve/prefs again, no new
   non-loopback TCP listener (only the exact local manager ports may be added),
   and full extracted-source closure unchanged.

The synthetic manager has no apps or recommendations. Its existing supported
`WAM_NETWORK_WATCH=0` switch avoids network reconciliation. The real generated
worker unit retains its existing Docker systemd dependency INSIDE this VM; no
host daemon or container build/cleanup is admitted. A missing runtime dependency
or unavailable worker is a refusal, not permission to rebuild/install the stack.

## Evidence and limits

- Static-seal entire source against f18: exactly these three new QA files, no
  changed/deleted f18 member. Genuine archive commit, pushed named branch and
  bundle head are bound. Root-extracted archive modes are normalized with an
  explicit 022 umask and `--no-same-permissions`.
- Before/after committed synthetic mapping, completion, journal, sentinel and
  protected-storage source digests use the exact inherited read-only state
  helper. Original backing disks' SHA-256 and stat records are rechecked after
  the one VM stops. The old partial continuation evidence stays untouched.
- Preserve reserve **217179869184** bytes. Original remaining campaign budget
  **66220945408**, minus independently inventoried old 474 evidence allocation
  **2949689344** ONCE, leaves **63271256064** before new staging/allocation.
  Admission/each host operation require `free >= reserve + max(0, remaining -
  actual_new_stage_allocation)`. Do not credit 851, the Service archive, duplicate
  474 entries, nominal virtual disk sizes, deletion, or a lower floor.
- New stage/evidence cap **2147483648** bytes. QEMU inherits a hard per-file
  **402653184** byte cap for each COW disk and QEMU/serial log. Package/boot writes
  exceeding a cap fail; never expand disks or retry. Native execution deadline
  is 1500 seconds plus 300 seconds cleanup; durable job max 2100 seconds.
- Cleanup explicitly stops the native systemd tailscaled binary and verifies
  inactive/MainPID=0 plus no matching process BEFORE guest poweroff and QEMU
  cleanup. Stop the synthetic worker, leave protected socket custody unchanged
  until shutdown, preserve original error and classify cleanup failures. No
  source remove/retirement call is part of this probe.
- Export only structured projections, exits, byte counts, digests and fixed
  operation names. Auth URLs, raw status identity fields, auth state, credentials,
  CLI output and journal tails are not exported. Genuine authority-free Serve
  JSON is retained; unknown/nonempty output is retained only by digest and
  refusal classification. No URL is followed if one is generated automatically.

A successful result is only a signed-out prerequisite observation. Actual owner
identity, authenticated tailnet/Serve, full installer, new f18 54-case installer
suite, old failure-negative tests, remaining Service consumers and cleanup of
the old partial guest remain NOT RUN / NOT PROVEN here. The inherited 278+3+12+27
storage/identity evidence is neither replayed nor promoted into these six checks.
Only the parent may independently review/admit the sealed job. No helper executes
VMs/tests/builds or submits it. Any prerequisite failure ends with diagnostic
refusal, NOT full acceptance.
