# Storage, settings and secrets

## What it is

rheoStream keeps your records in Postgres and everything else (settings, secrets,
uploads, exports, cached model files) in one data directory outside the source checkout.

All of it belongs to a workspace: the unit you own, export, set retention for, and share
with collaborators. Most people running rheoStream for themselves have exactly one
workspace and never think about it. A studio can give several people access to that one
workspace, and an operator can host several workspaces side by side.

Inside the database, the core and each installed module keep their tables in their own
schema: `core`, `recallatron`, `relationships`, `leads`, `current`.

## What you can do with it

- Give a second person a membership with the role `owner` or `member`.
- Read the workspace's composition with `workspace_status`: the core version, each
  module's package and schema version, and its state.
- Set workspace and member settings within the limits the operator allows.
- Keep credentials as references that domain code can never resolve.
- Create more workspaces when you need them, and switch between them in the shell.

## How it works

### One schema per module

Tables are always written fully qualified (`leads.opportunity`), and the core sets no
`search_path`. That makes "a module writes only its own storage" checkable: a test diffs
the live catalog to confirm every object a module's migrations create lands in its own
schema, and scans the module's code for another schema's names.

### Migrations have one entry point

The core's migration orchestrator runs every chain, under an advisory lock, so two
processes cannot interleave DDL. A failed core migration marks the workspace `unavailable`
with the error recorded, and the boundary refuses it instead of serving it half-migrated.
A database whose recorded schema is newer than the running code is refused as
`schema_ahead`.

### Revisions are compare-and-set

A mutable record carries a `revision`. Every write names the revision it read: `UPDATE ...
WHERE id = $id AND revision = $expected`. If no row matches, the write is refused as
`record_stale` (HTTP 409) rather than silently losing an update. Of two racing writers,
exactly one wins, with or without a lock. The approval guards rely on this.

### Configuration has three layers and a floor

Package defaults, then the operator's deployment settings, then workspace and member rows.
Every key is declared once with its type, scope and default; undeclared keys are rejected
at every layer. Keys the operator's security policy owns carry a floor: `min`, `union`,
`subset` or `and`. A workspace override that would loosen the operator's value is refused
at write time, naming the key, and a stored row is clamped at read time the moment the
operator tightens it.

### Secrets are references

A secret is named as `secret://file/...` or `secret://env/...` and lives in a file store
under the data root or in an environment variable. Only components the core hands a scope
to (the Claude CLI adapter, the identity provider, the storage layer, the intake receiver)
can resolve one, and only under their own prefixes. Domain modules get no scope at all, so
they have no way to call `resolve`. Even the references do not travel: a model run names a
credential slot such as `model`, and the adapter maps it to a reference privately at the
moment it starts the process.

### Files live under one data root

Everything the deployment writes outside Postgres sits in one operator-configured
directory: deployment settings, the secret store, uploads, exports, scratch space and
cached model files. It is resolved outside the source checkout by default, and modules
receive paths only for a closed set of purposes. A module cannot name a path.

### Identifiers

Durable records use UUID version 7, minted without a coordinator and time-ordered for
index locality. Records are named across modules by a reference such as
`leads.opportunity:<uuid>`. No identifier encodes where it lives, so a record keeps its id
through an export and a restore into another deployment. People see a short ID, the last
8 hex characters, lengthened like a git short hash when two records share a tail.

### Every request names its workspace from the server side

Every entry point (the web session, an MCP or CLI token, a job, an intake connection, a
channel) builds a `WorkspaceContext` before any service runs, from the authenticated
session or token. Only the boundary factories can construct one. A workspace or database
named in a request body, a model's tool argument or a payload is ignored, and a test feeds
one through every input channel to prove it.

## Why it's built this way

The parts that matter day to day are the boring ones done strictly. Each module owns its
own schema, so modules stay separable. Credentials are references that only the component
presenting them can resolve, so a module or a model never holds one. Identifiers survive
export and restore, so your data can move.

## Running more than one workspace

This matters to an operator hosting several workspaces, or to anyone curious about the
hosted design. The [storage specification](../architecture/storage-and-workspaces.md#a1-the-per-workspace-unit-is-a-database)
has the full reasoning.

Each workspace is its own Postgres database, named `ws_` plus the 32 hex digits of its
UUID, beside one small control-plane database, `rheo_control`, that holds accounts,
memberships, sessions, tokens and the workspace registry. Storage routing goes from the
server-resolved context to the registry row to that database, so a connection opened for
one workspace cannot read another's rows at all. Export, backup and deletion then act on
one database each.

Creating a workspace is a small state machine, because `CREATE DATABASE` cannot run inside
a transaction: each step is idempotent and recorded on the registry row, and
`rheo workspace repair` finishes one a crash interrupted. Connection pools are bounded per
process (6 cached engines of 5 connections, plus the control plane and a maintenance
connection: 36 per process, 72 for core and worker) and `rheo doctor` checks that figure
against Postgres's limit. The worker visits only workspaces with work due, so an idle
workspace costs no connection.

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
