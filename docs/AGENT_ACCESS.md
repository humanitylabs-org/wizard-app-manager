# Which apps each agent may use (Connections → Agents)

An **agent** is one configured instance: a name, the harness it runs on and its model. Miguel's main agent is
**Oscar · Hermes** (Claude Opus); the Agent Service's agents registry holds any others (wizard-os spec/AGENTS.md).
Connections lists them at the top: "Oscar · Hermes — Can use 15 apps, 5 agent apps, 11 connectors". An agent's page has
three groups (Apps, Agent apps, Connectors, the same groups as the map, `wam/kinds.py`), each item with an on/off switch,
and Turn all on per group.

## Where it is kept

The Agent Service keeps each agent's app lists; this is the one record, also used by the app settings sheet's "which
agents can use this app" (wizard-agent-service `docs/agent-app-access.md`):

| Agent | File (the Agent Service's data, `kinds.agents_dir`) |
|---|---|
| main agent (`hermes`) | `agents/.main-agent.json`: `{"schema": 1, "wizard_apps": [ids], "agent_apps": [ids]}` |
| any other agent | `agents/<id>/agent.json`: `permissions.wizard_apps`, `permissions.agent_apps` |

On Miguel's box: `~/runtipi/app-data/wizardapps/wizard-agent-service/data/wizard-os/agents/.main-agent.json`.
wam never writes these files: it changes them through the Agent Service's owner API on the box's loopback
(`PUT /wizard/v1/apps/<app>/agents`, which sets exactly which agents may use one app), `wam/agent_access.py`.

The main agent's name is what the owner calls his agent, the Agent Service's `settings.json` `agent.name` (e.g.
`"Oscar"`; the shell's `WizConfig.agent()`), else its `WIZARD_MAIN_AGENT_LABEL` (default "Hermes"). Its harness is
Hermes; its model is the host Hermes's own (`config.yaml` `model`) on an alongside box.

## Where it is enforced

The agent reaches app tools only through the Agent Service gateway (`/wizard/mcp`, wizard-agent-service
`server/agent-bridge.mjs`; the host Hermes's `mcp_servers.wizard` points at it, `linked.json`). For each agent it:

- lists only the apps on that agent's lists (`tools/list`, `wizard_apps`, the kernel text and pointer skills), and sends
  `notifications/tools/list_changed` to the agent's open sessions when the owner changes them;
- re-reads the lists on every `tools/call`, so a call to an app just turned off is refused at once, even if the agent
  still holds the tool from an older list: **"Oscar is not allowed to use Movies. The owner can turn it on in
  Connections."** The call never reaches the app. (A private app stays answered like one that is not installed.)

The host agent's other way to an app, the `wizard` CLI (`wizard tools|call <app>`), reads the same `.main-agent.json`
and refuses the same way (exit code 10, `not_allowed`); `wizard apps list` marks such an app `allowed: false`. A lists
file it can't read allows nothing (fail closed).

## Who may change it

Only the owner: the Connections page through `wam serve`'s owner listener (`POST api/agents/<agent>/access`
`{"apps": [ids], "on": bool}`, the same gate as every Connections change: the owner's Tailscale login from another
tailnet device, the shell's origin, `X-Wizard-Store`; never from the box itself, where every app container reaches Serve
as the owner), and on the server `wam agents access <agent> <app> on|off` (`wam agents list` shows each agent and what
is off). An app can't reach the Agent Service's owner API (it listens on the host's loopback). On an alongside box the
host agent runs as the owner's own Unix user, so this is the owner's switch, not a wall against an agent that already
holds the owner's account; on a fresh box the agent is a container without that access.

## Defaults

Everything on (Miguel, 29 Oct 2026). The main agent uses every installed app until the owner turns one off, and an app
installed later joins it. A new standard agent starts with every installed app on (the Agent Service's create; a client
that sends its own lists picks them). Private apps still need a tick, and a private agent starts with the private apps
only (#59).
