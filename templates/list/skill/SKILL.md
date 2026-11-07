---
name: wizard-list
description: Use when reading or changing the owner's list in this app.
version: 0.1.0
author: Humanity Labs
license: GPL-3.0-only
platforms: [linux, macos]
metadata:
  hermes:
    tags: [lists]
    related_skills: []
---
# The list

Use the single Wizard MCP gateway. `wizard_list_list` reads saved items in order with their groups; filter
(`active`, `completed`, `all`) and search are optional. `wizard_list_change` adds (`create: true`, `title`, optional
`group_id`, `after_id`), edits a title or configured `fields`, completes or reopens (`completed` true/false), deletes or
restores (`deleted` true/false) or moves (`group_id`, `after_id`) one item. `wizard_list_group_change` creates or
renames a group.

Every write needs a fresh lowercase UUID `operation_id` and, for a new item, a fresh UUID `id`. After a connection
failure retry the identical payload: it is never applied twice. Confirm writes with `wizard_list_list`; never claim
an item was saved without it. Keep the owner's words literal. Delete is reversible: set `deleted: false` to undo.
