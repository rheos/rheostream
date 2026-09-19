## Requirements

**Outcome / bottleneck:** `core.module.install` and `core.module.enable` go from **not existing at
all** to installing and enabling Recallatron's skeleton distribution into a fresh workspace with
zero edits required to any file under `packages/core/` or `packages/contracts/` — checkable by a
test, and failing today because neither operation exists, the migration orchestrator raises
`NotImplementedError` for any chain but `core`, and the only code that has ever written a
`core.module_state` row is a hand-rolled test fixture (`tests/harness/registry.py`'s
`enable_harness_module`). This is a metric the work can fail: if the install/enable operations, the
module migration chain, and the real `module_state`/`module_schema_version` writers are not built,
Recallatron's skeleton cannot install, and the test proving criterion 24 has nothing to exercise.

### User Personas

- **A module author** (Recallatron first, then Relationships and Leads in phase 3) — needs a
  manifest shape that Pydantic will validate at import time, and an install/enable path that asks
  nothing of `packages/core` beyond what the manifest already declares.
- **The operator** (Robin, running the CLI or calling the operations directly) — needs
  `core.module.install`/`.enable` to name the exact thing that went wrong (a missing field, a
  missing extension, an unmet dependency) rather than fail silently or vaguely.
- **The phase-one acceptance suite (CI)** — needs to keep passing, unchanged, in a workspace that
  never named `recallatron` in `modules.installed`, proving the core carries no dependency on a
  module added through the public extension points (criterion 33).
- **A later run's Analyst/Architect (1a1, 1a2, 1a3, 1b)** — needs the manifest's field set, the
  `modules.installed` key's shape, and the install/enable operations to already be real and stable
  so those runs build memory functionality on top rather than also building the contract.

### Functional Requirements

**Manifest (resolves finding F3 — home; criterion 25):**

**FR 1** The real `ModuleManifest` type is defined in `rheo_core.modules` (replacing today's stub
   dataclass in `packages/core/src/rheo_core/modules/manifest.py`), as a Pydantic model. It carries
   every field `docs/architecture/module-contract.md`'s "The manifest" table lists: `module_id`,
   `package_version`, `core_contract_versions`, `dependencies`, `record_types`, `storage`,
   `configuration_schema`, `operations`, `tools`, `events`, `subscriptions`, `jobs`, `schedules`,
   `resolvers`, `deletion_participants`, `export`, `web`, `agent_guidance`, `secret_scopes`,
   `connector_bindings`, `health_checks`, `contract_tests`, `sensitivity`. Every one of these 23
   ratified fields is required unless the table marks it optional — only `web` and
   `agent_guidance` are — plus one 24th field the ratified table does not list, `audit_sink`
   (below), which is optional too. A manifest missing a required field fails Pydantic validation
   and the raised error names that field.
   `rheo_contracts.manifest` continues to declare no `ModuleManifest` symbol, for the same reason
   `OperationDeclaration.handler` already lives outside it: the manifest's operation and tool
   handlers are typed against `UnitOfWork` (`packages/core/src/rheo_core/storage`), and
   `rheo_contracts` may import only stdlib, pydantic, and itself. `operations` keeps today's stub
   shape, `tuple[tuple[OperationDeclaration, Handler], ...]` — the handler travels beside the
   declaration exactly as `OperationRegistry.register(decl, handler, *, origin)` already accepts it.
   The model also keeps the stub's `audit_sink` field (`manifest.py:82`), optional with default
   `None`, as a 24th field the ratified table does not list; its pydantic shape is the
   Architecture's (Decision B), not the stub's bare annotation, which does not import on a
   pydantic model: it is the only channel by which a module supplies
   the audit sink `loader.py:135-139` installs and that `dispatch.py:184`'s `audit_sink_missing`
   refuses every above-`READ` operation without. The stub's `schema_name` and `create_schema` are
   dropped — neither has a production caller.
**FR 2** `docs/architecture/module-contract.md`'s "The manifest" section is corrected in this run to name
   the manifest's real home (`rheo_core.modules.ModuleManifest`, or whatever module path the
   Architect finally settles it under) in place of `rheo_contracts.ModuleManifest`, and states the
   handler-travels-beside-declaration pattern as the reason, mirroring the note already in
   `packages/contracts/src/rheo_contracts/manifest.py`'s own docstring. This is a documentation
   correction this run makes directly, not a deferred flag for someone else.
**FR 3** Fields whose *consumption* belongs to a later run are still required for *presence* in 1a0:
   `deletion_participants`, `connector_bindings`, `sensitivity`, `jobs`, `schedules`, `events`,
   `subscriptions`, and `record_types` may all be empty tuples/mappings on Recallatron's skeleton
   manifest (an empty collection is a declared, present field — not a missing one), and nothing in
   1a0 reads them for behaviour beyond what FR 6–8 below need. `secret_scopes` is empty for
   Recallatron, matching "empty for domain modules in release one." `web` and `agent_guidance` are
   `None`, using the fields' own optionality.

**Discovery and loading (resolves F5 — the settings key; F17 — installed vs. loaded stay distinct
sets):**

**FR 4** A real deployment settings key `modules.installed` is declared in
   `rheo_core/settings/schema.py`'s `PRODUCTION_KEYS` (`ValueType.STR_LIST`, `scope =
   Scope.DEPLOYMENT`, no floor, `default = ()`). Its default is the **empty list**, not "every
   discovered module" — an unlisted module id is not loaded, with no exception, resolving the
   documented contradiction in F5. Every call site that currently reads the `RHEO_MODULES`
   environment variable — `rheo_core/modules/loader.py`'s `allowed_module_ids()`,
   `apps/core/src/rheo_app_core/startup.py`'s module-loading step, and
   `apps/cli/src/rheo_app_cli/commands/openapi.py` — reads the resolved `modules.installed` setting
   instead. The `RHEO_MODULES` environment variable name and every line that reads it are deleted;
   a repository-wide search for the literal string `RHEO_MODULES` returns nothing outside historical
   docs/changelog prose.
**FR 5** Three distinct sets stay visibly distinct in the code and are never conflated under one name or
   one function (F17): **(a) host-installed / on-disk** — every `rheo.modules` entry point
   `importlib.metadata.entry_points` reports, decided by the Dockerfile's `COPY modules/` +
   `uv sync --frozen` build context, never by configuration; **(b) deployment-loaded** — the subset
   of (a) that the resolved `modules.installed` setting names, computed by `load_modules()`;
   **(c) workspace-installed/enabled** — the per-workspace `core.module_state.state` value, changed
   only by `core.module.install`/`.enable` for that one workspace. A module id can be in (a) without
   being in (b) (private module on disk, not loaded this deployment) and in (b) without being in (c)
   for any given workspace (loaded this deployment, never installed in this workspace).
**FR 6** `load_modules()`/`_register()` in `rheo_core/modules/loader.py` registers, for every module
   selected by (b), the manifest's extension points beyond today's two (operations, resolvers).
   Tools register into the shared `ToolRegistry` (`rheo_core.tokens.sets`). Subscriptions register
   into the consumer registry (`rheo_core.events`) and job kinds register into a `JobKindRegistry`
   (`rheo_core.work.kinds`) — but `JobKindRegistry` and the consumer registry are **composition-root
   locals owned by `apps/worker`**, not process-global singletons like the other three registries
   (`OperationRegistry`, `ResolverRegistry`, `ToolRegistry`), so `load_modules()` gains two optional
   parameters, `kinds` and `consumers`, defaulting to "do not register that category." Events have
   no dedicated registry — no code in this tree indexes event *declarations*, and none is built this
   run; a loaded module's declared events are validated at load (each declaration's `type` must
   namespace under the module's own id, and two loaded modules may not declare the same event type)
   and stay readable per-module from what the loader loaded. The module's declared schedules become
   readable by the enable operation (FR 8) without themselves being process-global registrations,
   since a schedule is a per-workspace `core.schedule` row created at enable, not a process-wide
   registration. Web surface and audit sink registration are unchanged from today. A manifest whose
   `core_contract_versions` does not list the core's current contract version fails to load, naming
   both versions.

   **`apps/worker/src/rheo_app_worker/main.py` is not a bare stub — it is the worker's composition
   root**, and already builds `JOB_KINDS` and `CONSUMERS` there. This run adds the one call that
   supplies them, `load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)`, inside `main()` (this closes
   Open Question 2 and corrects the assumption, carried in an earlier draft of this section, that
   `apps/worker/src` is a bare stub with no startup sequence to extend). Without this call, this
   requirement's job-kind and subscription registration would have no production caller at all, and
   AC 14 would be proved only by a fixture exercising a path no deployed process ever runs.

   A manifest that declares a tool, subscription, or job kind that the loader does not attach fails
   a test asserting the corresponding registry is non-empty after `load_modules()` runs; a manifest
   that declares an event fails a test asserting that event is present and validated in the loader's
   per-module record — for every category the manifest declared.

**Install and enable (resolves F15 — nothing can install a module today; criteria 24, 25):**

Public criterion 24 must be recorded as **partially** closed by this run — its **module-contract
half** (install and enable through the registration contract described below, with no core file
change, plus the workspace row recording package version and schema version) — and must never be
recorded as closed in full: criterion 24's **interface-contribution half** (the `web` contribution,
the `apps/web/src/modules.generated.ts` composition step, and the test asserting a module's screens
appear) is not built by this run and belongs to 1a3 (see MVP Scope and AC 1).

**FR 7** `core.module.install` (mutate, long-running; roles owner, operator) exists and implements the
   release-one 7 steps from `module-contract.md` § Install and enable:
   1. Refuse `module_unavailable`, naming the module, when it is not in the deployment-loaded set
      (FR 5b).
   2. Verify each declared required dependency is `installed` (at minimum) in this workspace at a
      satisfying version range; an optional dependency absent from this workspace is recorded as
      absent rather than refused.
   3. Run `CREATE EXTENSION IF NOT EXISTS <name>` in the workspace database for every name in the
      manifest's `storage.required_extensions`, before any migration statement; a failure names the
      extension and stops the install before any migration step runs.
   4. Create the module's own Postgres schema (named for `storage.schema_name`, which must equal
      `module_id`) and apply its migration chain (FR 9) under the workspace's advisory lock; each
      applied step writes one `core.module_schema_version` row.
   5. Write the `core.module_state` row for this module as `installed`, with its package version.
   6. The profile-`test` resolution of `contract_tests` is evaluated at pre-flight, ahead of step 3
      — alongside the `module_unavailable`, `module_already_installed`, and dependency refusals —
      so a `contract_tests_missing` refusal reaches the caller by name before any mutation begins:
      when the resolved profile is `test`, install resolves the module's `contract_tests` path and
      refuses `contract_tests_missing`, naming the path, if it does not exist. Install validates
      that the declared test target is real; it does not execute it — running `contract_tests` is
      CI's job, not install's, since spawning a test runner from inside a mutate operation's own
      transaction is re-entrant (the suite that drives install is itself pytest) and unbounded in
      duration inside a leased job. For any other profile, at this point in the sequence, run every
      declared `health_checks` callable, refusing `health_check_failed`, naming the failing check,
      on any failure.
   7. Leave the module's `configuration_schema` keys settable; none are required in order to enable.

   **Closing Open Question 1: a repeat install is refused, not a no-op and not an upgrade.** Ahead
   of step 1, `core.module.install` refuses `module_already_installed`, naming the module's current
   state, when a `core.module_state` row for this module already exists in this workspace in any
   state. A silent no-op would swallow the case that matters — the deployment-loaded package is a
   *different version* from the recorded one — and report success; an upgrade path is release-seven
   scope this run does not build. This decision is not itself one of `module-contract.md`'s numbered
   7 steps; it is this run's resolution of a gap those steps leave open. See Edge Case 2.
**FR 8** `core.module.enable` (mutate; roles owner, operator) exists and implements the release-one 4
   steps:
   1. Refuse unless the module's current state in this workspace is `installed` or `disabled`.
   2. Refuse, naming the dependency, unless every required dependency is `enabled` in this
      workspace.
   3. Write settings rows for every key the module's `configuration_schema` marks
      `explicit_per_workspace`, from their package defaults; create a `core.schedule` row for every
      schedule the manifest marks `enabled_by_default`.
   4. Set the module's state to `enabled`. From the next request, `WorkspaceContext.enabled_modules`
      includes it and its tools, routes, navigation, subscriptions, and jobs are live for that
      workspace.
**FR 9** `rheo_core/migrations/orchestrator.py`'s `migrate_workspace` continues to refuse a module
   chain name — with a `ValueError` naming the module-chain entry point in place of today's
   `NotImplementedError` — because its failure branch (`orchestrator.py:239-245`) is already
   generic over `chains` and admitting one there is the FR 11 violation. Module chains run through
   a new `run_module_chain`, which resolves its own Alembic script directory from the manifest's
   `storage.migrations_path` (the module-side analog of the core chain's `script_location()`), runs
   under the *same* per-workspace advisory lock the core chain already takes, and writes into that
   module's own `alembic_version_<module_id>` version table inside the module's own schema (per
   `storage-and-workspaces.md` § Migrations) — never the core's version table. `CHAINS` is no longer
   a fixed `(control, core)` pair for this purpose; module chains run in the manifest's
   dependency-sorted order.
**FR 10** New storage-repository write functions in `rheo_core/storage/repositories.py` — for
    `core.module_state`, an `insert_module_state` writer (called by install, at install step 5) and
    a `set_module_state` writer for the enable transition (called by enable, at enable step 4); and
    for `core.module_schema_version`, an `insert_module_schema_version` append, one row per applied
    migration step — are the outcome this run must establish as the *only* code outside a
    migration's own DDL that writes either table. That property does not hold today:
    `exports/artifact.py`'s `_install_modules` (the export-restore path, lines 558 and 571)
    hand-builds `insert(core_tables.module_state)` and `insert(core_tables.module_schema_version)`
    directly in production code, outside `repositories.py` and outside any migration; this run must
    route those two calls onto `insert_module_state`/`insert_module_schema_version` so the sole-writer
    property holds. `core.module.install`, `core.module.enable`, and the export-restore path in
    `exports/artifact.py` are the only callers of any of the three — three callers, not two: routing
    restore through the writers necessarily makes it a caller, so the caller clause widens while the
    writer clause stays absolute (exactly one module owns the write path — what makes it auditable and
    what AC 7's static scan measures).
    `tests/harness/registry.py`'s `enable_harness_module` is **rewritten to call
    `insert_module_state`, not deleted**: 18 files depend on it (15 under `tests/postgres/` plus
    `tests/harness/consumers.py`, `registry.py`, and `runtime_matrix.py`), including coverage for
    `core.workspace.status`, context routing, approvals, and the `module_disabled` branch — deleting
    it would take that coverage with it, which is scope this run does not own. `core.workspace.status`'s
    existing handler (`_workspace_status` in `operations/core_ops.py`) already reads both tables
    generically through `list_module_states`/`list_module_schema_versions`; it needs no change —
    only real rows to read.
**FR 11** A module-migration failure inside `core.module.install` (a bad revision, a `schema_ahead`
    condition, or any exception from `run_chain`) is surfaced to the caller as a refused/failed
    operation outcome. It does **not** silently set the control-plane `workspace.state` to
    `unavailable` the way a core-chain failure at *startup* does (`migrate_workspace`'s existing
    behaviour for the `core` chain) — install is a caller-initiated mutate operation with someone
    to tell, not an unattended startup pass with no caller waiting on a result.

**Boundary-gate coverage (resolves F18 — see decision below; NOT part of Requirement 13/criterion
26):**

**FR 12** `tests/test_boundary.py`'s `_SCAN_DIRS` and `tests/test_boundary_scans.py`'s `_SCAN_DIRS` (both
    currently `("packages", "apps", "scripts", "tests")`) are extended to also walk `"modules"`, so
    the `WorkspaceContext`-construction gate (B15) and the `SecretScope`-construction gate (B9) cover
    code under `modules/*/src` now that Recallatron's skeleton puts real code there for the first
    time. **Decision (F18): this is a separate fix, not folded into Requirement 13 / criterion 26.**
    Criterion 26 is about a module's *storage/table ownership* (a module writes only to its own
    schema, reaches storage only through the core's API); the `_SCAN_DIRS` gap is about two
    *unrelated* pre-existing gates — a module must not mint its own `WorkspaceContext` or construct
    a `SecretScope` — that happened to have never been exercised against real module code because
    none existed. The two properties are checked by different mechanisms (a table-name/schema
    check vs. an AST scan for named-class construction) and can fail independently of each other. It
    belongs to 1a0 because this run ships the first real code under `modules/*/src`, which is the
    moment the blind spot stops being theoretical — but it is its own requirement and its own
    acceptance criterion, not a restatement of criterion 26's.

**Recallatron skeleton (the distribution, not the memory module):**

**FR 13** `modules/recallatron/` gains: a `rheo.modules` entry point in `pyproject.toml` naming a real
    `ModuleManifest` instance whose `module_id == "recallatron"`; a dependency on `rheo-core` and
    `rheo-contracts`; a minimal Alembic migration environment under the path its manifest's
    `storage.migrations_path` names, whose one revision creates the `recallatron` schema and
    **nothing else** — no memory tables, since Phase 2's memory schema is 1a1/1a2's work; a
    `contract_tests` path naming a real (minimal) test entry under `modules/recallatron/tests/` that
    install *resolves and validates* under profile `test` (step 6) — install checks the declared
    path exists and refuses if it does not, it does not execute the suite (see FR 7 step 6); the
    entry itself is a trivial smoke assertion that the manifest validates and its migrations package
    resolves, which is sufficient content without pre-building 1a1/1a2's behavioural suite (this
    closes Open Question 3); and every other required manifest field populated with the smallest
    well-formed value — empty tuples for `record_types`, `operations`,
    `tools`, `events`, `subscriptions`, `jobs`, `schedules`, `dependencies`, `deletion_participants`,
    `connector_bindings`, and `secret_scopes`; an empty mapping for `sensitivity`; an empty
    `configuration_schema` (no keys declared); a trivial `export` declaration; `web = None`;
    `agent_guidance = None`. The manifest's own `storage.required_extensions` is empty for the
    skeleton (see Assumptions) — the extension-creation mechanism itself (Requirement 7 step 3) is
    proven by a harness/test fixture module that does declare one, not by forcing Recallatron to
    claim an extension its empty schema does not need.

**Proving the boundary by absence (criterion 33):**

**FR 14** The phase-one acceptance matrix's own demonstrators — the pytest node ids
    `docs/acceptance/phase-1-matrix.md` names for criteria 1–23 and 69, not the whole `tests/` tree —
    produce an identical pass/fail count across two configurations: **(A)** `recallatron` present on
    disk and installed into the environment, but `modules.installed` resolved to a value that does
    not include it (e.g. naming no modules, or naming only synthetic harness modules); and **(B)**
    `recallatron` removed from the checkout entirely and the environment re-synced. This proves
    `rheo_core` and `rheo_contracts` depend on no module distribution, by configuration (A) and by
    absence-on-disk (B) alike — neither configuration is left to stand in for the other. The
    comparison is scoped to the matrix's own demonstrators, not the whole `tests/` tree, because 1a0
    itself adds phase-two tests under `tests/` that legitimately require Recallatron on disk (e.g.
    AC 1, AC 10–13); those would fail under configuration B, and a whole-tree diff would report a
    difference that proves nothing. A third, cheap check runs beside the two configurations: a
    static scan asserting no file under `packages/`, `apps/`, or `tests/` names `rheo_recallatron`
    or `modules/recallatron`, except the phase-two test files that exist to drive it.

### MVP Scope

**In scope:**

- The real `ModuleManifest` in `rheo_core.modules`, replacing the stub, with the doc correction
  (FR 1–3).
- The `modules.installed` settings key, replacing `RHEO_MODULES` everywhere it is read, with
  installed/loaded/enabled kept as three distinct named sets (FR 4–5).
- The loader registering the manifest's extension points beyond today's two, including the
  `apps/worker` composition-root call site (`load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)`
  inside `main()`) that gives job-kind and subscription registration a production caller (FR 6).
- `core.module.install` and `core.module.enable`, the release-one 7- and 4-step subsets (FR 7–8).
- Module migration chains in the orchestrator, `CREATE EXTENSION IF NOT EXISTS`, and the real
  `core.module_state` / `core.module_schema_version` writers replacing the test fixture (FR 9–11).
- The `_SCAN_DIRS` boundary-gate coverage fix for `modules/*/src` (FR 12).
- The Recallatron distribution skeleton: entry point, real manifest, minimal schema-only migration,
  a contract-test path (FR 13).
- Criterion 33's no-memory-module proof run (FR 14).

**Out of scope (v1 — belongs to a sibling run or a standing issue):**

- Memory schema, remember/recall/read operations, embeddings, retrieval adapters (1a1, 1a2).
- EverAlgo and predecessor-retrieval constraints (1a2).
- Web contribution, navigation/theme unification, naming/fixture-provenance CI gates, memory
  browse/search/entity screens (1a3).
- Public criterion 24's **interface-contribution half** — the `WebContribution` shape, the
  `apps/web/src/modules.generated.ts` composition step, and the test asserting a module's screens
  appear — is out of scope for 1a0 and belongs to 1a3. This run closes only criterion 24's
  **module-contract half**: install and enable through the registration contract with no core file
  change, plus the workspace row recording package version and schema version (see AC 1).
- Migration and cutover from M.O.T. (1b).
- Module disable, remove, purge, restore — and any stub of them. Phase-seven work under D5;
  `core.module_state`'s lifecycle diagram beyond `installed`/`enabled` is not this run's to build.
- GitHub issues #66, #65, #44, #40, #12; the `dispatch.py` split; outbox retention. None of this
  run's requirements depend on any of them, so none is pulled in.
- Public criteria 27–32 and 34–37 (memory permissions, retention, retrieval strategy selection,
  migration verification, naming/fixture/export/navigation gates) — these are the sibling runs'
  acceptance criteria, not 1a0's.

### Edge Cases & Failure Modes

**EC 1** **Install called for a module not in the deployment-loaded set.** Refused `module_unavailable`,
   naming the module (Requirement 7 step 1) — this is the everyday case for every module id present
   on disk but not named in `modules.installed`.
**EC 2** **Install called on a module already `installed` or `enabled`.** The ratified lifecycle diagram
   and the install/enable text do not state whether a repeat install is a no-op, a refusal, or an
   upgrade path. **Resolved (closes Open Question 1): refusal.** `core.module.install` refuses
   `module_already_installed`, naming the module's current state, when a `core.module_state` row for
   it already exists in this workspace in any state — never a silent no-op (which would mask the
   deployment loading a different package version over an already-installed row and report success)
   and never an upgrade (release-seven scope this run does not build). Tested directly: an install
   attempt against a module already in state `installed` or `enabled` returns the refusal, naming
   that state.
**EC 3** **A required extension cannot be created** (the application role lacks `CREATEDB`/superuser-like
   privilege for extensions). Install fails, naming the extension, and no migration step for the
   module runs — verified by a test using an extension name the test role is denied, distinct from
   the "extension does not exist" case.
**EC 4** **Enable called before install** (state `absent`). Refused per Requirement 8 step 1.
**EC 5** **Enable called when a required dependency is not yet `enabled`** in this workspace. Refused,
   naming the dependency (Requirement 8 step 2). Recallatron's skeleton declares no dependencies in
   1a0, so this path is exercised with a synthetic two-module harness fixture, not with Recallatron
   itself.
**EC 6** **A module migration chain fails partway** (bad revision, `schema_ahead`, or a raised exception
   from the migration script). The failure must be visible to the `core.module.install` caller as a
   failed operation outcome, and must **not** silently flip the workspace's control-plane row to
   `unavailable` the way a core-chain failure at startup does (Requirement 11) — a workspace whose
   *other* modules and core schema are fine must not become globally unavailable because one
   module's install failed.
**EC 7** **`modules.installed` names a module id nothing on disk provides.** Not an install-time failure
   at all — it is simply never loaded, exactly as `RHEO_MODULES` behaves today (loader.py's existing
   "allow says what may load, not what must be present" rule carries over unchanged to the new key).
**EC 8** **A settings write attempts to override `modules.installed` at workspace or member scope.**
   Refused by the existing generic scope-enforcement in the settings write path (`Scope.DEPLOYMENT`
   keys cannot be set through `core.settings.set`/`.set_member`) — no new mechanism is needed, only
   confirmation this generic rule still applies to the new key.
**EC 9** **The phase-one acceptance matrix's demonstrators are run with Recallatron entirely absent from
   the checkout** (not just absent from `modules.installed`) versus **present on disk but absent
   from `modules.installed`.** Criterion 33 must pass in both configurations identically over the
   matrix's own demonstrators (`docs/acceptance/phase-1-matrix.md`'s node ids for criteria 1–23 and
   69) — not the whole `tests/` tree, since this run's own phase-two tests need Recallatron on disk
   and would make a whole-tree diff meaningless under the absent-from-checkout configuration. This
   proves the boundary is enforced by configuration and by absence-on-disk alike, not by one
   accidentally standing in for the other.

### Assumptions

- Recallatron's skeleton migration creates only the `recallatron` schema, no tables — ASSUMED
  default: source, the run brief's explicit "an empty-or-minimal schema that PROVES install and
  enable... NOT the memory tables."
- Recallatron's skeleton manifest declares an empty `storage.required_extensions` tuple; the
  `CREATE EXTENSION IF NOT EXISTS` mechanism (Requirement 7 step 3) is proven with a harness/test
  fixture module that does declare a real extension name, not by Recallatron's own skeleton —
  ASSUMED default, since the skeleton's schema has nothing that would need pgvector or `pg_trgm` yet
  (those extensions arrive with the retrieval adapter in 1a2, per
  `docs/architecture/storage-and-workspaces.md` § Retrieval adapter).
- The operator's remedy for an extension-creation failure (a pre-built `rheo_template` database,
  per `storage-and-workspaces.md` § Provisioning) stays documented operator work, not automated by
  this run — source: that same document, quoted verbatim in the run brief's read list.
- `apps/worker/src` is **not** a bare stub with no startup sequence to extend — corrected from a
  wrong assumption in an earlier draft of this section. `apps/worker/src/rheo_app_worker/main.py:117`
  is the worker's composition root; `JOB_KINDS` and `CONSUMERS` are built there at `:46` and `:67`.
  Source: direct read of `apps/worker/src/rheo_app_worker/main.py`, confirmed on this tree
  (Architect verification, 1a0). This run gives it one call,
  `load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)`, inside `main()` — see FR 6.
- `modules.installed`'s two *pre-existing* production call sites — the ones that already read
  `RHEO_MODULES` today — are `apps/core/src/rheo_app_core/startup.py` and
  `apps/cli/src/rheo_app_cli/commands/openapi.py` — source: direct repo grep for `RHEO_MODULES` and
  `load_modules()` across `apps/`, confirmed on this tree (no third *existing* call site found).
  This run adds a third call site, in `apps/worker/src/rheo_app_worker/main.py`'s `main()`, where
  none existed before (see FR 6) — the settings key has three readers after this run, not two.
- `core.workspace.status`'s handler (`_workspace_status`) needs no code change to satisfy criterion
  24's "workspace row now records the module's version and its schema version" — it already reads
  both tables generically. Source: direct read of
  `packages/core/src/rheo_core/operations/core_ops.py`, confirmed on this tree.

### Open Questions

All three of this run's original open questions are resolved by the Architecture; each resolution
is recorded at its own requirement rather than repeated here.

1. ~~Is a repeat install a no-op, refusal, or upgrade path?~~ **Resolved: refusal, naming the
   current state.** See FR 7's closing note and Edge Case 2.
2. ~~Who builds `apps/worker`'s own module-loading call site?~~ **Resolved: this run does**, since
   `apps/worker/src/rheo_app_worker/main.py` is already the composition root that builds
   `JOB_KINDS` and `CONSUMERS`, and closing this gap is what gives FR 6/AC 14's registration a
   production caller. See FR 6.
3. ~~What minimal content satisfies `contract_tests`?~~ **Resolved: a smoke test asserting the
   manifest validates and its migrations package resolves**
   (`modules/recallatron/tests/test_contract.py`). See FR 13.

Two new open questions surfaced by the Architecture, neither this run's to resolve:

4. **Should `publish()` refuse an undeclared event type?** This run validates a manifest's declared
   events at load time (FR 6) but builds no enforcement at publish time — `rheo_core.events.publish`
   is unchanged. The run that first ships a module actually publishing an event (1a1 or 1a2) should
   decide whether an undeclared type is refused there; that is a change to the core's shipped write
   path and no criterion in this run asks for it.
5. **Who owns a phase-two acceptance matrix** analogous to `docs/acceptance/phase-1-matrix.md`,
   recording criteria 27–37's node ids and pass/fail state? `tests/test_acceptance_matrix.py`'s
   parser covers criteria 1–23 and 69 only; no criterion in this run requires a phase-two matrix, so
   none is built here, but 1a1/1a2/1a3/1b will need some mechanism to record their own criteria's
   state the way this run's absence proof (FR 14) already does for phase one.

## Acceptance criteria

**AC 1** **(public 24)** A test installs then enables Recallatron's skeleton distribution into a fresh
   workspace calling only `core.module.install` and `core.module.enable`; no file under
   `packages/core/` or `packages/contracts/` is edited to make this pass (checkable: the module is
   added to `modules.installed` and the entry point/manifest/migration/tests live entirely under
   `modules/recallatron/`). After enable,
   `core.workspace.status`'s response lists `recallatron` under `modules` with a non-null
   `package_version`, `state == "enabled"`, and a non-null `schema_version`. **This closes public
   criterion 24's module-contract half only** — install and enable through the registration
   contract with no core file change, plus the workspace row recording package version and schema
   version. **It does not close criterion 24's interface-contribution half.** Recallatron's
   skeleton ships `web = None`; the `WebContribution` shape, the
   `apps/web/src/modules.generated.ts` composition step, and the test asserting a module's screens
   appear are not built by this run and are run 1a3's. A reader who marks criterion 24 fully closed
   after this run alone is reading a wrong record.
**AC 2** **(public 25)** A test constructs a `ModuleManifest` omitting, in turn, `record_types`,
   `storage`, `configuration_schema`, `tools`, and `export`, and asserts Pydantic validation fails
   each time with that exact field named in the raised error. `ModuleManifest.model_fields` (or
   equivalent introspection) contains every field module-contract.md's manifest table lists **plus
   `audit_sink`**, and exactly three fields — `web`, `agent_guidance` and `audit_sink` — are
   optional.
**AC 3** **(public 26)** A test asserts: Recallatron's skeleton migration creates no object outside the
   `recallatron` schema; no file under `modules/recallatron/src` imports another module's
   schema-qualified table name or another module's storage-layer symbols; and no file under
   `modules/recallatron/src` constructs a raw database connection, DSN, or engine directly (storage
   access happens only through a `UnitOfWork`/repository the dispatcher supplies). **This tests
   criterion 26's schema-isolation and no-foreign-import clauses only.** Criterion 26's third
   clause — that a cross-module change is effected only through a public operation or event — is
   not tested by this AC; Recallatron's skeleton declares no operations or events to exercise it,
   and no criterion in this run substitutes for it.
**AC 4** **(public 33)** Running the phase-one acceptance **matrix's own demonstrators** —
   `docs/acceptance/phase-1-matrix.md`'s pytest node ids for criteria 1–23 and 69, not the whole
   `tests/` tree — with `modules.installed` resolved to a value that never names `recallatron`
   (Recallatron present on disk, not loaded) produces an identical per-node-id outcome map (not
   merely an identical pass/fail count) as running the same demonstrators with Recallatron
   entirely absent from the `modules/` checkout — run twice,
   diffed. Scoped to the matrix's demonstrators because this run's own phase-two tests (AC 1,
   AC 10–13) legitimately require Recallatron on disk and would fail under the absent-from-checkout
   configuration, which a whole-tree diff would misreport as a boundary violation.
**AC 5** **(F3)** `rheo_core.modules` (or the Architect's final module path) defines `ModuleManifest`;
   `rheo_contracts` exposes no `ModuleManifest` attribute; `docs/architecture/module-contract.md`'s
   manifest section names the real module path instead of `rheo_contracts.ModuleManifest`.
**AC 6** **(F5)** `rheo_core/settings/schema.py`'s `PRODUCTION_KEYS` contains a `modules.installed` key of
   type `list[str]`, deployment scope, default `()`. A repository-wide search for the literal
   `RHEO_MODULES` returns no hits under `packages/`, `apps/`, or `tests/` (historical doc prose
   excepted).
**AC 7** **(F15)** `rheo_core/storage/repositories.py` contains a write function for `core.module_state`
   and one for `core.module_schema_version`; a repository-wide search for a hand-built
   `pg_insert(core_tables.module_state)`/`module_schema_version` construction finds none outside
   `repositories.py` itself and its own unit tests.
**AC 8** **(F17)** No single function or settings key answers more than one of "on disk," "loaded this
   deployment," and "installed/enabled in this workspace" — verified by a test that puts a module id
   on disk but not in `modules.installed` (loaded is false, on-disk is true) and a second test that
   loads a module this deployment but never installs it in a given workspace (loaded is true,
   workspace-installed is false), asserting both states are independently readable.
**AC 9** **(F18 — separate from criterion 3/26 above)** `tests/test_boundary.py`'s `_SCAN_DIRS` and
   `tests/test_boundary_scans.py`'s `_SCAN_DIRS` both include `"modules"`. A test constructing a
   `WorkspaceContext` or a `SecretScope` inside a file under `modules/recallatron/src` is caught by
   the corresponding existing gate (proven by a temporary fixture file exercised in the test suite,
   or by direct assertion that `"modules" in _SCAN_DIRS` for both files).
**AC 10** `run_module_chain` applies a named module chain (e.g. `"recallatron"`) under the same advisory
    lock as the core chain (`orchestrator.py:161`), writes into `alembic_version_recallatron` inside
    the `recallatron` schema with the core's own version table untouched, and leaves exactly one row
    in `core.module_schema_version` for that module after a fresh install.
    `migrate_workspace(chains=("recallatron",))` raises `ValueError` naming `run_module_chain` and
    leaves the `control.workspace` row unchanged — asserted, because a test that only checks for the
    absence of `NotImplementedError` passes against the implementation FR 11 forbids.
**AC 11** A manifest declaring a `required_extensions` entry causes `core.module.install` to run
    `CREATE EXTENSION IF NOT EXISTS <name>` before any migration statement (verified with a real
    extension name via a harness/test fixture module per the Assumptions section); a manifest naming
    an extension the database role cannot create causes install to fail, naming that extension, with
    no migration step for the module having run. The refusal name arrives in the operation record's
    `error_text` (`"extension_failed: …"`), not in `error_code`, which is `work/loop.py:146`'s
    single `job_failed` for any job failure; only the five pre-flight refusals
    (`module_unavailable`, `module_already_installed`, `dependency_missing`, `dependency_version`,
    `contract_tests_missing`) are their own `error_code`.
**AC 12** A forced module-migration failure inside `core.module.install` is returned as a failed/refused
    operation outcome to the caller, and the control-plane `workspace.state` row is unchanged by it
    (distinct from a core-chain startup failure, which does flip that row to `unavailable`). The
    failure name arrives in the operation record's `error_text` as `"migration_failed: <the
    chain's own exception, rendered type-name-first — e.g. StorageRefusal: schema_ahead: …>"`, not
    in `error_code`, which is `work/loop.py:146`'s single `job_failed` for any job failure.
    `migration_failed` is the `ModuleInstallFailed` code for a chain failure, beside
    `extension_failed` and `health_check_failed`; it is never spelled `extension_failed`, because
    the extension step has already completed when the chain runs. Only the five pre-flight
    refusals (`module_unavailable`, `module_already_installed`, `dependency_missing`,
    `dependency_version`, `contract_tests_missing`) are their own `error_code`.
**AC 13** `core.module.enable` refuses when state is not `installed`/`disabled` (naming the actual state),
    refuses naming an un-enabled required dependency, writes `explicit_per_workspace` settings rows
    and `enabled_by_default` `core.schedule` rows from the module's manifest, and sets state
    `enabled` — each of the four steps independently tested.
**AC 14** `load_modules()` registers a manifest's declared tools into `ToolRegistry`, job kinds into
    `JobKindRegistry` (via the worker's `kinds=`/`consumers=` parameters), and subscriptions into
    the consumer registry, in addition to today's operations and resolvers — verified by a manifest
    fixture declaring one of each and asserting all three (plus the two existing categories) appear
    in their respective registries after `load_modules()` runs. A manifest's declared **events**
    have no registry to appear in — this run builds none — so events are verified differently: the
    same fixture's declared event is present and validated (namespaced under the module's own id,
    and rejected as a duplicate against another loaded module's event of the same type) in the
    loader's per-module record after `load_modules()` runs.

## Architecture

Existing-project mode. Every choice below is made inside the shipped stack; no framework, no
library and no storage engine is added. Read against the tree at `9067552`.

### Decision A — how much of public criterion 24 this run closes

`docs/requirements/build-plan.md:271-275` asks one test to assert that a module's **records,
tools, migrations and interface contributions** all appear after install and enable. This run
closes three of those four and the workspace-row half, and it does not close the fourth.

| Half of criterion 24 | Closed here? | Where |
| --- | --- | --- |
| No core file changes to add the module | **yes** | AC 1's clause that the entry point, manifest, migration and tests live entirely under `modules/recallatron/`, plus phase 8's `tests/test_module_import_graph.py` — no file under `packages/`, `apps/` or `tests/` names `rheo_recallatron` or `modules/recallatron` outside the declared phase-two test files |
| Records, tools, migrations appear | **yes, vacuously for records and tools** — the skeleton declares empty tuples, so what is proved is that the *registration path* for each category runs and is readable (AC 14), plus one real migration (AC 10) | `loaded_manifests()`, `ToolRegistry`, `core.module_schema_version` |
| Workspace row records version and schema version | **yes** | AC 1's `core.workspace.status` assertion |
| **Interface contributions appear** | **no — CARRIED to 1a3** | the skeleton sets `web = None` (FR 13) |

**The interface-contribution half of criterion 24 is carried to run 1a3, explicitly and not by
inference.** 1a3 owns `WebContribution`'s real shape (`navigation`, `routes`, `recordViews`,
`forms`, `searchProviders` — `module-contract.md:160-174`), the `apps/web/src/modules.generated.ts`
composition step, and the test that asserts a module's screens appear. Until 1a3 lands, **criterion
24 is partially closed and must be recorded as such** in whatever matrix phase two keeps; a reader
who finds criterion 24 marked complete after 1a0 is reading a wrong record. The build-tail's last
phase writes this sentence into `docs/architecture/module-contract.md` beside the criterion-24
sentence at `module-contract.md:240-242`, so the carry lives in the ratified document and not only
here.

`web`'s **type** is a second, smaller carry. Today's `WebSurface(surface, host, path)`
(`manifest.py:56-68`) has a live consumer — `module_surfaces()` (`loader.py:75-77`) feeding
`routing_config()` through `apps/core/src/rheo_app_core/internal_routes.py:104`. That is a routing
surface, not the ratified TypeScript contribution — `module-contract.md:43` names
`WebContribution(package_name, navigation, routes, record_views, forms, search_providers)`, none of
which exists in this tree. This run keeps the field typed `WebSurface | None` and 1a3 widens it (or
replaces it with `WebContribution` carrying the surface). Renaming it now for a field set nobody
reads yet would be churn. **The gap between the ratified type and the shipped one is written into
`:43` by this run** (ADR Records, correction 4), so a 1a3 reader finds the carry in the ratified
document rather than inferring it from a type that does not match.

### Decision B — the manifest is a pydantic model, and what configuration that takes

**Confirmed: a frozen pydantic `BaseModel`, replacing the stub frozen dataclass plus its
hand-rolled `validate()` (`packages/core/src/rheo_core/modules/manifest.py:71-115`; `WebSurface`
above it at `:56-68` is kept verbatim).** The forcing requirement is public criterion 25 / AC 2: *a
manifest missing a required field fails validation with that field named*. Pydantic emits exactly
that (`ValidationError`, `type="missing"`, `loc=("record_types",)`) for free across all 21 required
fields. The dataclass alternative needs 21 hand-written presence checks that nothing keeps in step
with the field list — the defect AC 2 exists to catch, re-introduced in the code that is supposed to
prevent it.

The configuration:

```python
model_config = ConfigDict(frozen=True, extra="forbid")
```

**No `arbitrary_types_allowed`.** Every field type in Decision C's table is one pydantic v2 builds a
schema for natively — checked type by type against what pydantic actually does with the annotation,
not against what the field is called:

| Annotation kind | Where it appears | What pydantic builds |
| --- | --- | --- |
| `str`, `int`, `bool`, `tuple[...]`, `Mapping[...]`, `frozenset[Role]` (`Role` is a `StrEnum`, `rheo_contracts/context.py:18`), `type[BaseModel]` | `module_id`, `package_version`, `core_contract_versions`, `secret_scopes`, `contract_tests`, `agent_guidance`, `sensitivity`, and the fields of the nine declaration models | the ordinary schema |
| pydantic models | `OperationDeclaration`, `ToolDeclaration` (`rheo_contracts/manifest.py:138`), the nine new declaration models | a model schema |
| stdlib frozen dataclasses | `KeySpec` (`settings/schema.py:178-179`), `ConsumerSubscription` (`events/consumers.py:48-49`), `WebSurface` (`manifest.py:56-57`) | a dataclass schema from their fields — `ValueType`, `Scope`, `Floor` are enums, `FrozenValue` is `str \| int \| bool \| tuple[str, ...]`, `ConsumerHandler` is a `Callable` alias; an instance passes through unchanged under the default `revalidate_instances="never"` (verified: a `KeySpec` instance comes back by identity) |
| `Callable` aliases | `Handler` (`operations/registry.py:68`), `RecordResolver` (`refs/resolver.py:75` — a `Callable` alias, **not** a protocol), `JobHandler` (`work/kinds.py:31`), `HealthCheck`, `DeletionHandler`, `Exporter`, `Importer` | `callable_schema`, a `callable()` check; parameter types (`UnitOfWork` among them) are never inspected |
| **a `typing.Protocol`** | **`audit_sink: AuditSink`** (`audit/sink.py:71`) — the one field pydantic cannot handle on its own | see below |

**`AuditSink` is a `Protocol` without `@runtime_checkable`** (the only runtime-checkable Protocol in
`packages/` is `IdentityProvider`, `identity/boundary.py:44`). Under `arbitrary_types_allowed=True`
pydantic emits `is_instance_schema(AuditSink)` for it, and pydantic-core's `IsInstanceValidator`
build probes `isinstance(None, cls)` to confirm the class is usable as `isinstance`'s second
argument; against a non-runtime-checkable Protocol that raises `TypeError: Instance and class
checks can only be used with @runtime_checkable protocols`, which surfaces as `SchemaError: Error
building "model" validator` **at class-definition time**. `import rheo_core.modules.manifest` would
fail outright, and `apps/core` startup, the CLI and the worker with it. Reproduced in this
worktree's venv on pydantic 2.13.5. The stub's identical annotation (`manifest.py:82`) was safe only
because a frozen dataclass runtime-checks nothing; it is not safe here.

**The field's shape is therefore:**

```python
audit_sink: Annotated[AuditSink, PlainValidator(_audit_sink)] | None = None
```

where `_audit_sink(value)` returns `value` when `callable(getattr(value, "record", None))` and
otherwise raises `ValueError("an audit sink must provide record()")` — the same duck check
`install_sink` already applies at `audit/sink.py:110-111`, so the manifest and the installer agree
on what a sink is. `PlainValidator` replaces the is-instance schema, so no `isinstance` is ever built
or run; the annotation `AuditSink` stays visible to mypy and to a reader; `CORE_AUDIT_SINK` passes
through by identity; an `object()` fails validation with `loc=("audit_sink",)`. Verified in the venv:
a 24-field prototype with exactly these types defines, the Recallatron-shaped skeleton validates,
each of AC 2's five omissions raises `type="missing"` naming the field, `extra="forbid"` refuses an
unknown name, and `model_fields` holds 24 names with exactly `web`, `agent_guidance` and
`audit_sink` not required.

Rejected: **`@runtime_checkable` on `AuditSink`** — also correct, but it edits `audit/sink.py`,
which is outside this run's declared scope, and what it would buy (`hasattr(value, "record")`) is
what the validator does; **`SkipValidation`** — the class defines, but a bare `object()` is then
accepted (verified); **`Any`** — drops the type; **a local `@runtime_checkable` sub-Protocol** — a
second name for one contract. And **`arbitrary_types_allowed` is dropped rather than kept "just in
case"**: with it, pydantic silently falls back to `isinstance` for any type it cannot build, which
is exactly the fallback that broke here; without it, an unhandled type fails class definition with
`PydanticSchemaGenerationError` naming the type, and a builder adding a field to this contract
decides its validation on purpose. Verified: the same prototype defines and validates under a bare
`ConfigDict(frozen=True, extra="forbid")`. **A later Protocol-typed field gets a `PlainValidator`
the same way; a bare Protocol annotation is never correct on this model.**

- **A manifest is never deserialised from JSON.** It is constructed in Python by the module author
  at import time and validated once. No `model_json_schema()` is called on it, and the build-tail
  must not add one — the callable and Protocol fields have no JSON schema.
- `frozen=True` preserves the stub's `frozen=True` guarantee (`manifest.py:71`).
- `extra="forbid"` makes a typo'd field name a load-time failure rather than a silently ignored
  attribute. The operation registry already refuses `extra="allow"` input models, so this is the
  house position.
- `operations` keeps today's shape, `tuple[tuple[OperationDeclaration, Handler], ...]`
  (`manifest.py:79`), for the reason `rheo_contracts/manifest.py:89-97` already gives: the handler
  is typed against `UnitOfWork`, which `rheo_contracts` may not import, so it travels beside the
  declaration exactly as `OperationRegistry.register(decl, handler, *, origin)` accepts it.
- **`configuration_schema` is NOT a pydantic model class.** See Decision C.

Rules the stub's `validate()` enforces move into `@model_validator(mode="after")` and keep their
current text: `module_id` is a lowercase identifier, `storage.schema_name == module_id`
(`manifest.py:105-110`), and the module id is not a reserved segment
(`rheo_contracts.refs.is_reserved_module`). `ManifestInvalid` (`manifest.py:43-53`) survives as the
loader's wrapper for a *load-time* failure that is not a field-shape failure (an entry point whose
object is not a `ModuleManifest`, an id disagreeing with the entry-point name at
`loader.py:110-116`); pydantic's own `ValidationError` is what AC 2 asserts on, and it is not
wrapped or re-raised as something else, because wrapping is what would lose the field name.

### Decision C — the shipped field set (confirming FR 1 and FR 3, with five named deviations)

**Confirmed: all 23 ratified field names ship, required, with only `web` and `agent_guidance`
optional — plus one 24th field the ratified table does not list, `audit_sink`, which deviation 4
below argues for.** Requirement 3's "present but empty" mitigation stands: an empty tuple is a
declared field, and pydantic still refuses its omission.

The reason to confirm rather than cut to criterion 25's six: **1a0's whole product is a contract
that 1a1, 1a2, 1a3 and 1b build on without editing `packages/core`.** A field added per sibling run
is a `packages/core` edit per sibling run, four re-reviews of the same model, and four chances for
two runs to choose different names for the same thing. The cost of confirming is bounded and
measurable — of the 23 fields, **ten** need a declaration type that does not already exist in the
tree, **seven** of those ten are forced by criterion 25 or FR 6–8 regardless, and every one of the
ten is transcription from a ratified block, not invention.

| Field | Type | Required? | What forces it now |
| --- | --- | --- | --- |
| `module_id` | `str` | yes | shipped (`manifest.py:75`) |
| `package_version` | `str` | yes | install step 5 writes it to `core.module_state.package_version` |
| `core_contract_versions` | `tuple[int, ...]` | yes | discovery step 2 (`module-contract.md:184-186`); the loader refuses a module not listing `CONTRACT_VERSION` |
| `dependencies` | `tuple[Dependency, ...]` **(new type)** | yes | install step 2, enable step 2 (FR 7, FR 8); the loader's dependency sort and range check (`module-contract.md:184-187`) |
| `record_types` | `tuple[RecordType, ...]` **(new type)** | yes | criterion 25 names it |
| `storage` | `StorageDeclaration` **(new type)** | yes | criterion 25; install steps 3–4; FR 9 |
| `configuration_schema` | `tuple[KeySpec, ...]` — **deviation, see below** | yes | criterion 25; enable step 3 |
| `operations` | `tuple[tuple[OperationDeclaration, Handler], ...]` | yes | shipped (`manifest.py:79`) |
| `tools` | `tuple[ToolDeclaration, ...]` | yes | criterion 25; FR 6; `ToolDeclaration` shipped (`rheo_contracts/manifest.py:138`) |
| `events` | `tuple[EventDeclaration, ...]` **(new type)** | yes | FR 6, AC 14 |
| `subscriptions` | `tuple[ConsumerSubscription, ...]` | yes | FR 6, AC 14; type shipped (`events/consumers.py:49-59`) |
| `jobs` | `tuple[JobKind, ...]` **(new type)** | yes | FR 6, AC 14; `JobKindRegistry.register(kind, input_model, handler)` (`work/kinds.py:69-71` — **three parameters**, so `JobKind.max_attempts` and `.cancellable` are carried and not passed; see below) |
| `schedules` | `tuple[Schedule, ...]` **(new type)** | yes | enable step 3, AC 13 |
| `resolvers` | `tuple[tuple[str, RecordResolver], ...]` | yes | shipped (`manifest.py:80`) |
| `deletion_participants` | `tuple[DeletionParticipant, ...]` **(new type)** | yes | ratified field; **no reader in 1a0** — see the handler-alias note |
| `export` | `ExportDeclaration` **(new type)** | yes | criterion 25 names it |
| `web` | `WebSurface \| None` | **optional** | shipped and consumed (`internal_routes.py:104`); ratified optional |
| `agent_guidance` | `str \| None` | **optional** | ratified optional; no reader in release one |
| `secret_scopes` | `tuple[str, ...]` | yes | ratified; empty for domain modules; validated empty-or-prefixed |
| `connector_bindings` | `tuple[ConnectorBinding, ...]` **(new type)** | yes | ratified; no reader in 1a0 |
| `health_checks` | `tuple[HealthCheck, ...]` | yes | install step 6 (non-test profiles) |
| `contract_tests` | `str` | yes | install step 6 (test profile) |
| `sensitivity` | `Mapping[str, Mapping[str, SensitivityTier]]` **(new enum)** | yes | ratified; no reader in 1a0 |
| `audit_sink` | `Annotated[AuditSink, PlainValidator(_audit_sink)] \| None` (Decision B) | **optional** | **not in the ratified table — a 24th field, carried over from the stub (`manifest.py:82`). Deviation 4 below.** |

**Two stub fields are dropped, and neither has a reader.** `schema_name` (`manifest.py:77`) is
subsumed by `storage.schema_name`, whose after-validator carries the stub's own
`schema_name == module_id` rule. `create_schema` (`manifest.py:78`) is validated as callable
(`manifest.py:111-112`) and **called by nothing** — `grep -rn create_schema packages apps tests`
returns the stub's own two lines plus five fixture lines in `tests/test_module_loader.py`, no
production caller — and the migration chain (FR 9) is what creates a module's schema from now on.

Ten new types: nine frozen models and one enum. **Seven are forced** — `Dependency`, `RecordType`,
`StorageDeclaration`, `ExportDeclaration` (criterion 25 and install steps 2–4), `EventDeclaration`,
`JobKind` (FR 6 / AC 14) and `Schedule` (enable step 3 / AC 13). **Three carry no behaviour and no
reader in 1a0** and exist only to make the ratified field nameable and present per FR 3:
`DeletionParticipant`, `ConnectorBinding`, and the `SensitivityTier` enum (three ratified members:
`public`, `internal`, `restricted`). Each is transcribed field-for-field from `module-contract.md`
(`:56-66` for `RecordType`, `:30-49` for the rest). They live in
`packages/core/src/rheo_core/modules/manifest.py` beside `ModuleManifest`, not in
`rheo_contracts`, for the reason `rheo_contracts/manifest.py:5-10` states and this run does not
relitigate.

**Five named deviations from the ratified text, each recorded in `module-contract.md` by this run
(the fifth is stated below, after the `JobKind` note):**

1. **`configuration_schema` is `tuple[KeySpec, ...]`, not a pydantic model class.** The shipped
   settings system declares keys as `KeySpec` (`settings/schema.py:176-200`) and registers them
   through `SettingsRegistry.register(spec, *, origin)` (`settings/schema.py:313`). A model class
   would need a class-to-`KeySpec` compiler whose only caller is this field, and enable step 3's
   `explicit_per_workspace` read (`settings/schema.py:361-363`) already works directly on
   `KeySpec`. Keys are validated to start with `<module_id>.` at manifest validation, which is the
   ratified rule (`module-contract.md:33`). The loader registers them under `origin = module_id`,
   which leaves the `REGISTRY.keys(origin=CORE_ORIGIN)` identity check (`tests/test_settings.py:176`)
   untouched, and `SettingsRegistry.register`'s own profile gate for the `test_harness` origin
   (`settings/schema.py:317-330`) is unaffected because a module origin is neither.
2. **Handlers with no coordinator yet get an explicit widening alias rather than a guessed
   signature.** `DeletionHandler`, `Exporter` and `Importer` are declared as
   `Callable[..., object]` with a docstring naming the run that narrows each (`rheo_core/deletion/`
   holds only `__init__.py` today; the export declaration's exporter/importer belong to the
   deletion-export-migration work). `HealthCheck` is narrow now because install step 6 calls it:
   `Callable[[UnitOfWork], None]`, raising on failure. A guessed three-argument signature for the
   other three would be a shape a later run must break; `Callable[..., object]` is a shape it can
   narrow additively.
3. **`contract_tests` is a checkout-relative pytest target, resolved only under profile `test`.**
   See Decision E.
4. **A 24th field, `audit_sink` (optional, default `None` — the stub's field at `manifest.py:82`,
   with the pydantic shape Decision B gives it), is carried over from the stub and is not in the
   ratified table.** The ratified manifest table (`module-contract.md:25-49`) names 23 fields
   and no audit sink; the shipped stub carries one at `manifest.py:82` and the loader installs it at
   `loader.py:135-139`. **A 23-field model with `extra="forbid"` would delete the only channel by
   which a module supplies a sink**, and `dispatch.py:184`'s `AUDIT_SINK_MISSING` refuses every
   operation above `READ` whose owning module has none — so 1a1's first memory operation would be
   refused at dispatch, and `check_audit_paths` (`operations/audit_paths.py:32`) would refuse
   `apps/core`'s startup outright. 1a0's whole product is a contract 1a1–1b build on *without editing
   `packages/core`*; deleting the field would force exactly that edit. The field is therefore kept,
   with the stub's `None` default — but **not** with the stub's bare `AuditSink | None` annotation,
   which is safe on a dataclass and fatal on a pydantic model; Decision B gives the shape.

   Three alternatives considered and rejected. **Required rather than optional:** a module whose
   operations are all `READ` legitimately has no sink (`audit_paths.py:50-51` skips it), and
   `check_audit_paths` already fails loudly and names every offender at startup, so a
   required-but-`None` field would catch nothing that layer does not. **The loader installing
   `CORE_AUDIT_SINK` for every loaded module:** that silently changes a shipped guard — no module
   could ever reach `check_audit_paths`'s refusal again — and it contradicts `audit/sink.py:23`'s
   "a module supplies a **writer**; it never supplies the condition". **A registration call outside
   the manifest:** a second registration channel for a thing the manifest already carries, with the
   manifest↔registration disagreement that invites (see the loader's step 4 note below).

   What a module supplies is unchanged: `CoreAuditSink` holds no state and reads no module identity
   (`audit/core_sink.py:10-16`), so a module that wants the ordinary `core.audit_record` row writes
   `audit_sink=CORE_AUDIT_SINK` — an import of `rheo_core.audit`, which a module may make and the
   core may not reciprocate. **Recallatron's skeleton sets `audit_sink=None`** explicitly: it
   registers no operation, so it needs no sink, and 1a1 is where the field first carries a value.

**Two ratified `JobKind` members are shipped reader-less, named rather than quietly dropped.**
`module-contract.md:38` gives `JobKind(name, handler, max_attempts, cancellable)`, and
`JobKindRegistry.register` takes **three** parameters (`work/kinds.py:69-71`), so neither
`max_attempts` nor `cancellable` reaches the registry. Both are kept on the declaration, each with a
docstring naming the reader it waits for. `max_attempts` is read by whatever *enqueues* a job of that
kind — every shipped enqueue passes its own budget (`exports/operations.py:168`, `:220`,
`runtime/operations.py:316`, all `resolve().get_int("work.max_attempts")`), and release one has no
module that enqueues its own kind. `cancellable` is read by **Disable** (`module-contract.md:252`
— "each job kind's `cancellable` flag decides which"), which the MVP scope excludes as phase-seven
work. **Widening `JobKindRegistry.register` to take them is rejected**: a shipped signature change
whose two new parameters no caller would read is the mutually-justifying machinery the baseline
section exists to catch.

**Deviation 5 — `Dependency.version_range` is a PEP 440 specifier, not the ratified caret/tilde
form.** `module-contract.md:266-268` says "Dependency
ranges use the usual caret and tilde forms", which is npm's grammar. A module is a Python
distribution whose `package_version` is PEP 440 already, and the value install step 2 compares
against is `core.module_state.package_version`. Writing a caret/tilde parser would be a second
version grammar with one caller, and version comparison is precisely the thing a hand-rolled parser
gets wrong. So `version_range` is a `packaging.specifiers.SpecifierSet` string (`">=1.2,<2"`),
parsed at manifest validation so an unparseable range fails at load rather than at install, and
satisfied through `packaging.version.Version` at install step 2. **This adds `packaging` to
`rheo-core`'s runtime dependencies** — it is already resolved in `uv.lock:521` as a transitive
dependency, it is pure Python and it is the reference implementation of the versioning PEPs. See
the sequencing note in `plan.md` about which phases may change `uv.lock`.

### Decision D — a repeat `core.module.install` is refused, naming the state (closes Open Question 1)

**Refusal, not a no-op and not an upgrade.** `core.module.install` reads `core.module_state` for
its module id first; if a row exists in any state, it refuses `module_already_installed` with the
detail naming the module and its current state.

Why refusal beats the two alternatives:

- **Against no-op:** install takes a `package_version` from the manifest the deployment loaded. A
  silent no-op over an installed row would swallow the case that matters — the loaded package is a
  *different version* from the recorded one — and report success. That is the unattended-failure
  shape FR 11 is written against.
- **Against upgrade:** `module-contract.md:251` gives Upgrade its own contract (contract-version
  recheck, chain-from-recorded-version, destructive-migration export gate, per-workspace
  `unavailable`). Implementing any of that here preempts phase-seven work the MVP scope explicitly
  excludes.
- **For refusal:** it is one assertion to test, it cannot be mistaken for success, and it mirrors
  `core.module.enable`'s own shape ("refuse unless state is `installed` or `disabled`", FR 8 step 1).

**Crash-retry stays possible, and that is the point of ordering the state write last.** The
`module_state` row is written at step 5, after the extensions, the schema and the chain. A crash
before step 5 leaves no row, so a repeat install proceeds, and every step before it is idempotent
(`CREATE EXTENSION IF NOT EXISTS`, `CREATE SCHEMA IF NOT EXISTS` under the advisory lock at
`orchestrator.py:161-162`, `alembic upgrade head`). Because the whole install body runs in one
transaction (below), the realistic failure leaves nothing at all.

`idempotency = Idempotency.NONE` on the declaration, not `NATURAL`: `NATURAL` names an index that
makes a repeat *the same row* (`rheo_contracts/manifest.py:50-54`), and a repeat here is refused
rather than folded.

### Decision E — criterion 33's mechanism replaces the stale ratified sentence (confirmed)

`module-contract.md:285` says the no-memory run's test profile "sets it to `relationships,
leads`". **That sentence is stale and this run corrects it:** neither module exists as a
distribution (`modules/relationships/` and `modules/leads/` hold a one-line `__init__.py` each and
no entry point), and `modules.installed`'s default is the empty tuple, so naming two non-existent
ids and naming none are the same configuration.

**The Analyst's two-configuration diff is confirmed, with one correction to what is compared.**

- **Configuration A — absent from configuration.** `modules/recallatron` is on disk, installed into
  the environment, its `rheo.modules` entry point discoverable; `modules.installed` resolves to
  `()`. Proves *discovery does not activate*.
- **Configuration B — absent from the checkout.** `modules/recallatron` is removed and the
  environment re-synced. Proves no phase-one path reaches the distribution by a route a static
  import scan cannot see (an `importlib.metadata.version("rheo-recallatron")`, a path read under
  `modules/`).

**The compared selection is the phase-one acceptance matrix's own `pytest:` demonstrators, not the
whole `tests/` tree, and that is forced rather than convenient.** This run adds phase-two tests
under `tests/` that legitimately require Recallatron on disk (AC 1, AC 10–13); under configuration B
they would fail, and a whole-tree diff would report a difference that proves nothing.

**What `selected` is, exactly.** `tests/test_acceptance_matrix.py:53`'s `parse` reads
`docs/acceptance/phase-1-matrix.md` and yields each criterion's `Demonstrator` bullets *with their
kind prefix*, in two kinds (`:188-201`): `pytest:<node id>` and `ci:<job> / <step>`. On this tree
that is 103 `pytest:` entries — **102 unique node ids**, one listed under two criteria — and 8 `ci:`
entries, 7 unique. (Counted by running `parse`; a raw grep of the file for `- \`<kind>:` counts
more, because the `Cost` blocks' "first observed failure line" bullets carry the same prefixes and
are not demonstrators.) The proof's selection is

```python
selected = {
    demo.removeprefix("pytest:")
    for row in parse(matrix) for demo in row.demonstrators
    if demo.startswith("pytest:")
}
```

— prefix stripped, deduplicated. **The `ci:` demonstrators are not run and are not in `selected`.**
They are GitHub Actions `job / step` pairs that `test_acceptance_matrix.py:215-220` resolves against
`.github/workflows/repository-checks.yml`, not pytest items: pytest cannot collect
`ci:docker / Build the core/worker image`, and it cannot collect a `pytest:`-prefixed id either,
which is why the raw `parse` output can never equal anything pytest collects. The `ci:` steps run
in the workflow's ordinary `docker`, `python` and `web` jobs on every push and have no second
configuration to compare against. Each summary records them under `ci_excluded` so a reader sees
the 7 and why. A demonstrator of any third kind exits the mode non-zero, mirroring
`_demonstrator_errors`' own "kind is not resolved" (`:202-204`). 82 of the 102 selected node ids
are under `tests/postgres/`, and `pyproject.toml:73-76` sets no default marker filter, so **both
configurations need the test cluster** — `RHEO_TEST_CLUSTER_DSN` reachable — or
`tests/conftest.py:157-161` `pytest.exit`s the session; it never skips.

**`collected`, and what is compared.** Each configuration runs pytest in-process —
`pytest.main([...], plugins=[recorder])` with a recorder implementing `pytest_collection_finish` and
`pytest_runtest_logreport` — over the files `selected` names. `collected` is the set of collected
node ids that resolve to a selected id by `_pytest_resolves`' rule (`:208-212` — exact, or `<id>[`
for a parametrised expansion: the matrix names base ids, 8 of the 102 are parametrised —
`tests/test_routing.py::test_url_for_matches_the_fixture` among them — so 143 collected items
resolve to the 102, 94 exact and 49 expansions, and set equality would be the wrong test). `outcomes` maps each
collected node id to `passed`, `failed`, `error` or `skipped` — a per-node-id map, never a count, so
two different failures summing to the same total do not diff clean. A summary is
`{selected, collected, outcomes, ci_excluded}`.

**The proof carries controls, because a diff of two nothings is clean.** `--mode compare` on its
own has the defect this repo already has a convention against: an empty or unresolvable selection
produces two identical summaries and reports the module boundary proved.
`tests/harness/absence.py:10-15` states the rule — "a probe without a control cannot be expressed" —
and every helper there refuses to run without a `present=` control that finds something by the same
mechanism. The absence proof gets the same treatment:

1. **Non-empty selection.** Each mode exits non-zero when `selected` is empty. A matrix that names
   no `pytest:` demonstrators is a failed proof, not a passed one.
2. **Resolution.** Each mode exits non-zero unless every selected id resolves into `collected`
   *and* every collected id maps back to a selected id, naming the difference. A renamed or deleted
   demonstrator (Decision H's `THE_SEVENTEEN` test is the live example) reds the proof rather than
   silently shrinking its selection.
3. **Completeness.** Each mode exits non-zero unless `set(outcomes) == collected` — every collected
   item ran to an outcome — and no outcome is `skipped`. The first clause turns `conftest.py`'s
   `pytest.exit` on an unreachable cluster into a red proof instead of a short summary; the second
   says a skipped demonstrator is a demonstrator not demonstrated.
4. **Comparison.** `--mode compare` exits non-zero unless both summaries carry the same non-empty
   `selected`, that set equals what `parse` yields at compare time, both `collected` sets are equal,
   and the two `outcomes` maps are identical key by key. A `failed` outcome identical on both sides
   does *not* by itself red the compare — the property under proof is the *difference* between
   configurations, and "the phase-one matrix is green" is `make test`'s and the `python` job's
   property, already enforced on every push — but compare prints every non-`passed` outcome so a
   reader sees them.
5. **The mutation that proves the controls are load-bearing**, run in the ordinary suite. A new
   `tests/test_absence_proof.py` hands the per-mode checks and `compare` hand-built summaries and
   asserts each exits non-zero: one pair where a single node id's outcome differs, one where
   `selected` is empty, one where a selected id is not in `collected`, one where an outcome is
   missing, and one where an outcome is `skipped`. That is `tests/test_absent_behaviour.py:24-28`'s
   `test_the_positive_controls_are_live` shape — the control is proved load-bearing by making it
   fail on purpose — applied to the mechanism rather than to the fixture.

**Why these controls can still fail, one line each:** no `pytest:` demonstrators → control 1; a
demonstrator renamed, deleted or not collected → 2; the cluster down, or a demonstrator that skips →
3; any one node id behaving differently with Recallatron absent than with it merely unloaded → 4,
which is criterion 33's actual claim. And the proof can pass: verified today, `pytest --collect-only`
over the 29 files the selection names resolves every one of the 102 ids — 143 collected items, none
unresolved.

A third, cheap check runs in the ordinary suite beside those two configurations: a static scan
asserting no file under `packages/`, `apps/` or `tests/` names `rheo_recallatron` or
`modules/recallatron`, except the phase-two test files that exist to drive it. That is
`module-contract.md:282-283`'s "import-graph check in CI", which does not exist today.

### Decision F — a module chain never enters `migrate_workspace`, and that is FR 11's mechanism

**`run_module_chain` is the only entry point for a module chain. `migrate_workspace` keeps refusing
one, with a `ValueError` that names the right door instead of a `NotImplementedError` that names a
phase.**

The forcing fact is that `migrate_workspace`'s failure path is **already generic over `chains`**.
Its `try` at `orchestrator.py:227` wraps `for chain in chains: run_chain(...)` at `:229-230`; the
bare `except Exception` at `:235` falls through to `set_workspace_state(..., UNAVAILABLE, ...)` at
`:239-245`. Nothing distinguishes a core chain from a module chain there, and nothing needs
generalising to make a module chain reach that write — **deleting the `NotImplementedError` at
`:217` is by itself what opens the path FR 11 forbids.** A module whose migration raises would flip
the whole workspace's control-plane row to `unavailable`, taking core and every other module with
it. (`:238` and `:254` also hardcode `MigrationResult(chain=CORE_CHAIN)`, so a module chain run
through that function would misreport which chain ran.)

So the guard stays, and changes only its message and its type:

```python
raise ValueError(
    f"module migration chain {chain!r} does not run through migrate_workspace; "
    "call run_module_chain from core.module.install, which surfaces a failure to "
    "its caller instead of marking the workspace unavailable"
)
```

`ValueError` rather than `NotImplementedError` because this is no longer "not built yet" — it is a
routing rule, the same class `:216` already raises for the control chain on a workspace database.

**Nothing in release one wants the other behaviour.** The only case that would need a module chain
at an unattended startup pass is **Upgrade** — running a newer package's chain over an already
installed workspace — and `module-contract.md:251` gives Upgrade its own contract, which the MVP
scope excludes as phase-seven work. Install is the only module-chain caller release one has, and it
has a caller waiting on a result.

**FR 11 then holds structurally, and is provable two ways:** `migrate_workspace(backend, ws,
chains=("recallatron",))` raises `ValueError` naming `run_module_chain` (a positive control that the
door is shut), and a forced module-chain failure inside `core.module.install` leaves the
`control.workspace` row byte-identical (AC 12's negative assertion, read before and after).

**`run_module_chain` is also where AC 10's `core.module_schema_version` row is written.**
`module_schema_version` appears in `orchestrator.py` today only in the docstring at `:24` and is
written nowhere in that file; `run_module_chain` calls `insert_module_schema_version` once per newly
applied revision (System Components 4 gives the enumeration).

### Decision G — where each named refusal reaches the caller, and the install job's `max_attempts`

FR 7 puts `extension_failed`, `contract_tests_missing` and `health_check_failed` inside steps 3 and
6, and FR 11 adds a fourth in-job failure — the module migration chain at step 4 — all of which
this design runs in the job except the one it moves out. **The job has exactly one error code.**
`work/loop.py:146` defines `JOB_FAILED = "job_failed"` and says so explicitly — "one code for all
four routes to `failed`" — and `loop.py:549`'s bare `except Exception` hands the exception to
`_write_failure_outcome` as `error=str(failure)` (`:583-598`), which requeues until the row's budget
is spent and only then writes `error_code=JOB_FAILED, error_text=error` (`loop.py:647-665`). A
failure raised in the job therefore cannot arrive as its own code, and by default arrives eight
attempts late.

Three changes make each named failure reach a caller distinguishably, and none touches the shipped
loop:

1. **`contract_tests_missing` moves to the synchronous pre-flight.** It is a path-existence check
   over the manifest and `find_checkout_root()` (`storage/data_root.py:138-147`) — it needs no
   workspace database and no migration, so there is no reason for it to be inside the job at all.
   Raised as `OperationRefused("contract_tests_missing", …)` from the dispatch handler, it is the
   outcome's own `error_code`, exactly as `exports/operations.py:193`'s `artifact_missing` already
   is for a long-running operation's pre-flight. This makes the profile-`test` half of FR 7 step 6
   run **before** step 3's extensions rather than after step 5's state row; the ratified ordering
   describes when each check is satisfied, not which transaction it occupies, and an install that
   cannot pass step 6 should not have created a schema first.
2. **`extension_failed`, `migration_failed` and `health_check_failed` stay in the job — they
   genuinely need the workspace database — and reach the caller through the operation record's
   `error_text`.** The job handler raises `ModuleInstallFailed(code, detail)` whose `str()` is
   `f"{code}: {detail}"`, so `core.operation.get` returns `error_code="job_failed"` with
   `error_text="extension_failed: …"`, `"migration_failed: …"` or `"health_check_failed: …"`.
   **`migration_failed` is step 4's code, and it is produced by a wrap, not by the chain.**
   `run_module_chain` raises the chain's own exception unchanged — a `StorageRefusal(schema_ahead,
   …)`, whatever a revision raised, a `DBAPIError` — and the job handler's one `try` surrounds that
   call and re-raises `ModuleInstallFailed("migration_failed", error_text(exc)) from exc`, where
   `error_text` is `orchestrator._error_text` (`:184-189`) made public: the same `TypeName: message`
   rendering `migrate_workspace` writes to `state_detail`, unwrapping a `DBAPIError.orig`. So a
   forced `schema_ahead` reaches the caller as
   `error_text="migration_failed: StorageRefusal: schema_ahead: database 'ws_…' records recallatron
   revision(s) […] that this code does not know"` (`StorageRefusal.__str__` is `f"{state}:
   {detail}"`, `storage/backend.py:64`). The re-raise propagates, so the transaction still rolls
   back and `control.workspace` is never touched (FR 11); what the wrap adds is a name. It is never
   spelled `extension_failed`: by step 4 the extensions step has already succeeded, and a migration
   failure labelled as an extension failure would pass a test while describing the wrong event.
   `error_code` stays the loop's one code, because the loop's own reasoning is right: the code names
   *what* failed — the job carrying the operation — and the discrimination lives in the text, which
   is the job's `last_error` verbatim. Widening `JOB_FAILED` to a per-kind code is rejected: a
   change to a shipped write path shared by every job kind, for one operation's benefit.
   AC 11 and AC 12 assert on `error_text`'s prefix, not on `error_code`.
3. **`max_attempts = 1` for the `core.module.install` job kind**, named here because
   `work/jobs.py:150` makes it a required keyword of `enqueue_job` and no requirement named one.
   Not `resolve().get_int("work.max_attempts")` (8, the value the three shipped long-running
   operations pass at `exports/operations.py:168`, `:220` and `runtime/operations.py:316`): every
   failure reachable inside this job is deterministic — a role that may not create an extension, a
   revision that raises, a health check that returns false, a chain whose recorded revision is ahead
   — and the loop grants "exactly `max_attempts` runs" (`loop.py:387-391`), so 8 means the operator
   waits through seven pointless retries under a backoff of up to an hour for an answer that cannot
   change. One run, then `failed`, with the named detail in `error_text`. This is the same judgement
   `work/kinds.py:9-16` already applies to an unknown kind and an unparseable payload.

### Decision H — the phase-one matrix's demonstrator names are frozen, so `THE_SEVENTEEN` is not renamed

Registering `core.module.install` and `core.module.enable` makes
`tests/postgres/test_audit_dispatch.py:119`'s `THE_SEVENTEEN` hold nineteen names. The obvious
tidy-up — rename the constant and its test — **breaks two things, and the second is silent.**

`docs/acceptance/phase-1-matrix.md:909` lists
`pytest:tests/postgres/test_audit_dispatch.py::test_the_mutating_set_derived_from_the_registry_is_the_declared_seventeen`
as one of criterion 14's seven demonstrators, and `tests/test_acceptance_matrix.py:245-257` resolves
every demonstrator against `pytest --collect-only`. So a rename (a) reds `main` on a matrix check no
phase in this run owns, and (b) drops a demonstrator from the very selection Decision E's absence
proof runs over — quietly, because a smaller selection still diffs clean. Decision E's control 2
now catches (b), but the right fix is not to rename.

**Decision: the constant and the test keep their names.** The frozenset gains the two operations;
`THE_SEVENTEEN`'s docstring records that the name is fixed by an acceptance record and names both
reasons. This repo already has the precedent in writing: `tests/test_absent_behaviour.py:30-34` says
its own probe names "may not be renamed for house-style reasons either" because an acceptance
criterion pins them. A later run that wants the rename owns the matrix demonstrator id and criterion
14's recorded mutation cost in the same diff; that is not this run's scope.

### Tech Stack — what we are working within

| Layer | Choice | What this run adds to it |
| --- | --- | --- |
| Language / typing | Python 3.12, mypy `strict` over `packages`, `apps/*`, `modules` (`pyproject.toml:41`) | nothing; the new module code is inside the existing `files` list |
| Contract models | pydantic v2 | `ModuleManifest` and ten declaration types become frozen pydantic models; every field type is one pydantic handles natively, and the one `Protocol`-typed field carries a `PlainValidator` (Decision B) |
| Module discovery | `importlib.metadata.entry_points(group="rheo.modules")` (`loader.py:64-66`) | unchanged mechanism; the allowlist's *source* moves from an env var to a settings key |
| Configuration | `KeySpec` registry + `defaults.toml` + `RHEO__*` deployment layer (`settings/`) | one production key, `modules.installed`; module keys register under `origin = module_id` |
| Storage | SQLAlchemy 2 Core + psycopg3, one Postgres database per workspace | two new writers in `repositories.py`; no new table |
| Migrations | Alembic, one environment per chain, orchestrator-only (`migrations/orchestrator.py`) | module chains resolve their script directory from the manifest; one per-module version table |
| Operations | `OperationRegistry` + `dispatch` (`operations/`) | two new core operations, one new core job kind |
| Packaging | uv workspace, `modules/*` are members (`pyproject.toml:9`), hatchling wheels | Recallatron gains an entry point, real dependencies, and a packaged migrations subpackage |

### Data Models

**No new table.** `core.module_state` and `core.module_schema_version` already exist with the
ratified columns (`storage/core_tables.py:69-99`) and already have readers
(`repositories.py:97-125`, `operations/core_ops.py:256-258`). This run supplies the writers the
`repositories.py:1-10` docstring says arrive with module install.

`ModuleManifest` — the 23 ratified fields plus `audit_sink` (Decision C's table and its deviation 4)
= **24 `model_fields`, three of them optional**: `web`, `agent_guidance`, `audit_sink`. Constructed
once per distribution at import, validated at load, held by module id in the loader's table.

New declaration entities (all frozen pydantic models in `rheo_core/modules/manifest.py`):

```text
Dependency(module_id: str, version_range: str, optional: bool = False)
    # version_range is a PEP 440 specifier (">=1.2,<2"), parsed by
    # packaging.specifiers.SpecifierSet at manifest validation — Decision C, deviation 5
RecordType(name, table, deletable, delete_roles: frozenset[Role], exportable,
           audience_field: str | None)            # module-contract.md:56-66, verbatim
StorageDeclaration(schema_name: str, migrations_path: str, required_extensions: tuple[str, ...])
    # migrations_path is a dotted package path ("rheo_recallatron.migrations"), resolved by
    # importlib.resources exactly as orchestrator.script_location does (orchestrator.py:88-95)
EventDeclaration(type: str, schema_version: int, data: type[BaseModel])
JobKind(name: str, input_model: type[BaseModel], handler: JobHandler,
        max_attempts: int, cancellable: bool)
    # max_attempts and cancellable are carried, not registered: JobKindRegistry.register
    # takes three parameters (work/kinds.py:69-71). Reader-less in release one — see the
    # JobKind note in Decision C for which run reads each.
Schedule(name: str, job_kind: str, cron: str, enabled_by_default: bool)
DeletionParticipant(record_types: tuple[str, ...], handler: DeletionHandler)
ExportDeclaration(format_version: int, schema_path: str, exporter: Exporter, importer: Importer)
ConnectorBinding(transport: str, service_operation: str, route: str | None)
SensitivityTier = StrEnum("public" | "internal" | "restricted")
```

Two new rows written by this run's code, both through `repositories.py`:

- `core.module_state` — one upsert-shaped writer, `insert_module_state(conn, *, module_id,
  package_version, state, installed_at, enabled_at=None, state_detail=None)` plus
  `set_module_state(conn, *, module_id, state, enabled_at)` for enable's transition. Both take the
  caller's `Connection` and commit nothing, matching `repositories.py:8-10`.
- `core.module_schema_version` — `insert_module_schema_version(conn, *, module_id, schema_version,
  applied_at, core_version_at_apply)`, one row per applied step, appended under the same
  transaction as the chain.

One new settings key, in `PRODUCTION_KEYS` (`settings/schema.py:369`) **and**
`packages/core/src/rheo_core/config/defaults.toml` (the two are asserted identical at import):

```text
modules.installed  type=ValueType.STR_LIST  scope=Scope.DEPLOYMENT  floor=None
                   explicit_per_workspace=False  default=()
```

Its environment form is `RHEO__modules__installed` (or the uppercased variant),
comma-separated, per `settings/deployment.py:39-42` and the `STR_LIST` decode at
`settings/schema.py:275-276`. The key's absence today is deliberate and documented at
`settings/schema.py:26-28` ("a key with no reader is machinery with no caller"); this run supplies
the readers, which is what makes declaring it correct now. `Scope.DEPLOYMENT` is what makes edge case 8 (a workspace-scope
override attempt) a refusal from the existing generic write-path check, with no new mechanism.

### System Components

**1. `rheo_core.modules.manifest` — the contract.** Owns `ModuleManifest`, the ten declaration
types, the callable aliases, and the after-validator. Interfaces with: the loader (validation), the
install/enable operations (reads `storage`, `dependencies`, `configuration_schema`, `schedules`,
`contract_tests`, `health_checks`), the migration orchestrator (reads `storage.migrations_path`).
**Final home: `rheo_core.modules.ModuleManifest`** (re-exported from
`packages/core/src/rheo_core/modules/__init__.py`, as the stub already is). Requirement 2's
doc correction names that path at `module-contract.md:22`.

**2. `rheo_core.modules.loader` — discovery, selection, registration.** Three sets stay three
named things (FR 5 / F17), and no function answers more than one:

| Set | Name in code | Source | Reader |
| --- | --- | --- | --- |
| (a) host-installed / on disk | `discovered()` (`loader.py:64`, unchanged) | `entry_points(group="rheo.modules")` | the loader; `rheo doctor` later |
| (b) deployment-loaded | `allowed_module_ids()` (`loader.py:69`, **source changes**) and `loaded_manifests()` (**new**) | the resolved `modules.installed` setting | `load_modules()`, `core.module.install` step 1 |
| (c) workspace-installed/enabled | `list_module_states()` (`repositories.py:97`, unchanged) | `core.module_state` rows | `core.workspace.status`, `WorkspaceContext.enabled_modules` (`boundary/factories.py:86-88`) |

`allowed_module_ids()` keeps its name — it already means "what this deployment permits to load",
which is set (b) and not set (c) — and its body becomes `frozenset(resolve()["modules.installed"])`.
`_SURFACES` (`loader.py:52-61`) is replaced by `_LOADED: dict[str, ModuleManifest]`, with
`module_surfaces()` deriving from it, because install needs the manifest back by module id and two
tables of what was loaded would be two things to keep in step.

`_register()` (`loader.py:122-141`) grows from two registry categories to six. **The four
process-global registries it can reach today are module-level singletons; the two the worker owns
are not**, and that is load-bearing:

| Category | Registry | Instance |
| --- | --- | --- |
| operations | `OperationRegistry` | `REGISTRY` (`operations/registry.py:306`) — global |
| resolvers | `ResolverRegistry` | `RESOLVERS` (`refs/resolver.py:122`) — global |
| tools | `ToolRegistry` | `TOOL_REGISTRY` (`tokens/sets.py:294`) — global |
| `configuration_schema` keys | `SettingsRegistry` | `settings.schema.REGISTRY` (`settings/schema.py:860`; `register(spec, *, origin)` at `:313`) — global, under `origin = module_id` |
| job kinds | `JobKindRegistry` | `JOB_KINDS` (`apps/worker/src/rheo_app_worker/main.py:46`) — **composition-root local** |
| subscriptions | `ConsumerRegistry` | `CONSUMERS` (`apps/worker/src/rheo_app_worker/main.py:67`) — **composition-root local** |
| events | none exists | held on the loaded manifest — see below |

Two attachments that are not registries stay exactly as they are today: the **web surface**
(`loader.py:140-141`, now writing `_LOADED` rather than `_SURFACES`) and the **audit sink**
(`loader.py:135-139`, `install_sink(manifest.module_id, manifest.audit_sink)` under the `is not
None` guard). The sink branch is unchanged *because* the manifest keeps its `audit_sink` field —
Decision C's deviation 4 is what makes "audit sink registration is unchanged from today" a true
sentence rather than an assumption.

So `load_modules()` gains two optional parameters, `kinds: JobKindRegistry | None = None` and
`consumers: ConsumerRegistry | None = None`, defaulting to "do not register that category", and the
worker's `main()` passes its own two instances. The core process legitimately registers neither: it
enqueues job rows by kind string and never runs a handler.

**There is no event registry, and this run does not build one.** `rheo_core.events` holds
`consumers`, `deliveries` and `publish` and nothing that indexes event *declarations*
(`grep -rn "EventDeclaration\|EventRegistry" packages apps tests` → no hits). AC 14's "events
appear in their respective registry" is satisfied by `loaded_manifests()[module_id].events`, which
is a live, queryable set, plus real validation at load: each declaration's `type` must match
`<module_id>.<record_type>.<verb>` with the module's own id as the first segment, and two loaded
modules may not declare the same event type. Building an `EventRegistry` whose only reader is the
test asserting it is non-empty is the mutually-justifying machinery the baseline section exists to
catch. **CARRIED:** the run that first ships a module publishing an event (1a1/1a2) should decide
whether `publish()` refuses an undeclared type; that is a change to 0c0's shipped write path and no
criterion here asks for it.

**The loader implements the rest of the ratified discovery algorithm (`module-contract.md:180-190`),
which was neither built nor declared out of scope before.** Four steps, and each gets an answer here
rather than a silence:

- **Step 2, contract version.** A manifest whose `core_contract_versions` does not list
  `rheo_contracts.CONTRACT_VERSION` fails load naming both versions. One comparison, no new type.
- **Step 2, dependency ranges "against the other selected modules".** For every required
  `Dependency` of a loaded manifest, the dependency must itself be in the loaded set and its
  `package_version` must satisfy the range; otherwise load fails naming both modules and the range.
  Optional dependencies are skipped. `packaging.specifiers.SpecifierSet.contains` does the
  comparison (Decision C, deviation 5) — no hand-rolled version grammar.
- **Step 3, topological sort.** `graphlib.TopologicalSorter` (stdlib) over the loaded set's
  `dependencies` edges gives the registration order, and raises `CycleError` on a cycle, which the
  loader re-raises as `ManifestInvalid` naming both modules. This is not optional machinery: FR 9
  says module chains "run in the manifest's dependency-sorted order", so the sort is forced by the
  orchestrator whether or not the loader wants it, and the loader is the only place that can see the
  whole loaded set.
- **Step 4, the manifest↔registration cross-check, is satisfied by construction and needs no code.**
  The ratified sentence — "an operation registered in code that the manifest does not declare, or
  declared and not registered, fails startup" — assumes the `Module` object with its own
  `register(registry)` function that `module-contract.md:11-13` describes. **That object does not
  exist in this tree.** `load_modules()` calls `_register(manifest, …)` (`loader.py:117`) and
  `_register` iterates the manifest's own tuples and nothing else, so "declared and not registered"
  and "registered and not declared" are both unreachable: the manifest *is* the registration source.
  Recorded as correction 9 to `module-contract.md` rather than built, and pinned by a test asserting
  every registered item whose `origin` is a module id appears in that module's manifest.

**Edge case 7 needs no new rule and gets none.** A `modules.installed` entry that nothing on disk
provides is simply never loaded: `load_modules()`'s existing "the allowlist says what *may* load,
not what must be present" rule (`loader.py:91-97`) is a property of the iteration over
`discovered()`, not of the variable it read, so changing the allowlist's source carries it over
unchanged. The design adds no startup refusal for an unprovided id, and a test pins that.

**3. `modules.installed`'s three `load_modules()` call sites.** The variable has exactly **one
reader** — `allowed_module_ids()` (`loader.py:69-72`) — plus its constant (`ALLOWLIST_VARIABLE`,
`loader.py:50`); both go. Every other live hit **names** `RHEO_MODULES` in prose without reading it:
the docstrings at `loader.py:1-36`, `modules/__init__.py:7`, `apps/core/src/rheo_app_core/startup.py:7`
and `:21`, and `apps/cli/src/rheo_app_cli/commands/openapi.py:10` and `:15` (`rg -n RHEO_MODULES apps`
returns exactly those four `apps/` lines, all docstring prose). They are rewritten to name the
settings key. The `load_modules()` call sites become:

| Call site | Today | After |
| --- | --- | --- |
| `apps/core/src/rheo_app_core/startup.py:111` | `load_modules()` | unchanged call; the allowlist now comes from the settings the sequence already resolved at `startup.py:87` (`settings = resolve()`) |
| `apps/cli/src/rheo_app_cli/commands/openapi.py:51` | `load_modules()` | unchanged call — but see the risk below |
| `apps/worker/src/rheo_app_worker/main.py` | **no call at all** | `load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)` inside `main()` |

**The worker call site closes Open Question 2 and corrects a false assumption in the
Requirements.** The spec assumes `apps/worker/src` "is currently a bare stub with no startup
sequence to extend"; it is not — `apps/worker/src/rheo_app_worker/main.py:117-127` is the worker's
composition root, and `JOB_KINDS`/`CONSUMERS` are built there at `:46` and `:67`. Adding the call is
two lines and it is **forced**, not opportunistic: without it, FR 6's job-kind and subscription
registration has no production caller at all, and AC 14 would be proved only by a test fixture
calling a path no deployed process runs. The call goes inside `main()`, never at module level, for
the reason that file's own docstring gives about `get_backend()` (`main.py:7-12`) and so that
`tests/test_worker_job_kinds.py:25`'s module-level `JOB_KINDS.names()` assertion stays true.

**4. `rheo_core.migrations.orchestrator` — module chains.** Per Decision F, the module-chain entry
point is a new `run_module_chain`, and `migrate_workspace` keeps refusing a module chain name. The
chain vocabulary becomes:

- **`version_table_for(chain)` is a new helper and it has three call sites, not one.**
  `_VERSION_TABLES` (`orchestrator.py:75-78`) stays the closed table for `control` and `core`; a
  chain name that is neither resolves as a **module chain** against `loaded_manifests()`, giving
  `(schema = module_id, table = f"alembic_version_{module_id}")`. Every current subscript of
  `_VERSION_TABLES` must read through the helper: `recorded_revisions` (`:118`), **`run_chain`
  (`:153`)** — a direct `_VERSION_TABLES[_check_chain(chain)]` that would `KeyError` on a module
  chain the moment `_check_chain` stops rejecting one — and `_check_chain` (`:82-85`) itself, whose
  membership test is the thing being widened. `run_chain` is therefore **not** unchanged: `:153` is
  a one-line edit, and it is the line that makes `CREATE SCHEMA IF NOT EXISTS <schema>` (`:162`)
  create the *module's* schema.
- `script_location` (`:88-95`) grows the same second branch, resolving
  `resources.files(manifest.storage.migrations_path)` and keeping the existing `env.py`-must-exist
  assertion.
- Beyond `:153`, `run_chain` (`:144-170`) is unchanged and keeps taking the same transaction-scoped
  advisory lock (`ADVISORY_LOCK_KEY`, `:73`) on the same connection — which is FR 9's "the *same*
  advisory lock" without a second lock key existing.
- `CHAINS` (`orchestrator.py:69`) stays `(control, core)` as the *fixed* pair it names; module
  chains are passed per call in the manifest's dependency-sorted order (the loader's
  `graphlib.TopologicalSorter` order), which is what FR 9 means by "`CHAINS` is no longer a fixed
  pair for this purpose".
- The module's own `env.py` mirrors `packages/core/src/rheo_core/migrations/core/env.py` exactly,
  with `version_table=f"alembic_version_{module_id}"` and `version_table_schema=module_id`, and
  refuses to run without an orchestrator-supplied connection.
- **`run_module_chain(connection, manifest, *, expected_database, core_version)`** calls `run_chain`
  on the caller's connection, then appends one `insert_module_schema_version` row per newly applied
  revision. **The enumeration, because Alembic's version table records the head and not the
  history:** the function snapshots `recorded_revisions(connection, chain)` (`:116-126`) before
  `run_chain` and again after, then walks
  `ScriptDirectory.from_config(build_config(chain)).iterate_revisions(<after heads>, <before heads,
  or "base" when the table was absent>)`; every revision that walk yields is newly applied and gets
  one row, written oldest first, with `schema_version` = the Alembic revision id (the column is
  `Text`, `core_tables.py:93`), `applied_at` = now, and `core_version_at_apply` = the `core_version`
  argument, which install passes as `storage.provisioning.core_version()` — the value the export
  restore path already writes to the same column (`exports/artifact.py:553`, `:575`). One revision
  is one row (AC 10); the first multi-revision chain is one row per revision, from the same code,
  with no second mechanism. It **raises** on failure — it has no `try`, no `MigrationResult`, and no
  access to the control-plane engine — so the chain's own exception reaches `core.module.install`'s
  job handler, which re-raises it as `ModuleInstallFailed("migration_failed", …)` and lets it
  propagate (Decision G); that is what makes the failure the operation's own, named, with the
  transaction rolled back (FR 11, AC 12). This is where AC 10's `core.module_schema_version` row is
  written; it is written nowhere in `orchestrator.py` today (`module_schema_version` appears there
  only in the docstring at `:24`).
- `migrate_workspace`'s failure path (`orchestrator.py:239-245`) — set the control-plane row
  `unavailable` — is untouched and stays what it is: the unattended startup pass over the `core`
  chain. It is already generic over `chains`, which is exactly why Decision F keeps module chains
  out of the function rather than trying to keep them out of the branch.

**5. `core.module.install`.** Declared `long_running=True` per FR 7 and
`module-contract.md:214`, joining the three declarations that already carry it
(`core_ops.py:445`, `:466`, `:477`). Its work splits so that every refusal a criterion asks to see
by name reaches the caller *synchronously*:

*Handler, in the dispatcher's transaction — pre-flight, then queue. Every refusal here is an
`OperationRefused(state, detail)` and reaches the caller as the outcome's own `error_code`:*
1. `module_unavailable`, naming the module, when it is not in `loaded_manifests()` (FR 7 step 1).
2. `module_already_installed`, naming the current state, when a `core.module_state` row exists
   (Decision D).
3. `dependency_missing` / `dependency_version`, naming the dependency, when a required dependency
   is not `installed` in this workspace at a satisfying range; an optional one that is absent is
   recorded and not refused (FR 7 step 2).
4. **`contract_tests_missing`, naming the path, under profile `test`** — the declaration-validity
   half of FR 7 step 6, moved forward from the job per Decision G. It reads the manifest and
   `find_checkout_root()` (`storage/data_root.py:138-147`) and touches no database, so it belongs
   where a caller can hear it; `exports/operations.py:193`'s `artifact_missing` is the same shape in
   a shipped long-running handler.
5. `enqueue_job(core.module.install, ModuleInstallPayload(workspace_id, module_id),
   max_attempts=1)` and return the minted operation id. **`max_attempts=1` is Decision G's call** —
   every failure reachable in the job below is deterministic. A `long_running` handler that queues
   nothing is already a dispatcher failure path (`operations/dispatch.py:1057`).

*Job handler, in the worker's one transaction (`HandlerUnitOfWork`). A raise here rolls the whole
transaction back and, at `max_attempts=1`, terminalises the operation record on the first attempt
with `error_code="job_failed"` (`work/loop.py:146` — the loop's one code) and
`error_text="<name>: <detail>"`:*
6. `CREATE EXTENSION IF NOT EXISTS <name>` for each `storage.required_extensions`, **before any
   migration statement**; a failure raises `extension_failed` naming the extension and the
   transaction rolls back with no migration step applied (FR 7 step 3, AC 11, edge case 3).
7. `run_module_chain(uow.connection, manifest, expected_database=…, core_version=core_version())`
   — which runs `run_chain`, creating the module schema under the advisory lock, and appends one
   `insert_module_schema_version` row per newly applied revision (FR 7 step 4, AC 10). The call sits
   inside the handler's one `try`: any exception from it is re-raised as
   `ModuleInstallFailed("migration_failed", error_text(exc)) from exc` and propagates, so the record
   reads `error_text="migration_failed: StorageRefusal: schema_ahead: …"` (or whatever the revision
   raised) and the transaction rolls back (FR 11, AC 12, edge case 6; Decision G).
8. `insert_module_state(... state="installed", package_version=manifest.package_version)` (step 5).
9. Under any profile other than `test`, run every `health_checks` callable, raising
   `health_check_failed` naming the failing check (the runtime half of FR 7 step 6; the profile-
   `test` half ran at pre-flight step 4 above). **Step 7 of the ratified list — "keys become
   settable" — is a statement about the configuration system, not an action:** the module's
   `KeySpec`s were registered at load, so they are settable already, and nothing is written here.

**Install does not run pytest, and that is a recorded deviation.** `module-contract.md:227` says
the `contract_tests` "run when the profile is `test`". Spawning a test runner from inside a mutate
operation's transaction is re-entrant (the suite that drives install is itself pytest), unbounded in
duration inside a leased job, and gives a handler arbitrary database access outside its own
connection. What install owes is a *checkable declaration*, and what runs the suite is CI — which
the same ratified sentence already names. `contract_tests` is therefore typed `str` and holds a
checkout-relative pytest target (`"modules/recallatron/tests"`), resolved against
`rheo_core.storage.data_root.find_checkout_root()` (`data_root.py:138-147`) and only under profile
`test`. `pyproject.toml:74`'s `testpaths` widens from `["tests"]` to `["tests", "modules"]` so CI
really collects it. This deviation is written into `module-contract.md` § Install and enable beside
step 6, together with Decision G's note that the resolution happens at pre-flight so
`contract_tests_missing` is the caller's own refusal code.

**A migration failure inside install never touches `control.workspace.state`** (FR 11, AC 12). The
only writer of `unavailable` is `orchestrator.py:239-245`, inside `migrate_workspace`, and per
Decision F `migrate_workspace` refuses a module chain outright: install calls `run_module_chain`,
which raises to its caller and has no access to the control-plane engine at all. There is therefore
no code path from a module chain to that write, provable by both directions — the positive control
that `migrate_workspace(chains=("recallatron",))` raises `ValueError` naming `run_module_chain`, and
AC 12's negative assertion that the `control.workspace` row is byte-identical before and after a
forced module-chain failure. The caller reads that failure as `error_code="job_failed"`,
`error_text="migration_failed: …"` (Decision G).

**6. `core.module.enable`.** Plain `mutate`, **not** long-running (`module-contract.md:230`): four
short steps, all in the dispatcher's own transaction, each independently refusable.

1. Refuse `module_state_invalid` naming the actual state unless it is `installed` or `disabled`.
2. Refuse `dependency_not_enabled` naming the dependency unless every required dependency is
   `enabled` in this workspace.
3. For every `KeySpec` in `configuration_schema` with `explicit_per_workspace`, write the row from
   the package default — reusing `insert_workspace_setting_if_absent`
   (`storage/repositories.py:167`) and `settings.encode_text` (`settings/schema.py:284`) exactly as
   `storage/provisioning.py:229-236` calls them. For every `Schedule`
   with `enabled_by_default`, insert a `core.schedule` row through a new
   `insert_schedule_if_absent` writer mirroring the one insert that exists today
   (`migrations/core/versions/0006_runtime.py:34-42`).
4. `set_module_state(module_id, state="enabled", enabled_at=now)`. Nothing else is needed for
   "from the next request `enabled_modules` includes it": `boundary/factories.py:86-88` already
   derives that set from `core.module_state` rows where `state == "enabled"`.

Both operations are `roles = {owner, operator}` (`module-contract.md:100`) and carry
`audit=AuditSpec(subject_field=None)` — required because both are above `READ`.

**7. The Recallatron skeleton distribution.** `modules/recallatron/` gains: a
`[project.entry-points."rheo.modules"]` table naming `recallatron = "rheo_recallatron:MANIFEST"`;
`dependencies = ["rheo-core", "rheo-contracts"]`; `src/rheo_recallatron/manifest.py` holding the
`ModuleManifest` instance; `src/rheo_recallatron/migrations/{env.py,script.py.mako,versions/0001_schema.py}`
whose one revision creates the `recallatron` schema and **nothing else**; and
`modules/recallatron/tests/test_contract.py` asserting the manifest validates and its migrations
package resolves. Every collection field is `()`, `sensitivity` is `{}`,
`configuration_schema` is `()`, `storage.required_extensions` is `()`, `web` and `agent_guidance`
are `None`.

**The `CREATE EXTENSION` path is proved by a harness fixture module, not by Recallatron** (spec
Assumptions, AC 11) — a manifest in `tests/harness/` declaring a real extension name, and a second
declaring one the test role cannot create. The harness is also where the two-module dependency
fixture lives for edge case 5 and AC 13 step 2.

**8. The two boundary mechanisms, kept apart.** FR 12 and AC 3/AC 9 are different checks over
different properties and this design keeps them in different files so neither can silently stand in
for the other:

| | F18 / AC 9 — construction gates | criterion 26 / AC 3 — storage ownership |
| --- | --- | --- |
| Property | a module must not mint a `WorkspaceContext` or construct a `SecretScope` | a module writes only to its own schema and reaches storage only through the core |
| Mechanism | AST scan for named-class construction | (i) a live-Postgres schema diff across the module chain; (ii) a text/AST scan for another module's schema-qualified names; (iii) an AST scan for driver/engine/DSN construction |
| Where | `tests/test_boundary.py:83` and `tests/test_boundary_scans.py:30` — `_SCAN_DIRS` gains `"modules"` | a **new** `tests/postgres/test_module_storage_ownership.py` |
| Fails independently? | yes — a module can own its schema perfectly and still mint a context | yes — a module can respect both gates and still write another schema |

One consequential detail: `tests/test_boundary_scans.py:204` already walks
`(*_SCAN_DIRS, "modules")` for its private-mention scan. Once `"modules"` is in `_SCAN_DIRS` that
line double-walks the tree; it becomes plain `_SCAN_DIRS` in the same diff. The AC-9 proof is the
`harness.absence`-style pair the repo already uses (`tests/test_absent_behaviour.py:24-28`): a
fixture file under `modules/` that *does* construct each class, asserted to be caught, so a scan
pointed at a path that does not exist fails its control rather than passing empty.

**9. The absence proof.** `scripts/absence_proof.py` with four modes — `--mode run` (the inner
step both configurations share, executed by that configuration's own interpreter: compute
`selected`, collect, run in-process, write the summary), `--mode config` (Recallatron on disk and
in the environment, `modules.installed` resolving to `()`, then `run` here), `--mode checkout` (a
detached `git worktree` copy with `modules/recallatron/` deleted and `uv sync` re-run, then `run`
inside it through that copy's `uv run`), `--mode compare` (two summary files, non-zero on any
difference) — plus a `make absence-proof` target running `config`, `checkout`, `compare`, and a CI
job that carries the `python` job's Postgres service and DSN. Configuration B runs in a throwaway
worktree so a developer's tree is never mutated by a proof.

**The selection is the `pytest:` subset of what `parse` yields, prefix stripped and deduplicated —
102 node ids on this tree — and the 7 `ci:` demonstrators are recorded as `ci_excluded`, not run**
(Decision E gives the rule and the reason: they are workflow `job / step` pairs, not pytest items).
**A summary is a per-node-id outcome map plus its own controls, not a pass/fail count:**
`{selected, collected, outcomes, ci_excluded}`. Each mode exits non-zero when `selected` is empty,
when `selected` and `collected` do not resolve onto each other both ways, when `set(outcomes) !=
collected`, or when any outcome is `skipped`; `--mode compare` exits non-zero unless both summaries
carry the same non-empty `selected`, that set equals what `parse` yields at compare time, both
`collected` sets are equal, and the two `outcomes` maps are identical key by key. Decision E gives
the reasoning and names the fifth control — `tests/test_absence_proof.py`, which makes the checks
go red on purpose five ways.

### External Services

None new. Postgres is already the only external dependency, in the same cluster and the same
per-workspace database. The one new *capability* exercised against it is `CREATE EXTENSION IF NOT
EXISTS`, whose failure mode (an application role without the privilege) is documented operator work
with a template-database remedy (`storage-and-workspaces.md:155-163`) and is not automated here.
Tests reach Postgres through the existing `cluster` / `make_workspace` / `workspace` fixtures
(`tests/conftest.py:143`, `:229`, `:256`) and `RHEO_TEST_CLUSTER_DSN`.

### Technical Risks

1. **`rheo openapi` gains a settings dependency it deliberately did not have.** `openapi.py:4-10`
   states it "needs no settings, no data root and no database", and `make codegen` runs it with no
   `RHEO_*` variable at all (`Makefile:199-218`) so the emitted bytes depend only on the installed
   distributions. Moving the allowlist into settings makes `load_modules()` call `resolve()`, which
   reads the deployment layer, which resolves a data-root *path*. Mitigations, in order: (a)
   `resolve_data_root` "Reads only; creates nothing" (`storage/data_root.py:116`), so no directory
   is created and no database is touched; (b) `modules.installed` is `Scope.DEPLOYMENT`, so only a
   `deployment.toml` or a `RHEO__` variable can set it, and neither exists in CI's generation
   environment; (c) the residual exposure — a developer with a real `deployment.toml` naming a
   module would generate different bytes locally — is **loud** (the `git diff --exit-code` gate
   fails) rather than silent, and the `Makefile` comment is updated to say "no `RHEO_*` variable and
   no `deployment.toml`" instead of the narrower claim it makes today.
2. **Two new mutate operations break a shipped inventory, and the tidy-up is a trap.**
   `tests/postgres/test_audit_dispatch.py:119`'s `THE_SEVENTEEN` is an exact frozenset of every
   mutating operation, asserted both ways at `:330` and `:442` and iterated at `:1041`. Adding
   `core.module.install` and `core.module.enable` makes it nineteen. This is by design (the test is
   the loud failure AC 26 asked for) but it is a required edit in the same diff as the
   registrations. **Per Decision H the constant and its test are NOT renamed** — the test name is a
   criterion-14 demonstrator at `docs/acceptance/phase-1-matrix.md:909` — so the mitigation is a
   docstring, not a rename. Named here so a builder does not "fix" the test by narrowing its
   assertion, and does not "fix" the stale count by renaming a pinned node id.
   A second-order staleness that is *not* a failure and must not be chased: criterion 14's recorded
   **Cost** block in the matrix quotes a failure output measured at a past commit ("the fourteen
   operation names", "13 failed, 10 passed"). `tests/test_acceptance_matrix.py` checks only that the
   mutation hunk still applies (`git apply --check`) — and it applies to `audit/core_sink.py`, which
   this run does not touch — so nothing reds and nothing is re-measured here.
3. **`extra="forbid"` + 21 required fields (of 24) is a hard break for every existing manifest
   construction.** The stub's **eight-field** constructor (`manifest.py:75-82`: `module_id`,
   `package_version`, `schema_name`, `create_schema`, `operations`, `resolvers`, `web`,
   `audit_sink`) is used by `tests/test_module_loader.py` and the 0v-era fixtures; two of those
   eight fields are dropped and eighteen are added. Every fixture must be rebuilt against the new
   model in the same phase that lands it, or the suite is red between phases. Mitigated by phasing:
   the manifest phase carries its own fixture migration and must be green on its own.
4. **A frozen pydantic model accepts a wrong callable.** Pydantic's native callable schema
   checks `callable()`, not a signature. A `health_checks` entry with the wrong arity
   fails at install time, not at load. Accepted: mypy strict checks the signature at the
   construction site inside the module distribution, which is where the author writes it, and
   `modules` is already in mypy's `files` (`pyproject.toml:44`).
5. **The absence proof's configuration B is the most expensive thing in this run** — a second
   worktree, a re-sync, and a second run of the phase-one demonstrators. It is forced by AC 4 and
   edge case 9, and it catches a failure class the static import scan cannot (a path read or a
   `metadata.version` call). If the build tail runs short, the *order* to cut is: keep the static
   scan and configuration A in the suite, and leave configuration B as the CI job.
6. **`modules/recallatron/tests` entering `testpaths` changes what `make test` collects.** That is
   intended (it is how `contract_tests` "run by CI"), but it means the absence proof must name its
   selection explicitly rather than inheriting `testpaths` — which it does.
7. **`enable_harness_module` (`tests/harness/registry.py:638-654`) is the only writer of a
   `module_state` row today, and 18 files depend on it** — 15 under `tests/postgres/` plus
   `tests/harness/consumers.py`, `registry.py` and `runtime_matrix.py`
   (`grep -rln enable_harness_module tests/ | wc -l` → 18). FR 10 offers "calls a real writer or is
   deleted"; it must be **rewritten to call `insert_module_state`**, not deleted. Deleting it would
   take `tests/postgres/test_workspace_status.py`, `test_context_routing.py`, `test_approvals.py`
   and the `module_disabled` branch coverage with it, which is scope this run does not own.
8. **`rheo-core` gains one runtime dependency, `packaging`.** It is already in `uv.lock:521` as a
   transitive dependency of the test toolchain, so no new distribution enters the image, but it does
   mean `uv.lock` changes in the manifest phase as well as the Recallatron phase — which the
   sequencing note in `plan.md` previously said could only happen once. Accepted rather than
   hand-rolling caret/tilde range parsing (Decision C, deviation 5). If a reviewer rejects the
   dependency, the fallback is to type `version_range` as an opaque `str`, validate it non-empty,
   and defer satisfaction to the first run that ships a module with a real dependency — which
   leaves FR 7 step 2's "at a satisfying version range" unimplemented and must be recorded as such.
9. **A 24th manifest field is a visible departure from a ratified table of 23.** A reader comparing
   `ModuleManifest.model_fields` against `module-contract.md`'s table finds one extra name.
   Mitigated by writing the field into the ratified table in the same run (correction 11) rather
   than leaving the code and the document disagreeing, and by AC 2 asserting the exact 24-name set
   so a later field cannot arrive unannounced.
10. **A field annotated with a non-`@runtime_checkable` `Protocol` breaks the model at import, not
    at validation.** Under `arbitrary_types_allowed` pydantic falls back to an is-instance schema,
    and pydantic-core's is-instance build probes `isinstance(None, cls)` — a plain `Protocol` raises
    `TypeError` there, which becomes `SchemaError` at class definition, so `import
    rheo_core.modules.manifest` fails and every process that imports it fails to start. `AuditSink`
    (`audit/sink.py:71`) is exactly that, and the stub's identical annotation hid it because a
    dataclass runtime-checks nothing. Reproduced on this worktree's venv, pydantic 2.13.5.
    Mitigated three ways: Decision B drops `arbitrary_types_allowed`, so an unhandled type fails
    loudly with `PydanticSchemaGenerationError` naming itself instead of silently becoming an
    `isinstance` probe; `audit_sink` carries an explicit `PlainValidator` duck check; and
    `tests/test_module_manifest.py` imports the module at top level, so the class-definition
    failure reds that file at collection. A 1a1 builder adding a Protocol-typed field must give it
    a validator the same way — the rule is written into Decision B, not left to be rediscovered.

### ADR Records

none — no qualifying new decisions. `docs/adr/` is not present (`ls docs/adr` → "No such file or
directory"; `docs/` holds `acceptance`, `architecture`, `eval`, `ideas`, `notes`, `requirements`,
`README.md`, `workspace-layout.md`). Decisions that would be ADRs in a repo that had them are
recorded instead in `docs/architecture/module-contract.md`, which is where this repo keeps its
ratified architecture. **This run corrects it in eleven named places**, each one a sentence the run
makes false or that was already false on this tree:

| # | Where | Correction | Owning phase |
| --- | --- | --- | --- |
| 1 | `:22` | the manifest's home is `rheo_core.modules.ModuleManifest`, with the handler-travels-beside-declaration reason (FR 2) | 1 |
| 2 | `:33` | `configuration_schema` is `tuple[KeySpec, ...]`, not a pydantic model class (deviation 1) | 1 |
| 3 | `:38` | `JobKind.max_attempts` and `.cancellable` are carried and reader-less in release one, and which run reads each | 3 |
| 4 | `:43` | `web`'s ratified type is `WebContribution(package_name, navigation, routes, record_views, forms, search_providers)`; release one ships `WebSurface \| None` (`manifest.py:56-68`, consumed by `internal_routes.py:104`) and **1a3 widens or replaces it** — the type carry named in Decision A | 1 |
| 5 | `:181` | `modules.installed`'s default is the **empty list**, not "every discovered module"; an unlisted module id is not loaded, with no exception (FR 4) | 2 |
| 6 | `:189-190` | discovery step 4's manifest↔registration cross-check is satisfied by construction — the loader registers from the manifest and there is no separate `register(registry)` in this tree | 3 |
| 7 | `:227` | `contract_tests` are *resolved and validated* by install and *run* by CI, and the resolution happens at pre-flight (deviation 3, Decision G) | 7 |
| 8 | `:240-242` | criterion 24 is **partially** closed by 1a0; the interface-contribution half is 1a3's (Decision A) | 7 |
| 9 | `:266-268` | dependency ranges are PEP 440 specifiers, not caret/tilde forms (deviation 5) | 1 |
| 10 | `:285` | the stale "sets it to `relationships, leads`" sentence is replaced by the two-configuration proof (Decision E) | 8 |
| 11 | `:25-49` | the manifest table gains a 24th row, `audit_sink`, optional — the channel `loader.py:135-139` installs and `dispatch.py:184` refuses without (deviation 4) | 1 |

### Simplest-Model Baseline

**The simplest model that satisfies the written requirements.** One pydantic `ModuleManifest` in
`rheo_core.modules`. One settings key replacing one environment variable. The loader keeping the
manifests it loaded and calling five `register` methods instead of two. Two new repository
functions writing the two tables that already exist. Two new operations whose handlers call those
functions. One `if` in the migration orchestrator that resolves a module chain's script directory
and version table from the manifest. One Recallatron distribution that is an entry point, a
manifest instance, and a migration that runs `CREATE SCHEMA recallatron`. Plus tests.

That baseline is most of the design. Everything added over it, and what forces each:

| Mechanism over the baseline | Forced by |
| --- | --- |
| `core.module.install` as `long_running` + a `core.module.install` **job kind and payload** | FR 7's "(mutate, long-running)" and `module-contract.md:214`. Without it the declaration lies about itself and `dispatch` mints no operation record for a migration run that can take minutes. |
| Pre-flight refusals in the dispatch handler rather than in the job — including `contract_tests_missing` | AC 11/12/13 and edge cases 1, 2, 4, 5 all assert a **named** refusal reaching the caller, and `work/loop.py:146` gives a job exactly one error code (`JOB_FAILED`) for every route to `failed`. So a condition knowable before any database work — and `contract_tests` resolution is one, it reads a path — has to be raised synchronously or it loses its name. Decision G. |
| `max_attempts=1` on the install job kind | `work/jobs.py:150` makes it required and no requirement named a value; every in-job failure is deterministic, so the default 8 buys seven retries of a certain failure under an hour of backoff. |
| `load_modules(kinds=…, consumers=…)` parameters + the worker call site | FR 6 / AC 14. `JOB_KINDS` and `CONSUMERS` are composition-root locals (`apps/worker/.../main.py:46`, `:67`), not importable globals, so registration is otherwise impossible; and without the worker call the registration has no production caller. |
| `loaded_manifests()` replacing `_SURFACES` | install steps 1–6 need the manifest back by module id; two tables of "what loaded" would be two things to keep in step (`loader.py:52-61` says as much about the single-purpose one it replaces). |
| `insert_schedule_if_absent` | enable step 3 / AC 13. The only `core.schedule` insert today is inside a migration (`0006_runtime.py:34-42`); a runtime writer does not exist. |
| Ten new types (nine models, one enum) | seven are forced by criterion 25 and FR 6–8 (Decision C's table); three (`DeletionParticipant`, `ConnectorBinding`, `SensitivityTier`) exist only to make the ratified field *nameable and present* per FR 3. Those three carry no behaviour and no reader, and each is a transcription of a ratified block. |
| The contract-version gate in the loader | `module-contract.md:184-185`. One comparison; without it `core_contract_versions` is a field nothing ever reads, which FR 3 permits but the discovery algorithm does not. |
| The loader's dependency-range check and `graphlib` topological sort | `module-contract.md:184-187`, and FR 9's "module chains run in the manifest's dependency-sorted order" — the sort is forced by the orchestrator regardless, and the loader is the only place that sees the whole loaded set. Cut alternative: build neither and declare both out of scope; rejected because FR 9 then has no defined order and `dependencies` becomes a field nothing reads. |
| `packaging` as a `rheo-core` runtime dependency | the range check above, and FR 7 step 2's "at a satisfying version range". Cut alternative: a hand-rolled caret/tilde parser — one caller, and version comparison is the classic place a hand-rolled parser is wrong. |
| A 24th manifest field, `audit_sink` | `loader.py:135-139` installs it, `dispatch.py:184` refuses every above-`READ` operation without it, and `audit_paths.py:32` refuses startup. Not machinery added — machinery *kept*: dropping it is what would have cost 1a1 a `packages/core` edit, which is the one thing criterion 24 forbids. |
| `run_module_chain` as a separate entry point from `migrate_workspace` | FR 11 / AC 12. `migrate_workspace`'s `unavailable` write (`orchestrator.py:239-245`) is already generic over `chains`, so admitting a module chain to that function *is* the violation. Cut alternative: gate the write on the chain's kind inside the failure branch — more code, and it mis-attributes when `chains` mixes kinds. |
| `scripts/absence_proof.py` + a make target + a CI job | AC 4 and edge case 9's two configurations. Rejected cheaper alternative: the static import scan alone — it cannot see a `metadata.version("rheo-recallatron")` call or a path read under `modules/`. |
| The absence proof's four controls and `tests/test_absence_proof.py` | a comparison of two empty or unresolvable selections diffs clean, so the headline deliverable for criterion 33 would be a test that cannot fail — and a selection carrying kind prefixes or `ci:` steps is one pytest can never collect, so without the selection rule in Decision E it would be a test that cannot pass. `tests/harness/absence.py:10-15` makes a control mandatory for every other absence claim in this repo; this one gets the same. |
| A new `tests/postgres/test_module_storage_ownership.py` separate from the `_SCAN_DIRS` change | the Analyst's stated risk (FR 12): one mechanism standing in for the other leaves one property unverified. They are in different files and fail for different reasons. |

**Removed for want of a forcing requirement** (each considered and cut): an `EventRegistry`; a
`publish()` gate refusing undeclared event types; any `disable`/`remove`/`purge`/`restore` stub;
an upgrade path on repeat install; a class-to-`KeySpec` compiler for `configuration_schema`; a
second advisory-lock key for module chains; a widened `JobKindRegistry.register` taking
`max_attempts`/`cancellable`; a per-kind `error_code` replacing `work/loop.py:146`'s single
`JOB_FAILED`; a manifest↔registration cross-check for a `register(registry)` function this tree does
not have; an `apps/worker` startup report; a `phase-2-matrix.md`
(phase two's acceptance matrix has no criterion asking for it in this run — **registered here as
deferred**, since `tests/test_acceptance_matrix.py:34` covers criteria 1–23 and 69 only and no
mechanism records a phase-two criterion's state today).

### Design-Model Summary

No new table and no new external service. `core.module_state` and `core.module_schema_version`
already ship with the ratified columns and already have readers; this run adds their first real
writers in `repositories.py` and rewrites `tests/harness/registry.py`'s hand-rolled insert to call
one. The stub `ModuleManifest` frozen dataclass becomes a frozen pydantic model with all 23
ratified fields required except `web` and `agent_guidance` — pydantic is chosen *because* criterion
25 wants a missing field named, which is exactly what it emits — configured `frozen` +
`extra="forbid"` with **no** `arbitrary_types_allowed`: every field type is one pydantic handles
natively, and the one `Protocol`-typed field, `audit_sink`, carries a `PlainValidator` duck check,
because a bare non-runtime-checkable Protocol annotation fails class definition and would take
every process that imports the manifest down with it (reproduced; Decision B). **A 24th field, `audit_sink`, is kept from the stub and written into the ratified
table**: it is the only channel by which a module supplies the sink `dispatch.py:184` refuses every
above-`READ` operation without, so a 23-field model with `extra="forbid"` would cost 1a1 the
`packages/core` edit criterion 24 forbids. Five deviations from the ratified text in all, each
written back into `module-contract.md` (which this run corrects in eleven places).
`RHEO_MODULES` is deleted and replaced by one deployment-scope settings key
`modules.installed` (default empty), keeping on-disk / deployment-loaded / workspace-enabled as
three separately named readable sets. The loader grows from two registration categories to six and
gains two registry parameters, because the worker's job-kind and consumer registries are
composition-root locals rather than importable globals — which also forces a two-line
`load_modules()` call in `apps/worker`'s `main()`, closing the spec's Open Question 2 and
correcting its assumption that the worker is a bare stub; it also finishes the ratified discovery
algorithm with a dependency-range check and a `graphlib` topological sort. `core.module.install` is
long-running: every refusal knowable without the workspace database — unavailable,
already-installed, dependency, and `contract_tests_missing` — is raised synchronously as the
outcome's own `error_code`, and only extensions, the migration chain, the state row and the health
checks run in the job, at `max_attempts=1` so a deterministic failure is terminal on the first
attempt with its name in `error_text` (`extension_failed`, `migration_failed`,
`health_check_failed`). A module chain never enters `migrate_workspace`, whose `unavailable` write
is already generic over chains; `run_module_chain` is the only door and it raises to its caller,
whose one `try` renames the failure `migration_failed` and lets it propagate — FR 11's mechanism. A repeat install is **refused** naming the current
state, not a no-op and not an upgrade. Install validates but does not execute `contract_tests`; CI
runs them, via `testpaths` widening to include `modules`. Criterion 24 is closed except for its
interface-contribution half, carried to 1a3 in writing. Criterion 33 is proved by two configurations
diffed over the phase-one matrix's own `pytest:` demonstrators — 102 node ids, prefix stripped; the
7 `ci:` demonstrators are workflow steps and are recorded as excluded; never the whole `tests/`
tree, because this run adds phase-two tests that legitimately need the module present — and the
diff carries a non-empty-selection control, a two-way resolution control, a completeness control
and a mutation test, because a comparison of two nothings is otherwise clean.
