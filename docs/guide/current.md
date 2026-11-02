# Current

## What it is

The work-in-motion module: the tasks, projects, commitments and dependencies that move
work toward done. Work can start from an opportunity in Leads, from an alert, from an
email, from an agent, or from nothing but your own decision. Current requires no other
module and runs without Leads.

It holds five kinds of record:

| Record | What it holds |
| --- | --- |
| Work item | Title, body, status, priority, one area, an optional due date, project and snooze time, and where it came from (`manual`, `agent`, `email`, `alert`, `handoff`) |
| Project | Title, description, status (`active`, `paused`, `done`), area and due date. An item belongs to at most one project |
| Comment | Append-only, on one item. The author is the signed-in actor, never a value the caller supplies |
| Dependency | Item A is blocked by item B. An edge that would form a loop is refused |
| Area | A key and a label in the workspace's list. Areas are retired, never deleted, so old items keep a readable area |

## What you can do with it

- Capture work from anywhere: the queue screen, an agent through the `current_*` tools,
  an alert or an email source, or a handoff from Leads.
- Move items between `open`, `waiting` and `snoozed`, mark them `done`, and reopen them.
- Snooze an item until a time; when the time passes it shows up again, flagged as woke.
- Group items into projects and see each project's items by status.
- Mark what blocks what, and see both directions on an item.
- Work the queue: counts at the top, woke items first, then active items by priority and
  last update, with filters for status, area, project, priority, overdue and blocked.
- Ask for a summary of the workspace's work in one call.

## How it works

### Repeated signals become one item

Create an item with a source kind and a source reference, and Current looks for an
existing item with that pair, including snoozed and done ones. A fingerprint it has
already seen (the latest message id, say) changes only the item's update time. Any other
repeat raises the item's event count, adds a system comment, and raises its priority if
the new signal's is higher, never lower. A done item is reopened, a snoozed one wakes,
anything else is grouped. A repeat never changes the title, body, area or project you set.

### Snooze needs no job

Only a snoozed item holds a snooze time, it must be in the future when set, and database
checks hold the same rule. When the time passes, nothing has to run: the default active
list simply includes the item and flags it as woke.

### Every edit names the revision it read

Updates are compare-and-set. If the item moved on since you loaded it, the edit is refused
`record_stale` and the screen keeps what you typed and offers a reload. Leaving a field
out leaves it alone; emptying an optional field means naming it in `clear`, because an
agent's tool call sends every field, so presence cannot carry that meaning.

### A handoff becomes one approved item

A handoff, such as a qualified lead from Leads, arrives through
`current.item.create_external`. Owners and members may stage it, and it has no MCP tool.
Staging only creates an approval request: nothing in Current changes until a person
approves it in the shell. A token may stage the request but can never approve it.

The operation is external class and keyed on `idempotency_key`. After approval, the core's
continuation rebuilds the held caller, runs its guards again and writes the item through
Current's one registered destination, `current`. The operation's own handler always
refuses, so no other route creates anything.

The request carries a source reference, a title, a body, a priority, an area and a due
date. It has no project field, because filing the work under a project is a person's
later choice. The item gets the source kind `handoff`, which is reserved for this path:
`current.item.create` refuses it. The core's stored result answers a retry of the same
approved request, and when that result is gone Current's source link still returns the
first item. A lost response never creates a second item, and a retry never reopens,
wakes or regroups it.

### The source link outlasts the item

Each handed-off item has one `current.source_item_link` row, written in the same
transaction. It holds the source reference, the item reference (fixed for the row's
life), one revision, the item state (`available` or `deleted`), the live project, the
last project the item left, and a project state (`never_assigned`, `linked`, `cleared`
or `deleted`). Only paths Current already has change it:

- Assigning, moving or clearing the project stays in the existing item edit form,
  through `current.item.update`. There is no separate assign step.
- Approved item deletion marks the item `deleted` and leaves a linked project live. The
  row stays as a tombstone, so the same source can never create the item again.
- Approved project deletion moves that project to history, state `deleted`, on every link
  that holds it, including links whose item is already gone.

Each real change moves the revision by one and publishes one
`current.source_item_link.changed` event whose data is the whole link. Repeating the
same assignment, or clearing a project that is already empty, changes nothing and
publishes nothing.

`current.item.link_state` reads the link by source reference. It is its own read for
owners, members and services, with no tool, and it reads no item or project row: it
still answers after the item is deleted, and it neither needs nor grants
`current.item.get` or `current.project.get`. The reasons for this shape are in
[ADR-0001](../adr/0001-leads-current-project-link.md).

### One copy of the task

Current holds the only editable copy of the task. A module that handed work over reads
where it went from the link, through `current.item.link_state` or the change event, and
reads item or project content only through Current's own reads, each under its own
permission.

### Completion claims nothing else

Completing a work item publishes `current.item.completed`, which no other module may treat
as proof of anything else. Finishing the task "submit the dossier" does not mean the
dossier was accepted; the module that owns the dossier says that.

### Events carry references only

`current.item.created`, `.completed` and `.reopened` carry a reference and a revision,
never the item's text. `current.source_item_link.changed` carries the link's references,
states and revision, and no item or project text either.

### Deletion goes through approval

Deleting an item or a project is destructive, owner only, and uses the core's per-action
approval, so the screens have no delete button. Deleting an item removes its comments,
seen fingerprints and dependency edges. Deleting a project detaches its items and deletes
none of them.

### Screens

The `current` surface has a queue, an item page, a new-item form, projects and areas.
Screens read through Current's own read operations and write only through the forms the
manifest declares. Status buttons offer only the moves the service allows from the item's
current status.

## Why it's built this way

Most task tools fill up with the same alert over and over. Source dedup turns a stream of
repeated signals into one item that wakes, reopens or climbs in priority, and never
rewrites what you wrote about it.

The boundary matters as much as the features. Current carries work; it does not become
invoicing or accounting, and it does not get to declare that another module's outcome
happened. That keeps it useful for any profession: it can track the preparation work for a
regulatory dossier while a dossier module owns the evidence and the submission.

## Where to look

- Specification: [work-in-motion design](../architecture/later-phases.md#phase-5-the-work-in-motion-module),
  [keyed idempotency](../architecture/intake-and-events.md#idempotency),
  [the handoff operation](../architecture/confirmation-and-safety.md#the-handoff-operation-fr-45-criterion-64),
  [ADR-0001: the handoff and the source link](../adr/0001-leads-current-project-link.md),
  [module guide](../../modules/current/README.md)
- Code: [module](../../modules/current/src/rheo_current),
  [items](../../modules/current/src/rheo_current/items.py),
  [approved external items](../../modules/current/src/rheo_current/external.py),
  [source link](../../modules/current/src/rheo_current/links.py),
  [projects](../../modules/current/src/rheo_current/projects.py),
  [areas](../../modules/current/src/rheo_current/areas.py),
  [screens](../../modules/current/web)
- Tests: [Current module](../../tests/postgres/test_current_module.py),
  [approved external items](../../tests/postgres/test_current_external.py),
  [source link](../../tests/postgres/test_current_source_link.py),
  [module tests](../../modules/current/tests)
