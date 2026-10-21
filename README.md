# Wizard App Manager

Installs, runs, updates and backs up the apps on a Wizard. It is [Runtipi](https://github.com/runtipi/runtipi) v4.10.2 (GPL-3.0) with one part replaced: **the App Store reads the [Wizard App Index](https://www.aiwizards.com/wizard-apps) live** instead of a cloned catalog. Nothing is downloaded until you open, install or update an app, so the store scales to any number of apps.

The interface here is temporary. It exists so the system can be seen and used now; the long-term interface is Wizard UX, part of WizardOS.

```
Wizard App Index (apps.aiwizards.com)     search, categories, logos, pinned commit per app
        │  browse / search: live API calls, nothing stored
        │  open / install / update: fetch that one repo at that commit, re-check it
        ▼
Wizard App Manager (this repo)            Runtipi's install, update, backup, networking, dashboard, settings
        │
        ▼
your apps (Docker), ports on 127.0.0.1    reach them through Tailscale or a Cloudflare Tunnel
```

## What changed from Runtipi

| Area | Change |
|---|---|
| App Store | `packages/backend/src/modules/app-stores/wizard-index-store.ts`: the `wizardapps` store searches the index and fetches single apps on demand (pinned commit, container-safety check, `tipi_version` raised on every new commit so Update always appears). Git stores still work as before. |
| Categories | Agents, Interfaces, Apps, System Apps (from `wizard.md` `category`) in the store and as shelves in My Apps |
| Ports | Wizard apps publish on `127.0.0.1` only; "Open port" no longer adds a second all-interface mapping when the app already maps its port |
| Look | Wizard UX styling built in (`packages/frontend/src/styles/wizard.css`), Wizard branding |
| Everything else | Unchanged: dashboard, install forms, updates, backups, logs, app networking, settings |

## One-line install

```
curl -fsSL aiwizards.com/os.sh | bash
```

Sets up Runtipi with the Wizard image, the `wam` command, and the default app in every parking spot (`wam bootstrap`).

**Private access defaults to Tailscale.** The installer installs its native host daemon and prints a sign-in link
for the owner to connect the box to their own tailnet. Existing Tailscale connections and settings are left alone.
If sign-in is deferred, installation continues with Tailscale preinstalled but unpaired. Install Tailscale on your
phone/laptop too and use the same account. This does not yet publish app URLs: apps stay on loopback until a private
proxy is configured. No Funnel, Tailscale SSH, subnet routes or exit node are enabled by this step.

Cloudflare Tunnel + Access remains an alternative: choose `WIZARD_PRIVATE_ACCESS=cloudflare` (or `none` to skip).
This skips Tailscale setup; it does not configure Cloudflare or remove an existing Tailscale connection.
Tailscale is a host integration for the `private-access` system slot, not an ordinary privileged store container.

**Private apps.** A default that isn't in the public index installs straight from its GitHub repo, using this box's
GitHub login (`$GITHUB_TOKEN`, `$GH_TOKEN` or `gh auth token`). The login needs access to the repo and the
`read:packages` scope for its image (`gh auth refresh -s read:packages`). Boxes without that access skip those spots
and install everything else. Today every default is public and in the index, so the one-line install needs no login.

## Install / switch an existing Runtipi box

```bash
git clone https://github.com/humanitylabs-org/wizard-app-manager ~/wizard-app-manager
cd ~/wizard-app-manager && ./install.sh      # installs the base if missing, runs the Wizard build, sets up `wam`
wam doctor                                    # every line should say PASS
```

Existing apps, app data and settings stay where they are.

## `wam`, the command line

Every `runtipi-cli` command works through `wam` (app ids may drop the `:store` suffix):
`wam app start|stop|update|reset|uninstall|backup|restore|list-backups|delete-backup <app>`, `wam app start-all|stop-all|available-updates`, `wam installed`, `wam appstore add|list|remove|update`, `wam start|stop|restart|prepare|update|version|debug|reset-password`.

Wizard additions:

| Command | What it does |
|---|---|
| `wam search [query]` | Search the index |
| `wam install <id> [--set KEY=VALUE]` | Install an app and the apps it `uses`; required passwords you don't pass are generated and saved 0600 in `~/.wizard-os/secrets/<id>.json` |
| `wam uninstall <id>` | Remove an app |
| `wam apps [--json]` | Installed apps with their MCP addresses; writes `~/.wizard-os/apps.json` + `skills/` for agents |
| `wam doctor [--fix]` | Check the whole chain |
| `wam updates [--ask\|--auto] [--channel stable\|latest] [--apply]` | Update check and the owner's choice. Ask me is the default; Automatic installs only promoted **stable** releases. `--channel latest` follows every release as it is cut (testers); stable is the default |
| `wam serve` | Box-only HTTP API on `127.0.0.1:7790` (the `wizard-app-manager-api` user service runs it) |

## Building

```bash
docker build --build-arg LOCAL=true --build-arg TIPI_VERSION=v4.10.2-wizard.N -t ghcr.io/humanitylabs-org/wizard-app-manager:v4.10.2-wizard.N .
```

Runtipi's original README and agent notes are kept as `README.runtipi.md` and `AGENTS.runtipi.md`. License: GPL-3.0 (inherited).
