# Slice 1 sudo audit

Scope: `get.sh`, `install.sh`, `wam/`, `scripts/`, `system-skills/`.
Spec: humanitylabs-org/wizard-os#285. This slice does **not** implement full
privilege separation: the fresh host's `wizard` account is still the Docker
service account. The agent is the stock `hermes-agent` container, without a
runtime socket. Alongside principal migration belongs to later slices.

## Removed execution paths

- `get.sh`: no blanket sudoers grant; Docker detection/image readback never
  retries as root. Root's SSH keys are still copied to the service account.
- `wam/builder_bundle.py`: no root reads, installs or moves of agent config.
  A foreign stock config is read/written by Docker exec **as `1000:1000`**, with
  a fixed in-container path, atomic replacement and its mode preserved. Runtime
  doctor never escalates a failed Docker probe.
- `wam/agent_link.py`, `wam/backup/dockercli.py`, `wam/test_catalog.py`: no Docker
  sudo fallback. Existing rootless Docker discovery and sanitized backup child
  environment remain. Agent linking reports the administrator/new-login action.
- `wam/links.py`, `scripts/setup-private-access.sh`: Tailscale commands use the
  caller's operator permission, not a blanket sudo probe or retry. Root host
  preparation sets that operator through the existing `prepare-named-user.sh`.
- `install.sh`: Docker denial stops with an administrator/host-preparation/new
  login message, instead of selecting a root Docker transport.

## Deliberately retained uses (all remaining matches classified)

| File(s) | Uses | Classification / reason |
|---|---|---|
| `get.sh` | header, comments, diagnostics, `SUDO_USER`, sudoers directory variable, sudo group names, printed administrator command | Text/metadata only; no new rights. |
| `get.sh` | `sudo -n true` in preflight and named-user approval admission | Read-only capability probes for the existing alongside approval flow; never perform a privileged fallback and never prompt. |
| `get.sh` | `sudo -iu` in `as_user` / `as_wizard` | Root-only **de-escalation** into the selected owner/service account. |
| `get.sh` | installing the `sudo` package during `prepare_server` | Root host preparation installs the binary, not a blanket authorization. |
| `get.sh` | `sudo -S -k` inside the existing owner-approved host-prep relay | Existing explicit human approval path for alongside hosts; unchanged in this slice. Not an agent runtime fallback. |
| `install.sh` | noninteractive `sudo -n bash`, `chown`, `find`, package installs, mkdir/chown fallback | Legacy standalone installer/admin bootstrap, not whitelisted for `wizard`. Fresh root host preparation now completes these prerequisites **before** dropping to `wizard`. They fail without prompting when no administrator rights are available. No Docker or Tailscale sudo retry. |
| `wam/cli.py` | messages recommending root/owner for explicit admin import and cross-uid diagnostics | Human guidance only. No subprocess sudo. |
| `wam/links.py`, `wam/troubleshooter.py`, `wam/cli.py` | printed `sudo tailscale up` guidance | Existing human-admin action for an unpaired box, not executed by wam; the designated operator can also run `tailscale up` directly. |
| `wam/mirrors.py`, `wam/private.py` | historical "sudo user" comments | No execution. |
| `scripts/prepare-fresh-user.sh` | sudoers validation/installation and literal comparison with the old blanket grant | Root-only host preparation. Installs one argument-free helper grant, validates it, then removes only the exact historical file. A customized legacy file stops for administrator review. |
| `scripts/root-helpers/os-patch` | printed `sudo -n /usr/local/libexec/wizard-os/os-patch` | The sole permitted root command. Fixed root-owned program, exact empty argument list, isolated Python, clean child environment; security origins only, runtime/network/init packages held, service restarts blocked and automatic reboot off. |
| `scripts/prepare-named-user.sh` | comments/diagnostics mentioning sudo | Reused root-only host preparation, no actual sudo invocation or new grant; behavior unchanged. |
| `scripts/setup-private-access.sh` | `sudo -n "$@"` for **non-Tailscale** package/daemon bootstrap; printed status/admin advice | Optional explicit host package/daemon setup for standalone invocation; fresh and administrator-prepared alongside installs already have the running daemon. Never prompts. Tailscale actions themselves are never elevated. |
| `scripts/install.sh` | apt/yum/dnf/pacman, repository setup, service starts; `sudo -u "$SUDO_USER" yay` | Vendored upstream Runtipi installer, an explicitly administrator-run host step. Not a wam module or agent runtime fallback. `sudo -u` drops privileges for the AUR builder. Left upstream unchanged. |
| `scripts/unsafe-cleanup.sh`, `scripts/update-2.0.0-to-3.0.0.sh`, `scripts/update-3.0.0-to-4.0.0.sh`, `scripts/temp-update-3.0.0-to-4.0.0-beta.sh` | `sudo ./runtipi-cli start`; missing-jq install advice | Vendored historical, explicitly administrator-run Runtipi maintenance/migration scripts. Not installed as root helpers and not run by the normal fresh agent. |
| `scripts/dogfood-host-setup.sh`, `scripts/qa-release-candidate.py` | usage text/comments | Disposable host/VM QA instructions only. |
| `scripts/demo-host-acceptance.py` | `sudo -n true` refusal probes, explicit owner-approved administrator command | Existing isolated alongside acceptance fixture; intentionally verifies no passwordless sudo. Never normal production agent execution. |
| `system-skills/` | none | No sudo matches. |

There is no general `tailscale-operator` helper: the root host phase already
sets the service account as operator and refuses a conflicting existing operator.
No `wam maintain setup` or argument-taking patch dispatcher is added. The old
MAINTENANCE wording describes an unbuilt interface; Slice 1 deliberately exposes
only the requested small, argument-free security-upgrade helper. Quiet scheduling,
maintenance mode settings and the later privilege-separation slices remain out of
scope. The nightly updater never installs or replaces these helpers.
