---
wizard: 0.2
id: hello-wizard
name: Hello Wizard
type: service
version: 0.1.0
summary: The template Wizard app. Two MCP tools that prove an agent can reach an installed app.
license: MIT
source: https://github.com/humanitylabs-org/wizard-app-manager
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8799
  transport: streamable-http
skill: skill/SKILL.md
provides: [diagnostics]
system: false
needs: []
health: /healthz
---

# Hello Wizard

Copy this folder to start a Wizard app. It has the four things every app needs:

1. `wizard.md` (this file): what the app is, how to run it, what it gives the agent.
2. `runtipi/`: a stock Runtipi `config.json` and `docker-compose.yml`.
3. An MCP endpoint (`/mcp`) the agent calls.
4. `skill/SKILL.md`: when and how the agent should use the app.

Ask your Wizard "what time is it on my server?" after installing, and it should call `wizard_time`.
