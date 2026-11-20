# Agents and wizard-app-manager

Two kinds of agent read this file:

- **Installing WizardOS on a machine?** Go straight to [Installing WizardOS apps on this machine](#installing-wizard-os-apps-on-this-machine-instructions-for-an-ai-agent) at the bottom.
- **Changing this repo?** Read [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) (layout, tests, required CI checks and how to reproduce them, browser checks, how a change reaches users), "This repo", the [Team workflow](#team-workflow) and the team rules.

## This repo

The Wizard App Manager is [Runtipi](https://github.com/runtipi/runtipi) with one part replaced: its App Store reads the Wizard App Index. It is a thin fork (wizard-os DECISIONS #32).

- **Keep the diff from upstream Runtipi small.** Wizard behaviour goes in `wam`, the installer, or the `wizardapps` store module. New non-Runtipi code goes in another repo: see [What belongs in this repo](#what-belongs-in-this-repo-the-runtipi-boundary). For running the Runtipi dev server, see `AGENTS.runtipi.md`.
- **Checks:** `python -m pytest -q` and `node --test tests/runtipi-open.test.mjs tests/connections-map.test.mjs` must pass. These are the same commands CI runs.
- **Installer changes:** `get.sh`, `install.sh` and `os.md` are what every new box runs, through `aiwizards.com/os.sh`. A change to the install path needs a run on a clean VM or box, and its result goes in Verified. That covers both a fresh install and an install alongside an existing Hermes.
- **Releases:** a release is a `release/X.Y.Z` PR that updates `releases/`. A box pulls a release only once it has merged.
- **Checker rules:** the `wizard.md` checker copy here must match wizard-os `tools/check_wizard_md.py` and the wizard-app-index rules. Change all three together.

## What belongs in this repo (the Runtipi boundary)

This repo is a GPL fork of Runtipi. Every non-Runtipi piece mixed into it makes the next upstream Runtipi merge harder, so the rule is: **the fork stays close to Runtipi, and new non-Runtipi code goes elsewhere.** No code is being moved now; this section stops the pile from growing and makes a later split easy (WAM#851).

**Belongs here:** Runtipi itself, plus the thin Wizard layer that makes it install Wizard apps:

- the `wizardapps` store and other small Runtipi patches under `packages/` (app index, on-demand fetch, ports, queue timeouts, compose rules before an app starts);
- `wizard.md` checks (the checker copy kept in step with wizard-os and wizard-app-index);
- `apps.json` / the app registry, and the other files `wam` writes for installed apps;
- `wam/` (install, update, backup, restore and moves of apps), with its tests and docs.

**Does not belong here:** new OS features, new UI, new skills or agent tooling. Put them in:

- `wizard-os`: installer, release and OS-level behaviour, contracts in `spec/`;
- `wizard-ux`: anything the owner sees in the shell;
- `wizard-agent-service`: agent skills, agent tooling, anything the agent runs;
- its own repo: anything else that stands on its own (an app, a CLI, a template).

If a change needs a new piece here that is not Runtipi or the thin layer above, say why in the PR and add the piece to the list below.

### Current non-Runtipi folders and where they would go

Measured on 20 Nov 2026 against upstream `v4.12.0`. If upstream merges start to hurt, move these first; the target is where each would go.

| Folder / file | Size | What it is | Would move to |
|---|---|---|---|
| `wam/` | 106 files, ~37k lines | `wam` CLI: app install/update/backup/restore, registry, release pins | stays (the thin Wizard layer); if it keeps growing, its own Python package repo that this repo installs |
| `get.sh`, `install.sh`, `os.md`, `scripts/` (11 added files, e.g. `setup-private-access.sh`, `prepare-named-user.sh`) | ~2.9k lines | the WizardOS installer behind `aiwizards.com/os.sh` | wizard-os (or a `wizard-installer` repo) |
| `wizard-cli/` | 5 files, ~2.4k lines | the `wizard` CLI (a copy of wizard-os `tools/`, see `SOURCE.json`) | wizard-os `tools/` (its upstream) |
| `wizard-views/` | 10 files, ~5k lines | App Manager views loaded by the shell | wizard-ux |
| `system-skills/` (`wizard-builder`, `wizard-guide`) | 9 files, ~0.5k lines | skills shipped to every install | wizard-agent-service |
| `templates/` (`hello-wizard`, `list`) | 32 files, ~3.5k lines | app templates for `wam new` | a template repo (e.g. `hello-wizard`); `wam new` would fetch it |
| `releases/` | 340 files | mirror of wizard-os `releases/` for boxes | stays a byte-identical mirror, or boxes read wizard-os directly |
| `tools/` | 6 files | `mirror_defaults.py`, `install_crane.py` (stay, used by `wam`/CI); `sync_wizard_style.py`, `wizard_icons.py` (style: wizard-ux / wizard-os `style/`); `side_branches.py` (wizard-os tools) | as listed |
| `qa/`, `tests/` (Python and node), `docs/` (Wizard docs) | ~46k lines | checks and docs for the pieces above | move with the code they test or describe |
| `AGENTS.md`, `CLAUDE.md`, `THIRD_PARTY_NOTICES.md`, `.gitleaks.toml`, `pyproject.toml` | small | repo plumbing | stay |

Upstream's own `README.md` and `AGENTS.md` are kept as `README.runtipi.md` and `AGENTS.runtipi.md`. Upstream's `cli/`, `screenshots/`, `.internal/` and `.gitmodules` are not in the fork.

### Upstream base and divergence

- **Base:** Runtipi `v4.12.0` (upgraded from 4.10.2 in WAM#835; how to move to a new release: `docs/DEVELOPMENT.md`). This repo shares no git history with upstream, so compare against the tag: `git fetch --depth=1 https://github.com/runtipi/runtipi.git tag v4.12.0` then `git diff --shortstat FETCH_HEAD HEAD -- packages`.
- **Runtipi code itself (`packages/`), 20 Nov 2026:** 109 files differ, +6,521 / -138 lines. Backend 106 files (34 new files, mostly in `modules/docker`, `modules/app-stores`, `modules/app-lifecycle`, `modules/queue`), frontend 2 files, common 1 file. This is the part that makes upstream merges hard: keep it small.
- **Whole repo:** 969 files differ, +162k / -1.8k lines; almost all of that is the non-Runtipi folders above, which upstream never touches.

## Portable machine configuration

Machine-specific paths and hosts come from env vars: `WIZARD_WORK_ROOT`,
`WIZARD_BUILD_HOST`, `WIZARD_ORIGIN`, `QA_PLAYWRIGHT`, and `HERMES_HOME`
(`WIZARD_HEAVY_ROOT` where the shared heavy lane is used). Never commit personal
paths or tailnet hostnames; the tracked-file portable-paths guard in the normal
test/CI command enforces this. Historical evidence/receipts are explicitly exempt.

## Team workflow

Agreed 5 Nov 2026 and being rolled out by Miguel; tracking issue: [wizard-ux#887](https://github.com/humanitylabs-org/wizard-ux/issues/887). This is agreed policy, not a claim that every label, switch or release job already enforces it. Where it disagrees with the older "WizardOS team rules" block below, **this section wins**. Void from that block: an author merging their own PR (including "fix" tier PRs), `needs: ... review` gating, `preview only` and developer-mode branches on a box. Tier labels and title prefixes may still describe a change but give no merge rights. Branch naming, PR body, cross-repo order and safety from that block still apply.

- **Humans file issues, agents write PRs.** Every PR links an issue. If none exists, the agent files one from the human's request.
- **Two issue kinds.** *Goals* are big and closable with a done-list; only humans open and close them; there is no permanent catch-all Goal. *Bugs* are small: a core-team human's bug is accepted; outside reports and agent-drafted issues wait for a human yes. Bugs need no parent Goal.
- **Priority:** labels P1–P3 (older `priority: 1/2/3` labels are the same levels). Agents take P1 first. Triage is weekly and async.
- **`main` is the only truth.** Everyone's box runs plain `main` (**Latest**). Users get a **Stable** release cut from approved work after a soak. A breaking or data-loss fix is labelled `hotfix` and skips the soak once tests pass.
- **New features ship behind off-by-default switches** (the existing Look switches). A "works" from a human who did not build the feature makes it the default and removes the switch. A big redesign that cannot be switched gets a temporary preview link, never a device branch.
- **Review:** a non-author agent reviews "is it safe and sound" and merges the same day. **Authors never merge their own PRs.** Never change branch protection, settings, boxes or deployments as part of a change.
- A single company implementer agent is being trialled; until then the existing team agents implement under these rules. Put knowledge in the repo, not in an agent's memory.


<!-- wizard-os-team-rules v4 (24 Oct 2026). Same text in every WizardOS repo; the canonical copy is humanitylabs-org/wizard-os AGENTS.md. Change it there first, then copy the block to every repo in one round of PRs. -->
## WizardOS team rules (identical in every repo)

WizardOS is built by James, Jan and Miguel together with their agents. Archimedes commits as `defleur-agents` and Oscar as `oscarhenrycollins`. Every repo belongs to WizardOS as a whole, and no repo belongs to one person. Make the change that is best for the whole system, and build on each other's work.

### Branches

- **`main` is the trunk in every repo.** It is what runs live and what releases are built from. `main` changes only through a merged pull request. Nobody pushes, force-pushes or rewrites `main` directly.
- **Start every change from a fresh `main`:**
  ```sh
  git fetch origin
  git switch -c fix/short-purpose origin/main
  ```
- **Name branches by purpose.** Use one of these prefixes:
  - `feature/`: new behaviour.
  - `fix/`: a bug fix.
  - `docs/`: documentation only.
  - `spec/`: a contract or decision change, in wizard-os.
  - `release/`: a version bump or release snapshot.
  - `integration/`: combines several branches for testing together.
  - `evidence/`: test evidence only.

  Write the rest in lowercase words joined by hyphens. A date suffix like `-20261019` is fine. People's names stay out of branch names.
- **Push after every commit:** `git push -u origin <branch>`. Work that exists only on your machine is invisible to the rest of the team.
- **One branch per change.** Leave other people's branches as they are. To build on someone's branch, branch from it and open your own PR.
- **Old branches are history.** Only `main` and the open PRs are current.

### Pull requests

- **Say what kind of change it is.** Every PR gets exactly one tier, shown both as a label and as the start of its title. This lets anyone triage a list of PRs at a glance.

  | Tier | Label | Title starts with | Means |
  |---|---|---|---|
  | Bug fix | `tier: recommended fix` | `Recommended fix:` | Something is broken and this makes it work. It must land. |
  | Improvement | `tier: strong suggestion` | `Strong suggestion:` | A clearly better behaviour or a new capability. Feel free to drop it. |
  | Personal preference | `tier: opinionated` | `Opinionated — feel free to drop:` | Taste: look, layout, gestures, wording, or how you like to work. In the shell it belongs in your own skin (below). |
  | Maintenance | `tier: maintenance` | `Maintenance:` | Docs, releases, CI, tests or refactors with no change in behaviour. |

  Optional labels:
  - `priority: 1` (review first), `priority: 2` (review next) and `priority: 3` (when there is time).
  - `needs: james review`: the PR waits for James to accept it or drop it.
  - `preview only`: an experiment that must not be merged.
  - `from: <where it already runs>`, for example `from: miguel test port`: the change is already in daily use there. A box in developer mode (below) uses `from: <owner> dev build`.

  Use the same tiers for issues. **Add the label when you open the PR** (`gh pr create --label "tier: ..."`); a PR without one shows as "No tier" on the board below. Agents label every PR they open, and add the missing label when they find one of their own without it.
- **Open the PR once your first commit is pushed.** Use a draft until it is ready: `gh pr create --base main --draft`.
- **Keep each PR to one change** that a reader can follow in a few minutes. Split unrelated fixes into separate PRs.
- **Title:** after the tier prefix, state the outcome in plain words ("Recommended fix: threads open instantly from the on-device cache"), rather than the mechanism.
- **Body:** fill in the PR template. Write it for someone opening it cold:
  1. **What and why:** what changes for the user or the system, and the problem it solves.
  2. **Verified:** the exact commands you ran and their results. Add runtime evidence (a live box, screenshots or a short recording) wherever behaviour changed. For a bug fix, show the new test failing before the fix and passing after it.
  3. **Not checked:** what you could not verify, and why.
  4. **Links:** related issues, the matching PRs in other repos, and the decision or spec this change follows.
- **If `main` moves while you work,** merge `origin/main` into your branch and re-run the checks.

### Two routes for every change

Every change reaches `main` one of two ways: **propose a fix** (`tier: recommended fix`) or **propose a feature** (any other tier). There is no third "my build" lane. Owners who only want Wizard to look or behave differently use their skin, settings or apps. People who change Wizard itself can run their branch first on a box in **developer mode**, which follows a named branch or fork instead of `latest`/`stable` (DECISIONS #97, wizard-os `releases/README.md`). That box is a test bench; the change still lands as a fix or feature PR.

### Tracking

**The [WizardOS · PR pipeline](https://github.com/orgs/humanitylabs-org/projects/1) board is the source of truth for where every PR is**, across all repos: Draft → In review → Merged → On Latest → On Stable, or Dropped. wizard-os `tools/pr_board.py` syncs it every 15 minutes from the PRs and the release pointers, so nobody moves cards by hand; keep the tier label right and the board stays right. Check it before asking someone where a change is, and link it in status updates instead of listing PRs.

### Merging

DECISIONS #96 (24 Oct 2026). `main` keeps **0 required approvals** and non-strict status checks, so process never blocks work.

- **Humans are the only admins:** James (`jdefleur`), Jan (`JanMesaric`) and Miguel (`mistermizel`). Agent accounts (`defleur-agents`, `oscarhenrycollins`) hold Write and sit on `main`'s review-bypass list; they never change branch rules, collaborators or repository settings.
- **Fixes ship immediately.** A `tier: recommended fix` PR that is not on the high-risk list below merges by its author, human or agent, without waiting for another review, once all of these hold:
  - a regression test that fails on `main` before the fix and passes after it;
  - the required checks are green;
  - every review thread is resolved (Codex or Agent review findings fixed or answered).

  The release carrying it can go to stable the same day (DECISIONS #82).
- **Everything else needs a review by someone other than its author**, and that reviewer may be an agent (the Agent review workflow or another collaborator's agent). This covers features, maintenance, taste and defaults, and the **high-risk list, fixes included**: security or privacy boundaries, host powers or credentials, migrations or anything that deletes or rewrites owner data, and a release that reaches every install. A high-risk fix still ships the same day; it just gets that review first. Once the review, the required checks and resolved conversations hold, the author's agent merges; no human has to visit GitHub to click merge.
- **Every PR gets an agent review.** The **Agent review** workflow (`humanitylabs-org/wizard-ux` `tools/pr-agent-review.mjs`) posts one review on each push to a ready PR. It flags taste changed for everyone instead of in your own skin, likely breaking changes, secrets and personal paths or private hosts, and `docs/COMPONENTS.md` reuse problems. Fix every 🔴 before merging; each 🟡 is something the reviewer checks.
- **The Verified section must reflect real results** before anything merges.
- **What each tier means for merging:**
  - **Recommended fixes** merge on the fix conditions above.
  - **Strong suggestions and maintenance** merge after a non-author review, unless they change something James sees or uses every day. In that case add `needs: james review` and wait.
  - **Opinionated changes** to the shell's look or UX go into your own skin: wizard-ux `web/skins/<yours>/` plus your `SKINS` entry (see wizard-ux `web/skins/README.md`). Never add a Settings toggle or core code for one person's taste.
  - **Making something the default for everyone** (any opinionated change outside your own skin) carries `needs: james review` and waits for James. Bring it once you have used it in your skin, with a preview he can try.
  - **`preview only`** PRs never merge.
- **Use "Create a merge commit"**, then delete the branch.
- **After merging anything that publishes or deploys,** confirm that the workflow run on `main` succeeded.

### When a change needs a decision

Decide and merge yourself, except when the change:

- reverses a locked decision in wizard-os `docs/DECISIONS.md`;
- changes a contract in wizard-os `spec/` that other repos depend on;
- changes James's skins (Nebula, Grove, Tide, Ember, Signal) or the base shell look;
- deletes user data, rewrites history, or is otherwise hard to undo.

For those, open the PR or an issue and explain the choice in one paragraph. Then:

- **If James decides:** add `needs: james review`.
- **If another agent's side has to act:** assign that agent's account. GitHub's Assignee field is used only for this.

Carry on with other work while you wait.

### Changes that span repos

1. Change the contract first, with a `spec/` PR in wizard-os.
2. Land the code PRs in dependency order: the service before the UI that calls it.
3. Keep each step backward compatible, so `main` works in every repo at every point.
4. Link the PRs to each other.

### Releases

Anything shipped, tagged or handed to a tester is built only from commits already on GitHub. Release notes name the repo, branch and commit.

### Safety

- **Keep secrets and private data out of commits.** That covers keys, tokens, passwords, `.env` files, personal data, real conversation content, owner hostnames and home-directory paths. Put these in config or environment variables. Private repos are still visible to every org member.
- **Hermes, the agent harness, stays stock.** Wizard behaviour goes in the Agent Service, the App Manager, Wizard UX or an app.
- **Run this repo's checks before opening a PR.** They are listed under "This repo" in this file.

### Where work comes from

Pick up work from the open issues in this repo and from wizard-os `docs/WORK_OUTSTANDING.md`. For how the pieces fit together and what is already decided, read wizard-os `docs/ARCHITECTURE.md` and `docs/DECISIONS.md`.
<!-- /wizard-os-team-rules -->

## Installing WizardOS apps on this machine (instructions for an AI agent)

You are installing the Wizard App Manager. It uses stock Runtipi to run apps. Do these steps in order and report each result.

1. **Check the machine.** Ubuntu/Debian, `x86_64` or `aarch64`, 2 GB+ free RAM, `git`, `python3` (3.10+), `python3-yaml`. Install missing packages with apt.
2. **Install Runtipi if it isn't already** (`~/runtipi/.env` missing): `curl -fsSL https://setup.runtipi.io | sudo bash`. Wait until `http://<INTERNAL_IP>/api` answers.
3. **Clone this repo** to `~/wizard-app-manager` and run `./install.sh`.
4. **Verify:** `wam doctor` must show only PASS lines.
5. **Prove the chain:** `wam install transcriber`, then call its MCP at the `mcp.url` shown in `wam apps --json` (`tools/list` should list `transcribe` and `transcriber_status`).
6. **Install what the owner asks for** with `wam search` and `wam install <id>`. Generated passwords are in `~/.wizard-os/secrets/<id>.json`; tell the owner where, never print them.
7. **Report** the Runtipi address, `wam doctor` output, and the installed apps.

Rules:
- Never edit Runtipi's files, database or app folders. Use `wam` or Runtipi's own UI.
- Never print secrets from `~/runtipi/.env` or `~/.wizard-os/runtipi-operator.json`.
- If the user wants an app that isn't listed, open `https://apps.aiwizards.com/v1/apps/<owner>/<repo>`: `last_check.failures` says which wizard.md rule failed.
- Apps answer on `127.0.0.1` only. To reach them from a phone, use Tailscale or a Cloudflare Tunnel to that address; never publish ports on the public interface.
