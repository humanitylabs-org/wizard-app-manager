# Contributor guide for agents

Entry point is [AGENTS.md](../AGENTS.md); this page holds the commands. Checked against the code and workflows on 5 Nov 2026. If a command fails, fix this page in the same PR.

## What this repo is, and its neighbours

A thin fork of Runtipi 4.10.2 (GPL-3.0) plus the Python **`wam`** CLI. App Manager installs and updates apps, writes the app registry and cuts Wizard OS releases. Keep the diff from upstream Runtipi small: Wizard behaviour goes in `wam/`, the installer, or the `wizardapps` store module.
- **wizard-ux** is the long-term interface and owns the shared Reuse and style gates. **wizard-agent-service** owns each agent's app allow-list; `wam/agent_access.py` changes it only through its loopback owner API. **wizard-troubleshooter** is a native host service that `wam troubleshooter` installs and pins (`wam/troubleshooter.py`). **Hermes** stays stock and updates through its own path (`docs/hermes-update.md`).

## Layout

- `wam/`: CLI and lifecycle (`cli.py`, `updates.py` OS releases, `app_updates.py`, `registry.py`, `backup/`, `agent_access.py`). `install.sh`, `get.sh`, `os.md`: what a new box runs; not a dev setup.
- `packages/backend/src/modules/` (NestJS; `app-stores/wizard-index-store.ts`), `packages/frontend/src/` (React), `packages/common/`. `wizard-views/`: Wizard-specific views. `system-skills/`, `templates/hello-wizard/`: shipped to installs and app builders.
- `tests/` (pytest, node and bun tests), `packages/backend/**/__tests__/`, `e2e/` (Playwright), `qa/` (harnesses), `releases/` (public OS release metadata). Upstream UI conventions: [AGENTS.runtipi.md](../AGENTS.runtipi.md).

## Setup, tests, checks

Python >=3.10 (`pyproject.toml`; CI uses 3.11), Bun 1.3.0 (CI backend job also uses Node 24; the Python job uses Node 22). Never run `install.sh` or `get.sh` just to develop or test.

```sh
python3 -m venv .venv && . .venv/bin/activate && pip install pyyaml pytest pyjwt
bun install --frozen-lockfile --ignore-scripts
# job "test"
python tests/test_portable_paths.py
python tools/sync_wizard_style.py --check
python -m wam.style_check templates/hello-wizard --policy wam/style-policy.json --strict
python -m pytest -q
node --test tests/runtipi-open.test.mjs tests/connections-map.test.mjs
bun test tests/curator-mirrors.test.ts
# job "backend"
node --test tests/backup-source-safety.test.mjs
(cd packages/common && bun run build) && (cd packages/backend && bun run test && bun x tsgo --noEmit)
```

`bun run lint` (Biome) and `bun run lint:ci` exist but CI does not run them. Required checks on `main` (branch protection on 5 Nov 2026; re-check with `gh api repos/humanitylabs-org/wizard-app-manager/branches/main/protection/required_status_checks`): **`test`** and **`gitleaks`**. The `backend` job and the Agent review also run on PRs; read results on the exact PR head. `image.yml` runs only when its path filters match.
- `gitleaks` (`secret-scan.yml`) scans the whole history of HEAD: `gitleaks git . --config .gitleaks.toml --no-banner --redact --log-opts="HEAD"` (Gitleaks 8.30.1).
- This repo's own CI has no `Reuse:` check and its PR template has none. Apps built from `templates/hello-wizard` run the shared gate from wizard-ux: `Reuse:` alone on a line, then bullets `- <capability>: REUSE|EXTEND <entry point from wizard-ux docs/COMPONENTS.md>` ([COMPONENTS.md](https://github.com/humanitylabs-org/wizard-ux/blob/main/docs/COMPONENTS.md), [app-reuse-gate.md](https://github.com/humanitylabs-org/wizard-ux/blob/main/docs/app-reuse-gate.md)). Reuse the shell's capability, never copy it. In a PR here, say in the body what you REUSE, EXTEND or add as NEW.

## Rules for UI and app changes

- **Never drop an existing feature.** Start from the source that serves today and list what is kept. `system-skills/wizard-builder/SKILL.md` names an `app-parity-gate` run by the release owner that is **not in this repo**; record your comparison and the gap, never claim a pass.
- App workflows, styles, presets and validators ship in the app's own source (`SKILL.md`, `wizard.md`) and release. Installed copies are rewritten on every registry write, so never edit them or keep the rule only in an agent's notes.
- Wizard views and app templates keep the shared rules: a tap paints on the same frame and never waits for the backend; Back never exits; tapping outside a sheet closes it; Appearance v1 `--wz-*` tokens with live skin changes and a dark standalone fallback; text-bearing floating panels fully opaque; screenshots on two skins (one light). The inherited Runtipi dashboard is not rewritten to enforce this.

## Browser and visual checks

- Throwaway upstream dev stack (needs Docker and Portless, no real box): `bun run start:dev:portless`, then `portless get runtipi` for the URL (dev login in AGENTS.runtipi.md). `bun run test:e2e` clears the database and resets settings: run it only against that disposable stack.
- Wizard views, synthetic data, no box state: `WIZARD_CORE_DIR=<wizard-ux checkout>/web/core PLAYWRIGHT_MODULE=<playwright module path> node qa/connections-appearance.cjs <outdir>` ([qa/connections-acceptance.md](../qa/connections-acceptance.md)). `qa/qa-playwright.cjs` resolves Playwright (`QA_PLAYWRIGHT`, `QA_CHROME`). Synthetic runs are not live or device proof; say what you could not run.

## How a change reaches users

1. **Code:** a PR merges to `main`. `image.yml` (only when `packages/**`, `Dockerfile`, `package.json`, `bun.lock`, `turbo.json`, the Hermes lock files or the workflow change) builds both architectures; on `main` it publishes `ghcr.io/humanitylabs-org/wizard-app-manager` (`:latest` and `:v4.10.2-wizard.<n>-<sha>`) and attests provenance. PRs do not publish. GHCR `latest` is not the OS Latest channel.
2. **OS release:** boxes read `stable` or `latest` plus a `<version>.json` (component commit pins) from `releases/` ([releases/README.md](../releases/README.md)). Releases are cut and promoted in the private Wizard OS canon repo (its `tools/promote_stable.py`); this repo mirrors the metadata, so a contributor here cannot cut one: file an issue. Today's executable gate: Stable needs a promotion record, normally 72 h on Latest plus a passed upgrade test, with `fast_track` for some releases (`wam/updates.py`). Never point `stable` at a release without its record; `tests/test_release_channels.py` fails.
3. **App updates** move separately and automatically with backup and rollback ([docs/app-updates.md](app-updates.md)). Hermes, the shell, Agent Service and Troubleshooter are excluded.
4. The agreed team policy (Latest is plain `main`, Stable after a soak, `hotfix` skips it) is in AGENTS.md. It does not change `wam/updates.py` until that code does; existing release docs that mention self-merging fixes are older than it.
