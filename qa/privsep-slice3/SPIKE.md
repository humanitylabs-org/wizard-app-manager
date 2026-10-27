# Slice 3: alongside privilege split

## Source audit / decisions

- `get.sh` detects Hermes by user/home/binary, then `prepare-named-user.sh` grants that SAME user Docker, linger and Tailscale operator, and `as_user` clones into its home. `install.sh` defaults Runtipi to `$HOME/runtipi`, state to `$HOME/.wizard-os`, writes `$HOME/.local/bin/wam`, installs user units and runs builder/link/bootstrap in that identity. Keep that path for existing installs; route NEW named-user alongside installs through root-owned host preparation instead.
- New layout: root creates `wizard-system` (home `/var/lib/wizard-os`), trusted checkout there, Docker + Tailscale operator ONLY there. Manager/Runtipi system units are root-owned. No system process follows a checkout, unit, config or token from the Hermes home. Refuse existing privileged Hermes accounts rather than silently migrating/revoking their rights.
- `agent_link.py` uses `docker exec` to edit fresh container Hermes; it explicitly refuses alongside. Do NOT call it for this layout. `wizard link` stays at the Hermes uid; stock Hermes is unchanged. `hermes_update.py` checks the linked agent uid; run locally at that uid, never proxy a host update through privileged wam.
- `alongside.py` detects and reads Hermes config/key as the current uid; its shell/service override uses that uid and mounts history and media. New layout instead records an administrator-selected identity snapshot, never reads Hermes config/key as wizard-system, and generates a dedicated service override. The Agent Service runs at the Hermes uid, cap-drop ALL, no-new-privileges, has separate writable `/var/lib/wizard-agent/<uid>` state. An in-container launcher at that uid reads `wizard link`'s key handoff. Docker loads only protected compose/launcher files. Agent-readable handoff is NOT an approval credential.
- Agent Service defaults to `~/.wizard-os` for apps, skills and settings. Its read-only apps projection comes from manager-owned `app-data/wizard-os`; writable state is separate. API key + wam URL/agent token come from link, NOT by privileged reads of agent files. No Agent Service source change is required for environment-based startup.
- Builder bundle mounts its own tooling into fresh Hermes; this is not alongside provisioning. New layout thin client explicitly refuses builder/backup/links (Slice 4) instead of falling back to local privileged commands.
- `wam serve` has install/search/list/uninstall/doctor but lacks lifecycle/logs; add finite authenticated endpoints, strict ids, bounded logs, and agent-vs-server token scopes. Owner routes deny agent tokens BEFORE identity-header handling. Do not expose arbitrary CLI execution, paths, ports, raw Docker arguments, private repo aliases or privileged bootstrap through the agent API.

## Shipped instruction call sites

- `system-skills/wizard-builder/SKILL.md`: builder-bundle doctor/install/fresh, new/check/install-local, publish, views. `references/build.md`: docker compose start/stop/down. `references/views.md`: views customize/apply/update/revert/reactivate/status/index/serve. All builder/view execution remains unavailable through the new client until Slice 4.
- `os.md`: install/get, wam doctor/apps/search/install; repair advice assumes the owner and manager are one uid. Add a separated-layout note; do not suggest Docker/sudo as an agent workaround.
- wizard-os `REPAIR_KIT.md`: Docker inspection/restarts and wam commands are server-administrator instructions, not an escape hatch for the unprivileged account. Keep full repair on the administrator side; use named client actions for app lifecycle/logs.
- Kernel plugin lives in Agent Service, reads app/skill projections; it must not receive a server/owner token. It is outside this repo and does not call a privileged wam executor.

Files uses Runtipi's actual `runtipi/media` owner-files root (not a disconnected second folder).
A named `wizard-files` group ACL plus default ACL preserves shared access even when the Files app
initializes the directory as gid 1000. The same-UID Agent Service keeps the usual writable Hermes
mount for its UI state; that cannot grant privilege because neither wizard-system nor root loads it.

## QA package choice and stock Hermes bootstrap

The public index currently has no `hello-wizard` listing. The harness uses `wizard-calendar`, a tiny public
source package with no `uses` dependencies, the upstream pinned Python Alpine image and no provider accounts.
A silent app's logs are valid: prove the installed app has a selected container and the log request returns text,
not fabricated nonempty output. Pin the same stock Hermes commit for both guests. Follow the current PM developer
workflow (`source ./activate`), which provisions uv-backed locked dependencies and records extras for the canonical
launcher. Raw editable pip rejects the upstream browser-harness/Pillow conflict; raw external `uv sync --active`
initially works but the canonical gateway launcher selects a different PM generation without aiohttp after restart.
Use stock `hermes gateway install` at the agent uid: an unrelated QA unit is treated as a manual process by
`wizard link` and may be stopped without restarting. Never edit stock Hermes. Change to the account's home after
`runuser` before entering its checkout; otherwise uv attempts to read `/root/uv.toml` and fails.

## Deferrals

Slice 4: backup/restore, link publication, builder/view staging; Slice 5: developer/server-access grants; Slice 6: all existing-box migration, operator reassignment, rollback. Root-owned helper/timer migration beyond manager/Runtipi, Mac agent-SSH, box census, rootless Docker and fresh-layout changes are not part of this alongside skeleton. No live box writes. No real model or tailnet credentials in QA; unauthenticated Tailscale means published private links remain unproven.

REUSE: existing Runtipi installer, policy/owner checks, registry, lifecycle and VM harness.
EXTEND: wam API authentication/routes, alongside override, wizard link handoff.
NEW: root-approved alongside host layout and stdlib-only remote client; no UI.
