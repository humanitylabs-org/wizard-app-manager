# Capped early-worker Bun 1.3.0 bootstrap repair

QA harness only. No Manager product code, package manifest, lockfile, provider,
credential, live runtime or owner-content changes.

## Source-established attempted write

The complete sanitized 0c89dac6 diagnostic log is 4,022,752 bytes, SHA256
`2707bd6628e15ad7ce2e7d803aa66adb27c8741365fdb2f9fd233b66f50c8f14`.
Its comparison is SHA256
`231a1d6d9f33215af88548873ea2f96cdc4d49aadfb49fdb50ac0c2ff655462b`.
The EROFS text at line 113696 is interleaved with p-locate extraction, not proof
that p-locate failed. The source chain is:

- `ssh2@1.17.0` runs `node install.js`. Its public install.js spawns `node-gyp`
  in `lib/protocol/crypto`. The log starts ssh2 scripts at 113358, reports EROFS
  at 113696, prints `Failed to build optional crypto binding` at 113731, then
  finishes ssh2 scripts at 113826. The script tolerates that child failure.
- The real bootstrap inventory has no make/g++/git, and the frozen lock has
  **node-gyp-build**, not **node-gyp**. Those are different binaries. Bun's
  generated fallback `node-gyp` shell script runs `bun x --silent node-gyp $@`:
  [PackageManager.zig:447–534](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/install/PackageManager.zig#L447-L534).
- [bunx_command.zig:416–534](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/cli/bunx_command.zig#L416-L534)
  defaults an unversioned package to `latest`, then constructs
  `<platformTempDir>/bunx-<uid>-node-gyp@latest`.
- [fs.zig:535–590](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/fs.zig#L535-L590)
  returns literal `/tmp` on Linux from **platformTempDir()**. Unlike tmpdirPath(),
  it does **not** consult TMPDIR or BUN_TMPDIR. Adding either environment setting
  cannot repair this call site.
- [bunx_command.zig:705](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/cli/bunx_command.zig#L705)
  propagates `std.fs.cwd().makeOpenPath(bunx_cache_dir, .{})`. With uid 1000 the
  attempted directory creation is **`/tmp/bunx-1000-node-gyp@latest`**, on the
  read-only root. This is source-derived, not a captured syscall pathname.
- [crash_handler.zig:715–732](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/crash_handler.zig#L715-L732)
  emits the exact `An internal error occurred (ReadOnlyFileSystem)` message for
  a propagated root error. This child error does not stop the outer installer.

## Smallest redirect

Mount a generated directory read-only over container `/tmp`. It contains exactly
one uid-bound symlink:

```
/tmp/bunx-1000-node-gyp@latest -> /work/tmp/bunx-1000-node-gyp@latest
```

Precreate that destination inside the existing **8 GiB `/work` tmpfs**. This
redirects only the source-established bunx cache. It does not add writable root,
writable `/tmp`, another tmpfs, a host cache, a host tool install or capabilities.
Root, source and pinned Node remain read-only. The alias mount holds only a fixed
symlink; dependency bytes cannot be written to its host-backed directory.

The worker checks actual read-only mounts, the exact link inventory/target and
target device, then writes/reads/removes a synthetic probe through the alias.
Regression tests cover uid binding, extra entries, target drift, one tmpfs,
read-only bind flags and actual write-through. Original frozen install, scripts,
exit handling, caps, reserve and cleanup gates remain unchanged.

## Comparison repair

Bun's isolated linker installs actual manifests below
`node_modules/.bun/<name@version+peer>/node_modules/<name>`. The old diagnostic
skipped dot directories and treated lock keys as hoisted placement promises:
1,678 of 1,687 rows appeared missing and the actual inventory was empty.

Inventory now follows only controlled module roots inside the synthetic export,
includes isolated scoped/peer contexts, deduplicates physical packages, and
compares exact package name/version identities. Each row retains informational
`hoisted_path`/`hoisted_state`, actual matching locations and bin-file checks.
Presence anywhere is not claimed to prove every dependency edge resolves.
Wrong versions, absent manifests and escaping symlinks have focused regressions.

## Acceptance boundary and separate failure

The EROFS fix is **staged, not runtime accepted**. Do not call the outer frozen
install successful until a new queued receipt actually returns zero.

The optional cpu-features compiler failure was independently deleted at log
113357. It is not the source of the EROFS child error. However, pinned Bun source
also proves why merely fixing EROFS does not establish a successful outer exit:
[lifecycle_script_runner.zig:343–351](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/install/lifecycle_script_runner.zig#L343-L351)
passes optional script failure to isolated `onTaskComplete(..., .fail)`;
[Installer.zig:287–289](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/install/isolated_install/Installer.zig#L287-L289)
increments `summary.fail`, and
[install_with_manager.zig:912–914](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/install/PackageManager/install_with_manager.zig#L912-L914)
sets `any_failed_to_install`, causing
[install_command.zig:82–84](https://github.com/oven-sh/bun/blob/bun-v1.3.0/src/cli/install_command.zig#L82-L84)
to exit 1. The complete log has one optional script deletion and no nonoptional
script failure. This is a separate source-backed accounting path, not permission
to skip scripts, ignore exit 1, change the lock, or repair the compiler here.

The parent admits one changed early spec. Full Manager/Docker acceptance,
existing documentation acceptance gaps and live rollout remain outside this fix.
