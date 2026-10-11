# Current

Work in motion: work items, projects, blocked-by dependencies and a workspace list of
areas. Current requires no other module and runs without Leads. Its web screens arrive
separately; everything below is reachable through operations and MCP today.

Specialist modules own their specialist records and outcomes. Completing a work item
does not establish that an external submission or business decision succeeded.

## Records

- **Work item** (`current.item`): title, body, status, priority, one area, an optional
  due date, project and snooze time, provenance (`manual`, `agent`, `email`, `alert`,
  `handoff`), an optional source kind and reference, an event count and a revision.
- **Project** (`current.project`): title, description, status (`active`, `paused`,
  `done`), area, due date and revision. An item belongs to at most one project.
- **Comment**: append-only, on one item. The author is the authenticated actor, never a
  value the caller supplies. Source dedup writes `system` comments.
- **Dependency**: item A is blocked by item B. Edges that would form a loop are refused.
- **Area**: a key and label in a workspace list, seeded with eight defaults. Areas are
  retired rather than deleted, so a retired area stays readable on the items that carry
  it; new values must name a live area. The list is a table, not a settings key, because
  the settings types (string, integer, boolean, string list) cannot hold a key, a label
  and a retirement time together.

## Status and snooze

`open`, `waiting` and `snoozed` move freely between each other; any status can become
`done`, and `done` reopens to `open`. Only a snoozed item holds `snoozed_until`, which
must be in the future when set; database checks hold the same rule. An expired snooze
needs no job: the default `active` list includes it, flagged `woke`.

## Source dedup

`current.item.create` with `source_kind` and `source_ref` matches any stored item with
that pair, snoozed and done ones included. A `fingerprint` (such as the latest message
id) already seen changes only `updated_at`. Any other repeat raises the event count,
adds a system comment and raises priority if the new signal's is higher, never lower:
a done item is `reopened`, a snoozed one `woke`, anything else `grouped`. A repeat never
changes title, body, area or project.

## Operations and roles

| Operation | Class | Roles | MCP tool |
| --- | --- | --- | --- |
| `current.item.create` | mutate | owner, member | `current_create` |
| `current.item.get`, `current.item.list` | read | owner, member, service | `current_get`, `current_list` |
| `current.item.update` | mutate | owner, member | `current_update` |
| `current.item.set_status` | mutate | owner, member | `current_status` |
| `current.item.comment` | mutate | owner, member | `current_comment` |
| `current.dependency.add`, `.remove` | mutate | owner, member | `current_add_dependency`, `current_remove_dependency` |
| `current.project.create`, `.update` | mutate | owner, member | `current_create_project`, `current_update_project` |
| `current.project.get` | read | owner, member, service | none |
| `current.project.list` | read | owner, member, service | `current_list_projects` |
| `current.area.list` | read | owner, member, service | `current_areas` |
| `current.area.create`, `.update` | mutate | owner | none |
| `current.summary.get` | read | owner, member, service | `current_summary` |
| `core.record.delete` on `current.item`, `current.project` | destructive | owner | `current_delete_item`, `current_delete_project` |

Updates are compare-and-set on the revision and answer `record_stale` when it moved on.
A null or omitted update field stays as it is; `clear` names the optional fields to
empty. The MCP tool facade forwards every field, defaults included, so field presence
cannot carry that meaning. Comments and dependency edges do not change an item's
revision.

Deleting an item removes its comments, seen fingerprints and dependency edges. Deleting
a project detaches its items, bumping each item's revision, and deletes none of them.
Nothing outside Current refers to its records yet, so it registers no deletion
participants.

Events carry a reference and revision only: `current.item.created`,
`current.item.completed` and `current.item.reopened`.

Export format 1 covers every table. Restore expects a freshly installed module: it
replaces the seeded area list with the exported one and refuses if any other table
holds rows.
