# Saved install-integrity failure: offline single-log diagnostic

REUSE: the successful fa42a49b classifier appliance's namespace/capability,
explicit four-node read-only block graph, sole-init, no-NIC and ext4 noload guards.
EXTEND: a new stopped disk binding and finite public WAM source-template IDs.
NEW: no product behavior, installer retry, storage replay, cleanup transaction or
normal failed-OS boot. This is not an install acceptance runner.

## Actual source diagnosis

At unchanged product revision 610716410029f2f70fe4233102584c30e9b9022f,
prepare-alongside.py:291 invokes install.sh. The saved trusted traceback instead
reaches main:339, the first `wam install` in the subsequent app loop. Private
in-process projection of the already-retained outer error identifies
`wizard-agent-service`, exit 1, not `wizard-ux` or install.sh. Therefore the
ordinary separated install.sh returned success and execution reached the app
installation phase. This is NOT a claim that the installed manager works end to
end. Its inner reason is still unknown: stdout/stderr was redirected to
`/root/storage-contract-a/installer-private.log` within the stopped 8ca disk.
The profiler sees exceptional return(None) only, retains no command operation or
inner stderr; the host gets a credential-bearing outer argv exception which must
never be dumped. No saved host phase log contains the inner private stderr.

The existing parser's source scope stops before WAM's app install. The new parser
retains its complete bounded/control-aware projection, adds six SHA-bound public
WAM source files, trusted frame IDs, public raise-template IDs and numeric HTTP
status only. Values interpolated into templates, API response bodies, exception
payloads, argv, environment, credentials and arbitrary lines are never exported.
Templates are phrase observations, not proof of a causal chain. Unrecognized or
truncated output remains UNKNOWN. A complete UNKNOWN observation may exit zero;
`review_required=true` and `full_acceptance=false` are mandatory.

## Cleanup failure: no retrospective repair claim

contract_a_guest.py:352 called `systemctl stop wizard-runtipi.service` via run's
60-second communicate timeout. prepare-alongside.py writes that oneshot unit with
RemainAfterExit=yes and ExecStop running `docker compose ... stop` without its own
stop-time setting. The unit has no explicit TimeoutStopSec. Its exact systemd
manager/default stop duration and container that stalled are not retained. The
run helper SIGKILLs the client process group; PID1 owns the unit/ExecStop and Docker
owns container processes, so that does not prove descendant/service/container
absence. Timeout occurs before exact-owned cleanup_containers. No cleanup receipt
exists, so old guest cleanup remains UNPROVEN even though host QEMU is stopped.

No source-only claim proves that a shorter stop or forced removal is safe for the
original failed disk. This diagnostic never starts those services or invokes
cleanup there. Any later disposable install runner should separately use a bounded
asynchronous exact-unit stop + explicit unit/job readback, then unchanged exact-ID
owned-container proof, while preserving the native lock and orphan-writer census.
Do not boot the old failed overlay just to finish cleanup.

## Exact proposed observation

- Only the existing synthetic 851 maintenance root may boot, read-only noload,
  init=/bin/sh, then reviewed reader as sole PID1. Never relax its process census.
- Attach stopped 8ca install-integrity disk as separate `WZ_FAILED_INSTALL_RO`;
  all four chain files and exact kernel/initrd have before/after whole-file
  SHA256 and stat equality. No new overlay or mount on the host; no NBD.
- Mount the failed filesystem ro,noload,nosuid,nodev,noexec inside the appliance
  only. Open precisely the single private-log path using directory-fd traversal,
  O_NOFOLLOW, regular-file/single-link/device/stable-stat checks. No other data read.
- Namespace isolation, RO remounts + replaced O_RDONLY descriptors, dropped
  capabilities, seccomp, no network, and exact owned process-group reaping remain.
- One observation, 240-second console deadline, 2 MiB private console cap,
  1 MiB/8192-record private-log cap, 96 event rows, 15-second mount/unmount calls.
  Save validated observations and first host errors before teardown; independently
  retain a teardown error rather than replacing the first error. Incomplete reads,
  missing logs, identity refusal or cleanup uncertainty cannot pass completion.
- Start floor remains 217179869184 reserve + 60011827200 pending + 8589934592
  allowance = 285781630976 bytes, even though this read-only job allocates far less.
  Prior storage/source/security evidence is hash-inherited, not rerun or upgraded.

## Local synthetic execution only

49 methods: the original 35 methods against the changed parser + 14 new methods.
New tests cover source omission/drift, source-template/HTTP projections, sentinels,
forged export fields/types/IDs, incomplete/unknown reads, actual reader traversal
and actual subprocess execution through fake mount/unmount executables, five
identity-refusal controls (no fake command called), symlink/mount failures, and
actual host console transport through an executable fake peer. Checkpoints are
read while simulated host teardown times out; first errors/observations survive.
Fake system identity and fake console namespace numbers are explicitly synthetic,
not native admission proof. No mount, Docker, QEMU, systemd stop or installer ran.

Parent alone may admit the frozen stage after review. Do not submit from a helper.
If the saved log cannot identify the cause, return UNKNOWN and request a new exact
read scope; never rerun the installer automatically. Product rows remain identical
to 610. Manager archive/image repair evidence is inherited unchanged from 8ca;
it is not needed to execute this read-only diagnostic.
