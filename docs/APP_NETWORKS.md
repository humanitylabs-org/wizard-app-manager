# App networks: open by default, the owner in control

Stock Runtipi puts every app's main service on one shared Docker network (`runtipi_tipi_main_network`), so any app
can open a connection to any other app. With isolation on, the App Manager replaces that with networks it manages, in
one of two modes the owner picks (App Manager → Connections, or `wam network mode open|declared`):

- **Open (the default):** everything is connected unless you disconnect it. Every app reaches every other app, except a
  pair the owner disconnected (cut both ways) and new connections to an app the owner marked sensitive, which wait for
  approval. See [Open mode](#open-mode-the-default).
- **Declared:** each app reaches only the apps it declares (`uses`, `requires`) or the owner connected: one network per
  declared connection. The rest of this page up to "Open mode" describes it.

Docker enforces both; no app holds a secret for another app. Code: `wam/network.py`.

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

A `uses` / `requires` declaration links only while its grant is approved. The App Manager approves it automatically
when the app is installed (`approved: install`), and the owner's `wam grants revoke` takes the network away even though
the declaration is still there. An automatic grant never links on its own: when an update drops the declaration, the
link goes. An owner's grant (`wam grants grant`) links by itself. On an App Manager without grants, declarations alone
decide. `wam connections` shows a declared but revoked connection as "grant revoked or not approved: no network".

Anything else has no shared network: the name doesn't resolve and the address isn't routed.

## Who decides: seamless by default, the owner in control

The App Manager (`packages/backend/src/modules/docker/wizard-app-grants.ts`, `applyConnectionPolicy`) decides new
connections; the owner can overrule any of them. Nothing asks the owner except connections to an app they marked sensitive.

- **Install:** an app's declared connections are approved (`approved: install`), quietly.
- **Update:** a connection an update declares is added automatically too, and the owner gets a quiet notice
  ("Carlos Campaigns now connects to Resend"). When an update drops a declaration its automatic grant goes, so declaring
  it again later is new (and noticed) again.
- **Runtime request:** an app asks for a connection by writing `{"connections": ["<app id or slot:name>", ...]}` to
  `/wizard-requests/connections.json` (env `WIZARD_CONNECTION_REQUESTS`). That folder is the app's own
  (`app-data/<store>/<id>/wizard-requests`, mounted only into that app), so the App Manager knows who asked. It checks the
  folder every 5 seconds (no symlinks, 16 KB, at most 32 entries) and approves the request the same way
  (`approved: request`, links without a declaration) with a notice. An app can only ask: it can't grant, revoke or mark
  anything.
- **Disconnect (sticky):** the owner's Disconnect is a `denied` row. No update, reinstall or runtime request adds it back.
  Reconnect makes it an owner grant (`approved: owner`).
- **Sensitive:** new connections TO an app the owner marked sensitive are never approved automatically. They wait in
  `pending`, and the owner gets a notification; Approve makes an owner grant, Deny a sticky `denied` row. Marking an app
  sensitive keeps its existing connections; turning it off approves what was waiting.

State lives in `wizard-grants.json` beside the grants: `pending`, `sensitive` and the last 50 `notices`. The owner acts in
the Connections view or with `wam grants approve|deny|grant|revoke <app> <to>`, `wam grants sensitive <app> on|off` and
`wam grants pending`. These go to the App Manager's owner-authenticated API (`POST /api/apps/<id>:wizardapps/app-grants`,
`POST /api/apps/<id>:wizardapps/sensitive`, `GET /api/apps/wizard/connections`; the controller is behind the App
Manager's `AuthGuard`, the same sign-in as install/start/stop). wam's loopback page (127.0.0.1:7790/views/) has no
write route without the CLI's token, so there the view is read-only.

`wam serve` polls the connection state every 3 seconds. When grants, Disconnects, pending requests or sensitive apps
change, it reconciles the networks live (no restarts) and refreshes the registry, so a Disconnect removes the link
within seconds. It delivers new notices through the installed Notifications app's `notify` tool (the provider of the
`notifications` slot; quiet ones at low priority, a pending approval at high priority, older than a day not sent) and
marks them delivered. Notifications can't carry Approve / Deny buttons that are safe without the owner's sign-in, so the
notification points to App Manager → Connections, where the request waits with Approve / Deny. Without a Notifications
app, notices are shown only in the Connections view ("Recent connection changes").

## The gateway path

- **Alongside boxes:** the shell and Agent Service run on the host network. They reach apps on their published
  `127.0.0.1:<APP_PORT>`. That doesn't change, and the gateway joins no app network.
- **Fresh boxes:** the shell, Agent Service and agent run in containers. They join every app's `wizard-gw-<id>`, so
  the gateway keeps its internal addresses. Apps still don't share a network with each other.

A recipe that attaches networks the app does not own is refused the same way and never isolated: a reserved
`wizard-link-*`/`wizard-gw-*`/`wizard-core` network, any `external` network (for example a logical network whose
`name:` is `runtipi_tipi_main_network` or another app's compose network), a fixed Docker `name:` outside the app's own
compose project (`<id>_wizardapps_*`), a non-bridge driver, and `network_mode: host|bridge|container:<x>`. The owner
can allow one app explicitly (`wam install <id> --allow-host-access`, `wam app update <id> --allow-host-access` or
`wam network allow <id>`; `wam network deny <id>` takes it back and stops the app if it is running with that access). If isolation is on and an app's networks can't be prepared (for example the address pool is full),
`wam install` stops before the first start rather than start it on the shared network. `wam app update` writes the
override for the incoming recipe before Runtipi applies it, and `wam grants grant|revoke` reconciles at once.

Apps whose recipe uses `network_mode: host` get a refusal from `wam install` unless the owner passes
`--allow-host-access`, which prints a loud warning. The same applies to `privileged: true` and to a mounted
Docker/containerd/podman socket. Apps installed that way, and apps with hand-written networks in their user-config
override, stay as they are. The service an app publishes through Runtipi's reverse proxy keeps the shared network, because
Traefik reaches it there, so every app can still reach that service; the app's other services leave it. It also gets its declared links. The Connections view flags all of
these.

## Every start, whoever starts the app

wam is not the only way an app starts. The App Store views and Runtipi's own UI install, update and restart apps
through the App Manager API directly. So the App Manager itself (the Runtipi fork,
`packages/backend/src/modules/docker/wizard-networks.ts`) applies the policy before every `compose up` of a Wizard app:

- with isolation on it never puts a service on the shared network, unless the service is published through the reverse
  proxy or the owner set its networks by hand in user-config. A new app therefore starts with only its own networks;
- it refuses a compose that attaches networks the app does not own (the rule above) unless the owner allowed that app;
- it drops entries in wam's managed override block for services the new recipe no longer has, so an update that
  renames a service still starts.

wam mirrors its switch and the owner's allowances to `user-config/wizard-app-networks.json`. A missing or unreadable
file means isolation on and nothing allowed. `wam serve` watches Docker for Wizard app starts and reconciles after each
one, so an app installed or updated from the UI gets its declared links and gateway network within seconds (and loses
links it no longer declares). Between starts it reconciles every 10 minutes without restarting anything.

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
- in `wam bootstrap`, which every installer run executes, so existing boxes convert on upgrade;
- in `wam serve` after every Wizard app start (installs and updates from the UI included), see above.

`wam app update` refuses an update that brings in host access or foreign networks the installed version didn't have,
unless the owner allows it, and stops before the update when its networks can't be prepared (isolation on).

## Commands

- `wam grants pending|sensitive|approve|deny|grant|revoke`: the owner's connection decisions (see above).
- `wam connections [--json]`: what each app can reach and who reaches it, how the gateway reaches it, internet access,
  host access, and the live check of Docker membership against the declarations. It exits 1 on drift. `wam doctor`
  reports the same check.
- `wam network reconcile [--dry-run] [--no-restart]`
- `wam network off` rolls back. It removes every override block, puts the apps back on the shared network (live) and
  deletes the wam networks. `wam network on` redoes it. `WIZARD_APP_ISOLATION=0` also turns it off.
- `GET /api/connections` on the loopback wam API (`wam serve`, 127.0.0.1:7790) is the live report; apps.json carries
  the same report as `connections` (a snapshot from the last registry write). The Connections view
  (`renderConnections` in `wizard-views/wizard-apps.js`, `#connections` on the App Manager page that `wam serve` also
  serves at http://127.0.0.1:7790/views/) reads the live report and falls back to the snapshot. That loopback page
offers only Connections: My Apps, the App Store and Resources need Runtipi's API and the Wizard front, which live on the
shell's origin.

The same view with its owner controls opens from Wizard on the owner's phone or computer at
`https://<box>:<shell port>/wizard-os-home/store-install/_connections/` (tailnet only). `wam serve`'s owner listener
(:7791, behind the shell's Tailscale Serve mount, `wam/connections_view.py`) serves the page, the live report and the
owner's decisions, and makes a change through the App Manager's owner API, exactly as `wam grants` does. Reads need
the owner's Tailscale login. A change also needs another tailnet device: Serve labels connections from the box's own
address with the owner's login, and every app container reaches Serve from there, so from the box itself the page is
read-only and says why (use `wam grants` there). Only the Connections decisions are reachable on that path (Disconnect /
Reconnect, Approve / Deny, Sensitive, Open / Declared), never installs, removals or other Runtipi calls. To give it a
launcher tile, add it to the owner's `launcher.json` `links` (Agent Service).

## Open mode (the default)

The owner's mode is kept with the other connection decisions in the App Manager's `wizard-grants.json` (`mode`, missing
= open) and changed only through its owner-authenticated API (`POST /api/apps/wizard/connection-mode`, the Connections
view's Open / Declared switch, `wam network mode open|declared`). `wam serve` reconciles live within seconds of a switch.

### Who reaches whom

- Every app reaches every other app, both ways, by its app id (and its main service's name).
- **Disconnect** (the owner's sticky `denied` row, by app id or by a slot the app fills) cuts that one pair in both
  directions: Docker networks are two-way, so "A can't reach B" also means "B can't reach A". Every other pair stays
  connected. Reconnect removes the row. No update, reinstall or runtime request brings a Disconnect back.
- **Sensitive:** marking an app sensitive keeps the connections it has: the App Manager records one `approved: open`
  grant for every installed app (no key is minted; it never links in declared mode). An app installed later waits for
  the owner (`pending`, reason `open`, a high-priority notification); so does a new declaration or runtime request.
  Approve makes an owner grant, Deny a sticky Disconnect. A declaration of a connection the app already has (its open
  grant) is not new: it gets an ordinary grant with its key and never waits. Turning Sensitive off drops the open grants
  and what was waiting: open mode connects them anyway. Because a link is two-way, a sensitive app can't open a
  connection to an app that may not reach it either.
- A cut between two apps that both keep Runtipi's shared network (each publishes a service through the reverse proxy)
  can't be enforced: they still meet there. `wam connections` ("NOT CUT") and the view say so; unpublish one of them to
  enforce it. The shell, Agent Service and agent reach every app through the gateway, so they can't be marked sensitive.
- The gateway path, the start-time gate, host access, foreign/reserved networks, Traefik labels and the owner-only API
  are the same as in declared mode. `wizard-mesh` is reserved like every `wizard-*` network: a recipe that names it is
  refused (wam and the App Manager).

### How Docker does it

Docker can't remove one pair from a shared network, and one network per pair grows as N·(N−1)/2: 528 networks for 33
apps, 1770 for 60. Each is a Linux bridge with its own iptables rules, every container joins N−1 of them, and with
/27 subnets the 10.255.0.0/16 pool holds 2048, so ~64 apps would exhaust it (Docker's own default pools are smaller
still). Open mode therefore uses:

| Network | Members | Internal | Purpose |
|---|---|---|---|
| `wizard-mesh` | every app with no cut pair (its services that Runtipi would put on the shared network), each main service answering as its app id | yes | everything connected |
| `wizard-link-<a>--<b>` | an app out of the mesh + one app it may still reach (ids sorted); both main services answer by their app id | yes | one allowed pair |
| `wizard-app-<id>` | as in declared mode | no | internet and the published port |

Who leaves the mesh: every sensitive app (its reach depends on approvals), then a greedy vertex cover of the remaining
Disconnects (the app cut most often first; on a tie the app that was disconnected from). An app out of the mesh gets one
link per app it may still reach, so the box needs `1 + (apps out of the mesh) x (apps)` shared networks instead of
`N·(N−1)/2`: 1 network for 33 apps with nothing cut, 32 with one Disconnect, 59 for 60 apps with one Disconnect, 154 for 60 apps
with two Disconnects and a sensitive app that 40 of them may reach (computed by `plan()`, tests/test_network.py). The mesh is one /22 (about 1000 services) from the same pool; links stay /27. A Disconnect is applied live:
the app leaving the mesh joins its new links first, then leaves the mesh, so other pairs never drop. Gateway networks
(fresh boxes) are unchanged and the gateway components never join the mesh.

`wam connections` shows the mode in the first line ("Open: everything is connected unless you disconnect it") and, per
app, "can reach: every other app except ..." with the reason (you disconnected it, waiting for your approval, sensitive
and not connected) and whether it is on the mesh or on how many links. The live check compares Docker with this plan the
same way as in declared mode. The Connections view draws this as one picture (`renderConnections`, model in `wizard-views/connections-map.js`):
the agents on top, wired into one "All connected" cluster that holds the Apps and the Connectors, with the Wizard core
(System) below; each group has a plain title and one line saying what it is (see "Groups on the map" below). Only the
exceptions are drawn (a red broken line
for a Disconnect, an amber pulse for a request waiting for the owner, a lock on an app that asks first). Tapping an app
opens a sheet that says what it can talk to ("Movies can talk to: everything except Shopping") with Cut off from…,
Reconnect and Ask me first (= sensitive); a waiting request is a card with Allow / Don't allow. The mode switch ("Strict
mode: apps only reach what they ask for" = declared), the live check, the gateway and what can't be enforced sit under
Details.

### Groups on the map

Every item on the map sits in one group, and its sheet says which ("In Connectors"). The group comes from the
app's own `wizard.md`, by one rule in `wam/kinds.py`; no app id is listed anywhere. `wam connections --json` reports
it per app as `kind`.

| Group | What the owner reads | Rule, first match wins |
|---|---|---|
| (override) | | `kind: agent \| app \| connector \| system` in `wizard.md` |
| Agents | Your agents. They do things for you with your apps and connectors. | `type: agent` or `category: agent` |
| System | Wizard itself. It runs everything and links your agent to your apps. | the active shell and Agent Service (the gateway core), or `category: interface` |
| Connectors | Background services your apps and agent use. Nothing to open day to day. | `type: external`; or no `view:`; or `category: system` with a view (a screen that only sets it up) |
| Apps | Things you open and use. Each has its own screen. | `type: view`, and everything else |

`kind:` is optional and read only here: the checker ignores it, and an app without it is grouped by the rule. Use it
only when the rule gets an app wrong. Bad values are ignored.

Agents that are not apps are listed too (report `agents`): the host's own agent on an alongside box (`alongside.json`)
and the Agent Service's agents registry (`agents/<id>/agent.json`, wizard-os `spec/AGENTS.md`). The main agent shows as
"Your agent"; none known shows one "Your agent". An agent's line says what it can use: "every app and connector",
or "only the apps you picked for it" when its `agent.json` lists `permissions.wizard_apps`. Connectors are on the app
networks like apps: they can be cut off, asked first, and are in the "All connected" cluster. System items can't be
cut off; their sheet says so.

Switching to declared leaves the decisions as they are (Disconnects stay, open grants are kept but never link, open-mode
waits are dropped); switching back to open restores the mesh. `wam network off` still turns isolation off entirely.

## Limits

- Host processes reach every app's published `127.0.0.1` port, as before. Those are the gateway and anything else
  running as a host process. Layer 2 (per-grant keys) covers them.
- Ports that the host binds on all interfaces stay reachable from containers, as before.
- Links, the mesh and a Disconnect are two-way (see above). One-way enforcement would need per-link firewall rules.
- On fresh boxes each app can also reach the shell, Agent Service and agent over its `wizard-gw-<id>`, as it could on
  the shared network. The view lists them as "Wizard core". Other apps stay out of reach.
- Apps from other Runtipi stores stay on the shared network and can't reach isolated Wizard apps.
