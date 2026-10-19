# Agents and wizard-app-manager

Two kinds of agent read this file:

- **Installing Wizard OS on a machine?** Go straight to [Installing Wizard OS apps on this machine](#installing-wizard-os-apps-on-this-machine-instructions-for-an-ai-agent) at the bottom.
- **Changing this repo?** Read "This repo" and the team rules first.

## This repo

The Wizard App Manager is [Runtipi](https://github.com/runtipi/runtipi) with one part replaced: its App Store reads the Wizard App Index. It is a thin fork (wizard-os DECISIONS #32).

- **Keep the diff from upstream Runtipi small.** Wizard behaviour goes in `wam`, the installer, or the `wizardapps` store module. For running the Runtipi dev server, see `AGENTS.runtipi.md`.
- **Checks:** `python -m pytest -q` and `node --test tests/runtipi-open.test.mjs` must pass. These are the same commands CI runs.
- **Installer changes:** `get.sh`, `install.sh` and `os.md` are what every new box runs, through `aiwizards.com/os.sh`. A change to the install path needs a run on a clean VM or box, and its result goes in Verified. That covers both a fresh install and an install alongside an existing Hermes.
- **Releases:** a release is a `release/X.Y.Z` PR that updates `releases/`. A box pulls a release only once it has merged.
- **Checker rules:** the `wizard.md` checker copy here must match wizard-os `tools/check_wizard_md.py` and the wizard-app-index rules. Change all three together.

<!-- wizard-os-team-rules v1 (19 Oct 2026). Same text in every Wizard OS repo; the canonical copy is humanitylabs-org/wizard-os AGENTS.md. Change it there first, then copy the block to every repo in one round of PRs. -->
## Wizard OS team rules (identical in every repo)

Wizard OS is built by James, Jan and Miguel together with their agents. Archimedes commits as `defleur-agents` and Oscar as `oscarhenrycollins`. Every repo belongs to Wizard OS as a whole, and no repo belongs to one person. Make the change that is best for the whole system, and build on each other's work.

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

- **Open the PR once your first commit is pushed.** Use a draft until it is ready: `gh pr create --base main --draft`.
- **Keep each PR to one change** that a reader can follow in a few minutes. Split unrelated fixes into separate PRs.
- **Title:** state the outcome in plain words ("Threads open instantly from the on-device cache"), rather than the mechanism.
- **Body:** fill in the PR template. Write it for someone opening it cold:
  1. **What and why:** what changes for the user or the system, and the problem it solves.
  2. **Verified:** the exact commands you ran and their results. Add runtime evidence (a live box, screenshots or a short recording) wherever behaviour changed. For a bug fix, show the new test failing before the fix and passing after it.
  3. **Not checked:** what you could not verify, and why.
  4. **Links:** related issues, the matching PRs in other repos, and the decision or spec this change follows.
- **If `main` moves while you work,** merge `origin/main` into your branch and re-run the checks.

### Merging

- **The author merges their own PR** once three things hold:
  - the required checks are green;
  - the Verified section reflects real results;
  - nothing in the PR needs a decision (see the next section).

  There is no wait for a human approval.
- **Use "Create a merge commit"**, then delete the branch.
- **After merging anything that publishes or deploys,** confirm that the workflow run on `main` succeeded.

### When a change needs a decision

Decide and merge yourself, except when the change:

- reverses a locked decision in wizard-os `docs/DECISIONS.md`;
- changes a contract in wizard-os `spec/` that other repos depend on;
- changes James's skins (Nebula, Grove, Tide, Ember, Signal) or the base shell look;
- deletes user data, rewrites history, or is otherwise hard to undo.

For those, open the PR or an issue, explain the choice in one paragraph, and assign it to the agent account of the person who needs to decide. GitHub's Assignee field is used only for this. Carry on with other work while you wait.

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

## Installing Wizard OS apps on this machine (instructions for an AI agent)

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
