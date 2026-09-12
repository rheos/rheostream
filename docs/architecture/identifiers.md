# Identifiers and record references

**Part of:** the [architecture specification](README.md). Closes guardrail 9 (identifiers are
globally safe) and the record-reference responsibility the idea document assigns to the core.
**Decision:** A2 in the [decision list](README.md#architecture-decisions).

## Why this needs a rule at all

rheoStream keeps one database per workspace ([storage](storage-and-workspaces.md)), exports a
workspace into a portable artifact and restores it into another deployment (FR 52), and has a
declared goal of a later local-first edition that syncs (D2). Identifiers are therefore minted in
many databases with no coordinator and later meet. A sequential integer would collide the first
time two workspaces were restored side by side. The house preference for small serial keys applies
to tables that live and die in one database; these do not.

## The scheme

| Kind | Form | Minted by | Notes |
| --- | --- | --- | --- |
| Durable record identifier | UUID version 7 (RFC 9562), stored as Postgres `uuid` | The service that creates the record, inside the creating transaction | Time-ordered, so primary-key indexes stay append-mostly. 16 bytes in every foreign key. |
| Event identifier | UUID version 7 | The outbox writer | One per outbox row. Distinct from the source event identifier below. |
| Operation, approval, job, session, token identifiers | UUID version 7 | The core | Same type everywhere, so a caller cannot tell a workspace record from a control-plane record by shape, and neither leaks routing. |
| Source event identifier | Opaque text, at most 512 bytes | The external source, or the transport connector when the source supplies none | Unique only within `(connection, source_event_id)`. Never a UUID by contract; never used as a primary key. |
| Configuration identifier | Slug `[a-z][a-z0-9_]{0,31}` | A person or a package author | Module ids, record type names, stage ids, field names, mapping ids, operation-set names. Human-chosen, immutable once referenced. |
| Workspace slug | Slug, unique in the control plane | The workspace creator | Display and switcher label only. Never used for storage routing; the UUID is. |
| Outbox position | `bigint` sequence, per workspace database | Postgres | Total order of events inside one database. Local, never exported as identity. |

No identifier encodes the workspace. Two records from two workspaces are distinguishable only by
which database they were read from, which is the property FR 2 wants: knowing an identifier is not
knowing where it lives, and it is not authorization (edge case 9).

Version 7 rather than version 4: the random part still gives coordination-free uniqueness, and
the leading timestamp keeps B-tree inserts local. Version 7 rather than a ULID string: Postgres has
a native 16-byte `uuid` type and no native ULID; storing ULIDs as text costs 26 bytes per key and
loses type checking. The Python core mints with the standard library where available and a vetted
fallback otherwise; the web tier never mints durable identifiers.

## Record references

A **record reference** is the string form the core, the modules, the MCP facade, events, audit
records, and exports all use to name a record without owning it:

```text
<module>.<record_type>:<uuid>

leads.opportunity:018f6b2e-8c1a-7d3e-9a4b-1c2d3e4f5a6b
relationships.party:018f6b2e-8c1a-7d3e-9a4b-7c8d9e0f1a2b
recallatron.memory:018f6b2f-0000-7000-8000-000000000001
core.operation:018f6b2f-0000-7000-8000-000000000002
```

Rules:

- The module segment is the owning module's id; `core` is reserved for core-owned records
  (operations, approvals). A delivery receipt is Leads-owned, so its reference is
  `leads.delivery_receipt:<uuid>`, not `core.`.
- The record type is declared in the owning module's manifest
  ([module contract](module-contract.md#owned-record-types)). A reference whose type no manifest
  declares fails validation.
- A reference is a value, never a join. A module that holds a reference to another module's record
  stores the string (or the UUID plus the type) in its own tables and resolves it through the core.
  It never has a foreign key into another module's schema. This is how FR 11 stays checkable by a
  static test (criterion 26).

## Resolution under permission

The core exposes one resolver:

```text
resolve(ref, ctx) -> RecordHead | Unavailable
```

`ctx` is the authenticated [workspace context](overview.md#the-workspace-context). The resolver
finds the owning module from the module segment, checks that the module is enabled in `ctx`'s
workspace, and calls the resolver that module registered for the record type
([extension points](module-contract.md#extension-points)). The module's resolver runs its own
permission check and returns a `RecordHead`:

| Field | Meaning |
| --- | --- |
| `ref` | The reference as given |
| `display` | A short label safe to show the caller (a title, a display name) |
| `readable` | Whether `ctx` may read the full record |
| `state` | `live`, `deleted`, or `unavailable` (module absent or disabled) |
| `revision` | The record's current revision when `live`: a mutable type's `revision` column, advanced only by a compare-and-set write ([storage adapter seam](storage-and-workspaces.md#revision-is-a-compare-and-set-not-a-counter)), never by a bare increment; the constant `1` for an immutable type (an observation, a receipt, a qualification). Null when not `live`. What `RecordStateGuard` compares with `approval.subject_revision` ([guards](confirmation-and-safety.md#execution-guards)). |

A caller that gets `readable = false` learns that the record exists in this workspace and nothing
else. A caller from another workspace gets `Unavailable`, indistinguishable from a reference that
never existed, because the lookup runs against the caller's own database and the record is not
there. Deleted records resolve to `state = deleted` from the deletion record
([deletion](deletion-export-migration.md)) so that references in audit and event history stay
identifiable, as the idea document requires for detached modules too.

A module's resolver may resolve an **alias**: the relationships module resolves a merged party
by following `merged_into_id` exactly once and returns the survivor's display with
`state = live`, so a reference written before a merge keeps resolving after it
([relationships](relationships.md#merge-and-unmerge-r4-criterion-53)). The core knows nothing of
aliases; a module that wants "the canonical reference" offers a read operation for it.

## Namespaces in one place

| Surface | Form | Example |
| --- | --- | --- |
| Record type | `<module>.<type>` | `leads.observation` |
| Event type | `<module>.<type>.<past-tense verb>` | `leads.observation.accepted` |
| Service operation | `<module>.<noun>.<verb>` | `leads.opportunity.transition` |
| MCP tool | `<module>_<verb>[_<noun>]` | `leads_transition`, `recallatron_recall` |
| Postgres schema | `<module>` inside the workspace database | `leads.opportunity` (schema.table) |
| Web route | path mode `/<module>/...`; subdomain mode `<host>/...` | see [topology](identity-and-topology.md) |
| Configuration key | `<module>.<section>.<key>` | `recallatron.retention.days` |
| Job kind | `<module>.<name>` | `recallatron.retention_sweep` |
| Consumer id | `<module>.<handler>` | `recallatron.on_record_deleted` |
| Secret scope | `<component>.<purpose>` | `runtime.claude_cli.credential` |

The registry refuses any registration whose name does not start with the registering module's id
(or `core`), which is what keeps the namespaces from colliding without a central list.
