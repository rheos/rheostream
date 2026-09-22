# Module contract, version 1

**Part of:** the [architecture specification](README.md). Designs against D5, D11, FR 9, FR 11,
FR 24, FR 26, FR 52, and criteria 24, 25, 26, 33, 54, 66. Closes idea-document question 21 for
release one and writes the full lifecycle on paper as D5 requires.
**Decision:** A6. See the [decision list](README.md#architecture-decisions).

## The shape in one paragraph

A module is an ordinary Python distribution that publishes one `rheo.modules` packaging entry
point, and that entry point resolves straight to a **manifest**: a `ModuleManifest`, the typed,
validated model in `packages/core` the next section describes. The core's own `_register`
(`packages/core/src/rheo_core/modules/loader.py`) is what attaches the manifest's operations,
resolvers, tools, job kinds, subscriptions, settings keys and audit sink to the live registries,
by iterating the manifest's own tuples, and records the manifest itself — the `web` surface is
read back off that record by `module_surfaces()` rather than registered anywhere. Loading is
idempotent rather than once-per-process: `apps/core`'s FastAPI lifespan runs it more than once in
a single process under test, and every registry it calls treats an identical re-registration as a
no-op. **Named deviation:**
this paragraph once described a `Module` object carrying both a manifest and a `register`
function that the core called. No such object and no such author-supplied function exists in the
tree, and the difference is load-bearing rather than cosmetic — it is precisely why discovery
step 4's manifest-to-registration cross-check below is satisfied by construction rather than
performed. Host-installed availability is "the distribution is importable"; per-workspace
activation is a row in `core.module_state`. The core
imports no module; a module imports the core and the contracts and, if it declares one, another
module's public contract package. That is the whole mechanism; the rest of this document is what
the manifest says and what the lifecycle does.

## The manifest

`rheo_core.modules.ModuleManifest`, a frozen pydantic model. Every field is required unless
marked optional; a missing field fails validation at install with the field named
(criterion 25), and nothing wraps pydantic's own `ValidationError`, because wrapping is what
would lose that name.

**The type's real home is `rheo_core.modules`, not `rheo_contracts`.** This document once
named it `rheo_contracts.ModuleManifest`. It cannot live there: the manifest carries each
operation's handler, typed against `UnitOfWork`, and `rheo_contracts` may import nothing but
the standard library, `pydantic` and itself (the boundary `tests/test_imports.py` enforces).
So the handler travels *beside* its declaration in an `(OperationDeclaration, Handler)` pair,
which is the same deviation, for the same reason, that already keeps `handler` off
`OperationDeclaration` itself — `packages/contracts/src/rheo_contracts/manifest.py` carries
that argument in full.

| Field | Type | Meaning |
| --- | --- | --- |
| `module_id` | slug | Stable identity; the namespace for everything the module registers. |
| `package_version` | semantic version | The distribution's version. |
| `core_contract_versions` | list of integers | Contract major versions this module is written against. Release one publishes contract version 1. |
| `dependencies` | list of `Dependency(module_id, version_range, optional)` | Required and optional modules. Cycles and unsatisfiable ranges are rejected at install. |
| `record_types` | list of `RecordType` | See [owned record types](#owned-record-types). |
| `storage` | `StorageDeclaration(schema_name, migrations_path, required_extensions)` | `schema_name` must equal `module_id`. `required_extensions` names Postgres extensions the migrations need. **The chain's `env.py` must configure `version_table="alembic_version_<module_id>"` and `version_table_schema=<module_id>`**: the orchestrator derives that same pair to read the chain's recorded head, and nothing at runtime makes the two agree — an `env.py` left on Alembic's default `alembic_version` migrates cleanly while the orchestrator reads a table that never fills, so no `core.module_schema_version` row is written and the `schema_ahead` guard is vacuous. A module's `contract_tests` are where the pairing is asserted. |
| `configuration_schema` | `tuple[KeySpec, ...]` | The module's settings keys with scope and floor annotations ([configuration](storage-and-workspaces.md#a5-configuration-precedence-and-the-policy-floor-fr-12)). Keys must start with `<module_id>.`. **Named deviation:** this row once read "a pydantic model class". The settings system already declares a key as a `KeySpec` and registers it through `SettingsRegistry.register(spec, *, origin)`, so a second declaration form would be a shape with no reader; the manifest carries the `KeySpec` values themselves. |
| `operations` | list of `OperationDeclaration` | Name, safety class, input and output models, idempotency, audit subject, guards. See [operations](#operations-tools-events). |
| `tools` | list of `ToolDeclaration` | MCP tools, each naming an operation and restating its class. |
| `events` | list of `EventDeclaration(type, schema_version, data)` | Events the module publishes. **Named deviation:** the third field is `data`, not the `data_model` this row once printed — the shorter name is what the code carries, what every later run types, and what the namespacing check reads past, so the row was corrected to the code rather than the other way round. |
| `subscriptions` | list of `Subscription(event_type, consumer_id, replay_safe)` | Events the module consumes. |
| `jobs` | list of `JobKind(name, input_model, handler, max_attempts, cancellable)` | Background work kinds. **Named deviation:** this row once omitted `input_model`. `JobKindRegistry.register(kind, input_model, handler)` (`packages/core/src/rheo_core/work/kinds.py`) takes exactly those three positional arguments, so a `JobKind` carrying no input model could not be registered at all; the row gained the field rather than the registry losing it. **`max_attempts` and `cancellable` are carried on the declaration and read by nothing in release one:** the loader passes the registry only the three arguments it takes, and widening a shipped signature for a caller that does not exist is what this deliberately does not do. Each has a named future reader — an enqueue call site for `max_attempts` (today `work.max_attempts` supplies the budget and the job row's own column is the authority from then on), and Disable for `cancellable`, whose contract below already says each job kind's flag decides which queued jobs are cancelled and which drain. |
| `schedules` | list of `Schedule(name, job_kind, cron, enabled_by_default)` | Per-workspace schedules created at enable. |
| `resolvers` | mapping of record type to resolver | One per owned record type ([identifiers](identifiers.md#resolution-under-permission)). |
| `deletion_participants` | list of `DeletionParticipant(record_types, handler)` | Hooks the [deletion coordinator](deletion-export-migration.md#the-cascade) calls. `handler` was narrowed in run 1a1 from an untyped callable to `(ctx, uow, ref, *, disposition) -> RemovedMemories`, read off its first real caller rather than guessed. A participant that raises aborts the whole deletion, so a handler has no refusal channel of its own and needs none; `disposition` is keyword-only, so a handler that ignores it still reads as one that was offered it. |
| `export` | `ExportDeclaration(format_version, schema_path, exporter, importer)` | The module's versioned export format. **Both callables were narrowed in run 1a1, when the export collector became their first caller.** `exporter` receives only `rheo_core.exports.artifact.ExportSnapshot` — the sealed view of one source transaction, carrying the connection, a fixed `snapshot_at` and the transaction-bound settings source — and never a bare connection, which is what would let a module read a second, later view into the same artifact. `importer` receives the restore transaction's unit of work and the already-validated rows, and returns the **second pass** the core calls after core's own deletion evidence has landed: restore is two passes with the ledger between them, so parents go in with their self-references null and only then are those references resolved and marked ancestry validated against that evidence. A module cannot schedule that itself without committing or reaching into the core's ordering, and an importer never commits, because the whole restore is one transaction and a module ending it would take the rollback guarantee with it. |
| `web` | optional `WebContribution(package_name, navigation, routes, record_views, forms, search_providers)` | The TypeScript contribution composed into `apps/web`. **Release one ships `WebSurface \| None` instead** (`packages/core/src/rheo_core/modules/manifest.py`): the surface name, host label and path prefix, and nothing else, because its one consumer is the internal listener's `routing_config()` (`apps/core/src/rheo_app_core/internal_routes.py`). The interface contribution above is run 1a3's, and widening or replacing this field is that run's to do. |
| `agent_guidance` | optional path | Text a runtime may include as tool guidance. It describes use; it grants nothing (idea document). |
| `secret_scopes` | list of scope prefixes | Empty for domain modules in release one. Only components that present secrets declare any. |
| `connector_bindings` | list of `ConnectorBinding(transport, service_operation, route)` | Which of the module's operations a transport connector may call, and the HTTP route the binding registers on the `api` surface when the transport has one (`route` is null otherwise). Leads declares three, one per transport, all naming `leads.intake.accept_delivery` ([the three bindings](intake-and-events.md#transports)). |
| `health_checks` | list of callables | Run by `rheo doctor` and the workspace status operation. |
| `contract_tests` | path | The module's behavioural suite entry: a repo-relative directory. **Named deviation:** this row once said the suite is "run by the install path in test profile and by CI". Install **validates the path and does not run the suite** — see § Install step 6 for why — and CI runs it, through the `testpaths` widening that puts `modules/` in `make test`. The row is corrected here as well as there because it is the line a module author reads first, and leaving it would have this document asserting the superseded behaviour in its most-read table. |
| `sensitivity` | mapping of record type to field tiers | Which fields are `public`, `internal`, `restricted` for the [redaction contract](runtime-and-mcp.md#the-redaction-contract). |
| `audit_sink` | optional `AuditSink` | The writer for the module's own audit rows, installed under the module's id by `_register` in `packages/core/src/rheo_core/modules/loader.py`. **Not one of the twenty-three ratified fields**, and added because it is the only channel a module has: `rheo_core.operations.dispatch` resolves a sink by the *operation's* owning module and refuses every above-`READ` operation with `AUDIT_SINK_MISSING` when that module has none. Optional, because a module declaring only `READ` operations needs no sink. |

The manifest is code, not a sidecar file: one source of truth, type-checked, no cross-validation
between two declarations. The export `schema_path` is the one static file: a JSON Schema for the
module's export records, so an export can be validated by anything that has the package installed,
whether or not the module is enabled anywhere.

### Owned record types

```text
RecordType
  name: slug                      # "opportunity"; the reference form is "leads.opportunity"
  table: str                      # "leads.opportunity"; must be in the module's schema
  deletable: bool                 # true for the three R5 types and any type a module chooses
  delete_roles: set[Role]         # who may call core.record.delete for it; required when deletable
  exportable: bool
  audience_field: str | None      # column holding the record's audience, when records carry one
  authorize_delete: DeleteAuthorizer | None   # (ctx, uow, ref, *, disposition) -> DeleteAuthorization | Refusal
  delete_owned: OwnedDeleter | None           # (ctx, uow, authorization) -> RemovedMemories
```

A record type appears in exactly one manifest. Two modules declaring the same
`<module_id>.<name>` cannot happen because the module id is the prefix; two modules declaring a
table outside their schema fail install.

**Named deviation: the owned-delete pair is a field of `RecordType`, added in run 1a1.**
**Ratified by the maintainer on 2026-09-21, reviewed at run 1a1's checkpoint 13**, and recorded here and
in the [decision ledger](../ideas/rheo-stream-idea.md#recorded-changes-of-direction) rather than
resting on a build-stage commit message: it changes the module contract version 1 that PR #7
settled, so it gets the same explicitness that ratification did. The
[deletion coordinator](deletion-export-migration.md#the-cascade) reaches a module's own delete
through two callables, and the manifest carried no field for them — so until a real owner
existed, only a test harness registered a declaration, by hand. The pair lives here rather than
in a twenty-fifth manifest field because "a record type appears in exactly one manifest" is
already the invariant the owned-delete registry needs: hanging the pair off the type makes
"exactly one declaration owns a record type" structural, with nowhere to write a declaration for
a type this manifest does not own and nowhere to write two for one type. Both halves default to
null — the empty default every manifest written before an owner keeps — and the pair is whole or
absent: a type carrying one half fails validation, and so does a pair on a type that is not
`deletable`. `authorize_delete` is content-free by shape, answering a reference and a revision
or a refusal and never the record, and it authorizes a retained or expired row as readily as a
current one, because those are exactly the rows retention exists to remove.

The loader registers the pair on the process-global owned-delete registry **unconditionally**,
unlike `deletion_participants`, which a composition root opts into by passing that registry.
The coordinator resolves against that one instance, so a declaration routed anywhere else is one
it never finds; and ownership is not a cascade choice the way a participant hook is — it is the
record type's own statement that it is deletable at all, declared beside the `deletable` and
`delete_roles` the loader already accepts without a parameter.

The registry keys declarations on `(module_id, record_type)` and admits exactly one owner per
type. A module may claim record types under its own id only, `core` and `harness` are reserved
to their origins, a `test_harness` origin is accepted under the test profile alone, and
re-registering an identical declaration is a no-op so a process that loads its modules twice
registers once. A reference no declaration owns, a reference whose owning module is not enabled
in this workspace, and a reference naming a core-owned record all refuse with the same generic
`record_not_deletable`: distinguishing them would turn `core.record.delete` into a probe for
which record types a deployment carries and which modules a workspace runs. An owner's own
authorizer refusal passes through unchanged, because that one is the owner's to phrase.

**Named deviation: `Disposition` is a required keyword-only argument, with no default, on both
`DeleteAuthorizer` and `DeletionHandler`. Ratified by the maintainer on 2026-09-21, reviewed at
run 1a1's checkpoint 14**, and recorded here and in the decision ledger for the same reason as the pair
above: it is a change to a core interface every module implements, not a build detail. Its two
values, `user_erasure` and `retention_expiry`, are the same vocabulary the deletion ledger's
`cause` column records, so "which disposition ran" and "what the ledger says happened" cannot
drift apart. The coordinator passes it because an owner cannot work it out: the two dispositions
traverse different edges — an erasure follows marked supersession-lineage hops so a replacement
stays reachable from an ancestor nobody can read any more, an expiry skips every one of them so
a fresh replacement survives its predecessor's sweep on its own clock — and they carry different
authority, a person's role and the record's audience in one case and the workspace's own
retention policy, sealed into a verified scheduled execution, in the other. Neither callable can
read that capability. It has no default deliberately: a caller that did not say which
disposition is running has not decided, and a default would pick one for it silently.

The authorizer is **content-free by shape**: it answers a `DeleteAuthorization` of a reference
and a revision, or a refusal, and there is nowhere on that object to put a title, a body or a
payload. The revision is what the coordinator captures before the effect and rechecks under the
lifecycle lock, so an approver cannot have the record move underneath an approval. `delete_owned`
takes that authorization rather than the bare reference, so the revision authorized is the
revision deleted at, and answers a `RemovedMemories` of identifiers only — distinct memory rows
physically removed, never a child row, a vector, an already-absent row, a retained replacement,
or a row merely marked invalidated. The coordinator unions those sets across the owner and every
participant and stores the size, which is why overlapping reports collapse instead of
double-counting.

**What 1a1 implements here is narrow, and the full [§ A14 cascade](deletion-export-migration.md#the-cascade)
is not it.** This run builds the owned delete for `recallatron.memory`, the module's own
invalidation participant, content-free deletion evidence and the transactional audit row. The
job, external-action, transcript and held-export legs of the cascade — and criterion 65 with
them — are phase three's, and until they land the deletion record's first three counters are
zero by construction rather than by coincidence.

### Operations, tools, events

```text
OperationDeclaration
  name: "<module_id>.<noun>.<verb>"
  safety_class: SafetyClass                 # exactly one of the six (R3, FR 24)
  input: type[BaseModel]                    # may not declare workspace_id, actor_id, tenant, database, schema, connection fields
  output: type[BaseModel]
  handler: Callable[[WorkspaceContext, UnitOfWork, input], output]
  roles: set[Role]                          # who may call; Role in {owner, member, operator, service}; default {owner, member}
  idempotency: Idempotency                  # NONE | NATURAL(unique index); KEYED arrives with phase five
  audit: AuditSpec | None                   # required unless safety_class is READ
  guards: list[ExecutionGuard]              # domain rechecks added to the core's, for DESTRUCTIVE, EXTERNAL, FINANCIAL
  long_running: bool                        # returns an operation id and runs as a job
```

`roles` is checked by the dispatcher against `ctx.role` on every call. `service` is the role a
`connection` actor at `entry = intake` and a `system` actor at `entry = job` carry
([workspace context](overview.md#the-workspace-context)); an operation that lists it may be
called from processing and jobs, and one that does not (every configuration operation) cannot.
The release-one operations and their roles are listed in the document that owns each:
[relationships](relationships.md#operations-and-roles), [memory](memory.md#operations-and-roles),
[Leads](confirmation-and-safety.md#operations-and-roles), and the core's below.

| Core operation | Class | Roles |
| --- | --- | --- |
| `core.workspace.status`, `core.operation.get`, `.list` | read | owner, member, operator |
| `core.workspace.create` | mutate, long-running | operator, and any account when `identity.allow_workspace_create` (deployment, default `true`); the creator becomes owner |
| `core.module.install`, `.enable` | mutate | owner, operator |
| `core.settings.set` (workspace keys), `core.settings.set_member` (member keys) | mutate | owner; owner, member |
| `core.token.issue` (for the calling account), `.revoke` | mutate | owner, member (a token never exceeds its account's role and never exceeds its issuer's own permitted set); operator for another account. Non-token-issuable: no token can issue or revoke a token ([tokens](identity-and-topology.md#what-a-token-can-never-carry-and-what-it-holds-for-a-gated-operation)). |
| `core.approval.approve`, `.refuse` | mutate | owner, member. Non-token-issuable, so reachable only from a web session in release one: the only contexts that carry one of those roles and hold the operation. |
| `core.standing_grant.create`, `.revoke` | mutate | owner. Non-token-issuable. |
| `core.audit.list`, `core.work.failures` | read | owner, operator |
| `core.work.failure_summary` | read | owner, operator. The counts the shell's banner reads ([retry](intake-and-events.md#retry)); a summary rather than a listing so the layout costs one bounded read. |
| `core.operation.resolve` | mutate | owner, operator |
| `core.work.retry`, `.skip`, `.replay` | mutate | owner, operator — **declared here and built nowhere.** As of run 0c3 no handler, declaration or registration for these three names exists anywhere in `packages/`, so nothing in a running system answers them, and they are split out of `core.operation.resolve`'s row above — which is registered — rather than sharing one that reads as though all four were live (issue #65). They stay open for a future run to build. `core.retention_sweep`, named as a daily core schedule under [jobs and the worker](intake-and-events.md#jobs-and-the-worker-fr-16), is a job kind rather than an operation: it is registered on the worker, provisioned as a daily `core.schedule` row per workspace, and ticked by `run_due_schedules`. It does not share this unbuilt row. |
| `core.workspace.export`, `.digest` | mutate, long-running; read | owner, operator |
| `core.workspace.restore` | mutate, long-running | operator, or an account restoring into a workspace the control plane does not yet know |
| `core.record.delete` | destructive | the record type's `delete_roles`: owner for `leads.observation`, `leads.opportunity`, `relationships.party`; owner, member for `recallatron.memory` |

Registration rules the core enforces at startup (criterion 18, criterion 14, criterion 6):

- No `safety_class`: refused, naming the operation.
- Class above `READ` with `audit = None`: refused, naming the operation.
- Class in `DESTRUCTIVE`, `EXTERNAL`, `FINANCIAL`: the core attaches `ActorPermissionGuard`,
  `WindowGuard`, and, when the input names a `subject_ref`, `RecordStateGuard`; the module's
  `guards` list adds domain guards and may be empty
  ([confirmation](confirmation-and-safety.md#execution-guards)). There is no "no guards" refusal,
  because the core's guards make that state unreachable.
- An input model with a reserved field name (`workspace_id`, `workspace`, `actor_id`, `actor`,
  `tenant_id`, `database`, `schema`, `connection_string`, `dsn`, `sql`, `table_name`,
  `statement`): refused. The last three make criterion 20's "accepts no SQL, table name, or query
  fragment" a mechanical check over the registered input models. The one workspace-taking
  endpoint, the session's active-workspace switch, is not a registered operation
  ([identity](identity-and-topology.md#accounts-sessions-and-the-active-workspace)).
- `NATURAL` idempotency names the unique index that makes a repeat the same row; the operation's
  handler is what returns the existing row on a repeat, and the declaration exists so the
  registry can assert the index is present in the module's schema at install. Release one has no
  keyed idempotency and no stored-result table; the first operation that must return a created
  reference on a retry is phase five's ([idempotency](intake-and-events.md#idempotency)).
- A tool registered by a module may name a core operation (the deletion tools do) only when its
  input restricts the reference to the module's own record types.

```text
ToolDeclaration
  name: "<module_id>_<verb>[_<noun>]"       # MCP-safe characters only
  operation: str                            # the operation it calls; nothing else
  safety_class: SafetyClass                 # must equal the operation's, or registration fails
  description: str
  input_schema: derived from the operation's input model
```

A tool is a name and a description over an operation. It has no handler of its own, so it cannot
reach storage except through the operation (FR 22, criterion 20).

```text
EventDeclaration
  type: "<module_id>.<record_type>.<verb>"
  schema_version: int
  data: type[BaseModel]                     # references and small facts, no record copies
```

Event and subscription shapes, delivery, and replay are in [intake and events](intake-and-events.md#events-and-the-outbox-fr-15).

### Extension points

The `web` contribution is a TypeScript package under the module directory (`modules/leads/web`)
that the web application composes at build time. It exports:

| Export | Shape | Filtered by |
| --- | --- | --- |
| `navigation` | list of `{ id, label, surface, path, roles }` | enabled modules for the session's workspace, role |
| `routes` | a route tree mounted under the module's surface ([topology](identity-and-topology.md#the-routing-table)) | enabled modules |
| `recordViews` | `{ recordType, component }` | permission through the record resolver |
| `forms` | `{ operation, component }` for operations that need a bespoke form | operation set, role |
| `searchProviders` | `{ id, operation }` naming a read-class search operation | enabled modules |

Composition is a generated file (`apps/web/src/modules.generated.ts`) produced from the installed
distributions' manifests by `rheo web compose`; it is never hand-edited. Enablement is applied at
runtime from the core's `workspace_status` response, so a module that is installed on the host and
not enabled in the workspace contributes nothing visible. A build is required to add a module's
screens, which is the "code installation may require a build or restart" the idea document allows.

## Discovery and registration

At process start, in `core` and `worker` alike:

1. `importlib.metadata.entry_points(group="rheo.modules")` lists the host-installed modules. The
   deployment setting `modules.installed` (a list of module ids, default: the empty list) selects
   which of them the host loads; a discovered module the list does not name is simply not loaded,
   with no exception raised and no refusal to start, so a private module's presence on disk does
   not activate it (workspace-layout rule). The default is empty and never widens to "every
   discovered module": the image installs every `modules/*` distribution, so a default of
   everything would make discovery itself the activation.
2. Each selected module's manifest is validated: schema, namespace prefixes, dependency ranges
   against the other selected modules, contract version against the running core.
3. Dependencies are topologically sorted; a cycle or an unsatisfied required dependency fails
   startup naming both modules.
4. The core's `_register` runs per module in that order, attaching what the manifest declares:
   operations, resolvers, tools, job kinds, subscriptions and `configuration_schema` keys, each
   through the same shipped `register` the core's own startup calls. **The manifest-to-registration
   cross-check this step once described is satisfied by construction in this tree, and there is
   nothing for it to check against.** It was written for the `register(registry)` function the
   opening section used to describe, where a module author wrote registration calls by hand and
   could write one the manifest did not declare. No such function exists: `_register` iterates the
   manifest's own tuples and nothing else, so "registered but not declared" and "declared but not
   registered" are both unreachable rather than refused. `tests/test_module_registration.py` pins
   the property instead of building a second mechanism for it. A future run that lets a module
   supply registration code of its own is the run that has to build the check.
5. The production-profile assertion runs (criterion 18): under `RHEO_PROFILE=production` the
   registry must contain no item whose `origin` is `test_harness` and no operation in the
   `EXTERNAL` or `FINANCIAL` classes. Test fixtures register with `origin=test_harness` through a
   harness-only entry point that the production profile does not load.

The registry is global to the process. Per-workspace activation is applied at every boundary by
`WorkspaceContext.enabled_modules`, so a tool listing, a route resolution, an event fan-out, a
job dispatch, and a schedule tick each filter by the workspace they are acting for.

## Lifecycle

Per workspace, in `core.module_state.state`:

```text
(absent) --install--> installed --enable--> enabled
                                    ^          |
                                    +--re-enable--+--disable--> disabled --remove--> removed --purge--> (absent, data gone)
                                                                   |
                                                                   +--restore--> installed (from an export)
```

### Install and enable (release one, in code)

**Install** (`core.module.install`, owner or operator, class mutate, long-running):

1. The module must be host-loaded. Otherwise refuse with `module_unavailable`.
2. Dependencies must be installed in this workspace at a satisfying version; optional ones may be
   absent and are recorded as absent.
3. `required_extensions` are created in the workspace database (`CREATE EXTENSION IF NOT
   EXISTS`); failure names the extension and stops before any migration. When the application
   role may not create extensions, the operator pre-creates a template database that carries
   them ([storage](storage-and-workspaces.md#provisioning)).
4. The module's schema is created and its migration chain applied under the workspace's advisory
   lock; each applied step writes `core.module_schema_version`.
5. `core.module_state` gets the row `installed` with the package version.
6. The module's `contract_tests` path is **resolved and validated** when the profile is `test`;
   in other profiles the health checks run. **Named deviation:** this step once said the tests
   "run". They do not run here, and they do not run in the install job either — the suite is
   CI's to run, through the `testpaths` widening that puts `modules/` in `make test`. Spawning a
   test runner from inside a mutate operation's own transaction would be re-entrant (the suite
   that drives install *is* pytest) and would put an arbitrary subprocess inside a transaction
   holding the workspace's advisory lock. What install does instead is resolve the declared
   repo-relative path against the checkout and refuse when it is absent — and it does that at
   **pre-flight**, before any job is enqueued, which is why `contract_tests_missing` is one of
   the caller's own five refusal codes rather than an in-job `job_failed`. The check is guarded by
   `profile == "test"` because a built wheel does not package a module's test suite, so the
   question is only meaningful in a checkout.
7. Configuration keys from the module's schema become settable; none are required to enable.

**Enable** (`core.module.enable`, owner or operator, class mutate):

1. State must be `installed` or `disabled`.
2. Required dependencies must be `enabled`.
3. Settings keys the module's schema marks `explicit_per_workspace` are written as rows from
   their package defaults (FR 29's retention setting; criterion 30). Schedules declared
   `enabled_by_default` are created in `core.schedule` for this workspace.
4. State becomes `enabled`; from the next request, the workspace's `enabled_modules` includes it,
   and its tools, routes, navigation, subscriptions, and jobs are live for that workspace.

Enable has **three** caller-visible refusals, not the two its steps imply. Steps 1 and 2 refuse
`module_state_invalid` (naming the state the workspace actually holds, including `absent` for a
module with no row at all) and `dependency_not_enabled`. The third is `module_unavailable`,
install's own name reused rather than a fourth code meaning the same thing: enable needs the
manifest from step 2 on, and a workspace can hold an `installed` row for a module this deployment
has since dropped from `modules.installed`. It is checked *after* step 1, so a module that was
never installed is still `module_state_invalid` naming `absent`.

Step 1's read takes `SELECT … FOR UPDATE` on the target `core.module_state` row and holds it until
commit. Without it, two concurrent enables read the state from their own snapshots, both pass step 1
and both succeed — and for a manifest declaring a schedule but no `explicit_per_workspace` key,
step 3's `core.schedule` write duplicates as well, because that table has no unique index over
`(module_id, name)` to fall back on. The loser of the race waits at that read and then sees the
state the winner committed, so it refuses `module_state_invalid` naming `enabled`.

The memory module's install and enable is criterion 24's test: no core file changes, and the
workspace status operation reports the version and schema version afterward. Relationships and
Leads install the same way in phase three.

**Criterion 24 is only partially closed by run 1a0, and a reader who marks it done after that
run alone is reading a wrong record.** What 1a0 closes is the module-contract half:
`core.module.install` and `core.module.enable` exist, Recallatron goes from `absent` to `enabled`
through those two operations alone, and `core.workspace.status` reports its version, its state and its
schema version afterwards (`tests/postgres/test_module_lifecycle.py`). What it does not close is
the interface-contribution half — the `WebContribution` shape this document's manifest table still
describes as run 1a3's, the `apps/web/src/modules.generated.ts` composition step, and the test
that a module's screens appear once it is enabled. That half belongs to run 1a3, `apps/web/**` is
outside 1a0's scope by decision, and until 1a3 lands, criterion 24 stays open.

### Upgrade, disable, re-enable, remove, purge, restore (on paper, D5)

Written now so a builder in the later milestone has a contract, not a blank page. None of it is
release-one code, and no release-one path depends on it.

| Operation | Contract |
| --- | --- |
| **Upgrade** | The host loads the new package version. Per workspace: check `core_contract_versions` and dependency ranges; run the migration chain from the recorded schema version; a migration marked `destructive` requires an export newer than the upgrade's first attempt; on success update `module_state.package_version`. A workspace that fails stays on the old schema version and goes `unavailable` for that module only (its tools and routes refuse with `module_unavailable`) while the rest of the workspace serves. |
| **Disable** | Block new operations of the module (refused `module_disabled`); mark schedules paused; stop fanning new events to its consumers; cancel or drain its queued jobs (each job kind's `cancellable` flag decides which); remove its tool and navigation contributions from the next listing; invalidate cached tool lists for open MCP sessions by bumping the workspace's registration epoch, which every session compares on each call. Running jobs finish or fail through the ordinary operation path; already-dispatched external actions reconcile through `external_action` and are never pretended reversed. Data untouched. |
| **Re-enable** | Same checks as enable. Schedules resume from now, not from where they paused. Events fanned out while disabled were never delivered to the module (fan-out is at write time), so there is no catch-up and therefore no replay of completed external effects. |
| **Remove** | Refuse while a required dependent is enabled or installed, naming it. Check unresolved operations and external actions owned by the module; refuse while any exists. Produce a workspace export that includes the module's records (retained export). Revoke module-owned connector bindings and secret references. State `removed`; the schema and its data remain, readable through the record resolver as `state = unavailable`. |
| **Purge** | Destructive class, per-action confirmation. Requires state `removed` and a retained export newer than the removal. Runs the deletion coordinator for every deletable record type the module owns (so derived memories, queued actions, and held exports cascade under the R5 rules), then `DROP SCHEMA <module> CASCADE`, then deletes `module_state` and `module_schema_version` rows. |
| **Restore** | From an export whose manifest lists the module at a version the host can load: install at that version, then import through the module's `importer`. Pending approvals inside the import are written `requires_reapproval` ([export and restore](deletion-export-migration.md#export-and-restore-fr-52)). |

Host-level package removal is a separate operator concern: `rheo module hosts <id>` lists every
workspace still in a state other than `absent` for the module, and the operator removes those
first.

## Capability and version compatibility

- **Contract version.** `packages/contracts` carries `CONTRACT_VERSION = 1`. A breaking change
  to any model in it increments the major; a module lists the majors it supports. The core loads
  a module only for a listed major.
- **Module version.** Semantic. Dependency ranges are **PEP 440 version specifiers**
  (`>=1.2,<2`, `~=1.2`, `==1.*`), parsed by `packaging.specifiers.SpecifierSet` in the
  manifest's own validator, so an unparseable range is a validation error at construction.
  This document once said "the usual caret and tilde forms", which describes npm's grammar,
  not Python's; a caret range is not a PEP 440 specifier and is refused. A
  dependency on a module's *behaviour* is a dependency on its package version, because release
  one has no separate capability registry (the requirements keep it out of scope); the idea
  document's "capabilities as versioned semantic contracts" arrives with the framework-proof
  phase as named capability declarations layered over the same manifest.
- **Replacement providers.** Nothing in release one replaces a module with another
  implementation of the same contract. When that arrives, the mechanism is a manifest field
  `provides: [capability@version]` and a dependency form that names a capability rather than a
  module; both are additive to this manifest.

## Proving the boundary by absence

Criteria 33 and 66 require the phase suites to pass in a workspace where the memory module was
never installed. Structurally that holds because:

- `rheo_core` and `rheo_contracts` depend on no module distribution (an import-graph check in
  CI).
- The composition root reads `modules.installed` from configuration. The absence proof compares
  two configurations node by node: Recallatron remains installed on disk while the setting is
  empty, then a detached checkout removes the distribution before syncing and running the same
  phase-one demonstrators. `relationships`, `leads`, and `current` are placeholder directories,
  not distributions, so naming them would be equivalent to naming no installed module here.
- `leads` declares `recallatron` as an optional dependency and calls it only through
  `ctx.enabled_modules` checks around a registered operation name, never an import.
- Event fan-out at write time consults the workspace's enabled consumers, so an event nobody
  subscribes to is written to the outbox with zero deliveries and is complete on commit.

The same three facts are what make a synthetic specialist module in phase seven a manifest and a
distribution, with no change to the core.
