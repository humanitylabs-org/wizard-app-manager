---
name: hello-wizard
description: Use when the owner asks for the server time or wants to check that installed Wizard apps are reachable.
---

# Hello Wizard

- `wizard_time` returns the server's UTC time. Convert it to the owner's timezone when you answer.
- `wizard_echo` echoes text. Use it once to prove the app is reachable, then say so plainly.

If the MCP call fails, say the app is unreachable and suggest `wam doctor`.
