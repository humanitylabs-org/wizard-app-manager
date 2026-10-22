# App-owned state and folder shares (#79/#80)

Source contract: `humanitylabs-org/wizard-os`, branch `spec/app-owned-logins`
(commit `dc8b00a`). Companion checker: `feature/storage-checker`.

## Runtime

- The Wizard compose builder rewrites named volumes in the `wizardapps` store
  after architecture overrides. Other stores stay unchanged at compile time;
  any installed recipe carrying `wizard.md` also gets the rewrite in the
  pre-start extension (including private stores).
- Short and long syntax retain targets, read-only flags and other service
  options. Top-level volume declarations are removed except verified legacy
  Claude bind aliases (below). `x-wizard-volumes`
  is migration metadata only; pre-start recomputes plans from the source recipe
  and never trusts author-supplied migration instructions.
- Fixed `name:`, external volumes and volume-driver configurations are refused.
  External volumes are **not** silently left outside backups. The only runtime
  compatibility exception is the already-used `wizard-ai-accounts-claude`
  login: Claude/Hermes migrate to a share, and other installed consumers may
  retain a verified bind alias (below). New recipes still fail rule 15.
- Pre-start looks for Compose's `<app>_<store>_<volume>` (the same project name
  DockerService passes to Compose), or the legacy Claude fixed volume. A
  digest-pinned, network-disabled Alpine helper mounts the source read-only,
  copies with `cp -a` into a sibling staging folder, renames it, and writes a
  marker beside the destination. Source Docker volumes are never removed.
- A running container on the source volume blocks an **actual copy** with an
  explicit instruction to stop it. Stop **all** writers, including generated
  agent apps, before the first copy. A regular host-side completion marker is
  checked before Docker queries, so a migrated provider starts even while
  legacy-name consumers are running. A local bind alias whose `Options.device`
  exactly matches the daemon-side destination (`APP_DATA_DIR` from `app.env`)
  needs no copy: write the missing marker and return before checking writers.
  Symlink/non-file markers are refused. No hot-copy or empty-login fallback occurs.
- A failed helper/daemon call aborts before `compose up`. A partial staging copy
  retries from the intact source. An unmarked nonempty destination is refused,
  not overwritten or silently adopted. A completed copy is not recopied.

## Shares

`shares: [{id: claude-login, path: claude}]` exports
`${APP_DATA_DIR}/data/claude`. A consumer declares
`mounts: [{app: wizard-claude, share: claude-login, path: /claude}]`.
Only the consumer's main service receives the mount, and only while its exact
`kind: share` grant exists. No share grant grants network access or RPC keys.
Existing per-app `wizard/grants.json` RPC credentials are not changed.

- `GET /api/apps/<id-or-urn>/proposed-grants` computes install proposals from
  installed apps plus a materialized candidate recipe, including consumers
  when a provider arrives later. Denied proposals remain denied.
- `GET /api/apps/wizard/grants` reads the canonical grant rows.
- Authenticated `POST /api/apps/<consumer>/share-grants` accepts
  `{"to":"wizard-claude","share":"claude-login","approve":true}`
  (`false` revokes). It writes the canonical file atomically, then takes the
  consumer down and force-recreates it; a plain Docker restart would retain the
  old mount namespace. Revoke remains durable if recreation fails.
- Canonical file: `<Runtipi dataDir>/state/wizard-grants.json`, schema 1.
  Other grant kinds and unknown top-level fields are retained. `apps.json`
  mirrors the rows: the owner-side registry rebuild reads this file, and the
  backend also updates an available `WIZARD_OS_HOME/apps.json` mirror.
- Missing folders are created with the provider service's numeric uid/gid;
  existing folder ownership is not changed. A provider with a nonnumeric user
  must use a numeric `user:` before sharing a newly managed folder.
- Absolute/traversing share paths, relative/traversing mount targets, malformed
  declarations, duplicate identities and symlinks at any source ancestor are
  refused. A missing granted provider fails closed.
- Consumer uninstall removes grants and its own app-data only. Provider
  uninstall detaches/recreates consumers before deleting provider files.
  The legacy Docker volume is never removed by uninstall.

## Claude upgrade and alongside compatibility

An existing pair of fixed-volume recipes **and the existing Docker volume**
qualifies for one `approved: install` share grant. Claude's old login is copied
into its `data/claude`; Hermes mounts that folder, never a second consumer-owned
credential copy. A remembered denial is not auto-approved on restart.

### Live box compatibility

Agent Service generated installed apps (including `agent-*`, `wizard_type: agent`
labels, and other ids) can already mount the exact fixed name
`wizard-ai-accounts-claude` without declaring `mounts`. Keep those volume
references, every service's mount path (including `/claude`) and flags intact.
Do not copy credentials into a consumer-owned folder or rewrite its runtime
configuration. This is safer for existing agents than silently changing their
mount topology or granting new authority. The exception is keyed by the exact
volume name, not by an agent id prefix or label. Other named volumes still migrate.

Before starting such an installed consumer, inspect the legacy volume and require
`Driver: local`, `Options.type: none`, a `bind` option, and `Options.device` equal
to the installed Claude provider's daemon-side `data/claude` folder. Discover
Claude across stores (including `core`), not only `wizardapps`. A provider's
completion marker alone is **not** proof that the alias is safe. Missing or
mismatched aliases and daemon failures abort before container creation. The
provider itself is still rewritten to its app-owned bind; WAM never removes,
recreates or retargets the legacy alias, so running consumers keep their mount.

This is a retained-authority compatibility bridge, **not** a new share grant;
the share-grant revoke API does not detach an undeclared legacy alias. To manage
that consumer with owner grants, first update its recipe to remove the fixed
volume and declare the provider share. The existing Hermes grant-based shim
is unchanged, and new install candidates still fail the fixed-name rule.

`wam/claude_login.py` no longer writes a host-login override for a share-aware
Claude recipe. Its legacy alongside bridge is retained for existing owner
host-Hermes bindings; changing a live host's single-use refresh token location
is not done implicitly. That host compatibility binding needs an explicit
owner disconnection before moving the host itself to app-owned state.

## Verification and boundaries

- `tests/test_storage_rule15.py` covers WAM checker parity, registry grants and
  legacy-vs-share-aware alongside behavior.
- Backend `wizard-storage.test.ts` covers both mount syntaxes, all services,
  compile-time vs stock behavior, refusal paths, proposal/approve/revoke,
  provider-later, uninstall preservation, concurrent approvals, failure gates
  and the explicit old Claude/Hermes upgrade. Live-rollout regressions use a fake
  Docker runner to cover marker-first starts, alias adoption without hot copying,
  alias verification across stores and generated-consumer compile/pre-start
  preservation. These are isolated unit proofs, not a live-container rehearsal.
- Backup-manager regression tests assert migrated login bytes enter its archive
  input, including a named volume called `backups` (the old substring filter
  erroneously dropped it).
- `qa/storage-copy-proof.ts` runs production `migrate()` against a throwaway
  Docker volume. It verifies numeric uid/gid/mode, dotfiles, links, copy-once,
  nonempty refusal, interrupted-copy retry, actual tar archive bytes and
  retention of the old volume, then removes only its own QA objects.
  Require `WIZARD_STORAGE_DOCKER_PROOF=1`, `TMPDIR`, and an explicitly selected
  Docker daemon. Linux-side temporary binds are used so Unix uid/gid proof is
  not confused with macOS virtiofs ownership mapping.
- No owner Runtipi tree, installed app or live app container is part of this
  verification. End-to-end owner UI approval and a real boxed Claude/Hermes
  refresh-token upgrade still need an isolated integration rehearsal.

The supplied WAM `main` (`5ffbc28`) has no network-grants implementation. The
older draft `miguel/app-permissions`/PR #8 is deliberately not imported: it
still contains agent-default authority that the current spec explicitly
removed. This extension preserves its schema fields/other grant kinds, but
compatibility with a future network/key-grants implementation needs joint
integration testing; share grants must be excluded from network reconciliation.
