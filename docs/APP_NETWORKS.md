# App networks: declared connections are the only app-to-app paths

Stock Runtipi puts every app's main service on one shared Docker network (`runtipi_tipi_main_network`), so any app
can open a connection to any other app. The App Manager replaces that with one network per declared connection.
Docker enforces it; no app holds a secret for another app. Code: `wam/network.py`.

## Networks

| Network | Members | Internal | Purpose |
|---|---|---|---|
| `wizard-app-<id>` | the app's own services that Runtipi would put on the shared network | no | internet access and the published `127.0.0.1` port |
| `wizard-link-<consumer>--<provider>` | the consumer's services + the provider's main service, which answers as `<provider id>` (and its compose service name) | yes | one declared connection |
| `wizard-gw-<id>` | the app + the shell, Agent Service and agent | yes | fresh boxes only: the tool gateway runs in a container and calls apps at `http://<id>:<port>` |
| `wizard-core` | the shell, Agent Service and agent | yes | fresh boxes only |

A link is a Docker network shared by exactly two apps. Two consumers of the same provider sit on different links, so
they can't reach each other. Docker networks are two-way: on its link the provider can also open connections to the
consumer. `wam connections` and the Connections view show that as "two-way".

Subnets are /27s from `10.255.0.0/16`, outside Runtipi's `10.128-10.254` app subnets and Docker's default pools, and
they never overlap an existing network. Set `network_pool` in `~/.wizard-os/app-manager.json` to use another range.

## What counts as a declared connection

- the consumer's wizard.md `uses:` entry: an app id, or a slot that installed apps `provide`;
- `requires.models` / `requires.services`, which connect the app to the Connection Router;
- an approved app grant (`wizard-grants.json`, kind `app`). Share grants carry no network.

Anything else has no shared network: the name doesn't resolve and the address isn't routed.

## The gateway path

- **Alongside boxes:** the shell and Agent Service run on the host network. They reach apps on their published
  `127.0.0.1:<APP_PORT>`. That doesn't change, and the gateway joins no app network.
- **Fresh boxes:** the shell, Agent Service and agent run in containers. They join every app's `wizard-gw-<id>`, so
  the gateway keeps its internal addresses. Apps still don't share a network with each other.

Apps whose recipe uses `network_mode: host` get a refusal from `wam install` unless the owner passes
`--allow-host-access`, which prints a loud warning. The same applies to `privileged: true` and to a mounted
Docker/containerd/podman socket. Apps installed that way, and apps with hand-written networks in their user-config
override, stay as they are. An app exposed through Runtipi's reverse proxy keeps the shared network, because Traefik
reaches it there, so every app can still reach it. It also gets its declared links. The Connections view flags all of
these.

## How it is applied

Each isolated app's stock Runtipi user-config override (`user-config/wizardapps/<id>/docker-compose.yml`) gets a block
marked `x-wizard-networks`, and the rest of the file is kept. The block replaces the shared network with the app's
wizard networks (`networks: !override`) and declares them `external`. Every start, restart, update and reboot that
Runtipi does keeps the topology.

`wam network reconcile` is idempotent and does everything else:

1. Creates the missing networks.
2. Writes the override blocks and removes them from apps that are no longer isolated or no longer installed.
3. Changes running containers live. Containers join their networks first, then leave the shared network and any
   network nothing declares. Docker moves the default route and keeps published ports. Each published port is
   checked afterwards, and only an app whose port stopped answering is restarted.
4. Removes wam networks that nothing needs any more.

Reconcile runs automatically:

- `wam install` plans the app's networks before its first start, so it never joins the shared network, then
  reconciles again;
- after `wam uninstall`, which removes its links and override block;
- after `wam app update`;
- in `wam bootstrap`, which every installer run executes, so existing boxes convert on upgrade.

## Commands

- `wam connections [--json]`: what each app can reach and who reaches it, how the gateway reaches it, internet access,
  host access, and the live check of Docker membership against the declarations. It exits 1 on drift. `wam doctor`
  reports the same check.
- `wam network reconcile [--dry-run] [--no-restart]`
- `wam network off` rolls back. It removes every override block, puts the apps back on the shared network (live) and
  deletes the wam networks. `wam network on` redoes it. `WIZARD_APP_ISOLATION=0` also turns it off.
- `GET /api/connections` on the loopback wam API (`wam serve`, 127.0.0.1:7790) is the live report; apps.json carries
  the same report as `connections` (a snapshot from the last registry write). The Connections view
  (`renderConnections` in `wizard-views/wizard-apps.js`, `#connections` on the App Manager page that `wam serve` also
  serves at http://127.0.0.1:7790/views/#connections) reads the live report and falls back to the snapshot.

## Limits

- Host processes reach every app's published `127.0.0.1` port, as before. Those are the gateway and anything else
  running as a host process. Layer 2 (per-grant keys) covers them.
- Ports that the host binds on all interfaces stay reachable from containers, as before.
- Links are two-way (see above). One-way enforcement would need per-link firewall rules.
- On fresh boxes each app can also reach the shell, Agent Service and agent over its `wizard-gw-<id>`, as it could on
  the shared network. The view lists them as "Wizard core". Other apps stay out of reach.
- Apps from other Runtipi stores stay on the shared network and can't reach isolated Wizard apps.
