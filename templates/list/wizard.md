---
wizard: 0.2
id: wizard-list
name: My List
type: service
category: app
version: 0.1.0
summary: A list with groups, one item per line, done and undo, through a screen, documented HTTP API and MCP.
license: GPL-3.0-only
source: https://github.com/humanitylabs-org/wizard-app-manager
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
api:
  port: 8799
  path: /api
  spec: openapi.json
mcp:
  path: /mcp
  port: 8799
  transport: streamable-http
skill: skill/SKILL.md
view:
  path: /
  port: 8799
  source:
    path: web
    build: python3 app/build_view.py
    dist: web
provides: [lists]
system: false
uses: []
needs: []
health: /healthz
---

# My List

Built from the Wizard `list` template (a generalised Tasks). Groups that fold, one item per line, tap a line to edit it,
Enter adds the next line below, done and undo, saved order, SQLite in app-data, an HTTP API and MCP over one service.

## Customise by editing text

`web/list.json` holds the words and fields. Both the server and the screen read it; change it, then run
`wam builder package-inline .` and `wam verify .`.

- `title`: the heading.
- `item.one` / `item.many` / `item.placeholder`: what one entry is called ("task", "book", "gift").
- `group.one` / `group.many` / `group.default`: what a section is called and the first section's name.
- `fields`: extra details per item, edited in the item's sheet: `{"name": "due", "label": "Due", "type": "date"}`.
  Types: `text` (with `max`), `number`, `date`. Keep it to two or three.
- `done.filter`, `empty.title`, `empty.copy`: everyday words for the filter and the empty screen.

Change code only for behaviour the config cannot express. Keep the shared kit (wizard-ui.css, wizard-sheet.js,
WizardApp.back): pop-ups are WzSheet sheets, Back closes the editor or sheet and never exits, and a tap paints at once
then the server reconciles.

Appearance decisions: [the canonical Wizard app style guide](https://github.com/humanitylabs-org/wizard-os/blob/main/style/APP-DESIGN-RULES.md).
