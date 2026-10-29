# Self-confining bind metadata checker

REUSE: the manager/backend runner image, DockerService authority lease, durable
bind-helper custody journal, cleanup quarantine, and 12-second BusyBox watchdog.
EXTEND: target-native static checker and exact running-manager image attestation.
No new principal, registry, configurable policy/profile, approval waiver, host
namespace mutation or CAP_SYS_ADMIN is introduced.

## Threat boundary

The manager, daemon, image, BusyBox watchdog and fixed self-restricting bootstrap
are trusted. A malicious **post-filter body** cannot write a host filesystem,
even if the read-only recursive host bind contains writable nested mounts now
or receives one later. A malicious whole entry image executing its first
instruction before confinement is **not** covered. Neither image-label metadata
nor a successful Docker create is independently a kernel-enforcement proof.

The static ELF has no libc, dynamic loader, constructors, post-filter allocation,
open, fd changes, fork, mapping, networking or signal setup. Before reading any
selected-path/UID argument bytes it checks zero effective/permitted/inheritable
and bounding capabilities, no_new_privs, fd1/2 write-only FIFO metadata, closes
all other descriptors, disables core dumps/dumpability, and installs the compiled
cBPF. Unsupported architecture, x32/compat ABI, capability state, fd type,
close_range, or seccomp installation fails closed without `safe`/`missing`.

The filter permits only native statx with exact full-word AT_FDCWD,
AT_SYMLINK_NOFOLLOW and STATX_BASIC_STATS; native write with full-word fd 1 or 2;
and native exit/exit_group. All other native calls return EPERM. Other ABIs die.
Only an actual ENOENT is `missing`. Paths have fixed byte/component/UID bounds;
all symlinks and unsafe ancestors are refused. Root/manager-owned non-writable
ancestors protect an otherwise app-owned writable file/directory endpoint.

The helper root mount deliberately omits explicit propagation for Docker
20.10.24 compatibility. Docker automatic slave handling/private-source fallback
is not a promise of future visibility from a private source. Ordinary admitted
app mounts still use rprivate. Write confinement is syscall-based, independent
of that propagation limitation.

## Source and artifact custody

DockerService resolves its exact running full container ID/default hostname and
`inspect.Image` immutable sha256, checks the image's fixed packaging label and
local immutable helper path/ancestry, and uses that same image with a fixed
entrypoint, watchdog and typed UID/path request. It never trusts Config.Image,
a floating tag, an agent image/argv/profile or an environment image override.
The helper child has none of the manager's source/runtime overlays.

The fixed entrypoint is `/bin/busybox sh -c` with literal shell text:

```sh
/bin/busybox timeout -s KILL 12 "$@"; rc=$?; exit "$rc"
```

The fixed `$0` is `wizard-bind-metadata-supervisor`; the fixed packaged helper
path, validated UID and selected path are separate positional arguments, never
interpolated into shell text. There is no agent-selected executable/argv surface.
The trailing status/exit commands deliberately prevent shell tail-exec: the
trusted shell remains namespace PID1 and waits for its child. BusyBox timeout
execs the metadata body in that **non-PID1 child**, while its detached watcher
sends the fixed SIGKILL after 12 seconds. The shell reaps its target and exits
with its status (137 on watchdog KILL); init exit terminates any remaining
namespace processes, and Docker AutoRemove removes the container. This lifetime
does not rely on an alive manager or its CLI. External force-removal is still
through the ancestor daemon; graceful stop is not promised by this shell, and
the fixed one-second Docker stop timeout bounds its fallback KILL. No host init
binary/configuration, new image dependency, or metadata seccomp exception is used.

Creation intent is fsynced before create. Full create ID/name/nonce readback is
required before start. Cleanup checks the journal full ID, name and nonce,
removes only that ID, and confirms exact name absence under a fresh deadline.
Unknown create intent, an invalid journal, replacement identity, transport error
or mixed missing diagnostic stays quarantined. Primary safe phase/exit metadata
is captured before cleanup; combined failure preserves both errors.

## Bounded acceptance matrix (not a pass claim)

- A: exact shipped ELF startup trace (filter before selected path); packaged
  safe/missing/unsafe ancestry; nonroot endpoint bytes/UID/GID/mode preserved;
  zero-caps/NNP/terminal-fd/unsupported-filter bootstrap refusal.
- B: separately compiled hostile body with byte-identical .wizard_policy;
  native mutation/open flags/mappings/fd replacement/metadata/IPC/signal/core
  families must return actual EPERM; x32/compat must die. An intentionally
  weakened filter must make the detector fail, not silently pass.
- C: actual writable nested tmpfs with unfiltered write control; future tmpfs
  after a post-filter metadata barrier in an isolated synthetic shared/slave
  child mount, matching device/inode and a subsequent unfiltered write control.
  Both controls accept fd0 after the unchanged production bootstrap closes it;
  they require changed sentinel bytes on the actual mounted device. The barrier
  case also reads exact supervisor argv and asserts a non-init post-filter body.
  This does not upgrade private-root visibility into a shared-root claim.
- D: existing backend lifecycle engine cases, including backend death and hang,
  use the exact NEW backend image as the manager namespace. Their test-only
  sleep-body adapter proves custody/watchdog, not seccomp. Python412 runs before
  any helper activity after pinned Node/Bun + repaired UV + protected basetemp.
  The backend-death case verifies the exact packaged supervisor/image and a
  non-init sleep target, SIGKILLs/reaps its manager, then waits for watchdog exit
  within 18 seconds (45-second hard Docker CLI bound). Exact-ID and exact-name
  absence must precede surviving-test journal reconciliation; cleanup cannot
  conceal an ineffective daemon watchdog.

`verify-metadata-policy.py` interprets emitted cBPF bytes; it is static proof,
not a substitute for the kernel EPERM tests. `bind-metadata-security.py` is
strict disposable-guest-only, finite and removes its own containers/mounts.
Runtime results, final ELF/image/compiled-main hashes, ARM64 native compilation
and ARM64 runtime acceptance remain pending until real admitted execution.
Do not merge on a syntax/static receipt alone.
