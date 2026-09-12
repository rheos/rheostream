# Storage, workspaces, configuration, and secrets

**Part of:** the [architecture specification](README.md). Designs against D1, D2, D9, R1, and
FR 2, FR 5 to FR 14, FR 29, FR 30. Resolves the database-or-schema question the requirements
leave open.
**Decisions:** A1 (database per workspace), A3 (schema per module), A4 (secret store), A5
(configuration precedence and the policy floor). See the [decision list](README.md#architecture-decisions).

## A1. The per-workspace unit is a database

D1 allows "one database (or schema) per workspace". This specification chooses **one Postgres
database per workspace**, all in one cluster for release one, plus one control-plane database.

Why a database and not a schema:

1. **A connection is bound to exactly one workspace.** With schemas, every connection can reach
   every tenant and isolation depends on `search_path` being set correctly on every query, every
   time, including inside library code that never heard of the rule. That is the class of mistake
   FR 2 and guardrail 3 exist to make structurally impossible. With a database per workspace, a
   connection opened against the wrong database cannot read another workspace's rows at all.
2. **The workspace is the unit everything else already wants.** Export, restore, backup, and
   deletion (FR 51, FR 52) are per workspace. `pg_dump` of one database is that unit exactly;
   `pg_dump --schema` across shared tables is not, and a schema-per-workspace layout leaves
   sequences, extensions, and grants shared.
3. **Extensions and their versions are per database.** pgvector and `pg_trgm` are installed and
   versioned in the workspace database when the module that needs them is installed there
   (D9, FR 30). Two workspaces can carry two module versions with different extension needs.
4. **Migration state has one place to live.** Each database carries its own core and module
   schema versions (FR 9), so a half-migrated cluster is a set of databases each in a known state,
   never one database in an unknown mixed state.

What it costs, and how the cost is bounded:

- **One connection pool per workspace, bounded by idle close and then by a count cap.** An
  engine untouched for `storage.pool_idle_close_seconds` (default 300) is disposed on the next
  call; `storage.pool_cache_size` (default 16) is the hard ceiling behind that, evicting
  least-recently-used; each pool is capped at `storage.pool_max_connections` (default 5).

  **Two bounds, because the scarce resource is connections and not pools.** The product of the
  last two is the process's worst-case connection count — 80 on the defaults — and the core and
  the worker are separate processes each holding their own, against a stock Postgres
  `max_connections` of 100. An earlier default pair of 32 and 5 put one process at 160, over-
  subscribing a default cluster before the cache ever filled, which is why the ceiling is stated
  here as arithmetic an operator can check rather than as a number in isolation. `rheo doctor`
  reports it.

  **Idle close is the bound that normally binds, and that is the point.** A count cap on its own
  is adversarial to any caller that walks workspaces in turn: under a pure LRU the
  least-recently-used engine is always the one the walk is about to reach next, so past the cap
  every visit evicts the engine it needs and the deployment thrashes rather than degrades.
  Expiry by idleness reclaims the engines nobody wanted instead, so the walk pays for the
  workspaces it is actually serving.

  **The worker must not defeat this** ([jobs and the worker](intake-and-events.md#jobs-and-the-worker-fr-16)):
  it visits workspaces with due work, not every active workspace, so an idle workspace costs no
  engine at all. A worker that polled all of them would hold every pool hot and put the count cap
  back in charge. Release one runs one human's workspaces; the hosted edition revisits the whole
  scheme ([later phases](later-phases.md#phase-8-the-hosted-edition)).
- **`CREATE DATABASE` cannot run inside a transaction.** Provisioning is therefore an idempotent,
  registry-tracked operation with an explicit state machine (below), not a step inside workspace
  creation's transaction.
- **No cross-workspace SQL.** There is no query that joins two workspaces. Operational metrics
  across workspaces are computed by iterating the registry, and that iteration is the operator
  command's job, not the domain layer's.
- **Migration orchestration iterates databases.** Described under [migrations](#migrations).

## The control plane

One database, `rheo_control`, holding what has to exist before any workspace is routable. It holds
no domain records and, in release one, no billing (D1). Every table below is in the `control`
schema.

| Table | Columns | Notes |
| --- | --- | --- |
| `account` | `id uuid`, `display_name text`, `created_at timestamptz`, `disabled_at timestamptz null` | A person. Not a workspace. |
| `identity` | `id uuid`, `account_id`, `provider_id text`, `provider_subject text`, `email text null`, `email_verified boolean`, `created_at` | One row per login identity; unique on `(provider_id, provider_subject)`. `provider_id` is the identity provider's registered id (`github` first). |
| `workspace` | `id uuid`, `slug text unique`, `display_name text`, `database_name text`, `state text`, `created_at`, `state_changed_at`, `state_detail text null` | `state` in `provisioning`, `migrating`, `active`, `unavailable`, `restoring`. `database_name` is derived, see routing. |
| `membership` | `account_id`, `workspace_id`, `role text`, `created_at` | Primary key `(account_id, workspace_id)`. `role` is `owner` or `member` (R1, FR 5). |
| `session` | `id uuid`, `account_id`, `active_workspace_id uuid null`, `active_workspace_changed_at timestamptz null`, `created_at`, `last_seen_at`, `expires_at`, `revoked_at null` | The web session. Its active workspace is the only source of "which workspace" for web requests; `active_workspace_changed_at` records the last switch. The id never leaves the server. |
| `session_secret` | `secret_hash bytea pk`, `session_id`, `host text`, `created_at` | The SHA-256 of the per-host session secret the browser holds in its cookie; unique `(session_id, host)`. One session has one row per application host it has visited ([identity](identity-and-topology.md#sessions-and-cookies)). |
| `session_grant` | `code_hash bytea`, `session_id`, `target_host text`, `nonce_hash bytea`, `expires_at`, `used_at null` | The one-time cross-host exchange in subdomain mode; `nonce_hash` binds the grant to the browser that asked for it ([identity](identity-and-topology.md#sessions-and-cookies)). |
| `access_token` | `id uuid`, `token_hash bytea`, `account_id`, `workspace_id`, `kind text`, `issued_from text`, `set_name text null`, `purpose text null`, `created_at`, `expires_at`, `revoked_at null`, `last_used_at null` | CLI and MCP tokens (FR 4). `kind` in `cli`, `mcp`, `runtime`; `issued_from` in `session`, `operator`, `runtime`, with a check constraint that `kind = runtime` and `issued_from = runtime` hold together. `set_name` is the package set the snapshot was expanded from, for display; null on a run-scoped token. `purpose` is set on a run-scoped token from the run's purpose and is null on a person's token; the MCP facade renders tool outputs under it ([facade](runtime-and-mcp.md#the-mcp-facade)). |
| `access_token_operation` | `token_id`, `operation_name text` | Primary key both columns. The token's operation set, snapshotted at issuance and never widened ([tokens](identity-and-topology.md#tokens-for-cli-and-mcp-fr-4)). Deleted with a run-scoped token at run end. |
| `identity_provider` | `provider_id text`, `enabled boolean`, `client_id text`, `client_secret_ref text` | Deployment-level provider configuration; the secret is a reference, never a value. |

The control plane is small on purpose. Anything that could live in a workspace database does.
There is no cluster table: release one has one cluster, named by the deployment setting
`storage.cluster_dsn_ref` (a secret reference). A second cluster is phase-eight work and arrives
as a `control.cluster` table plus a `workspace.cluster_ref` column in a control-plane migration
([later phases](later-phases.md#phase-8-the-hosted-edition)); nothing in release one reads a
cluster from a row.

## Server-derived routing (FR 2)

Storage routing is a pure function of the authenticated context:

```text
ctx.workspace_id
  -> control.workspace row (state must be active)
  -> database_name
  -> storage.cluster_dsn_ref (deployment setting) resolved by the storage component's secret scope
  -> pool for that database (created on first use, cached)
```

`database_name` is `ws_` followed by the workspace UUID's 32 hex digits, computed at provisioning
and stored. It is never accepted from a request, a model argument, a payload, a workspace setting,
or a configuration file; the only code that writes it is the provisioning operation. Criterion 6's
test supplies a database name, a connection string, a schema name, and a workspace identifier
through every input channel and asserts each is ignored because none of those channels is read by
this function.

The web tier never holds a database connection. It calls the core's internal API with the session,
and the core routes ([overview](overview.md#processes)).

## Provisioning

`core.workspace.create`, callable by the operator and by any account when
`identity.allow_workspace_create` permits, with the creator written as the workspace's owner
([first run](identity-and-topology.md#first-run-and-who-may-sign-up)):

1. Inserts the `workspace` row in state `provisioning` with the derived `database_name`.
2. Runs `CREATE DATABASE <database_name>` on the cluster. If the database already exists and the
   registry row is `provisioning`, this is a retry and continues.
3. Creates the `core` schema and applies the core migration chain, then records the core version
   in `core.workspace_composition` (below).
4. Writes the core settings rows that must exist explicitly per workspace, from their package
   defaults. Module keys marked `explicit_per_workspace` (FR 29's memory retention among them)
   are written the same way at that module's enable, not here.
5. Sets the registry row `active`.

Each step is idempotent, and the row's `state` plus `state_detail` say where a failed attempt
stopped, so a crash between steps leaves a workspace that the next `create` retry or the operator
command `rheo workspace repair` can finish. No caller other than these two operations changes
`state`.

Deletion of a whole workspace is not a release-one operation (R5 is record-level). The registry
state `unavailable` with a detail exists so that a workspace whose database is unreachable is
visible rather than silently absent.

**Extensions and the application role.** Module install runs `CREATE EXTENSION IF NOT EXISTS`
for each extension the manifest requires ([module contract](module-contract.md#install-and-enable-release-one-in-code)),
which needs the application's database role to be allowed to create extensions in the workspace
database. Some managed Postgres images restrict that to a superuser. The install then fails
naming the extension, and the operator's remedy is a **template database**: create
`rheo_template` with the extensions installed, and set `storage.template_database` (deployment
setting, default `template1`) so provisioning runs `CREATE DATABASE ... TEMPLATE rheo_template`
and every workspace inherits them. `rheo doctor` reports whether the role may create each
extension the loaded modules need. This is documented operator work, not automated.

## Inside a workspace database

### A3. One Postgres schema per module

Every workspace database has a `core` schema plus one schema per installed module, named by the
module id (`recallatron`, `relationships`, `leads`). Tables are always written fully qualified
(`leads.opportunity`); the core sets no `search_path` and the storage API rejects unqualified
table names in module migrations.

What this buys: a module's storage is a nameable unit. "Writes only to its own storage"
(FR 11, criterion 26) becomes a static check that every table name a module's code or migrations
mention starts with its own schema. Module purge in the later lifecycle milestone is
`DROP SCHEMA <module> CASCADE` after the export and reference checks the lifecycle contract
requires (D5). Per-module export can start from `pg_dump --schema`. The later SQLite backend maps
`schema.table` to `schema__table` inside its adapter, which is the kind of difference guardrail 7
wants named explicitly rather than hidden.

### Core-owned tables

All in the `core` schema. Column detail for each lives in the document that owns the behaviour;
this is the inventory.

| Table | Owner document |
| --- | --- |
| `workspace_composition`, `module_state`, `module_schema_version` | this document, [composition](#composition-and-schema-versions-fr-9) |
| `workspace_setting`, `member_setting`, `member_credential` | this document, [configuration](#a5-configuration-precedence-and-the-policy-floor-fr-12) |
| `outbox_event`, `event_delivery`, `consumer_processed`, `job`, `schedule`, `operation`, `audit_record` | [intake and events](intake-and-events.md) |
| `approval`, `approval_payload`, `standing_grant`, `standing_grant_operation`, `external_action` | [confirmation and safety](confirmation-and-safety.md) |
| `runtime_request`, `runtime_request_context`, `runtime_session`, `runtime_transcript` | [runtime and MCP](runtime-and-mcp.md) |
| `deletion_record`, `export_record`, `export_record_ref`, `migration_verification` | [deletion, export, migration](deletion-export-migration.md) |

### Composition and schema versions (FR 9)

| Table | Columns | Notes |
| --- | --- | --- |
| `workspace_composition` | `core_version text`, `core_contract_version integer`, `updated_at` | One row. |
| `module_state` | `module_id text pk`, `package_version text`, `state text`, `installed_at`, `enabled_at null`, `disabled_at null`, `state_detail text null` | `state` in `installed`, `enabled`, `disabled`, `removed`. Release one writes the first two (D5). |
| `module_schema_version` | `module_id`, `schema_version text`, `applied_at`, `core_version_at_apply text` | One row per applied migration step, so history is inspectable; the latest row is the current version. |

Readable through `core.workspace.status` (a read-class operation, exposed to the web interface,
the CLI, and the MCP tool `workspace_status`), which is the "supported operation rather than a
migration table" criteria 10 and 24 require. Alembic's own version tables exist underneath and are
an implementation detail nobody reads for product purposes.

### Migrations

Tooling: Alembic, one migration environment per package (the core and each module), each with its
own version table named `alembic_version_<module>` inside that module's schema. The manifest names
the module's migration directory ([module contract](module-contract.md#the-manifest)). **The
core's migration orchestrator is the only entry point.** Alembic's own command is not installed
as a console script in the image, the migration environments read their database URL from the
orchestrator and refuse to run without it, and `rheo migrate` is the operator's command. Running
Alembic directly against one database would leave that database's version tables ahead of the
registry's record and the workspace would be refused as `schema_ahead` at the next start; the
rule exists so that cannot happen by habit. The orchestrator:

- **Per workspace, under a lock.** `pg_advisory_lock` keyed on the workspace database for the
  duration of that database's migration, so two core replicas or a replica and the operator
  command cannot interleave.
- **Core first, then modules in dependency order** (the manifest's declared dependencies form the
  order; cycles were rejected at install).
- **When:** the core chain runs at provisioning and at core upgrade (startup with a newer core
  version than `workspace_composition` records, or the operator command `rheo migrate`). A
  module's chain runs at install and at module upgrade (later milestone).
- **On failure:** the workspace row goes `unavailable` with the error in `state_detail`; the
  orchestrator continues to the next workspace; startup completes; the failed workspace is
  refused at the boundary with an explicit unavailable state rather than served half-migrated.
- **Downgrade:** not supported in release one. A destructive schema change in a module migration
  must be preceded by a tested export (FR 52); the lifecycle contract says so and the migration
  runner enforces the weaker rule it can check: a migration flagged `destructive = true` in its
  header refuses to run unless the workspace has an export record newer than the migration's
  first attempt.
- **Ahead-of-code:** a workspace whose recorded version is newer than the running code is refused
  as `unavailable` (detail `schema_ahead`) rather than served by code that does not know its
  shape.

Startup on a deployment with many workspaces migrates them serially. That is acceptable for
release one and is the hosted edition's problem to parallelise.

## Storage adapter seam (FR 8, D2)

Three layers, each with a defined owner:

| Layer | Owner | Contents |
| --- | --- | --- |
| `StorageBackend` protocol | `packages/core` | `open_unit_of_work(ctx) -> UnitOfWork`, `provision(workspace)`, `migrate(workspace, chains)`, `advisory_lock(key)`, capability flags (`supports_vector`, `supports_lexical`, `supports_skip_locked`). |
| `UnitOfWork` | `packages/core` | One transaction against one workspace database. Carries the session, the outbox writer, the audit writer, and commit/rollback. Every service operation runs inside exactly one. |
| Repository protocols | each module, in `packages/contracts` for shared ones and in the module for private ones | Typed methods (`OpportunityRepository.get(id)`, `.list(filter)`, `.save(record)`) returning contract models, never rows. A `save` of a record type carrying `revision` is a **compare-and-set**: `... WHERE id = $id AND revision = $expected`, with the increment in the same statement, and zero rows affected raises `StaleRecord`, which the dispatcher surfaces as the refusal `record_stale`. See [below](#revision-is-a-compare-and-set-not-a-counter). |

The reference implementation is SQLAlchemy 2.x Core with explicit mapped classes and psycopg 3.
Raw SQL is allowed inside a repository implementation and nowhere else; a static check greps for
`text(` and driver imports outside `*/storage/` packages (criterion 20's "no file in the MCP
facade imports a database driver or a repository directly" is the same check applied to
`apps/mcp`).

The behavioural test suite (FR 8) is written against the protocols with a `backend` fixture.
Release one runs it against Postgres only. D2 asks the seam to be provable later, so the suite
must contain the cases where backends are known to differ: concurrent compare-and-set on a
`revision` column, concurrent `SKIP LOCKED` leasing,
serialization failures under concurrent receipt insert, advisory locks, generated `tsvector`
columns, vector distance ordering, and `ON CONFLICT` semantics. Each is a named test today so the
future SQLite adapter has a contract to fail against rather than a vague promise.

### `revision` is a compare-and-set, not a counter

`revision` is not bookkeeping. It is the optimistic-concurrency token the approval binding rests
on: `RecordStateGuard` refuses an approved destructive, external, or financial operation when the
subject's current revision differs from the `approval.subject_revision` recorded when the person
looked at it ([guards](confirmation-and-safety.md#execution-guards)). That is the mechanism
stopping an approved effect from landing on a record that changed after it was approved.

"Incremented on every write" implemented as a plain `UPDATE ... SET revision = revision + 1`
under `READ COMMITTED` is a lost update: two writers read *n*, both write, one write is discarded,
and the revision lands at *n+1* either way. The guard then compares equal against a revision that
does not describe the state the approver saw. The failure is silent, and it is in the safety path.

So the rule belongs to the repository protocol and not to each module's memory of it: every write
of a record type carrying `revision` names the revision it read, and a write that matches nothing
is a refusal rather than a no-op. A module cannot opt out, because a module does not write SQL
outside its repository.

An immutable type (an observation, a receipt, a qualification) carries no `revision` column and
reports the constant `1`, so the guard on it can only fail by deletion
([identifiers](identifiers.md#resolution-under-permission)).

Release one has no subject for this: the six core tables carry no mutable domain record type, so
the first repository it binds is the first one 1a writes. It is stated here rather than there so
that the first module author inherits the rule instead of inventing it.

## Retrieval adapter (D9, FR 30)

Owned by the memory module ([memory](memory.md), which owns the records this indexes) but shaped
by the core's seam rules. One protocol:

```text
RetrievalStrategy
  index(ctx, item: IndexItem) -> None          # item: record ref, text, audience, purposes
  invalidate(ctx, ref: RecordRef) -> None      # removes every derived index entry for ref
  search(ctx, query: Query, k: int) -> list[Hit]  # Hit: ref, score, strategy
```

Three implementations ship, selected by `recallatron.retrieval.strategy`:

| Strategy | Storage | Notes |
| --- | --- | --- |
| `lexical` | `recallatron.memory.search_tsv tsvector` generated column with a GIN index, plus `pg_trgm` on `memory.title` for fuzzy title match | No embedding provider needed; the no-network configuration. |
| `dense` | `recallatron.memory_embedding(memory_id, model_id text, dimensions integer, vector vector)` with an HNSW index, cosine distance | Requires the pgvector extension and an embedding provider. |
| `hybrid` | both | Reciprocal rank fusion of the two lists (constant 60), then a bounded rerank by recency within ties. |

Filtering by audience, purpose, and state happens in SQL **before** ranking, inside `search`:
only memories the caller's audience covers, that carry the request's purpose, and that are live
are scored (FR 27). The per-link record permission and contact-permission checks then run on the
ranked slice before anything is returned, and can only remove hits
([memory retrieval](memory.md#retrieval-fr-27-fr-30-criteria-27-and-31)). Criterion 31 runs the
module's behavioural suite against `lexical` and `dense` in turn. The embedding provider sits
behind the same provider seam the runtime uses ([runtime](runtime-and-mcp.md#the-embedding-provider))
and its inputs go through the [redaction contract](runtime-and-mcp.md#the-redaction-contract).

## The data root (FR 10)

The **data root** is one operator-configured directory holding everything the deployment writes
that is not in Postgres:

```text
<data_root>/
  config/deployment.toml        private deployment settings (see below)
  secrets/                      the file secret backend, mode 0700, one file per secret, mode 0600
  workspaces/<workspace_id>/
    uploads/                    attachments and evidence files, referenced from workspace tables
    exports/<export_id>/        export artifacts (FR 52)
    runs/<operation_id>/        scoped working directories for CLI runtime runs, removed on completion
  logs/
```

Resolution order for the root: `RHEO_DATA_ROOT` if set; otherwise the platform application-data
directory (`~/.local/share/rheo-stream`, `~/Library/Application Support/rheo-stream`, or
`%LOCALAPPDATA%\rheo-stream`); in the container image, `/var/lib/rheo-stream`. The checkout-local
directory `.rheo-local/` is used only when `RHEO_DATA_ROOT` names it explicitly (criterion 3). At
startup the host validates the root: it exists or can be created, it is not inside the source
checkout unless it is the explicit opt-in, it is not a symlink escaping its parent, and its
permissions are owner-only (a warning, not a refusal, on filesystems that cannot express that).
Modules receive paths only through `storage.workspace_dir(ctx, purpose)`, which returns a path
under the workspace's directory for a purpose from a closed set (`uploads`, `exports`, `scratch`);
a module cannot name a path.

Workspace files are referenced from workspace tables by a `file_ref` (`uuid` plus a relative path
under the workspace directory); no URL is ever stored, and the web tier streams a file only after
the owning module's permission check.

## A5. Configuration precedence and the policy floor (FR 12)

Three sources, in ascending precedence:

| Source | Where | Format | Who edits |
| --- | --- | --- | --- |
| Package defaults | `packages/core/config/defaults.toml` and each module's `config/defaults.toml` | TOML, tracked | Package authors |
| Deployment settings | `<data_root>/config/deployment.toml`, then environment variables `RHEO__<section>__<key>` (double underscore as the nesting separator) | TOML, private | The operator |
| Workspace and member overrides | `core.workspace_setting(key text pk, value text, value_type text, updated_by uuid null, updated_at)` (`updated_by` is null when the row is written by provisioning or the operator, neither of which acts as an account) and `core.member_setting(account_id, key, value, value_type, updated_at)` | Rows, one per key, typed | An owner (workspace), any member (their own) |

Every settings key is declared once, in a typed schema (pydantic models generated from the
defaults files plus a `scope` and `floor` annotation per key). Undeclared keys are rejected at
every layer. The schema for a key states:

- `scope`: `deployment` (not overridable below the deployment), `workspace`, or `member`.
- `floor`: absent, or one of `min`, `subset`, `and`, `union`. Present only on keys the operator's
  security policy owns.
- `explicit_per_workspace`: when set, a row is written from the package default at workspace
  provisioning (core keys) or at module enable (module keys), so the value is always an explicit
  per-workspace fact rather than an inherited default. FR 29's `recallatron.retention.days`
  (package default 365, floor `min`) is the first such key.

**The floor rule.** For a key with a floor, the effective value is the deployment value combined
with the override under the comparator, and the override is rejected at write time when it would
relax the deployment value:

| Comparator | Effective value | Example key | Package default |
| --- | --- | --- | --- |
| `min` | the lower of the two | `runtime.max_deadline_seconds` | 600 (hard maximum 3600) |
| `union` | the union of the two lists | `approvals.confirm_operations` (mutate-class operations that additionally require confirmation; the operator's list can only grow at the workspace), `<module>.redaction.exclude_types` | empty |
| `subset` | the intersection | `approvals.standing_grant_classes` (package default `read, draft, mutate`; the operator may remove, a workspace may remove further, nobody may add) | see left |
| `and` | both must be true | `redaction.contact_points_to_model` (package default `false`; a workspace can only keep it false unless the operator set true) | `false` |

Enforcement is in two places on purpose: the settings write path validates against the resolved
deployment value and refuses with the key named, and the read path clamps, so a row written before
an operator tightened the floor is neutralised the moment the floor changes. The resolved settings
object handed to services is immutable for the request.

Member overrides exist only for keys with `scope = member` (display preferences, personal runtime
default within the workspace's permitted set). They are stored in the workspace database so that
export carries them and so that a second member's rows are distinguishable from the first's by
`account_id` (FR 6). Personal credentials follow the same shape:
`core.member_credential(account_id, name text, secret_ref text, created_at)`, holding a reference
into the secret store and never a value.

FR 12's precedence and the floor are tested by acceptance criterion 69 of the build plan (added
append-only during phase-one detailed planning): the settings schema, the write-time refusal
naming the key, and the read-time clamp ship in phase one, and the criterion's test drives them
end-to-end through the settings write path rather than through the resolver alone.

## A4. The secret store (FR 13, guardrail 14)

The requirements deliberately name no store. This specification chooses a **file-backed store
with an environment-variable backend**, both behind one protocol, and no dependency on any
platform's secret manager. A managed secret manager is a third backend a deployment can add
later; nothing in release one needs one, and guardrail 15 forbids requiring one.

**Reference format.** `secret://<backend>/<id>`, where `backend` is `file` or `env` and `id` is a
slug path:

```text
secret://file/cluster/primary-dsn
secret://file/identity/github/client-secret
secret://file/runtime/claude-cli/credential
secret://file/ws/018f.../connection/018f.../signing
secret://env/GITHUB_OAUTH_CLIENT_SECRET
```

References are the only form of a secret that appears in configuration, in the control plane, in
workspace tables, or in an export. The `file` backend stores one file per id under
`<data_root>/secrets/`; the `env` backend reads the named variable at process start and refuses to
start if a referenced variable is missing.

**Resolution boundary.** The protocol is:

```text
SecretStore.resolve(ref: SecretRef, scope: SecretScope) -> SecretValue
```

`SecretScope` is an unforgeable token the core constructs for a component at registration time,
naming the reference prefixes that component may resolve (a runtime adapter gets
`runtime.<adapter>.*`, the identity provider gets `identity.<provider>.*`, the intake receiver gets
`ws/*/connection/*/signing`, the storage component gets `cluster/*`). Domain services and modules
are constructed without a scope, so they have no way to call `resolve` at all; a test asserts no
module package imports `SecretStore`. `SecretValue` is a wrapper whose `repr` and `str` are
redacted, which cannot be serialised by the JSON encoders the core uses for events, operation
records, audit records, or runtime request records, and which exposes its bytes only through
`expose()` inside the presenting component.

**References do not travel either.** Criterion 17 requires that the runtime request, every tool
argument, the operation record, and the audit record contain "neither a secret value nor a
reference that resolves to one". So a runtime request names a **credential slot**
(`credential_slot = "model"`), and the adapter maps the slot to a reference from its own private
configuration at the moment it spawns the process. A connector's receipt names the connection; the
receiver looks up the connection's `signing_secret_ref` column itself. Nothing downstream of a
component ever sees the reference.

**Per-workspace secrets.** Connection signing secrets and member credentials are workspace data
by ownership but live in the deployment's secret store under a workspace-scoped id, referenced
from the workspace table. An export carries the reference and never the value; a restore marks
every connection `needs_credential` and every member credential `needs_value` until re-supplied
([export and restore](deletion-export-migration.md#export-and-restore-fr-52)). Encryption at rest of
the file store is the volume's job in release one; per-workspace keys and key management are the
hosted edition's prerequisite, as the build plan already records.

**Rotation** is not a store feature. A secret id holds one value; rotating means writing a new
value under a new id and having the referencing record name both, which is how the intake
connection's `signing_secret_ref`, `previous_secret_ref`, and `previous_valid_until` carry the
overlap window criterion 42 needs ([intake transports](intake-and-events.md#transports)). The
store has no notion of current and previous, so there is one rotation mechanism and it lives with
the record that owns the reference.

## Storage decision record (D1, D2)

Recorded here so the reasoning survives the requirements document's summary.

**Decision.** Postgres is the sole reference backend. One database per workspace in one cluster;
one small control-plane database. Per-workspace SQLite is a contract goal: the adapter seam, the
export format, and the behavioural suite are written so a second backend can be proven, and none
ships.

**Alternatives weighed.** Per-workspace SQLite from the start (the idea document's "serious
option"): strongest isolation and the simplest local install, but no server-side concurrency story
for a worker plus an API process plus a web tier writing the same file, no pgvector, and a second
production backend the moment a hosted edition needs Postgres anyway. Shared Postgres with
row-level security: conventional and horizontally scalable, but it moves isolation into policy
that every query must honour, and it makes per-workspace export, restore, and deletion a filtered
copy rather than a unit. Schema per workspace: covered under A1 above.

**Consequences accepted.** Pool-per-workspace with a bounded cache; serial migration at startup;
no cross-workspace SQL; a cluster's database count as the scaling ceiling the hosted edition must
plan around. The rejected options stay reachable: the seam is protocol-shaped, table names are
adapter-mapped, and no domain code names Postgres.

**Arrays and child tables.** The house rule prefers a normalized child table to an array or a
JSON column. This specification keeps an array column only where the column is never a query
predicate (provenance lists such as `qualification.evidence_refs` and `merge_record.evidence_refs`,
the run's `permitted_tools` and `requirements`, a deletion record's `participants`), and gives a
child table to every list a query filters by: a memory's purposes
([`memory_purpose`](memory.md#entities)) and a run's context references
([`runtime_request_context`](runtime-and-mcp.md#what-the-core-records-about-a-run)).
