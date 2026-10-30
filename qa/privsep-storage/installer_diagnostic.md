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

The parent-reviewed job first runs **20 new synthetic parser tests only**, with
URL/token, unknown input/hash, caps, qualified subprocess traceback, first-error,
source omission/drift, and forged export-field/enum negatives. Test output is a
four-counter JSON record, not arbitrary unittest tracebacks. Failure stops before
QEMU. No rerun loop. The sole native observation has a 240-second console deadline,
2 MiB private-channel ceiling, 15-second mount/unmount calls, and bounded exact
owned cleanup. Per-file hashing has a 60-second bound. Worker timeout: 1200 seconds.
Guest RAM: 1536 MiB, one vCPU. New disk budget: 64 MiB including source transport
and small receipts; reserve unchanged at 217179869184 bytes. The original allowance
66220945408 is not a cumulative quota. Existing retention is not deleted or
recounted as pending allocation. The prior stage's 2473054208 allocated bytes and
all previous receipts stay separate and unchanged.

The retained 105 passing tests and the two prior native checks (fresh synthetic
source and custody socket-before-dependency) are inherited facts only. They are
not rerun or relabeled whole acceptance. This diagnostic always says
`review_required=true`, `full_acceptance=false`; zero job exit can mean a complete
UNKNOWN observation, not successful installation. No enrollment, positive auth,
tailnet publication, reboot, release, full get.sh, merge, or live proof is granted.

## Parent admission review

Inspect the exact pushed diff, parser and 20 synthetic cases, guest reader,
namespace launcher, explicit graph and complete source-member manifest. Confirm
the stopped-610 image and all backing-chain metadata/hash pins, retained source
and native receipt hashes, fresh free-space gate, no new runtime directory, no
existing consumer/job for the stable key, and the actual source pin in job.json.
Only then submit the exact sealed job from the human-originated parent. Missing
user namespaces, mount permissions, KVM, guest runtime or other prerequisite is
one bounded refusal/UNKNOWN, not permission to install tools, broaden scope or
start a retry campaign.
