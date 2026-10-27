# `wam release`: an owner's own release (wizard-os#186)

A **developer owner** who wants shell changes that main does not have (or does not have yet) keeps them as commits on a branch
of a local wizard-ux checkout. `wam release` keeps that branch on top of the box's channel and serves it as the
box's shell. Turn on **Settings → About & updates → Developer mode** first. It defaults off; a release file alone
never exposes My release, Home notices or an owner layer. Turning it off leaves the last working image in place
but hides developer surfaces and pauses release automation. Stock owners keep their existing update behaviour.
WAM itself is never built this way; this first implementation builds wizard-ux only.

```sh
wam release init --name "Ana's release" --channel main  # main | latest (default) | stable
# Push the adopted branch to origin. Register each agent change by its OPEN source PR:
wam release item proposed --pr https://github.com/your-org/wizard-ux/pull/123
# Only the owner, at an interactive terminal, can confirm mine (maximum five):
wam release item mine --sha FULL_SHA --title "My change"
wam release rebuild                               # build your branch on the base it already has
wam release update                                # merge the channel's newest shell under your changes, build, switch
wam release check                                 # just record what is new upstream
wam release status
wam release go --hold "A feature to keep waiting"     # exact title, full SHA or PR URL; omit to accept the batch
wam release off                                   # remove the timer, put the channel's shell image back (branch kept)
```

`init` options: `--repo PATH` adopts an existing checkout instead of cloning; `--source URL` clones another fork;
`--dir` moves the work dir (default `~/.wizard-os/release/`, or `$WAM_RELEASE_DIR`); `--push REMOTE` pushes the
branch to an additional remote after each good build. Origin is always required: input HEAD must already be pushed; `--resolver` picks the clash resolver;
`--test-cmd` the tests (default `node --test tests/*.test.mjs`); `--no-timer` skips the units.

## The channel

- **Main**: the shell source's `main` (the developer-owner base).
- **Latest**: the existing latest source track (`main`).
- **Stable**: the wizard-ux commit the current stable release pins (`releases/<stable>.json` → `components.wizard-ux`).

## Timer and Update button

`init` installs user units: `wam-release.timer` (hourly, persistent) runs `wam release auto`. Merged PRs labelled
`fix` apply on the next successful check (within 72 hours on an online, healthy box), even in Ask me mode.
`feature`, mixed fix/feature, direct and unlabelled commits wait in one staging image for the owner's go.
A blocked fix stays on the last build and reports the failure; safety checks are never waived to meet the deadline.

The integration merges upstream once, preserving both ancestries. Explicit policy revert commits hold features
back, so an independent fix after a feature can land without releasing it. Go reverses those holds. A dependency
or revert conflict fails closed rather than guessing. `go --hold` permits partial approval, with exact item titles,
PR links or full SHAs; an unknown/ambiguous hold is an error. A changed upstream or owner HEAD invalidates go.
Core-flow resolver changes require the exact staged result, never a newly generated resolution at promotion.
Staging is a tested image, not a public preview or a second live install; review/run it in an isolated environment.
The Review update action opens a context chat; it does not itself approve or install the batch.

`wam-release-update.service` is what Settings → Update starts. `rebuild` validates all registered owner changes.
Adopting `--repo PATH --branch EXISTING` never rewrites history. `item proposed --pr URL` records a PR head already
in the branch; all non-upstream commits must be covered by registered item heads. Proposed items require open PRs.
An item disappears when GitHub reports its PR merged, including squash merges. Closed, unmerged proposals block
building until corrected. Mine needs explicit interactive owner confirmation; agents have no `--owner` bypass.
GitHub/gh authentication or label lookup failure blocks release changes, rather than guessing.

## What the shell reads

`GET /_updates` (and `/api/updates`) on a release box returns the usual fields plus:

```json
"release": {"name": "Ana's release", "base": "2ebbb689", "build": "26b1422a", "behind": 3,
            "new": [{"sha": "abc12345", "title": "Newest on main"}], "building": false,
            "failed": null, "checked_at": "2026-10-24T05:00:00Z"}
```

`installed` = `build`, `latest` = the upstream commit last checked, `update_available` = `behind > 0` and not
building, `installing` = building. `failed` is the last status line when it starts with `FAIL` or `CONFLICT`.
POST `{"action": "apply"}` starts `wam-release-update.service` (or a detached `wam release update`); Automatic's
nightly installer run is a no-op on a release box (the release timer does it). Every build also writes
`web/my-release.json` into the image: `{name, base: {ref, label}, build, built_at, changes: [{sha, title, status, date}]}`.

## Safety rules

- One run at a time (a lock in the work dir); a `building` marker (with pid) while update/rebuild runs.
- Upstream is **merged, never rebased**: history is kept, a clash is solved once, no owner commit is dropped.
- A clash is resolved in a throwaway worktree by the resolver: `auto` (default) = the box's Hermes agent
  (`hermes -z`) when installed, else none; `none`; or any shell command (it gets `WAM_RELEASE_WORKTREE`,
  `WAM_RELEASE_OURS`, `WAM_RELEASE_THEIRS`, `WAM_RELEASE_FILES`, `WAM_RELEASE_MESSAGE`, and the prompt as its last
  argument). Its result counts only if the merge is committed, both parents are ancestors, nothing is left
  uncommitted, no conflict markers remain and every test passes. Otherwise the branch is untouched, the box keeps
  its build, the status says `CONFLICT`, and the owner gets **one** notification per upstream commit through the
  box's Notifications app (whatever provides `notifications` in `apps.json`; nothing is sent when none is installed).
- Only committed work already reachable from origin is tested and built, in a temporary worktree; a dirty checkout
  (including untracked files) refuses. Generated merge/hold results are published to origin candidate refs before building.
- Images inherit `org.opencontainers.image.revision` and `ai.wizards.source-revision` = the full built HEAD; the
  served manifest and built record also carry `source_revision`.
- Resolver merges are marked with Git notes (`wam-release-resolvers`) and listed in status/manifest. Resolutions
  touching chats, service worker, outbox or boot are staged, never automatically switched live.
- Tests run before anything is switched; a failing test or image build keeps the running build.
- The switch pins the shell's image in its Runtipi user-config override (`pull_policy: never`) and recreates it.
  If the shell does not come back serving this build within the wait, the override is restored byte for byte and
  the container recreated. The override from before the first switch is kept for `wam release off`.

Files (work dir): `status` (one line), `pending.json`, `built.json`, `building`, `lock`, `clash-notified`,
`logs/{tests,resolve,update}.log`, `backups/`, `items.json`, `holds.json`, `staging.json`, `resolvers.json`,
`policy-commits.json`. Config: `~/.wizard-os/my-release.json`; developer setting: `~/.wizard-os/developer.json`.
Status includes `staging`, `held`, `resolver_merges`; `/_updates` includes `developer_mode` and accepts an
owner-authenticated POST `{"developer_mode": true|false}` using the existing Store origin/owner checks.

## Acceptance

Unit regressions: `python -m pytest tests/test_release.py tests/test_release_policy.py`.
On the Linux test worker, `python tools/qa_release.py --ux CHECKOUT --out EVIDENCE` creates a throwaway clone of
wizard-ux, simulates PR metadata, builds real images and serves an isolated real container. It registers one
proposed and one owner-confirmed mine item on Main, verifies a fix after a feature lands while that feature waits,
checks image/container source labels, promotes on go, and proves a conflict preserves the exact last container.
Nothing initialises or installs on the owner's box. Review both stacked PRs and the worker evidence first.
