# Slice 4: scoped backup, links, and source builds

Run on the dedicated Linux/KVM worker, never an owner box:

```sh
STAGING="$PWD"
BASELINE_SHA=67141228f6f73516ab68ece1c4c72bf3f67aebe0
CANDIDATE_SHA=$(python3 -c 'import json; print(json.load(open("source-export.json"))["head_sha"])')
python3 "$STAGING/source/qa/privsep-slice4/run.py" \
  "$STAGING/evidence" "$STAGING/source" "$BASELINE_SHA" "$CANDIDATE_SHA"
```

The staging directory must be under `/srv/work/privsep/slice4/`. Export the exact
candidate tree into `source/` and write its full `head_sha` in the sibling
`source-export.json`. The evidence directory must not exist yet (one use only).
The runner takes four **positional** arguments, not `--root`/`--source` flags.
`--smoke` skips the full suite and is **not** a full acceptance result.
The normal run executes the full suite with JUnit and accepts exactly the known
`tests.test_chats_views::test_typescript_public_index_path_has_identical_seal`
failure; every other failure/error blocks acceptance. It boots a clean
candidate guest using the Slice 1 base/mirror/finally-stop machinery and installs
unchanged stock Hermes as a user with no Docker/sudo groups. It retains the
accepted main baseline status from Slice 3 rather than running main again.

## Guest checks

- Inherited API admissions, owner-token denial, source lifecycle, doctor,
  service identity, and real Docker/sudo/socket/Tailscale refusals.
- Agent-only thin-client backup run/list, own-repository restore, and undo.
  Read back both shared-file and installed-app-data canaries after restore/undo.
- Agent publish/unpublish requests reach `wizard-system` and Tailscale. Only the
  exact running+loopback admission error may be retried, for at most 90 seconds
  across both operations, with every wait recorded. Without guest tailnet login,
  record the actual state and no published link; this is not proof of a signed-in
  tailnet's publication behavior.
- Links, build, build ownership, Hermes, privilege denials and service UID are
  collected independently: even a command exception cannot hide later phases.
  Their individual exits/logs remain mandatory for the overall pass.
- Build and update an agent-owned tiny source package and fetch real loopback
  health before and after the source change. Check service ownership/modes and
  the agent's inability to modify the staged source/Compose/build directory.
- Refuse source symlinks to `/etc` and `/`, and a `/:/host` Compose mount.
- Own-account Hermes update check and Agent Service handoff of the agent-scoped
  App Manager URL/token, without changing stock Hermes.
- Repeat privilege refusals after all operations and prove the guest stopped.

## Development verification (not clean acceptance)

Disposable overlays of the stopped Slice 3 guest were used to debug the real
transport. Backup/list, repository restore/undo and app-data canaries passed;
private-link requests returned the honest disconnected-tailnet result from the
service account. Build and source update passed real HTTP health, hostile source
was refused, own-account Hermes check/handoff passed, and final no-Docker/sudo
checks passed. Every debug VM was stopped. These resumed overlays are **not**
a fresh clean-guest acceptance result; the staged normal job must still run.

The debug work found and fixed three integration defects: image display names
lost digest pins during backup preflight, restore policy lacked the box runtime
adapter needed to independently validate managed networks, and service umask
made image source unreadable to the image's non-root process. Private links now
derive targets from installed loopback ports rather than container/internal or
gateway-projected registry URLs. Focused regression tests cover these cases.

The staged job's `job.json`, source archive identity, JUnit, stdout logs and final
`evidence/receipt.json` are the authoritative provenance/results. No deployment,
merge, signed-in tailnet publication, or full-suite pass is claimed here.

## Clean-VM acceptance at c113a91f (forum KVM, 28 Oct 2026): PASS

Evidence: forum `/srv/work/privsep/slice4/20261028T000818Z-staging/evidence` (receipt.json). As unprivileged `hermes`
through the agent-scoped client: backup, restore from own repository, undo (apps back to prior running state);
link publish/unpublish reached the `wizard-system` operator path (state NeedsLogin: no tailnet in guest, private URL
unproven); build from a hermes-owned source and update to 0.2.0 healthy, build directory not agent-writable;
symlink and `/:/host` sources refused; `hermes update --check` up to date; final sudo/Docker/socket/Tailscale denials
pass; doctor passed. Full pytest 1604, only the pre-existing `test_chats_views` seal failure. VM stopped.
