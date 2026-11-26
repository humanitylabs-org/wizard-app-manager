# Stock Runtipi (switch `WIZARD_STOCK_RUNTIPI`, off by default)

wizard-os#822, cut phase 1: a new box runs Runtipi exactly as Runtipi ships it, with the Wizard store as a plain git
store, instead of the App Manager image (the Runtipi fork, DECISIONS #32).

Off (the default): nothing changes. The installer runs the App Manager image and wam talks to it as before.

## Turning it on (new boxes only)

```sh
curl -fsSL https://aiwizards.com/os.sh | WIZARD_STOCK_RUNTIPI=1 bash
```

- Runtipi's own image (`ghcr.io/runtipi/runtipi:v4.12.0`) and CLI; no App Manager image, no release-pins override.
- Store `wizardapps` = the generated store, wizard-app-store's `stock` branch (`scripts/stock-store`: every
  index-approved app's runtipi folder at its approved commit; hermes-agent at exactly the Hermes lock pin).
  `WIZARD_STOCK_STORE=<git store url>` points a box at another branch (a trial store).
- The installer writes `~/.wizard-os/stock-runtipi.json` (`{"on": true, "store": ...}`), so every rerun and every
  `wam` command stays stock. `WIZARD_STOCK_RUNTIPI=0` turns wam's stock paths off whatever the file says.
- A box that already runs the App Manager refuses the switch (the installer stops before changing anything).

## What wam does instead of the fork

- Installs, updates and the installer's default apps come from the git store, never the App Index
  (`resolve_any` -> `stock_record`); a rerun moves installed defaults to a newer store copy (`tipi_version`).
- App grants, connections and file shares (DECISIONS #76/#79/#80): `wam/stock_grants.py` answers the routes the
  fork served, from the same `state/wizard-grants.json`; networks and shares go into Runtipi's stock user-config
  overrides.
- `wam recover` (every installer run) also starts an app a reboot left `starting`/`restarting`/`updating` with its
  containers stopped: stock Runtipi never settles those statuses.
