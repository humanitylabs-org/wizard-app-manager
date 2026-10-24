# `wam release`: an owner's own release (wizard-os#186)

An owner who wants shell changes that main does not have (or does not have yet) keeps them as commits on a branch
of a local wizard-ux checkout. `wam release` keeps that branch on top of the box's channel and serves it as the
box's shell. Stock boxes are unaffected until the owner runs `wam release init` **and** the first build succeeds.

```sh
wam release init --name "Ana's release"          # --channel latest (default) | stable
# commit your changes on the branch (default release/mine) in ~/.wizard-os/release/src, then:
wam release rebuild                               # build your branch on the base it already has
wam release update                                # merge the channel's newest shell under your changes, build, switch
wam release check                                 # just record what is new upstream
wam release status
wam release off                                   # remove the timer, put the channel's shell image back (branch kept)
```

`init` options: `--repo PATH` adopts an existing checkout instead of cloning; `--source URL` clones another fork;
`--dir` moves the work dir (default `~/.wizard-os/release/`, or `$WAM_RELEASE_DIR`); `--push REMOTE` pushes the
branch after each good build (off by default: owners may have no remote); `--resolver` picks the clash resolver;
`--test-cmd` the tests (default `node --test tests/*.test.mjs`); `--no-timer` skips the units.

## The channel

- **Latest**: the shell source's `main`.
- **Stable**: the wizard-ux commit the current stable release pins (`releases/<stable>.json` → `components.wizard-ux`).

## Timer and Update button

`init` installs user units: `wam-release.timer` (hourly) runs `wam release auto`, which follows the owner's single
updates choice (`~/.wizard-os/updates.json` `mode`): **Automatic** → `update`, **Ask me** → `check` only.
`wam-release-update.service` is what Settings → Update starts. The owner's own commits (`rebuild`) always build,
whatever the mode.

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
- Only committed work is tested and built, in a temporary worktree; a dirty checkout refuses.
- Tests run before anything is switched; a failing test or image build keeps the running build.
- The switch pins the shell's image in its Runtipi user-config override (`pull_policy: never`) and recreates it.
  If the shell does not come back serving this build within the wait, the override is restored byte for byte and
  the container recreated. The override from before the first switch is kept for `wam release off`.

Files (work dir): `status` (one line), `pending.json`, `built.json`, `building`, `lock`, `clash-notified`,
`logs/{tests,resolve,update}.log`, `backups/`. Config: `~/.wizard-os/my-release.json`.
