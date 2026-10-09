# wizard-app-manager

The Wizard OS layer that turns GitHub repos into installed Wizard apps, using **stock Runtipi** to run them.

```
 app repos (tagged wizard-app, root wizard.md)
        │  wam refresh: find → check → compile
        ▼
 compiled store (a normal Runtipi app store, served as git by wam)
        │  Runtipi pulls it like any other store
        ▼
 stock Runtipi ── installs/starts/stops containers
        │  wam install <id> calls Runtipi's own API
        ▼
 ~/.wizard-os/apps.json + ~/.wizard-os/skills/<id>/SKILL.md
        │
        ├─ Wizard Agent Service: connects each app's MCP + skill, tells the agent what's installed
        └─ Wizard UX Apps screen and `wizard apps list --json`: show the same list
```

Runtipi is a component. We don't fork it and we don't edit its files or database. Everything goes through its published API and its normal app-store format, so Runtipi upgrades stay free.

**Status:** reference implementation, 9 Oct 2026. It runs on the same machine as Hermes, the Agent Service and the UX, next to Runtipi; there is no extra VM. The first end-to-end test used a throwaway clean Ubuntu machine (a disposable arm64 box, since deleted) so it would prove a fresh install: refresh → Runtipi pulled the store → `wam install hello-wizard` → container running → `apps.json` written → agent-side MCP `tools/list` and `tools/call` answered. On a Mac, the whole Wizard OS stack (this included) runs inside the one Try Wizard VM. Owner from here: Miguel.

**Dogfood on an existing Hermes host:** `sudo bash scripts/dogfood-host-setup.sh <user>` once (Docker, stock Runtipi, and a firewall rule that keeps Runtipi's ports off the public interface), then `./install.sh` and `wam doctor` as the user.

## Where it sits

| Repo | Role | Owner |
|---|---|---|
| `wizard-os` | Architecture, rollout, contracts (wizard.md, apps.json, Agent Service API), style | shared |
| `wizard-ux` | The front end, including the Apps screen | James |
| `wizard-agent-service` | Proxy in front of the agent; reads `apps.json`, injects Wizard OS instructions | Jan |
| **`wizard-app-manager`** | **This repo. Finds, checks, compiles, installs; writes `apps.json`** | **Miguel** |
| `wizard-app-store` | Miguel's hand-curated Runtipi store; becomes a source this compiles | Miguel |
| one repo per app | `wizard.md` + Runtipi files + MCP + skill | app author |

## The one-prompt install

Give a fresh agent this:

> Install Runtipi and the Wizard App Manager on this machine by following https://github.com/humanitylabs-org/wizard-app-manager/blob/main/AGENTS.md

Or by hand on Ubuntu/Debian:

```bash
git clone https://github.com/humanitylabs-org/wizard-app-manager ~/wizard-app-manager
cd ~/wizard-app-manager && ./install.sh          # installs Runtipi if missing, then wam as a user service
wam doctor
wam install hello-wizard
```

## Commands

| Command | What it does |
|---|---|
| `wam refresh` | Find app repos, run the wizard.md checker, compile the store, tell Runtipi to pull it |
| `wam catalog [--json]` | What can be installed, and what was skipped and why |
| `wam install <id>` | Install through Runtipi (dependencies in `uses:` first), wait until running, rewrite `apps.json` |
| `wam uninstall <id>` | Remove through Runtipi, rewrite `apps.json` |
| `wam apps [--json]` | What's installed (reads `apps.json`) |
| `wam bootstrap` | Refresh, then install every `system: true` app |
| `wam doctor` | Check the whole chain |
| `wam serve` | HTTP API on :7790 and the compiled store over git smart-HTTP |

HTTP API (same data, for the UX and Agent Service): `GET /api/catalog`, `GET /api/apps`, `GET /api/doctor`, `POST /api/refresh`, `POST /api/apps/<id>/install`, `DELETE /api/apps/<id>`.

## Configuration

`~/.wizard-os/app-manager.json` (all optional):

```json
{
  "runtipi_root": "/home/me/runtipi",
  "store_name": "wizard",
  "port": 7790,
  "sources": [
    {"topic": "wizard-app"},
    {"git": "https://github.com/humanitylabs-org/wizard-app-store", "glob": "apps/*"},
    {"local": "/home/me/dev/my-app"},
    {"nostr": "wizard-os-app", "relays": ["wss://relay.ngit.dev"], "authors": ["npub1…"]}
  ]
}
```

Default sources are the GitHub topics `wizard-app` and `wizard-ai`. A topic only makes a repo **found**. Only a passing `wizard.md` gets it **listed**.

**Nostr source (off by default).** `WAM_NOSTR=1` or `"nostr": true` adds a source that reads NIP-34 repository announcements (kind 30617) tagged `t=wizard-os-app` from `nostr_relays` (default: relay.ngit.dev, gitnostr.com, relay.damus.io, nos.lol). Every event's signature is verified (stdlib BIP-340, no new dependency). If the app's `wizard.md` declares `publisher: npub1…`, the announcement must be signed by that key or list it as a maintainer; otherwise the app is skipped. A repo found on both GitHub and Nostr is listed once. `authors` restricts a source to known publishers. Each catalog row records `commit`, `found_via`, `publisher` and the announcement id. Why and when this turns on: `wizard-os/docs/ROLLOUT.md`, "Nostr path".

| Env var | Default | Meaning |
|---|---|---|
| `WIZARD_OS_HOME` | `~/.wizard-os` | Where `apps.json`, `skills/`, the compiled `store/` and config live |
| `RUNTIPI_ROOT` | `~/runtipi` | Runtipi install dir (reads `.env` for the API address and CLI secret) |
| `WAM_PORT` / `WAM_BIND` | `7790` / `0.0.0.0` | HTTP API + store |
| `GITHUB_TOKEN` | none | Raises the topic-search rate limit from 60/hour |
| `WAM_NOSTR` | off | `1` adds the Nostr catalog source |

## How it talks to Runtipi

Runtipi already trusts a short-lived HS256 token signed with its own `JWT_SECRET` (`sub: cli`); that's how `runtipi-cli` works. `wam` mints the same token, so it needs no password and no fork. On a brand-new Runtipi with no account yet, `wam` creates the operator account once and stores the password in `~/.wizard-os/runtipi-operator.json` (mode 600). Calls used: `marketplace/create|all|pull`, `app-lifecycle/<urn>/install|uninstall`, `apps/installed`, `apps/<urn>`.

## apps.json (contract v1)

```json
{
  "schema": 1,
  "store": "wizard",
  "apps": [{
    "id": "hello-wizard", "name": "Hello Wizard", "type": "service", "version": "0.1.0",
    "status": "running", "system": false, "provides": ["diagnostics"], "uses": [], "needs": [],
    "summary": "...", "urn": "hello-wizard:wizard",
    "mcp": {"url": "http://192.168.5.3:8799/mcp", "internal": "http://hello-wizard:8799/mcp", "transport": "streamable-http"},
    "skill": "/home/me/.wizard-os/skills/hello-wizard/SKILL.md",
    "view": null
  }],
  "slots": {"diagnostics": {"default": "hello-wizard", "candidates": ["hello-wizard"]}}
}
```

The Agent Service reads this file (or `GET /api/apps`). It never needs to know about Runtipi.

## Making an app

Copy `templates/hello-wizard/`. It's the smallest complete app: `wizard.md`, stock Runtipi files, a stdlib MCP server and a skill. Check it with `python3 -m wam.wizardmd <dir>` or `wizard-os/tools/check_wizard_md.py <dir>`. Then tag the repo `wizard-app`.

## Layout

```
wam/wizardmd.py    the checker (vendored from wizard-os; change it there first)
wam/store.py       find + compile + serve-as-git
wam/runtipi.py     Runtipi API client (CLI token, first-run operator)
wam/registry.py    apps.json + skills
wam/cli.py         CLI + HTTP API
templates/hello-wizard/   the template app
tests/             pytest; no Docker needed
install.sh         Runtipi (if missing) + wam user services
```

## Tests

```bash
python3 -m pytest -q        # 11 tests, no Docker
```

The compiled output was also checked against `wizard-app-store`'s own suite (official Runtipi schema): 47/47.
