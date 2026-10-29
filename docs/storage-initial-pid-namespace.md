# Initial PID namespace identity: narrow maintenance-fence repair

**Source-backed candidate; runtime proof not yet run.** This changes only the
namespace discriminator in `require_fence`. It does not admit a storage
transaction, prove a full install, or close the F1 entry/wiring gates.

## Why this rule, not `NS_GET_PARENT`

A process in its own nested PID namespace cannot see that namespace's parent
through `NS_GET_PARENT`; Linux returns EPERM for an out-of-scope parent, including
with capabilities in the initial user namespace. EPERM therefore proves nothing
about being initial. The retained diagnostic at
`dd15870c02dfbe7ece0b98f1cffee83cba270ab6` actually admitted nested cold PID1 while
a foreign initial-namespace process was alive. Its eight observations were
complete, not eight security passes. That evidence remains unchanged.

The replacement opens `/proc/self/ns/pid` once and, on that **same live FD**:

1. Uses `fstatfs` to require the nsfs superblock magic `NSFS_MAGIC`.
2. Uses `fstat` to require regular namespace-file metadata and obtain device/inode.
3. Uses `NS_GET_NSTYPE` to require `CLONE_NEWPID`.
4. Requires the kernel-reserved `PID_NS_INIT_INO` / `PROC_PID_INIT_INO` value
   `0xEFFFFFFC`, not a readlink string or guessed host namespace device number.

The device is per-boot nsfs identity, not a fixed ABI constant; the successful
fence returns the observed device, inode and type for evidence. `fs/nsfs.c`
uses one internal `nsfs_mnt` for its namespace paths; the helper does not assume
that the numeric device is the same on different boots. nsfs attestation
excludes regular files and user filesystem ioctl emulators. Namespace-type
attestation excludes a different namespace type with a reserved inode. An open
FD pins the namespace while all facts are inspected. The namespace FD is closed
on success and error; opening it and these observations do not change namespace,
mount, boot, sysctl or process state.

## Source proof: noncollision, including destruction/reuse/exhaustion

Exact URLs and SHA-256 hashes are in
[`initial_namespace_sources.json`](../qa/privsep-storage/initial_namespace_sources.json).
The source bytes are also staged outside the executable payload for review.

- Linux stable **v6.1.190** `include/linux/proc_ns.h:41-48` reserves
  `PROC_PID_INIT_INO = 0xEFFFFFFC`. `kernel/pid.c:73-85` assigns that value to
  `init_pid_ns.ns.inum`.
- `kernel/pid_namespace.c:76-114` allocates every new PID namespace through
  `ns_alloc_inum`, which in `include/linux/proc_ns.h:67-73` calls
  `proc_alloc_inum`.
- `fs/proc/generic.c:196-219` sets `PROC_DYNAMIC_FIRST = 0xF0000000`, allocates
  an ID in the bounded interval from zero through
  `UINT_MAX - PROC_DYNAMIC_FIRST`, and adds `PROC_DYNAMIC_FIRST` to it. The
  allocator interval is finite and cannot overflow that addition. IDA's source
  (`include/linux/idr.h`, `lib/idr.c`) bounds allocation and returns ENOSPC when
  exhausted; it does not wrap into the reserved interval. Free/reuse returns
  IDs only to that same dynamic pool. Neither churn nor inode recycling can
  produce the smaller initial-PID inode.
- `fs/nsfs.c:85` exposes `ns->inum` as `inode->i_ino`;
  `ns_ioctl:203-204` reports `ns->ops->type`;
  `pidns_operations` is typed `CLONE_NEWPID`. nsfs creates its pseudo superblock
  with `NSFS_MAGIC` (`fs/nsfs.c:284-290`). This is kernel object metadata, not a
  proc link's printable representation.
- The same chain was inspected at **v6.8, v6.12, v6.12.114 and v6.17**.
  v6.12/v6.17 use `ida_alloc_max` instead of the older bounded IDA wrapper;
  the dynamic interval is identical.
- At **v6.18.59** and **v7.3**, the initial inode constants are explicitly
  exported in `include/uapi/linux/nsfs.h` (`PID_NS_INIT_INO`, line 51). This is
  stronger than an undocumented printable convention. Initial PID creation
  uses `ns_init_inum(&init_pid_ns)` / `NS_COMMON_INIT(init_pid_ns)`.
  Newly allocated PID namespaces use `ns_common_init(ns)`, whose type-generic
  macro passes zero unless the object is the actual statically allocated
  initial namespace; `kernel/nscommon.c:__ns_common_init` then calls the same
  bounded `proc_alloc_inum`. v7.3's kernel itself also classifies reserved
  initial inodes in `is_ns_init_inum`.
- Debian's published **linux 6.1.190-1** packaging archive was retrieved over
  HTTPS. Its SHA-256 matches the published `.dsc`; scanning all 169 files in
  `debian/patches/` found no diff header touching any of the five decisive
  namespace/allocator files. No package signature was checked. This is a
  source comparison, not a claim that the disposable guest's running kernel
  binary has been reproduced or verified.

Thus, under the supported source contract, **no dynamically created PID
namespace can satisfy the complete identity predicate**, even if it is PID1,
remounts its own proc, has the same bootargs/UID map/capabilities, and cannot
see foreign userspace. No manual root-owned token or attested JSON is an input.

## Compatibility contract and fail-closed behavior

This is not a promise that all historical/future Linux or vendor forks preserve
internal implementation details. Pre-6.18 source names are internal, not UAPI;
the newer UAPI export corroborates the deliberate reserved identity but does
not retrospectively make every old vendor tree audited.

This implementation supports **source-conforming Linux kernel lines 6.1, 6.8,
6.12, 6.17, 6.18 and 7.3 only**, on **LP64 x86_64 and aarch64**. The two LP64
statfs layouts follow `include/uapi/asm-generic/statfs.h` and x86's statfs header;
other ABIs refuse before calling libc. Patch-level/vendor suffixes are allowed
within these lines as an explicit trusted-OS compatibility contract, not because
`uname` cryptographically attests kernel source. Vendor maintainers must retain
reserved-inode/dynamic-allocation/type/nsfs semantics. Kernel/root/runtime
integrity is already in this maintenance operation's TCB.

All other lines (including 5.15, 6.6, 6.14 and 7.2), unknown release formats,
unsupported architectures/ABIs, unavailable libc support, failed namespace
opens/stats/ioctls, wrong filesystem/type, or a dynamic inode **refuse**. A line
not listed is not claimed insecure; it simply has no admitted compatibility
contract here. The checker does not fall back to EPERM, bootargs, PID counts,
root-owned claims or another namespace alias. A future kernel requires a source
review and a new focused VM proof before extending the named allowlist.

Ordinary non-PID1 use keeps its existing early refusal. Fresh/alongside install
on an unlisted kernel cannot perform this maintenance path until supported;
this intentional availability restriction is not a silently supported install.
Runtime proof on one Debian guest does not grant ARM, Ubuntu, other kernel-line,
fresh-install or alongside-install runtime acceptance.

## Unchanged protections and threat boundary

The complete UID map, real/effective UID zero, actual PID1, required host init
capabilities, explicit protected boot init, kernel-thread-aware global userspace
census, single task, protected code/parent checks remain required. R1 source
recovery, R2 distinct target recovery and F2 rollback barriers are byte-for-byte
unchanged. The source manifest and static AST comparison bound this delta; the
whole file is **not** called unchanged.

Trusted root could substitute proc mounts, inject a different interpreter,
forge a kernel, ptrace the checker or alter code; that pre-existing trusted-root
attacker is outside this rule. The repaired case is an accidental/nested root
invocation under the same trusted kernel/runtime, not defense against a
host-root adversary. No host proc alias, persistent namespace FD/bind, observer
service, cgroup machinery or side-effecting discriminator is introduced.

## Focused proof to run only after parent review

`qa/privsep-storage/initial_namespace_run.py` has a read-only preflight and a
separate native execute mode. It binds a fresh source archive, the unmodified
baseline archive, historical 135-case JUnit/receipt, and the actual failed
cold-nested observation. Original reports and the original diagnostic are not
rewritten. Changed test dependencies are bounded by full archive manifests and
an AST check requiring every old top-level product node except `require_fence`
to survive unchanged. Only the existing storage module tests and new identity
module tests are selected; the old 135-case suite is not rerun or relabelled.

The eight strict oracles are:

| Observation | Required result |
| --- | --- |
| Ordinary initial-namespace process | refuses (not PID1) |
| Exact archived ordinary nested checker | NESTED_REFUSED, exit zero |
| Ordinary actual nested PID1 | refuses |
| Cold genuine initial PID1, sole userspace | admits with initial descriptor proof |
| Cold genuine initial PID1 with real foreign userspace | refuses for foreign userspace |
| Exact archived checker under actual cold bootargs | NESTED_REFUSED, exit zero |
| Actual nested cold PID1, hidden foreign outer task alive | **refuses `nested PID namespace`** |
| Genuine initial PID1 after foreign task reap | admits again |

The nested fixture additionally proves same real bootargs and initial user
namespace, actual PID1, only nested PID1 visible, different PID ns, and a parent
observer whose namespace ioctl reaches the real initial namespace. Caps and UID
maps are recorded; admission of the one-call-removal control exercises all
remaining guards in the same nested process. FD superblock/type/device/inode
facts are recorded on every snapshot independently of the guard.

**Mutation negative control:** generate a root-only source copy by replacing
exactly `pid_namespace = require_initial_pid_namespace()` with a non-authority
placeholder, leaving every other check intact. Under the same actual nested
cold PID1 fixture it **must admit** while the candidate refuses. Its exact
SHA-256 is precomputed and required in the report; no transaction is invoked.
The historical unmodified vulnerable checker/report stays immutable evidence.

Native exit zero requires every exact observation, the mutation control,
zero-error/skip focused JUnit, independent disk/serial cold-report equality,
and complete owned-VM cleanup. `focused_fence_acceptance` is separate from
`storage_acceptance`, `full_integration_acceptance` and release/install status;
those remain false. Collection completeness alone cannot turn an unsafe
observation green. Failure keeps the original stdout/stderr/serial/first error.

Capacity remains **217179869184 bytes reserve + 21474836480 bytes pending =
238654705664 bytes required**, without deleting old disks or relaxing the floor.
The job creates fresh 14 GiB OS and 4 GiB secondary overlays plus seed/log/source
margin; its three owned VM boots are stopped in finally and tracked. It uses
only synthetic cloud-guest data and no owner credentials/provider/core Hermes.
After this proof, the longer storage 77/38 cuts, F1 entry/wiring and full fresh
and alongside installations remain separate, open acceptance gates.
