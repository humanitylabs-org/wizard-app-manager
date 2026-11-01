# Lifecycle private-storage boundary

## What changes

The serving AppFilesManager executes `chmod -Rf a+rwx` on the entire app-data tree from `ensureAppDir`, and again after install/update `compose up`. A live app's 0600 writer cannot prevent that privileged operation reopening its current inode. Startup repair is not continuous protection.

This patch removes the recursive operation, and removes both post-up callers. The retained `setAppDataDirPermissions` compatibility entry point creates missing ancestors/app roots only (0755 subject to the process umask); it never enumerates app-owned descendants or changes their owner/mode. Directory descriptors are opened component-by-component with O_DIRECTORY/O_NOFOLLOW and operations use pinned `/proc/self/fd` parents. Invalid app/store components, parent symlinks and non-directory paths fail closed.

`app.env` is the manager-owned exception: existing regular single-link files are tightened to 0600 through a no-follow descriptor without reading their bytes, including `skipEnv` starts. New contents go into a unique owner-only O_EXCL inode, fsync, atomic rename and parent fsync. An old permissive inode is never overwritten with new secrets. Symlink/hardlink/special-file destinations are refused. No other app file is opened or chmodded.

## Reuse

- REUSE `extractAppUrn` validation and AppFilesManager's existing lifecycle entry points; no app-ID allowlist or new manifest extension.
- REUSE `WizardStorage`'s `wizard.storage` / named-volume-to-bind rewriting and numeric `service.user` create-only ownership (`prepare`, `initialiseFreshBindOwners`) for fresh managed storage. Existing managed data is not chowned again. `docs/APP_STATE_AND_SHARES.md` remains the contract.
- REUSE explicit recipe init services for direct binds needing a non-root service UID. No inferred image user, UID 1000 fallback or recursive global permissions.
- EXTEND the platform filesystem boundary with Linux descriptor-pinned directory creation and private app.env replacement. Existing `confined` does lexical/realpath checks but not descriptor-pinned mutation; `FilesystemService.writePrivateTextFile` writes then chmods an existing inode, so it cannot guarantee that new bytes never enter an old world-readable inode.
- NEW storage schema/broker/encryption: none. This is not a credential-isolation contract and does not require revisiting the separately reviewed broker design.
- Visual: none; no UI changes.

## Compatibility limit — blocks unreviewed deployment

Existing legacy nonsecret data keeps its exact modes, ownership and bytes. Existing 0600/0700 private data remains private across lifecycle operations, including while the app runs. Managed fresh storage retains the existing numeric service-user ownership contract; explicit direct-bind init services work with unrelated UIDs (synthetic gate: 1101 and 2202).

**A fresh non-root direct-bind recipe that relies only on Docker creating a root:0755 directory and WAM recursively opening it is no longer supported implicitly.** It needs an explicit create-only ownership init service or the existing managed storage contract. Named/image-defined users without numeric metadata likewise need explicit initialization; no guessed UID or blanket chmod is substituted. Recipes binding the app-data root itself to a non-root writer require the same review. This patch intentionally does not rewrite third-party recipes, migrate installs, or open already-existing data to preserve that unsafe behavior. Review exact recipes before promotion; otherwise this is a verified narrow source fix, not a universally deployment-ready release.

Previously exposed unknown secret files are not automatically repaired: the manager cannot infer which are private. Known app-owned writers remain responsible for their own secure modes. Legacy app.env is tightened on the next lifecycle invocation, not changed live by this source patch. A world-writable parent can still permit replacement/deletion, and same-UID/root/Docker-capable actors can still access plaintext; removing the lifecycle chmod does not create a host privilege boundary.

## Regression and acceptance homes

- `tests/lifecycle-private-storage.test.mjs`: real Linux filesystem tests of the shipped helper and exact AppFilesManager method bodies, without installing the backend. Covers install/restart/update, live atomic writer, old legacy modes, app.env under umask 000, skipEnv tightening, symlink/hardlink/FIFO refusal, path confinement, parent-rename pinning and a named-volume analogue. `tests/test_lifecycle_private_storage.py` wires this into pytest.
- Backend Vitest: the decorated AppFilesManager private-writer contract, actual start/install/update commands with fake services, and `wizard-storage.test.ts` create-only numeric ownership and repeated prepare/lifecycle mode preservation. These are staged, not claimed executed locally.
- `qa/lifecycle-private-storage-synthetic.mjs`: opt-in, disposable forum Docker gate with actual numeric-UID consumers, supported init-service ownership, running private writers and an actual named volume. It uses only synthetic state, network-none containers, digest-pinned existing COPY_IMAGE, and removes/verifies its own containers/volume. It does not launch the production WAM service or exercise owner accounts.
- `qa/lifecycle-private-storage-forum.sh`: frozen dependency install, common build, backend typecheck and targeted contracts, lightweight regressions, Docker gate; preserves separate logs and returns failure if any step fails. Use only the parent-authorized durable forum worker. This branch stages the runner, never submits it.

Source-only checks are necessary but not Docker acceptance. A future release still requires forum PASS, exact recipe compatibility review, an approved rollout/rollback window and account-safe canaries. Keep previous app-hardening baseline evidence unchanged, including unrelated known failures; do not relabel it as passing because this platform bug is fixed.
