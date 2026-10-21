# Workspaces and storage

## What it is

A workspace is the boundary for everything: isolation, ownership, export, retention and
collaboration. A one-person setup starts with one workspace; a studio can give several
people access to one. Every workspace is its own Postgres database, named `ws_` followed
by the 32 hex digits of its UUID. Beside them sits one small control-plane database,
`rheo_control`, which holds only what must exist before any workspace can be reached:
accounts, login identities, the workspace registry, memberships, sessions, tokens, and
the worker's due-work index. It holds no domain records.

Inside a workspace database, the core owns a `core` schema and every installed module
owns a schema named after it: `recallatron`, `relationships`, `leads`, `current`.

## What you can do with it

- Create a workspace for an owner. Its creator is its owner, and the database is
  provisioned on the spot.
- Give a second person a membership with the role `owner` or `member`. An account is not
  a workspace; one person can belong to several.
- Switch the shell's active workspace. Every later request in that session routes to the
  new workspace with no identifier in any URL.
- Read a workspace's composition with `workspace_status`: the core version, each
  module's package and schema version, and its state.
- Set workspace and member settings within the limits the operator allows.

## How it works

**Routing is a function of who you are, not what you send.** Every entry point (the web
session, an MCP or CLI token, a job, an intake connection, a channel) builds a
`WorkspaceContext` before any service runs. Only the boundary factories can construct
one. Storage routing then follows a fixed path:

```text
ctx.workspace_id
  -> control.workspace row (state must be active)
  -> database_name
  -> the cluster connection, resolved from a secret reference
  -> a pool for that database
```

A database name is never accepted from a request, a model's tool argument, a payload, a
workspace setting or a configuration file. The only code that writes it is provisioning.
A test feeds a database name, a connection string, a schema name and a workspace id
through every input channel and checks that each one is ignored.

**Provisioning is a small state machine.** `CREATE DATABASE` cannot run inside a
transaction, so creating a workspace is a sequence of idempotent steps tracked on the
registry row: insert the row as `provisioning` with its owner, create the database,
apply the core migration chain, write the settings rows that must exist explicitly, set
the row `active`. A crash between steps leaves a row whose state says where it stopped,
and `rheo workspace repair` finishes it.

**One schema per module.** Tables are always written fully qualified
(`leads.opportunity`), and the core sets no `search_path`. That makes "a module writes
only its own storage" checkable: a test diffs the live catalog to confirm every object a
module's migrations create lands in its own schema, and scans the module's code for
another schema's names.

**Migrations have one entry point.** The core's migration orchestrator runs every chain.
It takes a transaction-scoped advisory lock per workspace, so two processes cannot
interleave DDL. If a workspace's core migration fails, that workspace goes `unavailable`
with the error recorded, the orchestrator moves on to the next one, and the failed one is
refused at the boundary instead of being served half-migrated. A workspace whose recorded
schema is newer than the running code is refused as `schema_ahead`.

**Connection pools are bounded by arithmetic you can check.** Each workspace gets its own
pool. An engine idle for 300 seconds is closed, at most 6 are cached, and each holds at
most 5 connections. With the control-plane engine and one maintenance connection that is
36 connections per process, 72 for core and worker together, against a stock Postgres
limit of 100. `rheo doctor` reports `ok` only while that figure stays within 80% of
`max_connections`, and names the three settings that move it. Busy engines are never
evicted, and the worker visits only workspaces that have work due, so an idle workspace
costs no connection at all.

**Revisions are compare-and-set.** A mutable record carries a `revision`. Every write
names the revision it read: `UPDATE ... WHERE id = $id AND revision = $expected`. If no
row matches, the write is refused as `record_stale` (HTTP 409) rather than silently
losing an update. Of two racing writers, exactly one wins, with or without a lock. The
approval guards rely on this.

**Configuration has three layers and a floor.** Package defaults, then the operator's
deployment settings, then workspace and member rows. Every key is declared once with its
type, scope and default; undeclared keys are rejected at every layer. Keys the operator's
security policy owns carry a floor: `min`, `union`, `subset` or `and`. A workspace
override that would loosen the operator's value is refused at write time, naming the
key, and a stored row is clamped at read time the moment the operator tightens it.

**Secrets are references.** A secret is named as `secret://file/...` or `secret://env/...`
and lives in a file store under the data root or in an environment variable. Only
components the core hands a scope to (the Claude CLI adapter, the identity provider, the
storage layer, the intake receiver) can resolve one, and only under their own prefixes.
Domain modules get no scope at all, so they have no way to call `resolve`. Even the
references do not travel: a model run names a credential slot such as `model`, and the
adapter maps it to a reference privately at the moment it starts the process.

**Files live under one data root.** Everything the deployment writes outside Postgres
sits in one operator-configured directory: deployment settings, the secret store,
per-workspace uploads, exports and scratch space, and cached model files. It is resolved
outside the source checkout by default, and modules receive paths only for a closed set
of purposes. A module cannot name a path.

**Identifiers are safe to move.** Durable records use UUID version 7, minted without a
coordinator and time-ordered for index locality. Records are named across modules by a
reference such as `leads.opportunity:<uuid>`. No identifier encodes its workspace, so
knowing an id tells you nothing about where it lives. People see a short ID, the last 8
hex characters, lengthened like a git short hash when two records share a tail.

## Why it's built this way

A connection opened against the wrong database cannot read another workspace's rows at
all. With one shared schema, isolation would depend on every query remembering a tenant
filter, every time, including inside library code that never heard of the rule. Here it
is structural.

The workspace is also the unit everything else already wants. Export, restore, backup
and deletion act on one workspace, and a dump of one database is exactly that unit.
Extensions such as pgvector are installed per database, and each database carries its own
schema versions, so a half-migrated cluster is a set of databases each in a known state,
never one database in an unknown mixed state.

The cost is operational: pools, provisioning and migrations multiply with workspaces.
The design bounds each one and states the numbers so an operator can check them.
Shared Postgres with row-level security and a schema per workspace were weighed and lost.
Per-workspace SQLite stays a contract goal: the storage seam is protocol-shaped, the
export format is backend-neutral, and no domain code names Postgres, so a second backend
has a contract to prove itself against.

## Where to look

- Specification: [storage, workspaces, configuration and secrets](../architecture/storage-and-workspaces.md),
  [identifiers](../architecture/identifiers.md),
  [the workspace context](../architecture/overview.md#the-workspace-context),
  [workspace layout](../workspace-layout.md)
- Code: [boundary factories](../../packages/core/src/rheo_core/boundary/factories.py),
  [storage routing](../../packages/core/src/rheo_core/storage/routing.py),
  [provisioning](../../packages/core/src/rheo_core/storage/provisioning.py),
  [pools](../../packages/core/src/rheo_core/storage/pools.py),
  [settings](../../packages/core/src/rheo_core/settings),
  [secrets](../../packages/core/src/rheo_core/secrets),
  [record references](../../packages/core/src/rheo_core/refs)
- Tests: [context routing](../../tests/postgres/test_context_routing.py),
  [isolation](../../tests/postgres/test_isolation.py),
  [engine pool](../../tests/test_engine_pool.py),
  [settings floor end to end](../../tests/postgres/test_settings_floor_e2e.py),
  [secrets](../../tests/test_secrets.py),
  [module storage ownership](../../tests/postgres/test_module_storage_ownership.py),
  [record stale refusal](../../tests/test_record_stale_refusal.py)
