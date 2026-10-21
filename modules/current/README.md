# Current

Work in motion: work items, projects, blocked-by dependencies and a workspace list of
areas. Current requires no other module and runs without Leads. Everything below is
reachable through operations and MCP, and through the screens described under
[Screens](#screens).

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

`current.item.close_by_source` closes the item that holds a `source_kind` and
`source_ref`, so a caller that keeps no ref or revision can close what it filed. An
open, waiting or snoozed item becomes `done` with one system comment naming the closing
actor kind (plus the optional note, up to 2000 characters), and the result is
`closed: true` with the ref. When no item holds the pair, or it is already done, the
close is a quiet no-op: `closed: false`, no comment, no event, no new revision, never
`not_found`. Either way the close clears the found item's seen fingerprints, so the
next create with the same pair reopens it even if it carries a fingerprint sent before
the close. A person's move to `done` through `current.item.set_status` does not clear
them: an email thread closed by hand stays closed when intake re-reads it with the same
newest-message fingerprint.

## Operations and roles

| Operation | Class | Roles | MCP tool |
| --- | --- | --- | --- |
| `current.item.create` | mutate | owner, member | `current_create` |
| `current.item.get`, `current.item.list` | read | owner, member, service | `current_get`, `current_list` |
| `current.item.update` | mutate | owner, member | `current_update` |
| `current.item.set_status` | mutate | owner, member | `current_status` |
| `current.item.close_by_source` | mutate | owner, member | `current_close_by_source` |
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

## External writers and readers

A service or scheduled job that files alert items (a health probe, a disk guardrail, a
support form) holds an `alert_writer` token. That named set carries exactly two
operations, `current.item.create` and `current.item.close_by_source`, and is enumerated
so it never grows when an operation is added. Issue one token per writer repo with the
operator command, under the owner account:

```bash
rheo token issue --account <owner account id> --workspace <workspace id> --set alert_writer --kind cli
```

A `cli` token lives at most 90 days (`identity.token_max_days.cli`), and each one is
revoked on its own without touching the others. The writer calls
`POST /api/v1/operations/<operation>` with the token as its bearer. Its calls run as the
token, not the person: the audit record has `actor_kind` `token` and the token id as
`actor_id`, and the system comment on a close names the actor kind `token`. A person's
own web session shows as `actor_kind` `account`. `rheo token list` prints each token's
id, set and expiry but stores no label, so it cannot tell two writer tokens apart: record
each issued token id against its writer repo (for example in the Current renewal item),
and a token id in the audit trail then maps to exactly one writer.

A `read_only` `cli` token reads Current over the same route: its set holds every read
operation, including `current.item.list` and `current.item.get`, and a route test checks
both. It also reads every other module, because `read_only` is not limited to Current.

Residual exposure: a leaked writer token can open items in any area and close any
source-keyed item of any `source_kind`, including an email thread whose id it guesses,
until it expires or is revoked. It can also reopen any existing source-keyed item, or
raise its priority, by creating with that item's pair. It cannot read, update, comment
on or delete items, and it cannot reach another module.

Audit growth: every close by source writes one audit record, a no-op close included,
because the dispatcher audits every mutate. Release one does not prune audit records. A
writer that closes on every healthy check therefore adds one record per check.

## Screens

The web package is `modules/current/web` (`@rheo-stream/current-web`). It serves the
`current` surface (`current.<base host>` in subdomain mode, `/current` in path mode)
and adds one main-nav entry, Current. Routes are exact paths; a record travels as
`?ref=` in the query string.

| Path | Screen |
| --- | --- |
| `/` | Queue: summary counts, a Woke up section for expired snoozes, then active items by priority and last update. Filters for status, area, project, priority, overdue and blocked, full-text search and paging. Each row has Done, Snooze and Wait (or Back to open). |
| `/item?ref=` | Item: details, notes, status controls, snooze picker, comments, blocked-by and blocking links, and origin. |
| `/new` | New item. `?project=` preselects a project. |
| `/projects`, `/project?ref=` | Projects with item counts by status; one project's items, with create and edit. |
| `/areas` | Areas, owner only: add, rename, retire and restore. |

Screens read through Current's own read operations and write through the shell's
form path, which accepts only the `MUTATE` operations the manifest declares as forms.
Deleting an item or project stays with `core.record.delete` and its per-action
approval, so the screens have no delete button.

Status buttons offer only the moves the service allows from the item's current
status. Snooze quick picks (1 hour, this evening at 7pm, tomorrow at 9am, Saturday
and Monday at 9am) are computed in the browser at the moment of the click, in the
viewer's time zone; this evening is hidden after 7pm. Edits send only the fields that
changed with the revision the page read; blanking an optional field names it in
`clear`. A `record_stale` refusal keeps what was typed and offers a reload. Area order
is the order areas were added: there is no reorder operation.
