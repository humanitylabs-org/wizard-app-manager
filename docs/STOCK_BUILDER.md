# Stock builder provisioning

Wizard App Manager owns `system-skills/wizard-builder`, its references, Python tooling, hello-wizard templates and Wizard view assets as one content-addressed distribution. No Hermes core changes or private profile files are needed.

## Lifecycle

- `install.sh` installs the bundle before bootstrap. Fresh mode prepares the stock Hermes data config and additive Runtipi override, then adopts changed mounts after bootstrap. Installer failure is explicit rather than claiming a usable builder.
- `wizard link` provisions it and journals `skills.external_dirs` together with generated app skills. Repeated link refreshes the bundle and an unchanged WAM-managed `wizard-os` plugin. `--plugin-ref <commit>` pins the routing plugin; later repeats preserve an explicit pin unless another is supplied.
- The first linked plugin's file fingerprint is recorded. Older managed links are compared to their exact recorded upstream Git revision before refresh. Unknown/custom plugins and same-name skills are refused, not silently shadowed. A failed plugin refresh restores the previous tree. Modified/unattested plugins are preserved on unlink.
- `wizard unlink` reverses only its registrations, preserving later owner-added external skill directories and the OS bundle. Unchanged config is restored byte-for-byte. Generated app-skill deletion/regeneration cannot remove the builder.
- `wam builder-bundle install` repairs from the complete installed/source release. Modified OS-owned release files are retained, not overwritten; repair creates/reuses a verified sibling release. `wam builder-bundle install --hermes-home DIR` additionally registers a standalone home. Use `wizard link` instead for reversible alongside registration.
- `wam builder-bundle rollback --revision <release-id>` activates a verified retained release from an install receipt. No source/data or prior releases are deleted. A rollback changes only bundle content, not terminal settings or user app data.
- `wam builder-bundle doctor [--hermes-home DIR]` checks hashes, schema, assets, executable dependencies and optional discovery/collisions. `wam doctor` also probes the running stock container's actual local/SSH terminal route. `wam doctor --fix` repairs the bundle and fresh configuration; it does not restart an agent implicitly. Use `wam builder-bundle fresh --restart` to adopt a repaired mount. SSH probes require the configured key and trusted known-host entry; they never weaken host-key checking.

## Execution contract

Host release: `$WIZARD_OS_HOME/builder/releases/<hash>` (default `$HOME/.wizard-os`); `builder/current` switches atomically. The portable release contains no installation-specific interpreter path. A separate local binding records the installer's Python, so a non-login/SSH shell with a restricted PATH still has the installed PyYAML dependency. `$HOME/.wizard-os/bin/wam` remains available independent of shell startup files.

Stock container: the entire `builder` directory is mounted **read-only** at `/opt/wizard-builder`, and `skills.external_dirs` contains `/opt/wizard-builder/current/system-skills`. `WIZARD_BUILDER_ROOT=/opt/wizard-builder` makes the skill's terminal setup work there. The launcher selects `/opt/hermes/.venv/bin/python` in the stock container, never a host interpreter path. A local container can scaffold/check/edit without Docker access; executing developer containers still requires the configured host/worker Docker route.

The existing VM factory uses stock `terminal.backend: ssh` to the VM host. Its commands and file tools therefore use **host paths**, while `skill_view` reads the read-only container mount. Do not change this backend. The skill's setup command works on each side without a developer checkout:

```sh
export PATH="${WIZARD_BUILDER_ROOT:-$HOME/.wizard-os/builder}/current/bin:$PATH"
wam builder-bundle doctor
wam new habit-tracker --request 'Build a habit tracker'
wam check "$HOME/.wizard-os/dev/habit-tracker"
# After source edits, without Python module/PYTHONPATH setup:
wam package-inline "$HOME/.wizard-os/dev/habit-tracker"
```

## Release integration requirements

1. Pin a WAM commit containing this lifecycle and the Agent Service routing change together. Until the Service PR merges, an alongside acceptance install must use `wizard link --plugin-ref <service-commit> --restart`. Fresh agent requests must use the matching Service container/kernel revision. WAM does not edit or deploy Service.
2. **The current Try Wizard factory replaces the Hermes user-config compose file wholesale when choosing its slim image.** After `wizard-apps factory` finishes, invoke `wam builder-bundle fresh --restart` as the install owner. This merges the builder mount/environment back without changing the chosen image, extra hosts or SSH config. Do not run it before that overwrite and assume the mount survives. Future factory code should merge overrides or invoke this hook itself. This PR intentionally makes no Try Wizard edits and never seizes/restarts a release owner's build.
3. Build-time SSH may be unavailable until firstboot creates its per-box key; doctor reports this honestly. Verify terminal reachability again after firstboot, with the real configured trust/key, rather than calling build-time failure a successful SSH proof.
4. A bare wheel omitting templates/assets is not a supported provisioning source. Use the complete WAM source distribution already used by the installer. Runtime use needs only the installed bundle.
5. Restart/stop-start is opt-in for direct maintenance commands. The normal fresh installer adopts pending changes after bootstrap. Alongside link uses its existing `--restart` option. Running agent skill caches may require a fresh session/restart after a bundle rollback/update.

## Deterministic QA

Run on a disposable worker, with scratch/state on its work-data volume:

```sh
python -m pip install pytest PyYAML ruamel.yaml
python -m pytest -q tests
# Use an unmodified stock Hermes source and its dependencies, but a NEW home:
python qa/builder/stock_bundle.py --hermes-source "$STOCK_HERMES_SOURCE" --state-root "$NEW_PROOF_ROOT"
```

The proof invokes real stock `skills_list`, `skill_view` (including a reference), and `terminal_tool` to scaffold, check, modify a title and recheck. It writes `receipt.json` with exact WAM/Hermes Git revisions. This is deterministic tooling evidence, **not** an inference, browser or VM acceptance claim. Clean model prompts and actual VM/container rollout acceptance belong to the release coordinator.
