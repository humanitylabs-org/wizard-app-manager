# Stopped 610 installer forensics — QA only

REUSE: the retained 851 synthetic maintenance disk and its exact kernel/initrd.
EXTEND: the QA evidence path with one finite, read-only installer-log projection.
NEW: no product capability, replacement installer, daemon, or provider route.

## Authority and read scope

Only the owning parent may admit this job on native forum. Staging helpers may
inspect source, metadata and CLI help; they must not run these tests, QEMU,
unshare/mount, an installer, Docker, or a heavy submission. The static source
repertoire is drawn from six completely hash-bound files named in the parser.

The failed 610 installed OS is **never booted**. Boot only the existing 851
synthetic maintenance filesystem, root read-only ext4 `noload`, with the recorded
6.1.0-54-cloud-amd64 kernel/initrd and `init=/bin/sh` (not systemd). Do not run the
old fixture's init script. After its one shell bootstrap, the reviewed public
parser/reader replaces PID1. No ordinary installed services are started. The
failed disk is a separate, serial-identified, block-RO device, mounted only
`ro,noload,nosuid,nodev,noexec`. Its one allowed content read is:

`/root/storage-contract-a/installer-private.log`

Never read another guest file to diagnose a remaining unknown without a new
exact-path request. The maintenance root supplies executable runtime only, not
a diagnostic search target. Never boot old 474 or any other historical guest.

## Read-only proof and confidentiality

- Bind all four qcow2 chain members plus kernel/initrd by path, inode, device,
  uid/gid/mode, size, allocation, nanosecond timestamps and whole-file SHA-256.
  Verify the complete set again after the one attempt. No overlays or new disks.
- Isolate QEMU in distinct host user/mount/net/PID namespaces. Bind exactly those
  six regular files read-only in the private namespace, replace inherited input
  descriptors with descriptors opened from the RO binds, and verify both
  O_RDONLY and kernel ST_RDONLY. Drop all capabilities before QEMU exec. No NBD
  or host block devices. The private proc mount excludes outside-process FDs.
- Use an explicit QEMU read-only graph at **every** qcow2/file node, with implicit
  backing discovery disabled. Two serial-identified guest devices reference the
  failed chain and the existing maintenance layer. No NIC, monitor, shared
  directories, daemonization or core dumps. QEMU seccomp is required.
- Guest proof requires sole PID1/no userspace daemons, expected kernel, only lo,
  expected serials, both block devices RO, and mountinfo major:minor + ext4 RO
  noload for root and the failed filesystem. Refuse if any differs.
- Public reviewed source travels down the private console, never raw log bytes
  up it. The parser reads at most 1 MiB/8192 lines/8192 bytes per recognized line;
  output is capped at 96 event rows plus the separately retained first failure.
  Unknown lines remain only in the existing failed disk. Export their count and
  framed-byte hash, not text. A byte/line cap or unrecognized failure is UNKNOWN,
  never green. Earlier unknown lines are disclosed on first recognized failure.
- Emit only source-bound fixed marker/error IDs, allowlisted exception classes,
  errno, known source/function IDs + numeric line, and known command basename +
  numeric exit. Docker/Compose gets fixed reason vocabulary, never container
  logs/status dumps. No arbitrary exception text, source lines, argv, environment,
  credentials, owner URLs, tokens, or secrets. An independent host schema gate
  validates the already-filtered JSON before any retention. No raw serial log
  or stderr file is created; the bounded private channel exists only in memory.
- Retain the first error through cleanup; stop/reap exactly the newly owned
  process group. Namespace mounts disappear with the child. Hash/stat preservation
  and source closure remain required even if the outcome is UNKNOWN.

## Finite execution and distinct proof

The parent-reviewed refinement job reruns **only the original 21 synthetic methods
plus 14 new methods (35 total)** because the parser changed. The 14 new methods
expand to **213 finite checks**, including 26 realistic fixtures, the full 37-entry
Docker phrase and 13-entry shell suffix inventories, numeric bounds, ANSI mixed
with URL/token sentinels, CR ordering, unknown controls, fixed-marker assembly
negatives, source-line bounds and nested forged export fields/phrase IDs. See
`installer_classifier_refinement.json` for the exact phrases, expected IDs and
per-method finite count. No synthetic method or appliance has run in staging.
Test output is a four-counter JSON record, not arbitrary unittest tracebacks. Failure stops before
QEMU. No rerun loop. The sole native observation has a 240-second console deadline,
2 MiB private-channel ceiling, 15-second mount/unmount calls, and bounded exact
owned cleanup. Per-file hashing has a 60-second bound. Worker timeout: 1200 seconds.
Guest RAM: 1536 MiB, one vCPU. New disk budget: 64 MiB including source transport
and small receipts; reserve unchanged at 217179869184 bytes. The parent job wrapper
also retains the original pending 60011827200 bytes: **277258805248 bytes free**
is required at native exec under the inherited heavy lock. The smaller internal
reserve-plus-diagnostic check is not the job's admission floor. Never lower the
wrapper's pending or reserve amounts to fit a new stage. The original allowance
66220945408 is not a cumulative quota. Existing retention is not deleted or
recounted as pending allocation. The prior stage's 2473054208 allocated bytes and
all previous receipts stay separate and unchanged.

The retained 105 passing tests and the two prior native checks (fresh synthetic
source and custody socket-before-dependency) are inherited facts only. They are
not rerun or relabeled whole acceptance. This diagnostic always says
`review_required=true`, `full_acceptance=false`; zero job exit can mean a complete
UNKNOWN observation, not successful installation. No enrollment, positive auth,
tailnet publication, reboot, release, full get.sh, merge, or live proof is granted.

## One classifier refinement; never an installer retry

The inherited complete 6548-byte/127-record read found only `private-access-none`
at record 115, with 126 unknowns. Its failure cause remains UNKNOWN. The pinned
`install.sh` lines 112–122 next render release pins and execute the real Docker
Compose up; therefore source supports looking for release-render and Docker/
Compose errors, **not** asserting either as the actual failure. The static private-
access helper echoes and all admitted pre-exit installer literal messages are
source-asserted. Interpolated paths/status/link values and action-approval messages
are not exported. Backend/index literals are not added: Compose up does not dump
those services' logs; their unhealthy/start failures use fixed Compose IDs.

The expanded standard-tool vocabulary covers invalid bind/mount/volume specs,
missing source, propagation/shared mount, permission/readonly/no-file, daemon/
connection refusal, dependency/unhealthy, runtime/resource and bounded shell errno
or nonzero exit forms. These are **phrase observations**, not causal diagnoses.
Multiple matched Docker phrases are fixed IDs only, never message fragments.
Traceback line numbers must fit the hash-bound source file's numeric line count.

Allow at most 32 CSI sequences per record, at most 32 parameter characters each,
and only SGR (`m`) or erase-line (`K`). Unsupported CSI, OSC, other C0/C1 and invalid
UTF-8 stay unknown. Fixed phase/warning messages accept outer SGR decoration only,
never an embedded-control join or erase-line assembly. CR/CRLF/LF delimit records;
earlier progress records are not erased, skipped or joined. Unknown hashing uses
original raw framed records, never normalized text. An otherwise plausible error
without an approved prefix/reason or exceeding bounds remains unknown.

Pin the inherited completed diagnostic receipt and worker log by exact hash/stat,
all six original image/kernel/initrd inputs unchanged, and the expected private
log metadata/whole-file digest/count. The same maintenance reader, guest PID1,
namespace launcher, transport and explicit RO disk graph are retained unchanged.
After this **one** refined projection, if failure is still unknown, stop and report
counts/hashes and uncertainty. No automatic second vocabulary campaign, broader
file read, failed-OS boot, installer restart, guestfish install, Docker run or
installation rerun is authorized. Diagnostic exit zero is not acceptance.

## Parent admission review

Inspect the exact pushed diff, parser and 35 synthetic methods, guest reader,
namespace launcher, explicit graph and complete source-member manifest. Confirm
the stopped-610 image and all backing-chain metadata/hash pins, retained source
and native receipt hashes, fresh free-space gate, no new runtime directory, no
existing consumer/job for the stable key, and the actual source pin in job.json.
Only then submit the exact sealed job from the human-originated parent. Missing
user namespaces, mount permissions, KVM, guest runtime or other prerequisite is
one bounded refusal/UNKNOWN, not permission to install tools, broaden scope or
start a retry campaign.
