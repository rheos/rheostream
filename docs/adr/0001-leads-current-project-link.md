# ADR-0001: Leads hands work to Current through one approved item and a lasting source link
Status: accepted
Date: 2026-10-29

## Context

A qualified lead should become a work item in Current without Leads writing Current's
tables, and without Current knowing Leads exists. A person later files that item under
a project, moves it, clears it, deletes the item or deletes the project. Leads still
has to say where the work went after any of that, including after the item is gone.

Three tradeoffs shaped the decision.

1. **Who may create the item.** A plain Current write would let any caller holding
   a token create items in someone's list. Making it owner-only would stop a member
   from handing off their own lead. The create has a real effect outside the
   requesting module, so it is the first production `EXTERNAL` operation, and it must
   run only after a person approves it.
2. **Where the project relation lives.** Leads could copy project data, but a copy
   goes stale and needs its own permissions. Current could add a project field to
   the approved request, but then a handoff would quietly file work into a project
   nobody chose at review time.
3. **What survives deletion.** If the relation lived on the item row it would vanish
   with the item, and a retry of the same approved source could recreate an item a
   person had deliberately deleted.

## Decision

**One approved, member-staged create.** Current declares
`current.item.create_external`: `EXTERNAL`, `KEYED` on `idempotency_key`, roles owner
and member, the exact `ExternalSpec` core's production allowlist names, one in-process
destination `current`, and no MCP tool. Staging it only creates an approval request;
nothing in Current changes. A service context is refused at staging, because core
could never rebuild it as the held person. The item is written only inside core's
approved continuation, through the registered destination, after core rebuilds the
held caller and runs every guard again. The operation's declared handler refuses
inline, so a call that reached it any other way creates nothing.

**Tokens may stage, never decide.** The operation is deliberately not in core's
`rheo_core.tokens.policy.NON_TOKEN_ISSUABLE`. That list is core-owned and holds the
operations that disclose or decide an approval (`core.approval.approve`, `.refuse`,
`.preview`) or mint credentials. A token holding the external create can do one
thing with it: stage an approval that a person must decide in the shell. It cannot
approve its own request, no token can carry an approval operation, and the
continuation rebuilds the held token from its own row, membership and operation
snapshot before the effect. The Leads handoff router stages under the requesting
person's own context. Adding the create
to the list would buy nothing the approval does not already guarantee, and would
put a module operation into a core policy constant for no reason. A test pins this:
a member's `mcp` token dispatching the create gets `approval_required`, cannot
approve, and a worker visit creates no item and no link.

**A lasting source link owned by Current.** Current keeps one
`current.source_item_link` row per approved source: the source ref, the item ref
(fixed for the row's life), one revision, the item state (`available`, `deleted`),
the live project ref, the last project ref and the project state (`never_assigned`,
`linked`, `cleared`, `deleted`). The row is created with the item in one transaction
and is changed only by paths Current already owns:

- `current.item.update` assigns, moves and clears the project. It stays the only way
  to change an item's project, through the existing item edit screen.
- Approved item deletion marks the item deleted and leaves a linked project live.
- Approved project deletion moves that project to history on every link holding it,
  including links whose item is already gone.

Each real change moves the revision by one and publishes one schema-versioned
`current.source_item_link.changed` event whose data is the whole link. Repeating the
same assignment, or clearing an empty project, changes nothing and publishes nothing.
A deleted item's row stays as a tombstone, so the same source can never create the
item again, even after core's retained idempotency row has expired.

**A separate lifecycle read.** `current.item.link_state` returns the link by source
ref. It is its own `READ` operation for owner, member and service roles, with no
tool, and it reads no item or project row. Holding it does not grant
`current.item.get` or `current.project.get`, and holding those does not grant it;
each content read authorizes on its own. Dispatch still applies the caller's
workspace, role, operation set and module readiness.

**Ordinary create keeps its shape.** `current.item.create` refuses `source_kind`
`handoff`, which only the destination writes, because the destination adopts an
existing item that holds a handoff source pair. Its `provenance` may still be
`handoff`: provenance describes where a request came from and dedups nothing.

## Consequences

- Current installs and runs without Leads. It imports nothing from Leads, names no
  Leads type, and its event carries only references and states.
- Leads stores a minimal projection (project ref, history, state, revision) and
  copies only newer revisions from the event or from `current.item.link_state`. It
  shows item and project content only from live, separately authorized reads.
- Export format 2 carries the link table. Restore refuses an artifact in which a
  handoff item and its link disagree, so a restore cannot bring back a deleted
  source's item or drop the project history of one that survives. Format 1 artifacts
  no longer restore, as with every format change.
- Current publishes record routes for `item` and `project` (route ids `item` and
  `project`, query key `ref`), so another module can link a reference it holds
  without knowing Current's host or paths.
