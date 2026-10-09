# Installing Wizard OS apps on this machine (instructions for an AI agent)

You are installing the Wizard App Manager. It uses stock Runtipi to run apps. Do these steps in order and report each result.

1. **Check the machine.** Ubuntu/Debian, `x86_64` or `aarch64`, 2 GB+ free RAM, `git`, `python3` (3.10+), `python3-yaml`. Install missing packages with apt.
2. **Install Runtipi if it isn't already** (`~/runtipi/.env` missing): `curl -fsSL https://setup.runtipi.io | sudo bash`. Wait until `http://<INTERNAL_IP>/api` answers.
3. **Clone this repo** to `~/wizard-app-manager` and run `./install.sh`.
4. **Verify:** `wam doctor` must show four PASS lines.
5. **Prove the chain:** `wam install hello-wizard`, then call its MCP at the `mcp.url` shown in `wam apps --json` (`tools/list` should list `wizard_time` and `wizard_echo`).
6. **Report** the Runtipi address, `wam doctor` output, and the installed apps.

Rules:
- Never edit Runtipi's files, database or app folders. Use `wam` or Runtipi's own UI.
- Never print secrets from `~/runtipi/.env` or `~/.wizard-os/runtipi-operator.json`.
- If the user wants an app that isn't listed, run `wam catalog --json` and read `skipped`: it says which wizard.md rule failed.
