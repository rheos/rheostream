# Development Plan — 1a0, module contract: manifest, install, enable

Eight phases. Each one is a diff a reviewer can hold in one sitting, and each one leaves the suite
green on its own — no phase parks a red test for the next phase to fix. Every phase is owned by
**The Systemsmith**.

**Gate commands, identical for every phase** (run from the worktree root):

```bash
set -a; . ./.env; set +a                      # only if .env exists — see the warning below
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest <the phase's own selection>     # fast inner loop
make lint && make typecheck && make test && make check
```

**Two facts a builder will otherwise trip on, both verified on this worktree at `9067552`.**
(1) There is no `.env` here (`ls .env` → "No such file or directory"); `.env.example` is the
template and `make` sources no env file (`.env.example:79-82`). (2) The cluster port this host
serves is **5433**, and nothing in the repository says so: `.env.example:44` and
`tests/conftest.py:45`'s `DEFAULT_TEST_CLUSTER_DSN` both name **5432**, and
`grep -rn 5433 .env.example deploy/compose.yaml Makefile` returns nothing. A builder who copies
`.env.example` unchanged will point pytest at the wrong port and every `-m postgres` case will
fail with `tests/conftest.py:48`'s remedy message rather than with a real defect. Export the DSN
explicitly, as above. Do **not** "fix" `.env.example` or `conftest.py` to say 5433 — that is a
host fact, not a repository fact, and no requirement in this run asks for it.

`make codegen` additionally runs in phases 2 and 3 (both touch what `rheo openapi` loads) and its
`git diff --exit-code` must be clean afterwards. `make migrate` is never run by a phase; the
migration chains here are exercised through the test fixtures against the test cluster.

---

## Phase 1 — The manifest contract (and the doc correction) — [The Systemsmith]

**Goal:** `rheo_core.modules.ModuleManifest` is a frozen pydantic model carrying all 23 ratified
fields plus `audit_sink` (**FR 1**), a missing required field fails validation naming that field,
and every existing manifest construction in the tree is rebuilt against it. No behaviour changes
yet.

**Deliverables:**

- `packages/core/src/rheo_core/modules/manifest.py` — replace the frozen dataclass and the
  hand-rolled `validate()` (today `:71-115`; `WebSurface` at `:56-68` is kept verbatim) with:
  - `ModuleManifest(BaseModel)`, `model_config = ConfigDict(frozen=True, extra="forbid")` —
    **no `arbitrary_types_allowed`** (spec.md § Decision B: every field type is native to pydantic,
    and that flag's `isinstance` fallback is the mechanism that breaks the one Protocol field) —
    **24 fields** per spec.md § Decision C's table — the 23 ratified names plus `audit_sink`. Only
    `web`, `agent_guidance` and `audit_sink` carry `= None` (**FR 1**; `audit_sink` is Decision C's
    deviation 4). **FR 3** is what forbids a default on the deferred-consumption fields —
    `record_types`, `events`, `subscriptions`, `jobs`, `schedules`, `deletion_participants`,
    `connector_bindings`, `sensitivity` stay **required with no default**, so a manifest must declare
    them present-and-empty (`()` / `{}`) rather than omit them. A `= ()` default on any of those
    eight would make omission legal and silently defeat FR 3.
  - **`audit_sink` is carried over from the stub (`:82`) with its `None` default but NOT its bare
    annotation.** Its shape is `Annotated[AuditSink, PlainValidator(_audit_sink)] | None = None`,
    where `_audit_sink` returns the value when `callable(getattr(value, "record", None))` and
    otherwise raises `ValueError("an audit sink must provide record()")` — the duck check
    `install_sink` already makes at `audit/sink.py:110-111`. A bare `AuditSink | None` on a pydantic
    model fails **class definition**: `AuditSink` is a `Protocol` without `@runtime_checkable`, and
    pydantic-core's is-instance build probes `isinstance(None, cls)`, which such a Protocol refuses
    with `TypeError`, surfacing as `SchemaError` — so `import rheo_core.modules.manifest` fails and
    takes `apps/core`, the CLI and the worker with it. Reproduced on this worktree's venv (pydantic
    2.13.5). Do **not** repair it by editing `audit/sink.py` (outside this run's scope), by typing
    the field `Any`, or with `SkipValidation` (it defines, but accepts a bare `object()`). Deleting
    the field is the other change this phase must not make: `loader.py:135-139` is the only code
    that installs a module's sink; `dispatch.py:184`'s `AUDIT_SINK_MISSING` refuses every operation
    above `READ` whose owning module has none, and `operations/audit_paths.py:32` refuses
    `apps/core`'s startup for the same condition. With `extra="forbid"` there is no other way for a
    module to supply one, so a 23-field model would force 1a1 to edit `packages/core` — the single
    thing criterion 24 forbids. The two stub fields that **are** dropped, `schema_name` and
    `create_schema` (`:77-78`), have no production caller: `schema_name` moves inside
    `StorageDeclaration` and `create_schema` is replaced by the migration chain.
  - ten new types — nine frozen models (`Dependency`, `RecordType`, `StorageDeclaration`,
    `EventDeclaration`, `JobKind`, `Schedule`, `DeletionParticipant`, `ExportDeclaration`,
    `ConnectorBinding`) and the `SensitivityTier` StrEnum.
  - the callable aliases `HealthCheck = Callable[[UnitOfWork], None]`, and
    `DeletionHandler`/`Exporter`/`Importer` as `Callable[..., object]`, each with a one-line
    docstring naming the run that narrows it.
  - an `@model_validator(mode="after")` carrying the three rules the stub enforced — lowercase
    identifier `module_id`, `storage.schema_name == module_id` (today `:105-110`), and
    `not is_reserved_module(module_id)` (`packages/contracts/src/rheo_contracts/refs.py`) — plus
    the ratified `configuration_schema` rule that every `KeySpec.key` starts with `<module_id>.`,
    plus the rule that every `Dependency.version_range` parses as a
    `packaging.specifiers.SpecifierSet` (Decision C, deviation 5), so an unparseable range fails at
    load rather than at install.
- `packages/core/pyproject.toml` — add `packaging>=24,<26` to `dependencies`. Already resolved in
  `uv.lock:521` as a transitive dependency, so no new distribution enters the image, but the lock
  does change in this phase — see the sequencing note at the bottom of this plan.
  - `ManifestInvalid` (today `:43-53`) kept, for load-time failures that are not field-shape
    failures. Pydantic's own `ValidationError` is never wrapped: wrapping is what loses the field
    name AC 2 asserts on.
  - `WebSurface` (today `:56-68`) kept verbatim; `web` stays `WebSurface | None`.
- `packages/core/src/rheo_core/modules/__init__.py` — export the new names beside `ModuleManifest`.
- `packages/core/src/rheo_core/modules/loader.py` — `validate_manifest` becomes
  `ModuleManifest.model_validate`-based; `_register` is untouched in this phase.
- `tests/test_module_loader.py` — rebuild every manifest fixture against the 24-field model. The
  fixture at `:434` and the three at `:131`, `:150`, `:166` currently pass `create_schema=`, which
  no longer exists; each gains a `StorageDeclaration` instead.
- **New** `tests/test_module_manifest.py` — AC 2: omit `record_types`, `storage`,
  `configuration_schema`, `tools`, `export` in turn and assert the `ValidationError` names that
  field; assert `ModuleManifest.model_fields` holds exactly the 23 ratified names **plus
  `audit_sink`**, and that exactly three — `web`, `agent_guidance`, `audit_sink` — are not required.
  **The file's module-level `from rheo_core.modules.manifest import ModuleManifest` is itself the
  first assertion**: the class-definition failure Decision B describes reds this file at collection,
  before any case runs. Two `audit_sink` cases beside it: `audit_sink=CORE_AUDIT_SINK` validates and
  is returned by identity; `audit_sink=object()` raises naming `audit_sink`.
  **FR 3's** own case, in the same file: a manifest
  supplying `()` for all seven deferred collection fields and `{}` for `sensitivity` validates —
  present-and-empty is a declared field — while omitting any one of those eight still raises,
  naming it. Without this pair the required-vs-defaulted distinction FR 3 rests on is untested.
  Also: a `Dependency` whose `version_range` does not parse raises, naming the field.
- `docs/architecture/module-contract.md` — **this phase owns corrections 1, 2, 4, 9 and 11** of the
  eleven in spec.md § ADR Records, all in one edit:
  - **1 (FR 2):** the manifest section (`:22`) names `rheo_core.modules.ModuleManifest` and states
    the handler-travels-beside-declaration reason, mirroring
    `packages/contracts/src/rheo_contracts/manifest.py:89-97`.
  - **2:** the `configuration_schema` row (`:33`) records the `tuple[KeySpec, ...]` deviation.
  - **4:** the `web` row (`:43`) records that release one ships `WebSurface | None`
    (`manifest.py:56-68`, consumed by `internal_routes.py:104`) and that 1a3 owns the ratified
    `WebContribution` shape.
  - **9:** the version-compatibility section (`:266-268`) records PEP 440 specifiers in place of
    "the usual caret and tilde forms".
  - **11:** the manifest table (`:25-49`) gains a 24th row, `audit_sink`, optional, naming
    `loader.py:135-139` as its installer and `dispatch.py:184` as what refuses without it.
  FR 2 is a correction this run makes directly — it is not a flag left for a later run.

**Closes:** AC 2 (public 25), AC 5 (F3, its first two clauses plus the doc clause); **FR 1**,
**FR 2**, and **FR 3's** model half — FR 3's skeleton half (a real manifest that actually supplies
the empties) lands in phase 4.

**Checkpoint:** `uv run pytest tests/test_module_manifest.py tests/test_module_loader.py
tests/test_contracts.py tests/test_imports.py`, then the four `make` gates.

---

## Phase 2 — `modules.installed`, and the three named sets — [The Systemsmith]

**Goal:** `RHEO_MODULES` no longer exists anywhere in `packages/`, `apps/` or `tests/`; the
allowlist is a deployment-scope settings key (**FR 4**); on-disk, deployment-loaded and
workspace-enabled are three separately named readable things (**FR 5**).

**Deliverables:**

- `packages/core/src/rheo_core/settings/schema.py` — add the `modules.installed` `KeySpec` to
  `PRODUCTION_KEYS` (`:369`): `ValueType.STR_LIST`, `Scope.DEPLOYMENT`, `floor=None`,
  `explicit_per_workspace=False`, `default=()` (**FR 4**; the empty default is FR 4's
  "an unlisted module id is not loaded, with no exception"). Update the module docstring, which
  currently names this key as deliberately undeclared (`:26-29`).
- `packages/core/src/rheo_core/config/defaults.toml` — the same value, since
  `settings/defaults.py` asserts the two are identical at import.
- `tests/test_settings.py` — add the key to the `PRODUCTION_KEYS` dict at `:58` (the exact-set
  assertions at `:176-178`, `:222`, `:235` and `:302` are what fail without it).
- `packages/core/src/rheo_core/modules/loader.py` — `allowed_module_ids()` (`:69-72`) reads
  `frozenset(resolve()["modules.installed"])` (**FR 4**, the loader call site); `ALLOWLIST_VARIABLE`
  (`:50`) and the `os` import go; the module docstring (`:1-36`) is rewritten to describe the
  settings key.
- `packages/core/src/rheo_core/modules/loader.py` — `_SURFACES` (`:52-61`) becomes
  `_LOADED: dict[str, ModuleManifest]`; add `loaded_manifests() -> Mapping[str, ModuleManifest]`;
  `module_surfaces()` derives from it and keeps its signature; `reset_surfaces()` clears `_LOADED`.
  `discovered()` (`:64`) keeps its name and its meaning untouched, so **FR 5's** sets (a) and (b)
  stay two functions — `discovered()` answers on-disk, `allowed_module_ids()`/`loaded_manifests()`
  answer deployment-loaded, and neither reads `core.module_state`.
- `packages/core/src/rheo_core/modules/__init__.py` — export `loaded_manifests`; drop the
  `RHEO_MODULES` sentence at `:7`.
- `.env.example` — replace the `RHEO_MODULES` block (`:64-77`) with a
  `RHEO__modules__installed=` block carrying the same `# preflight: allow-empty` marker and a
  paragraph explaining the empty default. (The marker has no reader in the tree —
  `grep -rn "allow-empty\|allow_empty"` returns only `.env.example:76` — so it is carried for the
  external preflight that wrote it, not for a check in this repo.)
- `Makefile` — the `codegen` comment (`:199-218`) drops its `RHEO_MODULES` sentence and states the
  canonical generation environment as "no `RHEO_*` variable **and** no `deployment.toml`".
- `tests/test_openapi_document.py` (`:15`, `:228`, `:262`, `:290`) and
  `tests/test_production_registration.py:436` — switch to the settings key.
- `apps/core/src/rheo_app_core/startup.py` (`:7`, `:21`) and
  `apps/cli/src/rheo_app_cli/commands/openapi.py` (`:10`, `:15`) — **the two call sites FR 4 names
  besides the loader.** Neither reads the variable directly (each only calls `load_modules()`, which
  is why spec.md § System Components 3 records the *call* as unchanged), but both module docstrings
  state the allowlist as `RHEO_MODULES`, so both are live hits under `apps/`. Rewrite each to name
  the resolved `modules.installed` setting: startup.py's step list ("load the modules `RHEO_MODULES`
  names" → the setting) and its `RHEO_MODULES`-unset paragraph, and openapi.py's two paragraphs
  about what the emitted bytes depend on. Verified present on this worktree —
  `rg -n RHEO_MODULES apps` returns exactly those four lines.
- `tests/test_module_loader.py` — delete `test_the_allowlist_variable_is_not_a_settings_override`
  (`:204-209`, whose body is `assert ALLOWLIST_VARIABLE == "RHEO_MODULES"`) and rewrite
  `test_the_process_variable_is_not_set_by_the_suite` (`:463-467`, the file's last five lines — it
  is 467 lines long) against the settings key — with a
  `Scope.DEPLOYMENT` key there is no process variable for a developer's shell to leak, so the case
  either becomes "no `RHEO__modules__installed` in the environment" or goes with the variable it
  guards. Also the module docstring (`:1`). **Deleting `ALLOWLIST_VARIABLE` breaks both cases**, and
  this file is otherwise named in this plan only for phase 1's fixture rebuild.
- **New** `tests/test_module_sets.py` — AC 8 (F17) and **FR 5**: a module id on disk but not in
  `modules.installed` is discovered and not loaded; a module loaded this deployment and never
  installed in a workspace has no `core.module_state` row; both states independently readable, each
  through its own named function (`discovered()`, `loaded_manifests()`, `list_module_states()`).
  This is FR 5's "never conflated under one name or one function" made checkable: the test fails if
  any one function starts answering two of the three questions.
- `docs/architecture/module-contract.md` — **correction 5**: discovery step 1 (`:181`) currently
  says `modules.installed`'s default is "every discovered module", which is the contradiction F5
  records and which **FR 4** inverts. Rewrite it to state the empty-list default and that an unlisted
  module id is not loaded, with no exception.
- `tests/test_absent_behaviour.py` — extend the `SCAN_ROOTS`-style completeness check, or add one
  case, asserting the literal `RHEO_MODULES` has no tracked hit outside `docs/` (**FR 4's** last
  clause, and AC 6's second clause). Its own docstring at `:102` cites the `RHEO_MODULES` value as a
  worked example of a missed token and is updated in the same edit.

**Closes:** AC 6 (F5), AC 8 (F17); **FR 4**, **FR 5**.

**Checkpoint:** `uv run pytest tests/test_settings.py tests/test_module_loader.py
tests/test_module_sets.py tests/test_openapi_document.py`, the four `make` gates, then
`make codegen && git diff --exit-code apps/web/src/generated/`.

---

## Phase 3 — The loader registers all six extension points — [The Systemsmith]

**Goal:** a manifest's operations, resolvers, tools, job kinds and subscriptions all reach a live
registry, its events are validated and readable, and the worker actually loads modules.

**Deliverables:**

- `packages/core/src/rheo_core/modules/loader.py` — `load_modules()` gains
  `tools: ToolRegistry = TOOL_REGISTRY`, `kinds: JobKindRegistry | None = None`,
  `consumers: ConsumerRegistry | None = None`. `_register` (`:122-141`) grows four branches:
  `ToolRegistry.register(decl, origin=module_id)` (`packages/core/src/rheo_core/tokens/sets.py:197`),
  `JobKindRegistry.register(name, input_model, handler)`
  (`packages/core/src/rheo_core/work/kinds.py:69`), `ConsumerRegistry.register(subscription)`
  (`packages/core/src/rheo_core/events/consumers.py:83`) after asserting each subscription's
  `module_id` equals the manifest's, and `SettingsRegistry.register(spec, origin=module_id)` for
  each `configuration_schema` key
  (`packages/core/src/rheo_core/settings/schema.py:313`). A category whose registry argument is
  `None` is not registered and is not an error.
  The `audit_sink` branch at `:135-139` and the web-surface branch at `:140-141` are **not** touched:
  they already do the right thing and the manifest still carries both fields.
- Same file — the contract-version gate: refuse a manifest whose `core_contract_versions` does not
  list `rheo_contracts.CONTRACT_VERSION`, naming both (`module-contract.md:184-185`).
- Same file — **the rest of the ratified discovery algorithm**, which no deliverable covered before:
  - **dependency ranges** (`module-contract.md:184-185`): every required `Dependency` of a loaded
    manifest must be in the loaded set at a `package_version` its range accepts, checked with
    `packaging.specifiers.SpecifierSet.contains`; otherwise load fails naming both modules and the
    range. Optional dependencies are skipped.
  - **topological sort** (`:186-187`): `graphlib.TopologicalSorter` (stdlib) over the loaded set's
    dependency edges gives the registration order, and a `CycleError` is re-raised as
    `ManifestInvalid` naming both modules. This is also the order FR 9 means by "the manifest's
    dependency-sorted order", so phase 5 reads it rather than inventing a second one.
  - **step 4's manifest↔registration cross-check** (`:188-190`) is **satisfied by construction and
    gets no code**: `load_modules()` calls `_register(manifest, …)` (`loader.py:117`) and `_register`
    iterates the manifest's own tuples and nothing else, so neither "declared and not registered"
    nor "registered and not declared" is reachable. The ratified sentence assumes the `Module` object
    with its own `register(registry)` that `module-contract.md:11-13` describes, and that object does
    not exist in this tree. Pinned by one assertion in `tests/test_module_registration.py` below, and
    recorded as doc correction 6.
- Same file — event-declaration validation: `type` matches `<module_id>.<record_type>.<verb>` with
  the manifest's own id as the first segment, and no two loaded modules declare the same type.
- `docs/architecture/module-contract.md` — **corrections 3 and 6**: the `jobs` row (`:38`) records
  that `max_attempts` and `cancellable` are carried and reader-less in release one, naming the
  enqueue site and Disable (`:252`) as their future readers, because
  `JobKindRegistry.register` takes three parameters (`work/kinds.py:69-71`); and discovery step 4
  (`:189-190`) records the cross-check as satisfied by construction.
- `apps/worker/src/rheo_app_worker/main.py` — inside `main()` (`:117`), after
  `refuse_misconfigured_login()`, call `load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)`.
  Extract it as `register_modules()` so a test can drive it without a backend. **Not at module
  level**, for the reason that file's docstring gives about `get_backend()` (`:7-12`).
- `tests/test_worker_job_kinds.py` — a case asserting a fixture module's job kind and consumer land
  in `JOB_KINDS` / `CONSUMERS` after `register_modules()`; the existing module-level assertion at
  `:25` stays true because the call is inside a function.
- **New** `tests/test_module_registration.py` — AC 14: one manifest fixture declaring one operation,
  one resolver, one tool, one job kind, one event and one subscription; assert all six are readable
  from their respective registries (events from `loaded_manifests()`) after `load_modules()`. Three
  more cases in the same file: a fixture declaring an `audit_sink` is installed under its own module
  id and `sink_for(module_id)` returns it; a two-module pair whose dependency range is unsatisfied
  fails load naming both; a two-module cycle fails load naming both. Plus the step-4 assertion: for
  every registered item whose `origin` is a loaded module id, that item appears in that module's
  manifest — the cross-check the ratified text asks for, stated as a property rather than built as a
  mechanism.

**Closes:** AC 14 (FR 6), and the spec's Open Question 2.

**Checkpoint:** `uv run pytest tests/test_module_registration.py tests/test_module_loader.py
tests/test_worker_job_kinds.py tests/test_production_registration.py`, the four `make` gates,
`make codegen && git diff --exit-code`.

---

## Phase 4 — The Recallatron skeleton distribution — [The Systemsmith]

**Goal:** `modules/recallatron` publishes a real `rheo.modules` entry point whose manifest
validates, with a packaged Alembic environment and one revision that adds no tables, and a
contract-test entry CI collects. Nothing installs it yet.

**Deliverables:**

- `modules/recallatron/pyproject.toml` — `dependencies = ["rheo-core", "rheo-contracts"]`,
  `[tool.uv.sources]` entries matching the other members, and
  `[project.entry-points."rheo.modules"] recallatron = "rheo_recallatron:MANIFEST"`.
- **New** `modules/recallatron/src/rheo_recallatron/manifest.py` — the `ModuleManifest` instance,
  and **FR 3's** skeleton half: every deferred-consumption field is written out present-and-empty
  rather than left off the constructor call.
  Every collection `()`, `sensitivity` `{}`, `configuration_schema` `()`,
  `storage = StorageDeclaration(schema_name="recallatron",
  migrations_path="rheo_recallatron.migrations", required_extensions=())`,
  `export = ExportDeclaration(format_version=1, schema_path="export.schema.json", …)`,
  `contract_tests = "modules/recallatron/tests"`, `web = None`, `agent_guidance = None`,
  `audit_sink = None` — written explicitly, not omitted: the skeleton registers no operation, so it
  needs no sink, and 1a1 (whose memory operations are `mutate`) is the run that first supplies one,
  as `audit_sink=CORE_AUDIT_SINK` or a writer of its own. A comment on that line says so, because a
  1a1 builder who omits it gets `audit_sink_missing` at dispatch rather than a load-time failure.
- `modules/recallatron/src/rheo_recallatron/__init__.py` — re-export `MANIFEST`.
- **New** `modules/recallatron/src/rheo_recallatron/migrations/env.py`,
  `script.py.mako`, `versions/0001_schema.py` — `env.py` mirrors
  `packages/core/src/rheo_core/migrations/core/env.py` with
  `version_table="alembic_version_recallatron"`, `version_table_schema="recallatron"`, and the same
  refusal when no orchestrator connection is supplied. `0001_schema.py` creates **no tables** and
  **nothing else** beyond one statement: `CREATE SCHEMA IF NOT EXISTS recallatron`, never a bare
  `CREATE SCHEMA recallatron`. **That creation is owned by
  `packages/core/src/rheo_core/migrations/orchestrator.py:162`**, which runs it under the advisory
  lock taken at `:161` — before `command.upgrade` at `:170` invokes this revision — so by the time
  Alembic reaches the file, `recallatron` already exists and the bare form raises `DuplicateSchema`
  on every fresh install. The revision's own statement is an idempotent backstop, keeping the chain
  correct if it is ever applied through a bare `alembic upgrade`; the `IF NOT EXISTS` is
  load-bearing, not defensive noise, and a comment in the file says so.
- **New** `modules/recallatron/tests/test_contract.py` — the minimal contract suite (Open Question
  3): the manifest validates, `module_id == "recallatron"`, its migrations package resolves through
  `importlib.resources` and holds `env.py`. No database, so no conftest is needed under `modules/`.
- `pyproject.toml` — `testpaths` (`:74`) becomes `["tests", "modules"]`.
- `uv.lock` — regenerated by `uv sync` for the new dependency edges.

**Closes:** **FR 13**, **FR 3's** skeleton half; half of AC 1's "lives entirely under
`modules/recallatron/`" clause.

**Checkpoint:** `uv sync && uv run pytest modules/recallatron tests/test_module_loader.py`, the
four `make` gates. `make check` matters here: `scripts/check_repository.py` polices tracked/ignored
paths and the new directories must land on the right side of it.

---

## Phase 5 — Module migration chains in the orchestrator — [The Systemsmith]

**Goal:** `run_module_chain` accepts a module chain, resolves its script directory and version table
from the loaded manifest, runs under the same advisory lock as the core chain, writes into
`alembic_version_<module_id>` inside the module's own schema, and **raises to its caller** —
while `migrate_workspace` keeps refusing a module chain, which is what keeps a module's migration
failure away from the control plane (spec.md § Decision F, **FR 11**).

**Deliverables:**

- `packages/core/src/rheo_core/migrations/orchestrator.py` —
  - a `version_table_for(chain)` helper returning `(CORE_SCHEMA, CORE_VERSION_TABLE)` /
    `(CONTROL_SCHEMA, CONTROL_VERSION_TABLE)` from `_VERSION_TABLES` (`:75-78`) and
    `(module_id, f"alembic_version_{module_id}")` otherwise. **Three call sites read through it,
    not one:** `_check_chain` (`:82-85`), whose membership test is what is being widened;
    `recorded_revisions` (`:118`); and **`run_chain` (`:153`)**, whose bare
    `_VERSION_TABLES[_check_chain(chain)]` would `KeyError` on a module chain the moment
    `_check_chain` stops rejecting one. Missing `:153` is the defect that would turn every module
    install into a `KeyError` at the first `CREATE SCHEMA`.
  - `script_location` (`:88-95`) grows a module branch resolving
    `loaded_manifests()[chain].storage.migrations_path` through `importlib.resources`, keeping the
    existing `env.py`-must-exist assertion.
  - `run_chain` (`:144-170`) is otherwise unchanged — same `advisory_lock(connection,
    ADVISORY_LOCK_KEY)` (`:161`), same `CREATE SCHEMA IF NOT EXISTS <schema>` (`:162`, which now
    creates the *module's* schema because `:153` resolved it), same `schema_ahead` check. A module
    chain therefore takes the *same* lock the core chain does, which is what FR 9 asks for.
  - `migrate_workspace` (`:200-220`) — **the `NotImplementedError` at `:217` becomes a `ValueError`
    naming `run_module_chain`; it does not go.** Deleting it is what opens the path FR 11 forbids:
    the `try` at `:227` wraps `for chain in chains: run_chain(...)` at `:229-230` and its bare
    `except Exception` at `:235` already falls through, **generically over `chains`**, to the
    `set_workspace_state(..., UNAVAILABLE, ...)` write at `:239-245`. There is nothing to
    "generalise" and nothing to guard inside that branch — the pre-loop guard *is* the guard. (`:238`
    and `:254` also hardcode `MigrationResult(chain=CORE_CHAIN)`, so a module chain run there would
    misreport itself.) New message: `"module migration chain {chain!r} does not run through
    migrate_workspace; call run_module_chain from core.module.install, which surfaces a failure to
    its caller instead of marking the workspace unavailable"`. `ValueError` rather than
    `NotImplementedError` because this is a routing rule, not an unbuilt feature — the same class
    `:216` already raises for the control chain on a workspace database.
  - `:239-245` is untouched. Nothing about it changes in this run.
  - the module docstring's "Module chains … are phase 2" paragraph (`:24-26`) is rewritten to state
    the two-door rule: `migrate_workspace` for the unattended startup pass over `core`,
    `run_module_chain` for a caller-initiated module install.
- **New** `packages/core/src/rheo_core/migrations/module_chain.py` *or* a function beside
  `run_chain` — `run_module_chain(connection, manifest, *, expected_database, core_version)` which
  runs the chain and appends one `core.module_schema_version` row per newly applied revision.
  **Enumerating "newly applied":** Alembic's version table records the head, not the history
  (`recorded_revisions`, `:116-126`, reads it), so snapshot `recorded_revisions(connection, chain)`
  before `run_chain` and after, then walk
  `ScriptDirectory.from_config(build_config(chain)).iterate_revisions(<after heads>, <before heads,
  or "base" when the table was absent>)`; every revision the walk yields gets one row, written oldest
  first, `schema_version` = the revision id (a `Text` column, `core_tables.py:93`), `applied_at` =
  now, `core_version_at_apply` = the `core_version` argument, which phase 6 passes as
  `storage.provisioning.core_version()` — what `exports/artifact.py:553`/`:575` already writes to
  that column. One revision → one row (AC 10); a later multi-revision chain → one row each, from
  the same code. It has **no `try`, no `MigrationResult` and no control-plane engine**: it raises
  the chain's own exception, and `core.module.install`'s job handler (phase 6) is what re-raises it
  as `ModuleInstallFailed("migration_failed", …)` and lets the transaction roll back. This function —
  not `migrate_workspace` — is where the `core.module_schema_version` row AC 10 asks about is
  written; `module_schema_version` appears in `orchestrator.py` today only in the docstring at `:24`.
- `packages/core/src/rheo_core/migrations/orchestrator.py` — promote `_error_text` (`:184-189`) to a
  public `error_text`, body unchanged, so phase 6's `migration_failed` detail is rendered the way
  `migrate_workspace` already renders `state_detail` (type name plus message, `DBAPIError.orig`
  unwrapped) rather than by a second copy.
- `packages/core/src/rheo_core/storage/repositories.py` — `insert_module_schema_version`
  (**FR 10's** `core.module_schema_version` writer; its sole-writer clause is enforced in phase 6,
  where the second table's writers land and the scan that asserts exclusivity is built).
- **New** `tests/postgres/test_module_migrations.py` — AC 10: a fresh workspace, call
  `run_module_chain` for `recallatron`, assert `alembic_version_recallatron` exists inside the
  `recallatron` schema, the core's own version table is untouched, and exactly one
  `core.module_schema_version` row exists for the module. Plus **FR 11's positive control**:
  `migrate_workspace(backend, workspace_id, chains=("recallatron",))` raises `ValueError` whose
  message names `run_module_chain`, and the `control.workspace` row is unchanged by that call. A
  test that only asserted "no `NotImplementedError`" would pass against an implementation that
  admits a module chain to `migrate_workspace` — which is the FR 11 violation — so it asserts the
  refusal instead.

**Closes:** AC 10; the `module_schema_version` half of AC 7 (F15) and of **FR 10**; **FR 11's** structural half (no
module-chain path to the `unavailable` write, proved here by the refusal control — the behavioural
test is phase 6's AC 12 case).

**Checkpoint:** `uv run pytest tests/postgres/test_module_migrations.py tests/postgres/test_migrations.py
tests/postgres/test_provisioning.py -m postgres`, the four `make` gates.

---

## Phase 6 — `core.module.install` — [The Systemsmith]

**Goal:** a workspace can install Recallatron through one dispatched operation, every refusal is
named (**FR 7**), and a forced migration failure is a failed operation record that leaves
`control.workspace.state` untouched (**FR 11**).

**Deliverables:**

- `packages/core/src/rheo_core/storage/repositories.py` — `insert_module_state` and
  `set_module_state` (**FR 10's** `core.module_state` writers); update the module docstring's
  "writes arrive with module install in phase 2" sentence (`:5-6`).
- **New** `packages/core/src/rheo_core/modules/operations.py` — `ModuleInstallInput`,
  `ModuleInstallScheduled`, the declaration (`safety_class=MUTATE`, `roles={owner, operator}`,
  `idempotency=NONE`, `audit=AuditSpec(subject_field=None)`, `long_running=True`) and the handler
  carrying the **five** pre-flight steps of spec.md § System Components 5 — **FR 7** steps 1 and 2,
  Decision D's `module_already_installed` refusal, and (per Decision G) the profile-`test` resolution
  of `contract_tests`, refusing `contract_tests_missing` and naming the path. Each is an
  `OperationRefused(state, detail)`, which is what makes the state the caller's own `error_code`;
  `exports/operations.py:193`'s `artifact_missing` is the shipped precedent in a `long_running`
  handler. The `contract_tests` check reads the manifest and
  `rheo_core.storage.data_root.find_checkout_root()` (`:138-147`) and touches no database, so it
  belongs here rather than in the job.
- Same file — `ModuleInstallPayload` and `run_module_install_job`, the job handler carrying **FR 7**
  steps 3–5 plus the non-`test`-profile half of step 6: `CREATE EXTENSION IF NOT EXISTS` per
  `storage.required_extensions` **before** any migration statement, `run_module_chain`,
  `insert_module_state`, then every `health_checks` callable. Each failure raises
  `ModuleInstallFailed(code, detail)` whose `str()` is `f"{code}: {detail}"` — **three codes**:
  `extension_failed` (step 3, naming the extension), `migration_failed` (step 4), and
  `health_check_failed` (step 6, naming the check) — because the worker loop has exactly one error
  code for every job failure (`work/loop.py:146`'s `JOB_FAILED`, "one code for all four routes")
  and the discrimination has to live in `error_text`, which is the job's `last_error` verbatim.
  **`migration_failed` is a wrap, not a chain change:** the handler's one `try` surrounds the
  `run_module_chain(...)` call and re-raises any exception as
  `ModuleInstallFailed("migration_failed", orchestrator.error_text(exc)) from exc`, so the chain's
  own `StorageRefusal(schema_ahead, …)`, revision error or `DBAPIError` keeps its rendering and the
  raise still propagates for the rollback (spec.md § Decision G). It is **never** relabelled
  `extension_failed`: by step 4 the extensions step has already succeeded, and a test asserting the
  extension name on a migration failure would pass only against an implementation that mislabels
  the event. **Do not widen `JOB_FAILED`** — it is a shipped write path shared by every job kind.
  FR 7 step 7 ("`configuration_schema` keys stay settable") needs no code here: the module's
  `KeySpec`s were registered at load in phase 3, so nothing is written at install.
- Same file — the `enqueue_job` call passes **`max_attempts=1`**, named here because
  `work/jobs.py:150` makes it a required keyword and no requirement named a value. Not
  `resolve().get_int("work.max_attempts")` (8, what `exports/operations.py:168`, `:220` and
  `runtime/operations.py:316` pass): every failure reachable in this job is deterministic, and the
  loop grants "exactly `max_attempts` runs" (`loop.py:387-391`, `:647`), so 8 would make an operator
  wait through seven pointless retries under a backoff of up to an hour. One run, then `failed`,
  with the named detail in `error_text`.
- `packages/core/src/rheo_core/operations/core_ops.py` — register the declaration in
  `register_core_operations` (`:505`). It cannot go in the module-level `CORE_OPERATIONS` tuple
  (`:483`) if importing `rheo_core.modules` from `rheo_core.operations` closes a cycle; follow the
  deferred-import pattern the token and approval operations already use (`:556-566`).
- `apps/worker/src/rheo_app_worker/main.py` — register the `core.module.install` job kind beside
  the four at `:54-65`.
- `tests/harness/registry.py` — rewrite `enable_harness_module` (`:638-654`, a hand-built
  `pg_insert(core_tables.module_state)`) to call `insert_module_state` (**FR 10**); **do not delete
  it** — 18 files reference it, 15 of them under `tests/postgres/`
  (`grep -rln enable_harness_module tests/`).
- `packages/core/src/rheo_core/exports/artifact.py` — **`_install_modules` (`:550-576`) is the
  export-restore path's hand-built writer of *both* tables**: `insert(core_tables.module_state)` at
  `:558` and `insert(core_tables.module_schema_version)` at `:571`, production code outside
  `repositories.py` and outside any migration's DDL. Route both through `insert_module_state` and
  `insert_module_schema_version`; the signatures spec.md § Data Models gives take exactly the
  columns this call site supplies (`state`, `installed_at`, `enabled_at`, `state_detail`;
  `schema_version`, `applied_at`, `core_version_at_apply`), so no signature widens. Restore is the
  third caller **FR 10** names beside install and enable; the writer clause — one module owns the
  write path — is the absolute one, and leaving these inserts in place would break it.
- **New** `tests/test_module_state_writers.py` — **FR 10's** exclusivity clause and AC 7's second
  clause, which no deliverable asserted before: a static scan over `packages/`, `apps/` and `tests/`
  finding no `insert(...)`/`pg_insert(...)` construction against `core_tables.module_state` or
  `core_tables.module_schema_version` outside `repositories.py` and the migrations' own DDL. The
  exception set is declared and asserted both ways, as `tests/test_absent_behaviour.py` already
  does, so a stale exclusion fails too. **One known live hit to resolve rather than except:**
  `tests/postgres/test_export_restore.py:221` builds an
  `insert(core_tables.module_schema_version)` to seed a source workspace — rewrite it to call
  `insert_module_schema_version` in the same diff; the scan is worth little if the first real hit
  is waived.
- **New** `tests/harness/modules.py` — the fixture manifests: one declaring a real
  `required_extensions` entry, one declaring an extension the test role cannot create, and a
  two-module pair for the dependency cases.
- `tests/postgres/test_audit_dispatch.py` — **three edits, not one**, all forced by registering
  `core.module.install`: (i) add it to `THE_SEVENTEEN` (`:119`), which the exact-set assertions at
  `:330` and `:442` compare against; (ii) `:331`'s separate `assert len(mutating) == 17` becomes
  `18` (phase 7 makes it `19`); (iii) `:333`'s
  `test_one_dispatch_of_every_mutating_kind_leaves_a_matching_audit_record` builds a `dispatched`
  map with one real dispatch per mutating operation and asserts at `:445-450` that none of them
  refused `OPERATION_UNKNOWN`, `ROLE_NOT_PERMITTED`, `MODULE_DISABLED` or `AUDIT_SINK_MISSING`, so
  it needs an entry for `core.module.install` whose outcome is one of the operation's *own*
  refusals or a success. Dispatching `module_id="no_such_module"` refuses `module_unavailable` at
  pre-flight — a refused record, audited (`:345-351`: "a call that was refused before its handler is
  exactly the case an audit trail must not be silent about"), and it needs no worker. **Do not
  rename the constant or the test** (spec.md § Decision H):
  `test_the_mutating_set_derived_from_the_registry_is_the_declared_seventeen` is a criterion-14
  demonstrator at `docs/acceptance/phase-1-matrix.md:909`, and `tests/test_acceptance_matrix.py:245-257`
  resolves every demonstrator against `pytest --collect-only` — so a rename reds `main` **and**
  silently drops a demonstrator from the phase-8 absence proof's own selection. The staleness is
  answered by amending `THE_SEVENTEEN`'s docstring to record that the name is pinned by an
  acceptance record, the way `tests/test_absent_behaviour.py:30-34` already does for its own probe
  names.
- **New** `tests/postgres/test_module_install.py` — AC 11 (both extension cases, and that no
  migration step ran on the failure), AC 12 (forced chain failure → failed operation record,
  `control.workspace` row unchanged), edge cases 1, 2 (the repeat-install refusal naming the
  state), 3, 5, 7. **Forcing the AC 12 failure:** before dispatching install, create the
  `recallatron` schema and its `alembic_version_recallatron` table in the workspace database and
  insert a `version_num` this code does not know; `run_chain`'s `schema_ahead` check (`:163-169`)
  then raises `StorageRefusal(SCHEMA_AHEAD, …)` from inside `run_module_chain` — one of the three
  conditions FR 11 names, and it needs no fixture module. Three assertion shapes matter here and
  are easy to get wrong:
  - **The AC 12 case is FR 11's whole proof and is a *negative* assertion**, so it must read the
    `control.workspace` row before and after and compare it, not merely assert the install failed:
    an implementation that flips the row to `unavailable` still fails the install, and a test that
    only checks the outcome passes anyway.
  - **The in-job failures assert on `error_text`, not `error_code`.** `extension_failed`,
    `migration_failed` and `health_check_failed` arrive as `error_code="job_failed"` with
    `error_text="<code>: <detail>"` — the loop's single code plus the raised detail. Only the five
    pre-flight refusals (`module_unavailable`, `module_already_installed`, `dependency_missing`,
    `dependency_version`, `contract_tests_missing`) are their own `error_code`. Asserting
    `error_code == "extension_failed"` will fail, and "fixing" it by widening `JOB_FAILED` is the
    wrong repair.
  - **AC 12's name is `migration_failed`, not `extension_failed`.** The assertion is
    `error_text.startswith("migration_failed: ")` and `"schema_ahead" in error_text` — for the
    forced case above the full text is `"migration_failed: StorageRefusal: schema_ahead: database
    'ws_…' records recallatron revision(s) […] that this code does not know"`. A test asserting
    `"extension_failed: "` on a migration failure passes only against an implementation that
    mislabels the event.

**Closes:** AC 7 (F15), AC 11, AC 12, and the install half of AC 1; **FR 7**, **FR 10**, **FR 11**.

**Checkpoint:** `uv run pytest tests/postgres/test_module_install.py
tests/test_module_state_writers.py tests/postgres/test_audit_dispatch.py
tests/postgres/test_workspace_status.py tests/postgres/test_export_restore.py
tests/postgres/test_worker_loop.py -m postgres`, the four `make` gates.

---

## Phase 7 — `core.module.enable`, and criterion 24 end to end — [The Systemsmith]

**Goal:** install then enable Recallatron into a fresh workspace through those two operations
alone, and `core.workspace.status` reports its version, state and schema version.

**Deliverables:**

- `packages/core/src/rheo_core/storage/repositories.py` — `insert_schedule_if_absent`, mirroring
  the one `core.schedule` insert that exists today
  (`packages/core/src/rheo_core/migrations/core/versions/0006_runtime.py:34-42`).
- `packages/core/src/rheo_core/modules/operations.py` — `ModuleEnableInput`, `ModuleEnabled`, the
  declaration (`MUTATE`, `{owner, operator}`, **not** long-running) and the four-step handler,
  **FR 8** step for step:
  state check naming the actual state; dependency check naming the dependency; the
  `explicit_per_workspace` settings rows through `repositories.insert_workspace_setting_if_absent`
  + `settings.encode_text`, exactly as `packages/core/src/rheo_core/storage/provisioning.py:229-236`
  does; the `enabled_by_default` schedule rows; `set_module_state("enabled")`.
- `packages/core/src/rheo_core/operations/core_ops.py` — register it beside install.
- `tests/postgres/test_audit_dispatch.py` — `core.module.enable` joins the inventory, which now
  holds nineteen names: the frozenset at `:119`, `:331`'s length assertion (`18` → `19`), and a
  `dispatched` entry in `:333`'s per-kind dispatch test — `module_id="no_such_module"` refuses
  `module_state_invalid` naming state `absent`, an audited refusal outside the four that test
  excludes. **`THE_SEVENTEEN` and its test keep their names** (spec.md § Decision H, and
  the note in phase 6): the test name is a criterion-14 demonstrator at
  `docs/acceptance/phase-1-matrix.md:909`, so renaming it reds `main` on the matrix check and
  silently shrinks the phase-8 absence proof's selection. Widen the frozenset, extend the docstring,
  and do not narrow the assertion.
- **New** `tests/postgres/test_module_enable.py` — AC 13's four steps, each independently; that is
  **FR 8** steps 1–4, including the final clause that `WorkspaceContext.enabled_modules` contains
  the module from the next request (read through `boundary/factories.py:86-88`, which needs no
  change — only a row to read).
- **New** `tests/postgres/test_module_lifecycle.py` — AC 1: a fresh workspace, dispatch
  `core.module.install`, drive the worker loop to completion, dispatch `core.module.enable`, then
  assert `core.workspace.status` lists `recallatron` with a non-null `package_version`,
  `state == "enabled"` and a non-null `schema_version`. AC 1's "no file under `packages/core/` or
  `packages/contracts/` is edited" clause is not a `git diff` in this test: it is proved by phase 8's
  `tests/test_module_import_graph.py` (no file under `packages/`, `apps/` or `tests/` names the
  distribution) together with the entry point, manifest, migration and tests living under
  `modules/recallatron/` (phase 4).
- `docs/architecture/module-contract.md` — **corrections 7 and 8**: the `contract_tests` execution
  deviation beside install step 6 (`:227`), including Decision G's note that the resolution happens
  at pre-flight so `contract_tests_missing` is the caller's own refusal code; and the partial-closure
  sentence for criterion 24 beside `:240-242` naming 1a3 as the owner of the interface-contribution
  half.

**Closes:** AC 1 (public 24, except the carried interface-contribution half), AC 13; **FR 8**.

**Checkpoint:** `uv run pytest tests/postgres/test_module_lifecycle.py
tests/postgres/test_module_enable.py tests/postgres/test_workspace_status.py -m postgres`, the four
`make` gates.

---

## Phase 8 — The two boundary mechanisms and the absence proof — [The Systemsmith]

**Goal:** the construction gates cover `modules/*/src`, the storage-ownership property is checked
by its own mechanism (**FR 12** — two mechanisms, two files, separately verifiable), and criterion
33 is proved by two configurations diffed (**FR 14**).

**Deliverables:**

- `tests/test_boundary.py` — `_SCAN_DIRS` (`:83`) gains `"modules"` (**FR 12**, the B15
  `WorkspaceContext`-construction gate).
- `tests/test_boundary_scans.py` — `_SCAN_DIRS` (`:30`) gains `"modules"` (**FR 12**, the B9
  `SecretScope`-construction gate), **and** the scan at `:204` drops its now-redundant
  `(*_SCAN_DIRS, "modules")` in favour of `_SCAN_DIRS`.
- **New** `tests/fixtures/modules/` (or an in-test temporary file) — the positive-control fixture
  under a `modules/`-shaped path that *does* construct a `WorkspaceContext` and a `SecretScope`,
  driven through the `harness.absence` helpers the repo already requires
  (`tests/test_absent_behaviour.py:24-28`), so a scan pointed at a path that does not exist fails
  its control rather than passing empty.
- **New** `tests/postgres/test_module_storage_ownership.py` — AC 3 (public 26) and the other half
  of **FR 12**: criterion 26's storage-ownership property gets its own file and its own mechanism,
  so neither it nor the `_SCAN_DIRS` gates above can stand in for the other. Three separate
  assertions: (i) snapshot `pg_class`/`information_schema` across the `recallatron` chain and
  assert every new relation is in the `recallatron` schema; (ii) no file under
  `modules/recallatron/src` names another module's schema-qualified table or storage symbols;
  (iii) no file under `modules/recallatron/src` constructs a connection, engine or DSN.
- **New** `scripts/absence_proof.py` — **FR 14's** two configurations and the comparison, four
  modes:
  - `--mode run --out <summary.json>` — the inner step both configurations share, executed by
    *that configuration's own interpreter*: compute `selected`, collect, run, write the summary.
    In-process — `pytest.main([...], plugins=[recorder])` with a recorder object implementing
    `pytest_collection_finish` and `pytest_runtest_logreport` — because a subprocess `-p` plugin
    would have to be importable from the throwaway worktree too. `parse` is loaded from
    `tests/test_acceptance_matrix.py` by path (`importlib.util.spec_from_file_location`); `tests/`
    is not an installed package.
  - `--mode config` — Recallatron on disk and in the environment, `modules.installed` resolving to
    a value that never names it (the test profile's default, `()`), then `run` here.
  - `--mode checkout` — `git worktree add --detach <tmp> HEAD`, delete `modules/recallatron/` in
    the copy, `uv sync` **without `--frozen`** (the committed lock names `rheo-recallatron`; the
    root is a virtual workspace whose `modules/*` member glob, `pyproject.toml:9`, simply stops
    matching — no file edit is needed), then `uv run --directory <tmp> python
    scripts/absence_proof.py --mode run` inside it, and `git worktree remove --force` after. The
    developer's tree is never mutated, and `tests/test_git_clean.py`'s demonstrator is a
    before/after set difference (`:3-8`), so the copy's dirty `uv.lock` does not fail it.
  - `--mode compare <a.json> <b.json>` — non-zero on any difference, per control 4 below.
- **The selection (spec.md § Decision E), stated so it can be built:**
  `selected = {d.removeprefix("pytest:") for row in parse(matrix) for d in row.demonstrators if
  d.startswith("pytest:")}` — 102 unique node ids on this tree (103 entries, one listed under two
  criteria), 82 of them under `tests/postgres/`; verified today: `pytest --collect-only -q` over the
  29 files they name resolves every one of them — 143 collected items, because 8 base ids are
  parametrised (`tests/test_routing.py::test_url_for_matches_the_fixture` among them), which is
  why control 2 uses `_pytest_resolves`' `<id>[` rule and not set equality. The `ci:` demonstrators (8
  entries, 7 unique) are workflow `job / step` pairs, not pytest items; they are recorded under
  `ci_excluded` and not run. Any other kind exits non-zero. Never the whole `tests/` tree, because
  this run's own phase-two tests need Recallatron on disk and would turn configuration B red for a
  reason that proves nothing. `collected` is every collected node id resolving to a selected id by
  `_pytest_resolves`' rule (`:208-212`); `outcomes` maps each collected id to
  `passed`/`failed`/`error`/`skipped`. A summary is `{selected, collected, outcomes, ci_excluded}` —
  never a bare count.
- **Both configurations need the cluster.** 82 of the 102 selected ids are `postgres`-marked,
  `pyproject.toml:73-76` sets no default `-m` filter, and `tests/conftest.py:157-161` `pytest.exit`s
  rather than skips when `RHEO_TEST_CLUSTER_DSN` is unreachable. `make absence-proof` inherits the
  developer's exported DSN (the gate block at the top of this plan); the CI job below carries its
  own.
- **The proof's controls, without which it is a test that cannot fail** (spec.md § Decision E):
  1. every mode exits non-zero when `selected` is empty;
  2. every mode exits non-zero unless every selected id resolves into `collected` and every
     collected id maps back to a selected id, naming the difference — what catches a renamed or
     deleted demonstrator (see phase 6's `THE_SEVENTEEN` note);
  3. every mode exits non-zero unless `set(outcomes) == collected` (a `pytest.exit` mid-session
     leaves outcomes missing) and no outcome is `skipped`;
  4. `--mode compare` exits non-zero unless both summaries carry the same non-empty `selected`, that
     set equals what `parse` yields at compare time, both `collected` sets are equal, and the two
     `outcomes` maps are identical key by key; it prints every non-`passed` outcome. A `failed`
     identical on both sides is not a boundary difference and does not by itself red the compare —
     `make test` and the `python` job own "the matrix is green".
- **New** `tests/test_absence_proof.py` — the mutation that proves the controls are load-bearing,
  in the shape `tests/test_absent_behaviour.py`'s `test_the_positive_controls_are_live` already
  uses: hand-built summaries, no database and no subprocess pytest run — one pair differing in a
  single node id's outcome, one with an empty `selected`, one where a selected id is not in
  `collected`, one where an outcome is missing, one where an outcome is `skipped` — and assert each
  exits non-zero.
- `Makefile` — an `absence-proof` target running `config`, `checkout`, `compare` in order.
- `.github/workflows/repository-checks.yml` — an `absence-proof` job beside the existing
  `repository-checks`, `docker`, `python` and `web` jobs. **It copies the `python` job's
  `services: postgres` block and its `RHEO_TEST_CLUSTER_DSN` env (`:50-63`) verbatim** — without
  them 82 of the 102 demonstrators `pytest.exit` in both configurations and control 3 reds the job
  — plus the same checkout and `uv sync --frozen` steps, then `make absence-proof`.
- **New** `tests/test_module_import_graph.py` — **FR 14's** third, cheap check and
  `module-contract.md:282-283`'s "import-graph check in CI": no file under `packages/`, `apps/`
  **or `tests/`** names `rheo_recallatron` or `modules/recallatron`, with the phase-two test files
  that drive it as the declared, asserted-both-ways exception set. `tests/` is in the scanned set
  deliberately — FR 14 names all three roots, and an exception set of test files is meaningless if
  the root those files live under is never walked.

**Closes:** AC 3 (public 26), AC 4 (public 33), AC 9 (F18); **FR 12**, **FR 14**.

- `docs/architecture/module-contract.md` — **correction 10**: `:285`'s stale "the test profile for
  the no-memory run sets it to `relationships, leads`" is replaced by the two-configuration proof.
  Neither `modules/relationships/` nor `modules/leads/` is a distribution (each holds a one-line
  `__init__.py` and no entry point), and with `modules.installed` defaulting to `()` naming two
  non-existent ids and naming none are the same configuration.

**Checkpoint:** `uv run pytest tests/test_boundary.py tests/test_boundary_scans.py
tests/test_module_import_graph.py tests/test_absence_proof.py
tests/postgres/test_module_storage_ownership.py`, then `make absence-proof`, then the four `make`
gates.

---

## Dependencies & Sequencing Notes

- **1 before everything.** Every later phase constructs a `ModuleManifest`; until the model and its
  declaration types exist there is nothing to construct. Phase 1 also carries the rebuild of the
  existing fixtures, which is why it must be green on its own before phase 2 starts.
- **2 before 3.** Phase 3's `configuration_schema` registration calls `SettingsRegistry.register`
  under a module origin; phase 2 is where the settings story for modules is settled and where
  `loaded_manifests()` appears, which phases 5 and 6 both read.
- **4 before 5.** The orchestrator's module branch resolves `storage.migrations_path` against a
  loaded manifest; Recallatron's packaged migration environment is the first real one to resolve.
- **5 before 6.** Install calls `run_module_chain`.
- **6 before 7.** Enable refuses unless the state is `installed`, so nothing can enable until
  something can install; and AC 1's end-to-end test needs both.
- **8 last, but its `_SCAN_DIRS` change can move earlier if a reviewer wants the gates live while
  module code is being written.** It is placed last because the gates only have something to catch
  once `modules/recallatron/src` holds real code (phase 4), and because the absence proof needs the
  phase-two tests to exist before it can prove they are excluded from the phase-one selection.
- **Two pre-existing writers come with phase 6, and they are not scope creep.** FR 10's sole-writer
  property is false on the tree as it stands: `exports/artifact.py:550-576` (production restore) and
  `tests/postgres/test_export_restore.py:221` both hand-build inserts against the two tables. Both
  are rewritten onto the repository writers in phase 6, alongside `enable_harness_module`, because
  that is the phase where `insert_module_state` exists and where the exclusivity scan is built —
  landing the scan without them would put a red test on `main`.
- **`make codegen` is a gate in phases 2 and 3 only.** Both change what `rheo openapi` loads. The
  generated artifacts under `apps/web/src/generated/` must come back byte-identical; a diff there
  means the loader activated something the canonical generation environment should not have.
- **`uv.lock` changes in phases 1 and 4, and nowhere else.** Phase 1 adds `packaging` to
  `rheo-core`'s dependencies (Decision C, deviation 5 — the PEP 440 range comparator); phase 4 adds
  Recallatron's dependency edges. A lock change in any other phase means a dependency was added that
  no deliverable asked for.
- **The eleven `module-contract.md` corrections have owning phases, so none is orphaned.** Phase 1
  owns 1, 2, 4, 9 and 11; phase 2 owns 5; phase 3 owns 3 and 6; phase 7 owns 7 and 8; phase 8 owns
  10. The table in spec.md § ADR Records is the register.
- **Out-of-scope items registered rather than implied:** module disable/remove/purge/restore and
  any stub of them (phase seven, D5); the memory schema, retrieval, embeddings and EverAlgo (1a1,
  1a2); the `web` contribution, navigation, theme and the naming/fixture CI gates (1a3); M.O.T.
  migration (1b); an `EventRegistry` and a `publish()` gate over undeclared event types (carried to
  whichever run first ships a module that publishes one); a `docs/acceptance/phase-2-matrix.md`
  (nothing records a phase-two criterion's state today —
  `tests/test_acceptance_matrix.py:34` covers 1–23 and 69 only).
