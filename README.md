# wizard-app-manager

Installs Wizard apps from the [Wizard App Index](https://www.aiwizards.com/wizard-apps) onto **stock Runtipi**.

```
 Wizard App Index (apps.aiwizards.com)   search; id -> repo + pinned commit
        │  wam install transcriber
        ▼
 fetch that one repo at that commit, re-run the wizard.md check
        │
        ▼
 local store (~/.wizard-os/store): only this box's apps, served to Runtipi on its own network
        │  Runtipi installs it like any app-store app; main port published on 127.0.0.1 only
        ▼
 ~/.wizard-os/apps.json + skills/<id>/SKILL.md  ->  Agent Service / Wizard UX
```

Runtipi is a component. We don't fork it and we don't edit its files or database. Everything goes through its published API and its normal app-store format.

## Install (Ubuntu/Debian, x86_64 or arm64)

```bash
git clone https://github.com/humanitylabs-org/wizard-app-manager ~/wizard-app-manager
cd ~/wizard-app-manager && ./install.sh     # installs stock Runtipi if missing, then wam as a user service
wam doctor                                   # every line should say PASS
wam search                                   # what's in the index
wam install hermes-agent                     # dashboard password is generated, saved to ~/.wizard-os/secrets/
wam install transcriber
wam apps                                     # what's installed, with each app's MCP address
```

Or give your agent one line: *"Install the Wizard App Manager on this machine by following https://github.com/humanitylabs-org/wizard-app-manager/blob/main/AGENTS.md"*.

Apps answer on `127.0.0.1:<port>` only. Reach them from your devices through Tailscale or a Cloudflare Tunnel pointed at that address; nothing is published on the public interface.

## Commands

| Command | What it does |
|---|---|
| `wam <any runtipi-cli command>` | Passed straight to the stock `runtipi-cli`: `app start/stop/update/reset/uninstall/backup/restore/list-backups/delete-backup/start-all/stop-all/available-updates`, `appstore add/list/remove/update`, `installed`, `start`, `stop`, `restart`, `prepare`, `update`, `version`, `debug`, `reset-password`. App ids may drop the `:store` suffix. |
| `wam search [query]` | Search the index |
| `wam install <app> [--set KEY=VALUE]` | `<app>` is an id (`transcriber`), a repo (`owner/name`) or a local folder with `wizard.md`. Installs dependencies in `uses:` first, waits until running, rewrites `apps.json`. Required passwords you don't pass are generated and saved 0600 in `~/.wizard-os/secrets/<id>.json`. |
| `wam uninstall <id>` | Remove through Runtipi, rewrite `apps.json` |
| `wam apps [--json]` | What's installed (reads `apps.json`) |
| `wam refresh` | Re-register the local store with Runtipi and pull it |
| `wam doctor` | Check the whole chain |
| `wam skin on\|off` | Wizard UX look for the stock Runtipi dashboard. A small read-only proxy adds one stylesheet; Runtipi's files and image are unchanged, and `off` restores the stock look. |
| `wam serve` | HTTP API on `127.0.0.1:7790` (the user service runs this) |

HTTP API (for the Wizard UX Apps screen and the Agent Service): `GET /api/apps`, `GET /api/search?q=`, `GET /api/catalog`, `GET /api/doctor`; changes need `Authorization: Bearer $(cat ~/.wizard-os/api-token)`: `POST /api/install {"app": "...", "set": {...}}`, `POST /api/update {"app": "..."}`, `POST /api/refresh`, `DELETE /api/apps/<id>`.

## Security

- The API listens on `127.0.0.1` only; installs, updates and removals need the token in `~/.wizard-os/api-token` (0600).
- Installs pin to the exact commit the index listed and re-run the checker here, including the container-safety rule (no privileged, host network/PID, runtime socket, host system paths or dangerous capabilities).
- Updates are never automatic: `wam update` re-checks the new commit first.
- The local store's git server runs in a read-only container on Runtipi's network and refuses pushes; no host port.

## Where it sits

| Repo | Role | Owner |
|---|---|---|
| `wizard-os` | Architecture, rollout, contracts (wizard.md, apps.json, Agent Service API), style | shared |
| `wizard-ux` | The front end, including the Apps screen | James |
| `wizard-agent-service` | Proxy in front of the agent; reads `apps.json`, injects Wizard OS instructions | Jan |
| **`wizard-app-manager`** | **This repo. Installs from the index onto stock Runtipi; writes `apps.json`** | **Miguel** |
| `wizard-app-index` | The shared catalog at apps.aiwizards.com that this installs from | Miguel |
| `wizard-app-store` | Legacy hand-curated Runtipi store; replaced by one repo per app + the index | Miguel |
| one repo per app | `wizard.md` + Runtipi files + MCP + skill | app author |

## Configuration

`~/.wizard-os/app-manager.json` (all optional):

```json
{"runtipi_root": "/home/me/runtipi", "index": "https://apps.aiwizards.com", "store_name": "wizardapps", "port": 7790}
```

| Env var | Default | Meaning |
|---|---|---|
| `WIZARD_OS_HOME` | `~/.wizard-os` | `apps.json`, `skills/`, the local `store/`, `secrets/`, `api-token` |
| `RUNTIPI_ROOT` | `~/runtipi` | Runtipi install dir (reads `.env` for the API address and CLI secret) |
| `WAM_INDEX` | `https://apps.aiwizards.com` | Which index to install from (anyone can run one) |
| `WAM_PORT` / `WAM_BIND` | `7790` / `127.0.0.1` | HTTP API (keep it on loopback) |

Discovery (GitHub topic crawl, publishing, and later Nostr) happens once in the index, not on every box. `wam/nostr.py` stays here for the index's Nostr source and for re-verifying signed announcements at install time.

## How it talks to Runtipi

Runtipi already trusts a short-lived HS256 token signed with its own `JWT_SECRET` (`sub: cli`); that's how `runtipi-cli` works. `wam` mints the same token, so it needs no password and no fork. On a brand-new Runtipi with no account yet, `wam` creates the operator account once and stores the password in `~/.wizard-os/runtipi-operator.json` (mode 600). Calls used: `marketplace/create|all|pull`, `app-lifecycle/<urn>/install|uninstall|update`, `apps/installed`, `apps/<urn>`.

## apps.json (contract v1)

```json
{
  "schema": 1,
  "store": "wizardapps",
  "apps": [{
    "id": "hello-wizard", "name": "Hello Wizard", "type": "service", "version": "0.1.0",
    "status": "running", "system": false, "provides": ["diagnostics"], "uses": [], "needs": [],
    "summary": "...", "urn": "hello-wizard:wizardapps",
    "mcp": {"url": "http://127.0.0.1:8799/mcp", "internal": "http://hello-wizard:8799/mcp", "transport": "streamable-http"},
    "skill": "/home/me/.wizard-os/skills/hello-wizard/SKILL.md",
    "view": null
  }],
  "slots": {"diagnostics": {"default": "hello-wizard", "candidates": ["hello-wizard"]}}
}
```

The Agent Service reads this file (or `GET /api/apps`). It never needs to know about Runtipi.

## Making an app

Copy `templates/hello-wizard/`. It's the smallest complete app: `wizard.md`, stock Runtipi files, a stdlib MCP server and a skill. Check it with `python3 -m wam.wizardmd <dir>` or `wizard-os/tools/check_wizard_md.py <dir>`. Make the repo public, tag it `wizard-app`, and publish: `curl -X POST https://apps.aiwizards.com/v1/publish -H 'content-type: application/json' -d '{"repo":"owner/name"}'` (or copy `.github/workflows/publish.yml` from any listed app). Before publishing, `wam install ./my-app` installs it from the folder.

## Layout

```
wam/wizardmd.py    the checker (vendored from wizard-os; change it there first)
wam/index.py       Wizard App Index client (search, id -> repo + commit)
wam/store.py       fetch pinned commit, local Runtipi store, loopback ports
wam/store_server.py  read-only git smart-HTTP for the store (runs in the wizard-store container)
wam/runtipi.py     Runtipi API client (CLI token, first-run operator)
wam/registry.py    apps.json + skills
wam/cli.py         CLI + HTTP API
templates/hello-wizard/   the template app
tests/             pytest; no Docker needed
install.sh         Runtipi (if missing) + wam user service
```

## Tests

```bash
python3 -m pytest -q        # 27 tests, no Docker
```

The compiled output was also checked against `wizard-app-store`'s own suite (official Runtipi schema): 47/47.
