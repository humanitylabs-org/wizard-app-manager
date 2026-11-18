# Private first at install time

Issue: humanitylabs-org/wizard-app-manager#759 (split from wizard-os#676; decision wizard-os DECISIONS #115, contract
wizard-os `spec/AGENTS.md` section 10). Agent Service side: wizard-agent-service#545.

The Agent Service decides "first-run setup starts with a private (Tinfoil) agent" from the shell tweak
`wizard/private-first-setup` in the owner's Look file. A fresh box has no Look file when setup runs, so the Agent
Service also reads its env `WIZARD_PRIVATE_FIRST`: `1` forces it on, `0` forces it off, unset leaves the tweak in
charge. The App Manager's part is getting that env onto the box.

## At install

    curl -fsSL aiwizards.com/os.sh | sudo WIZARD_PRIVATE_FIRST=1 bash

`get.sh` forwards the variable to the install user (also through the detached runner), and `install.sh` runs
`wam private-first on` (or `off` for `0`) before bootstrap and alongside create the Agent Service. Any other value is
ignored with a note. **Unset (the default) records nothing and changes nothing.**

## On a box: `wam private-first on|off|clear|status`

The choice is recorded in `<wam home>/private-first.json` (0600) and written as its own block, marked
`x-wizard-private-first`, in the Agent Service app's Runtipi user-config override
(`user-config/wizardapps/<app>/docker-compose.yml`, `services.<app>.environment.WIZARD_PRIVATE_FIRST`). Only that
block is ever rewritten or removed: the alongside override and the browser-boundary block stay as they are, and
`wam install` / `wam alongside` re-add it whenever they write the override. Runtipi applies it whenever it creates the
container, and keeps it across app updates. `on`/`off` restart a running Agent Service (`--no-restart` skips that);
`clear` takes the block out so the tweak decides again. A hand-written `WIZARD_PRIVATE_FIRST` in the override is never
overwritten (the command refuses and changes nothing). `--dry-run` and `--json` work as elsewhere.
