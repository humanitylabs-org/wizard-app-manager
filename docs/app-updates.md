# Automatic app updates

**Problem.** A WizardOS release moves only the system apps it pins. Every other app installed from the Wizard App
Index stayed on the version it was installed at until someone ran `wam app update <id>` by hand. A publisher's fix
reached new installs only; existing boxes (including the publisher's own) kept the old version.

**What every box does now** (`wam/app_updates.py`):

1. **Check.** Runtipi's own update check: the index store re-reads each installed app's listing, and a new commit is
   always a newer `tipi_version`, so a publisher never has to remember to bump it.
2. **Update, one app at a time,** through the normal `app_update` path: Runtipi backup of the app's data, recipe and
   settings first, the same owner gate for new host access, networks reconciled.
3. **Verify.** The app is back in the state it was in (running or stopped), its version moved, no container restarts
   and no fewer containers run than before (after a 20 s settle), and its declared `health` URL or MCP endpoint
   answers within 90 s. That last check applies only when the endpoint answered just before the update: an app whose
   endpoint refuses the loopback probe anyway (e.g. a host check answering 403) is judged by its containers.
4. **Roll back** anything that fails from the backup the update just made. If the update never applied (Runtipi kept
   the old version), nothing is restored. A failed version is skipped for 7 days or until a newer version ships.
5. **Tell the owner** in one Notifications line: what was updated, what was kept back and why.
6. **Tell the team** (feature switch `oscar/update-bug-reports`, off by default, testers only; `wam/update_reports.py`):
   each failed or rolled-back app files a bug labelled `bug` in the repo the app was installed from, or comments on
   the open bug with the same failure (app, target version, failing step). It says box (short host name), app,
   from/to version, failing step and the error, with home folders and tailnet names scrubbed. It writes with this
   box's GitHub login (`private.token()`); without one nothing is filed. When it was filed, the owner's notice says
   the team was told instead of "Ask your Wizard to look at it."

**When.** `wizard-app-updates.timer` (systemd user timer, nightly 04:30 + up to 1 h, persistent), skipped while a
chat run is active or a WizardOS update is installing. The installer sets the timer up on every run
(`wam updates --ensure-timer` also ensures this one).

**Owner choices** (saved in `~/.wizard-os/app-updates.json`, separate from the WizardOS update mode):

| | |
|---|---|
| `wam app-updates` | what is due and what is skipped, and why |
| `wam app-updates --apply [ids]` | update now (`--dry-run` lists only); naming an app overrides its hold or past failure |
| `wam app-updates --ask` / `--auto` | turn nightly app updates off / on (Automatic is the default) |
| `wam app-updates --hold <id>` / `--unhold <id>` | keep one app where it is |
| `GET/POST /api/app-updates` | the same for the App Manager UI: `{"mode"}`, `{"hold"}`, `{"unhold"}`, `{"action": "apply"}` (runs in the background) |

**Never moved here:** the shell (`wizard-ux`), the Agent Service, Hermes and apps the WizardOS release pins (they
move with the release), apps prepared from an exact owner-local selection (`.wizard-selection`), and versions the owner
ignored in the App Manager.

**For publishers.** Merge to `main`, let the repo's publish workflow (or `POST /v1/publish`) list the new commit,
and every box picks it up the next night. Apps must run on every Wizard: feature-detect newer shell capabilities
(`wam check` warns on unguarded `WizardApp.nav.*` / `WizardApp.chat.*` calls) so one release fits old and new shells.
