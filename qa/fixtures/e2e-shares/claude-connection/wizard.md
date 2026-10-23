---
wizard: 0.2
id: wizard-claude
name: Claude
type: external
category: system
tags: [security]
version: 0.3.0
summary: Connect your Claude subscription once; every agent on the box uses it, and you see how much is left.
icon: icon.png
license: MIT
source: https://github.com/humanitylabs-org/claude-connection
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8799
  transport: streamable-http
skill: SKILL.md
view:
  path: /
  port: 8799
provides:
- ai-connection
- claude-subscription
shares:
- id: claude-login
  path: claude
health: /health
---

# Claude

Connect your Claude Pro or Max subscription once on the Claude page, and every agent on this Wizard uses it. The sign-in runs Anthropic's official Claude Code login and stays in this app's app-data (`data/claude`). The app exports the whole folder as `claude-login`; App Manager mounts it into a consumer only while the owner grants access.

This is a Wizard AI connection app (`wizard.ai-connection/2`): `GET /api/connection` reports whether Claude is connected, your plan, and your current usage limits, and the MCP tool `connection_status` gives agents the same answer. It replaces the Claude part of AI Accounts. App Manager migrates the old login volume once into app-data, retaining ownership and modes; the app never copies or migrates credentials itself.
