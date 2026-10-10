# Installing Wizard OS apps on this machine (instructions for an AI agent)

You are installing the Wizard App Manager. It uses stock Runtipi to run apps. Do these steps in order and report each result.

1. **Check the machine.** Ubuntu/Debian, `x86_64` or `aarch64`, 2 GB+ free RAM, `git`, `python3` (3.10+), `python3-yaml`. Install missing packages with apt.
2. **Install Runtipi if it isn't already** (`~/runtipi/.env` missing): `curl -fsSL https://setup.runtipi.io | sudo bash`. Wait until `http://<INTERNAL_IP>/api` answers.
3. **Clone this repo** to `~/wizard-app-manager` and run `./install.sh`.
4. **Verify:** `wam doctor` must show only PASS lines.
5. **Prove the chain:** `wam install transcriber`, then call its MCP at the `mcp.url` shown in `wam apps --json` (`tools/list` should list `transcribe` and `transcriber_status`).
6. **Install what the owner asks for** with `wam search` and `wam install <id>`. Generated passwords are in `~/.wizard-os/secrets/<id>.json`; tell the owner where, never print them.
7. **Report** the Runtipi address, `wam doctor` output, and the installed apps.

Rules:
- Never edit Runtipi's files, database or app folders. Use `wam` or Runtipi's own UI.
- Never print secrets from `~/runtipi/.env` or `~/.wizard-os/runtipi-operator.json`.
- If the user wants an app that isn't listed, open `https://apps.aiwizards.com/v1/apps/<owner>/<repo>`: `last_check.failures` says which wizard.md rule failed.
- Apps answer on `127.0.0.1` only. To reach them from a phone, use Tailscale or a Cloudflare Tunnel to that address; never publish ports on the public interface.
