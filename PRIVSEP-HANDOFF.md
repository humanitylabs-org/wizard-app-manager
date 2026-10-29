# Agent privilege separation: audit and test handoff

**Owners while James travels:** Miguel (audit) and Oscar (testing). **Status:** built and passing on clean VMs, on
do-not-merge branches. **Merge rule:** nothing merges to `main`, ships in a release, or touches James's box until James
confirms. Open findings as PR comments or issues; push fixes to the same branches.

## What it is and why

An external audit (David Gearhart, using Claude Code) found that installing Wizard OS made the AI agent
root-equivalent. The installer adds the Hermes user to the `docker` group, and Docker access is root. Prompt injection
into any agent could then own the whole server. While testing, we also found that `main` cannot complete an
alongside install for an unprivileged Hermes user on clean Debian 12 (`/run/user/1000/bus` is missing after host prep).

The goal is **one** install configuration, fresh or alongside, in which no AI agent is ever root-equivalent:

- **Root** runs only fixed, argument-checked helpers (`scripts/root-helpers/os-patch`, `server-access`).
- **`wizard-system`** (no model runs there) owns Docker, Runtipi and `wam serve`.
- **The agent** has no docker group, Docker socket, sudo or Tailscale operator rights. It calls named wam actions with
  an agent-scoped token: install, start/stop/restart, logs, backup/restore/undo, link publish, source build, and update.
- **Risky apps** (privileged mode, host namespaces, sockets, devices, added capabilities, mounts outside app data and
  owner files) need owner approval. wam verifies the approval itself through the tailscaled peer identity, matched to
  the node owner. The agent token is refused even with forged owner headers. Apps that are already installed are
  grandfathered once.
- **Developer mode** (Decision #97) gives James, Miguel and Jan an owner-granted "full server access" sudo window. It
  lasts at most 7 days, is logged, expires through sudo, and is revoked by `developer off`. Health shows a red row
  while it is on. It is alongside-only; fresh agent containers do not get it, and their owners keep SSH. It is the
  only way the agent ever gets sudo: there is no automatic migration grant.
- **Existing boxes** move over with an owner-run migration. The migration is journaled and resumable, has a full
  rollback, and refuses ambiguous layouts rather than guessing. The agent loses Docker at the end and gets no sudo.

Spec: wizard-os `spec/PRIVILEGE.md` and Decision #105 (PR humanitylabs-org/wizard-os#285).

## Where it lives

| Repo | Branch / PR | Contents |
|---|---|---|
| wizard-app-manager | `feature/agent-privilege-separation`, PR #282 | installer, wam, host policy, root helpers, migration, all QA |
| wizard-ux | PR #664 | approval cards, developer banner, red full-access row |
| wizard-agent-service | PR #232 | approval receipts |
| wizard-os | PR #285 | spec, decision, plan, repair-kit docs |

Key files in wam: `wam/host_policy.py`, `wam/privilege_migration.py`, `scripts/root-helpers/*`,
`scripts/prepare-privilege-migration.py`, `docs/PRIVILEGE_MIGRATION.md`, `docs/developer-mode.md`,
`tests/test_fresh_privilege_audit.py`, and `qa/privsep-slice{1..6}/` (runner + RECEIPT per slice).

## Security repair after the second audit (pending verification)

A follow-up audit found three gaps. Fixes are pushed to the same do-not-merge branches; **none is proven on a VM
yet**, and a second reviewer must check them before anyone calls them fixed.

1. **Manager credentials could reach apps, and recipes could ask the backend for host access directly.**
   - `app.env` now gets an allowlist of public base keys only (`APP_PUBLIC_ENV`), never a manager credential.
   - `app.env` is written 0600, owned by the manager. The app-data widening skips `app.env`, the app directory itself
     and symlinks.
   - `docker-compose` runs with a minimal environment, so recipes cannot interpolate manager credentials.
   - On upgrade, the backend deletes lines whose value is exactly a manager credential from existing `app.env`
     files. Nothing else in app data changes.
   - **Owner step after upgrade:** credentials an app already read cannot be un-read. Rotate the manager database,
     queue and session-signing secrets on every existing box with installed apps. The copy is older manager
     behaviour, not only this branch, so assume it happened until a box is checked.
   - **Final gate** (`packages/backend/src/modules/docker/wizard-host-guard.ts`): before any Compose command that can
     create or change a container, the backend reads Compose's own merged model of that exact command and refuses
     host access not listed for the app in `user-config/wizard-host-approvals.json`. Only `wam` writes that file
     (`wam/final_host_tokens.py`), for recipes its policy accepts; revoking clears it. `install.sh` runs
     `wam host-approvals sync` before restarting the manager so approved apps keep working. A missing file accepts
     nothing.
2. **Owner approvals could be laundered by script on the shell origin.**
   - The agent-service proxy now passes only JSON (and SSE on the events route) on every status, with a sandbox CSP.
   - Risk-raising owner actions (host access, data grants, isolation off, developer mode on or source change, server
     access, backup import) no longer run from the shell origin. They become a five-minute, single-use intent bound
     to the exact action and owner, confirmed on a **separate approval origin**: its own `tailscale serve` HTTPS
     port (default 8446) whose whole site is the manager's approval listener (127.0.0.1:7792).
   - Backing out (revoke, developer off, deny, isolation on, updates) stays one click.
   - **New setup step:** `wam approval-origin enable [--port N]` as the manager. Until it runs, risk-raising owner
     actions are refused with that command (fail closed). Approved apps keep running.
3. **Migration gave the agent a seven-day sudo window.** Removed, including on resume; resume also revokes a grant an
   earlier candidate left. The `server-access` helper now grants only while developer mode is on, and refuses a
   `migration` request.

Focused acceptance is staged on forum but not run (see the private handoff path held by Archimedes). It builds the
candidate backend image from source so an old image cannot pass for the fix.

## What is already proven (clean Debian 12 KVM guests)

1. Sudo removed from wam code paths. A strict audit test allows only the fixed helper.
2. Host policy and owner approval work. Grandfathering blocks 0 of the 32 apps on a real box.
3. A fresh alongside install gives `id hermes` = hermes + wizard-files only, kernel denials pass, services run as
   wizard-system, and the thin client works end to end.
4. Backup, restore and undo leave apps running. Source builds are refused for symlinks and `/:/host`. Agent update
   check works.
5. The developer grant works and expires. The agent cannot grant itself. (Proven before the repair above; the
   approval route changed, so re-prove it.)
6. A legacy box built from `main` migrates, rolls back and migrates again. All running apps come back with their data,
   grandfathered mounts keep working, and doctor passes. (Proven with the old seven-day migration grant; re-prove
   without it.)

Full suite: 1721 tests; the only failure, `test_chats_views::test_typescript_public_index_path_has_identical_seal`, also
fails on `main`. UI: 1344/1344, plus a browser matrix covering phone/desktop × dark/light.

## Not yet proven (the next steps)

1. **Audit (Miguel).** Read the spec, then `host_policy.py`, the root helpers, the migration state machine and the
   approval check. Try to find any path from the agent account to root: Docker, sudo, a writable file a root or
   wizard-system process later executes, an approval bypass, or a token leak. Where the tests disagree with the spec,
   the spec wins.
2. **Signed-in tailnet test (Oscar).** The VMs had no logged-in Tailscale, so two things are unproven: link
   publication resolving to a real private URL, and owner approval through the live tailscaled identity. On a spare
   Debian 12 box signed in to a test tailnet:
   ```
   curl -fsSL https://raw.githubusercontent.com/humanitylabs-org/wizard-app-manager/feature/agent-privilege-separation/get.sh -o get.sh
   sudo WAM_REF=feature/agent-privilege-separation WIZARD_MODE=alongside WIZARD_HERMES_USER=hermes \
        HERMES_HOME=/home/hermes/.hermes bash get.sh start
   ```
   Then check `id hermes` (no docker or sudo); as hermes, install an app and publish its link, and confirm the link opens
   from another tailnet device; install an app that wants a host mount and confirm it waits for approval, that the
   agent cannot approve it, and that the owner can approve it from a tailnet device. Also turn developer mode on and
   off, and confirm the grant expires.
3. **Migration on a real used box (Oscar).** Use a spare box running today's release, never James's. Follow
   `docs/PRIVILEGE_MIGRATION.md`, keeping a root SSH session open, then run rollback and migrate again. Report anything
   the migration refuses.
4. **James's box goes last.** `qa/privsep-slice6/JAMES-BOX-DRYRUN.md` lists three blockers to fix by hand first:
   `.wizard-os/alongside.json` is missing, some extra user services point at legacy paths, and the app-manager checkout
   is in a nondefault location.

## Gotchas

- `wam stop <app>` on `main` stops Runtipi globally and still exits 0. Use `wam app stop|start|restart <app>`.
- Heavy and VM tests run on Linux/amd64 hardware with KVM. The VM runners (`qa/privsep-slice*/run.py`) take an
  evidence dir, a source dir and a commit, and need about 30 to 60 minutes each.
- Report results as: pass/fail, the exact commit, and the evidence path or log.
