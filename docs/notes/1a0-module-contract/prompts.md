# Scoped Prompts — Rheo Stream 1a0, module contract: manifest, install, enable

> Execute these prompts in sequence in Claude Code, inside this run's worktree
> checkout of `rheo-stream`. Each prompt assumes the previous has been completed,
> is green on its own checkpoint, and is committed. Do not skip prompts or combine
> them — the eight-phase split in `plan.md` is intentional, and phase 1 in
> particular must be green standalone (it carries its own fixture migration).
>
> Every prompt below is tagged **Coder: The Systemsmith** — all eight phases in
> `plan.md` are tagged `[The Systemsmith]`, and the build stage dispatches on that
> tag. A missing or wrong tag is a defect in this file, not a free choice.

---

## Standing constraints — true for every prompt below

The **Scope** paragraph immediately below is restated verbatim inside every one of
the eight fenced prompt blocks, because a coder pastes one fenced block at a time
and may never see this section. The other three constraints here (Postgres DSN,
gates, refusal vocabulary) are named per-prompt where each applies, and are
collected here once for a human reading the whole file.

**Scope is `state.json#scope`, and it is authoritative.** Read
`RUN_DIR/state.json`'s `scope.allowed_paths` / `scope.denied_paths` before your
first edit. Denied for the whole run: `apps/web/**`, `apps/mcp/**` (criterion 24's
interface-contribution half belongs to run 1a3); `modules/relationships/**` and
`modules/leads/**`; `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md` and
`docs/acceptance/phase-1-matrix.md` — the criteria this run is judged against,
read-only inputs, never edited; `deploy/**`; `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and **appears in
neither list** — out of scope exactly as the other two placeholders are, and it
gets no entry point, no dependency edge and no edit in this run. A path in neither
list is out of scope: raise it rather than widen the guard.
`apps/core/src/rheo_app_core/startup.py` and
`apps/cli/src/rheo_app_cli/commands/openapi.py` are allowed **only** for removing
`RHEO_MODULES` docstring prose — no other change there is in scope.

**Postgres: this host's cluster is on port 5433, and there is no `.env` here.**
Every prompt's `make test` gate reaches Postgres-marked tests regardless of which
phase you are in, so export the DSN before running any checkpoint:

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
```

`.env.example` and `tests/conftest.py`'s `DEFAULT_TEST_CLUSTER_DSN` both still say
5432 — that is a *different*, foreign Postgres cluster on this host that will
reject the `rheo` role, not a repository defect. Never "fix" the port in
`.env.example` or `tests/conftest.py`; `tests/conftest.py` is out of scope for this
run in any case.

**Gates are `make` targets, run from the worktree root:** `make lint`,
`make typecheck`, `make test`, `make check`, and (phases 2, 3, 4 and 8 only)
`make codegen` / `make absence-proof`. `make migrate` is never run by any phase —
module migration chains are exercised through the test fixtures against the test
cluster, not through the operator CLI.

**The refusal vocabulary is exact — do not improvise wording.** `core.module.install`
has five pre-flight refusals, each its own `error_code`: `module_unavailable`,
`module_already_installed`, `dependency_missing`, `dependency_version`,
`contract_tests_missing`. Its in-job failures all arrive as
`error_code="job_failed"` with one of three `error_text` prefixes:
`extension_failed`, `migration_failed`, `health_check_failed`. `AC 11` and `AC 12`
assert on this split exactly.

**How a fixture manifest reaches `loaded_manifests()` — the one shipped recipe,
needed by Prompts 2, 3, 6 and 7.** Nothing in the checkout publishes a real
`rheo.modules` entry point until Prompt 4, and even after it Recallatron is the
only one. Every fixture module therefore goes through the mechanism
`tests/test_module_loader.py` already ships, and no prompt may invent a second:

- Build a real `importlib.metadata.EntryPoint` whose `value` points back at a
  module-level name in the test file itself —
  `EntryPoint(name=MODULE_ID, value=f"{__name__}:MANIFEST", group=ENTRY_POINT_GROUP)`
  (`tests/test_module_loader.py:141-143`), so `EntryPoint.load()` runs for real
  rather than being stubbed out alongside the discovery it exercises.
- Publish it with the `_publish(monkeypatch, *entry_points)` helper
  (`tests/test_module_loader.py:194-198`), which monkeypatches
  `rheo_core.modules.loader.entry_points` to a lambda returning exactly those —
  so the ambient environment's real entry points are invisible for that test.
- **Fixture module ids are `_probe`-suffixed**, never a plausible product name
  (`tests/test_module_loader.py:20-24`): a fixture that borrowed the id of a
  distribution shipped later would collide with it, and
  `test_the_fixture_id_cannot_collide_with_a_real_distribution` pins that against
  the real, unmonkeypatched environment. `recallatron` is a real distribution from
  Prompt 4 on and is never a fixture id.
- The loader's `_LOADED` / sink tables are process-wide. Reset them either side of
  every test that loads one, the way `clean_process_state`
  (`tests/test_module_loader.py:178-185`) already does with
  `reset_surfaces()` + `reset_sinks()`.

---

## Prompt 1 — The manifest contract, and its doc corrections
**Phase:** Plan Phase 1 — The manifest contract (and the doc correction)
**Coder:** The Systemsmith
**Execution-profile:** role-default
**Produces:** `rheo_core.modules.ModuleManifest` is a frozen pydantic model
carrying all 24 fields (the 23 ratified plus `audit_sink`); the hand-rolled
`validate()` is gone; every existing manifest construction in the tree builds
against the model; `docs/architecture/module-contract.md` carries seven
corrections. No install/enable behavior yet — this prompt is the contract only.
**Depends on:** nothing — start here.
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/modules/{manifest.py,__init__.py,loader.py}`,
`packages/core/pyproject.toml`, `tests/test_module_loader.py`, new
`tests/test_module_manifest.py`, `docs/architecture/module-contract.md`.
Expected generated churn — `uv.lock` only (the `packaging` pin; phases 1 and 4 are
the only two allowed to change it). Stop boundary: `loader.py` changes **only**
its manifest-validation step — the `validate as validate_manifest` import
(`:45`) and its call site (`:109`), replaced by the local `_as_manifest` in
task 4. `_register`, `_SURFACES`, `ALLOWLIST_VARIABLE`, `discovered()`,
`load_modules()`'s signature and `WebSurface` all belong to later prompts or to
nobody. Do not touch `packages/contracts/**` beyond reading it.

````
You are working in the `rheo-stream` repository (existing project, Python 3.12,
pydantic v2, uv workspace). This implements spec.md's FR 1, FR 2, FR 3 (the model
half only — FR 3's skeleton half lands in a later prompt) and closes AC 2 and
AC 5, per plan.md Phase 1.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

Today `packages/core/src/rheo_core/modules/manifest.py` defines `ModuleManifest`
as a frozen dataclass with eight fields (`module_id`, `package_version`,
`schema_name`, `create_schema`, `operations`, `resolvers`, `web`, `audit_sink`)
and a hand-rolled `validate()` function that the loader calls. Read that file in
full before you touch it — `WebSurface` (the small frozen dataclass just above
`ModuleManifest`) is kept verbatim; do not touch it.

## Task

**1. Replace `ModuleManifest` with a frozen pydantic model, in the same file.**

```python
model_config = ConfigDict(frozen=True, extra="forbid")
```

Do **not** add `arbitrary_types_allowed`. Every field type below is one pydantic
v2 builds a schema for natively except one, and that flag's `isinstance` fallback
is exactly the mechanism that breaks it (see the trap below) — an unhandled type
must fail loudly at class-definition time with `PydanticSchemaGenerationError`,
never silently fall back to an `isinstance` probe.

The model carries exactly 24 fields. Only `web`, `agent_guidance`, and
`audit_sink` carry a default (`= None`); every other field — including the eight
whose *consumption* belongs to a later run (`record_types`, `events`,
`subscriptions`, `jobs`, `schedules`, `deletion_participants`,
`connector_bindings`, `sensitivity`) — is **required with no default**, so a
manifest must explicitly supply `()` / `{}` rather than omit them. Field list:

- `module_id: str`
- `package_version: str`
- `core_contract_versions: tuple[int, ...]`
- `dependencies: tuple[Dependency, ...]`
- `record_types: tuple[RecordType, ...]`
- `storage: StorageDeclaration`
- `configuration_schema: tuple[KeySpec, ...]` — **not a pydantic model class**; see
  the deviation note below.
- `operations: tuple[tuple[OperationDeclaration, Handler], ...]` — unchanged shape
  from today (`Handler` from `rheo_core.operations.registry`, `OperationDeclaration`
  from `rheo_contracts`); the handler travels beside the declaration because it is
  typed against `UnitOfWork`, which `rheo_contracts` may not import.
- `tools: tuple[ToolDeclaration, ...]` (`ToolDeclaration` already ships in
  `rheo_contracts.manifest`)
- `events: tuple[EventDeclaration, ...]`
- `subscriptions: tuple[ConsumerSubscription, ...]` (already ships in
  `rheo_core.events.consumers`)
- `jobs: tuple[JobKind, ...]`
- `schedules: tuple[Schedule, ...]`
- `resolvers: tuple[tuple[str, RecordResolver], ...]` — unchanged shape
- `deletion_participants: tuple[DeletionParticipant, ...]`
- `export: ExportDeclaration`
- `web: WebSurface | None = None`
- `agent_guidance: str | None = None`
- `secret_scopes: tuple[str, ...]`
- `connector_bindings: tuple[ConnectorBinding, ...]`
- `health_checks: tuple[HealthCheck, ...]`
- `contract_tests: str`
- `sensitivity: Mapping[str, Mapping[str, SensitivityTier]]`
- `audit_sink: Annotated[AuditSink, PlainValidator(_audit_sink)] | None = None` —
  see the trap below; this field is **not** in the ratified 23-field table in
  `docs/architecture/module-contract.md`. It is carried over from today's stub
  (`manifest.py:82`) because it is the only channel by which a module supplies the
  sink `loader.py`'s `_register` installs and `dispatch.py`'s
  `AUDIT_SINK_MISSING` refuses every above-`READ` operation without.

Ten new frozen types live in this same file beside `ModuleManifest`. Eight of the
ten are transcribed field-for-field from `docs/architecture/module-contract.md`'s
manifest table (read it before writing these). **Two deviate from the table on
purpose, and both deviations are deliberate — do not "correct" the code back to
the table, and do record them in the doc (corrections 6 and 7 in task 7):**

- `EventDeclaration` — the table's row (`module-contract.md:36`) spells the third
  field `data_model`; the type below spells it **`data`**. The shorter name is the
  one every later run will type and the one the namespacing check in Prompt 3
  reads past; the doc row is corrected to match rather than the code.
- `JobKind` — the table's row (`module-contract.md:38`) is
  `JobKind(name, handler, max_attempts, cancellable)` and omits an input model
  entirely. The type below **adds `input_model: type[BaseModel]`**, because
  `JobKindRegistry.register(kind, input_model, handler)`
  (`packages/core/src/rheo_core/work/kinds.py:69-71`) takes exactly those three
  positional arguments and a `JobKind` without an input model could not be
  registered at all. The doc row gains the field rather than the registry
  losing it.

```
Dependency(module_id: str, version_range: str, optional: bool = False)
RecordType(name: str, table: str, deletable: bool, delete_roles: frozenset[Role],
           exportable: bool, audience_field: str | None)
StorageDeclaration(schema_name: str, migrations_path: str,
                    required_extensions: tuple[str, ...])
EventDeclaration(type: str, schema_version: int, data: type[BaseModel])
JobKind(name: str, input_model: type[BaseModel], handler: JobHandler,
        max_attempts: int, cancellable: bool)
Schedule(name: str, job_kind: str, cron: str, enabled_by_default: bool)
DeletionParticipant(record_types: tuple[str, ...], handler: DeletionHandler)
ExportDeclaration(format_version: int, schema_path: str, exporter: Exporter,
                   importer: Importer)
ConnectorBinding(transport: str, service_operation: str, route: str | None)
SensitivityTier — a StrEnum with exactly three members: public, internal,
                   restricted
```

`Role` comes from `rheo_contracts.context`. `JobHandler` comes from
`rheo_core.work.kinds`. Add three callable aliases beside these types:
`HealthCheck = Callable[[UnitOfWork], None]` (narrow, because install step 6
calls it directly), and `DeletionHandler`/`Exporter`/`Importer` as
`Callable[..., object]`, each with a one-line docstring naming that no run in the
current plan reads it yet (a guessed narrower signature would be a shape a later
run must break; this one is a shape it can only narrow).

Add an `@model_validator(mode="after")` carrying exactly these rules, transcribed
from the stub's `validate()`:
- `module_id` is a lowercase identifier (`str.isidentifier()` and
  `module_id == module_id.lower()`).
- `storage.schema_name == module_id`.
- `not is_reserved_module(module_id)` — import from
  `packages/contracts/src/rheo_contracts/refs.py`.
- every `KeySpec.key` in `configuration_schema` starts with `f"{module_id}."`.
- every `Dependency.version_range` parses as a
  `packaging.specifiers.SpecifierSet` (catch the parse error and re-raise as a
  `ValueError` naming the field, so it surfaces as a pydantic validation error
  rather than an uncaught exception).

Keep `ManifestInvalid` (today's exception class) exactly as it is today — it is
the loader's own wrapper for a load-time failure that is *not* a field-shape
failure (an entry point that resolves to something other than a `ModuleManifest`,
an id disagreeing with its entry-point name). **Never wrap pydantic's own
`ValidationError`** — wrapping is exactly what would lose the field name AC 2
asserts on.

**`manifest.validate()` does not survive — delete it, in this prompt.** It is
named in `state.json#scope`'s `cut_symbols` ("hand-rolled presence checks;
replaced by pydantic validation + `@model_validator`"), and every rule in its body
(`manifest.py:85-115`) is now either a field annotation or a `@model_validator`
rule above. Leaving it would be a second, weaker copy of a shipped gate — the
exact thing its own docstring argues against. Three consequences you must carry in
the same diff, or the tree does not import:

- `packages/core/src/rheo_core/modules/__init__.py` drops `validate` from both the
  `from rheo_core.modules.manifest import (...)` list and `__all__`, and its
  docstring bullet stops naming it.
- `loader.py`'s `from rheo_core.modules.manifest import validate as validate_manifest`
  (`loader.py:45`) goes. See task 4 for what replaces the call.
- `tests/test_module_loader.py` imports `validate` (`:54`) and has three tests
  named for it (`:427`, `:439`, `:444`). See task 5.

Two fields from today's stub are dropped entirely and have no replacement field:
`schema_name` (subsumed by `storage.schema_name`, whose validator carries the
stub's own `schema_name == module_id` rule) and `create_schema` (no production
caller today; the migration chain, built in a later prompt, is what creates a
module's schema from now on).

**The trap you must not walk into: `audit_sink`'s bare-Protocol failure.**
`AuditSink` (`packages/core/src/rheo_core/audit/sink.py`) is a `typing.Protocol`
**without** `@runtime_checkable`. A bare `audit_sink: AuditSink | None = None`
annotation on this pydantic model fails at **class-definition time**: without
`arbitrary_types_allowed` pydantic cannot build a schema for a plain `Protocol` at
all and raises `PydanticSchemaGenerationError` immediately (which is the correct,
loud failure — do not "fix" this by adding `arbitrary_types_allowed=True`; under
that flag pydantic instead falls back to an `is_instance_schema`, and
pydantic-core's is-instance build probes `isinstance(None, AuditSink)`, which a
non-runtime-checkable Protocol refuses with `TypeError` — surfacing as
`SchemaError` at the same class-definition moment, but now silently, from a flag
that looks like it should have made the type "just work"). The fix is a
`PlainValidator`, not a flag:

```python
def _audit_sink(value: object) -> object:
    if callable(getattr(value, "record", None)):
        return value
    raise ValueError("an audit sink must provide record()")


audit_sink: Annotated[AuditSink, PlainValidator(_audit_sink)] | None = None
```

This is the same duck check `install_sink` already applies in
`audit/sink.py`, so the manifest and the installer agree on what a sink is. Do
not type the field `Any`, and do not use `SkipValidation` (it lets a bare
`object()` through, which a real test in this prompt catches). `manifest.py`
does not import `Annotated` or `PlainValidator` today (the stub is a plain
dataclass) — add `from typing import Annotated` and `from pydantic import
BaseModel, ConfigDict, PlainValidator, model_validator` (beside whatever of
those four the file already needs for the other nine declaration models) when
you rewrite the file's imports.

**2. `packages/core/pyproject.toml`** — add `"packaging>=24,<26"` to
`[project].dependencies`, in the same style as the existing `sqlalchemy`/`alembic`
entries. This is the PEP 440 range comparator `Dependency.version_range` uses;
`packaging` is already resolved in `uv.lock` as a transitive dependency, so this
adds no new distribution to the image, but `uv.lock` *does* change in this
prompt — that is expected (the sequencing note in plan.md allows `uv.lock` to
change in phases 1 and 4 only, nowhere else).

**Nothing in the suite would go red if you skipped this**, which is exactly why
it needs its own assertion: `packaging` already resolves transitively, so
`import packaging.specifiers` works today and `mypy --strict` is happy
(`packaging` ships `py.typed`). An undeclared runtime dependency that happens to
be present is a break waiting for the next lock regeneration. Add a case to
`tests/test_module_manifest.py` (task 6) that reads
`packages/core/pyproject.toml` with `tomllib` and asserts a `packaging`
requirement appears in `[project].dependencies` — and, as its positive control,
that a known-declared name (`alembic`) is found by the same read, so a
mis-pathed or mis-keyed read cannot report success.

**3. `packages/core/src/rheo_core/modules/__init__.py`** — export every new name
beside `ModuleManifest`, and drop `validate` from the import list, from `__all__`,
and from the docstring bullet that names it.

**4. `packages/core/src/rheo_core/modules/loader.py`** — change only the manifest
validation step. Replace the `validate as validate_manifest` import and its call
site (`loader.py:45` and `:109`) with a small private `_as_manifest(loaded:
object) -> ModuleManifest` in this file that raises `ManifestInvalid` when an
entry point resolves to something that is not a `ModuleManifest` instance —
carrying over the stub's own message, `"a rheo.modules entry point must load to a
ModuleManifest"`, and its `str(getattr(loaded, "module_id", loaded))` module-id
argument verbatim. **Nothing else re-validates.** A `ModuleManifest` instance was
already validated by pydantic at construction, and a second
`model_validate` pass here would either be a no-op or, on a model carrying
callables, a rebuild with no gain. Leave `_register`, `_SURFACES`,
`ALLOWLIST_VARIABLE`, `discovered()` and everything else in this file untouched —
those change in a later prompt.

**5. `tests/test_module_loader.py`** — two kinds of change:
- Every existing manifest-construction fixture in this file passes
  `create_schema=`, which no longer exists. Rebuild each one against the new
  24-field model, giving each a `StorageDeclaration` instead of `create_schema=`.
  Keep the `_probe`-suffixed ids exactly as they are (`:20-24` explains why).
- The three `validate` tests, rewritten in place against what replaced it. None of
  these three is a pinned demonstrator (`grep validate docs/acceptance/phase-1-matrix.md`
  returns nothing), so renaming them is allowed here — unlike
  `THE_SEVENTEEN`'s test in a later prompt:
  - `test_validate_refuses_a_schema_that_is_not_the_module_id` (`:427`) — the rule
    it asserts is now the `@model_validator`'s `storage.schema_name == module_id`.
    Rewrite it to construct a `ModuleManifest` whose `StorageDeclaration.schema_name`
    disagrees with `module_id` and assert `pydantic.ValidationError`.
  - `test_validate_refuses_something_that_is_not_a_manifest` (`:439`) — the rule it
    asserts now lives in `_as_manifest`, which is private, so assert it at the
    loader's public seam: publish a fabricated entry point whose value points at a
    module-level non-manifest object and assert `load_modules()` raises
    `ManifestInvalid`. Do **not** reach into `_as_manifest` directly.
  - `test_validate_accepts_the_fixture` (`:444`) — becomes an assertion that the
    module-level `MANIFEST` is a `ModuleManifest` and that `load_modules()` loads
    it under its own id. Deleting it outright would drop the only positive case in
    this file's validation coverage.
- Drop `validate` from this file's import block (`:54`).

**6. New file `tests/test_module_manifest.py`** — this is AC 2's home:
- Import `ModuleManifest` at module level (`from rheo_core.modules.manifest import
  ModuleManifest`). This import is itself the first assertion: if the
  `audit_sink` field's shape is wrong, this file reds at collection, before any
  test body runs — that is deliberate, not a bug in the test.
- Five omission cases: construct a manifest omitting, in turn, `record_types`,
  `storage`, `configuration_schema`, `tools`, `export`; assert each raises
  `pydantic.ValidationError` with that exact field name in `loc`.
- Assert `ModuleManifest.model_fields` holds exactly 24 names — the 23 ratified
  names plus `audit_sink` — and that exactly three of them (`web`,
  `agent_guidance`, `audit_sink`) are not required.
- Two `audit_sink` cases: `audit_sink=CORE_AUDIT_SINK` (from `rheo_core.audit`)
  validates and is returned by identity; `audit_sink=object()` raises naming
  `audit_sink`.
- FR 3's own pair: a manifest supplying `()` for all seven deferred-collection
  tuple fields and `{}` for `sensitivity` validates (present-and-empty is a
  declared field); omitting any *one* of those eight still raises, naming it.
  Without this pair, FR 3's required-vs-defaulted distinction is unverified.
- A `Dependency` whose `version_range` does not parse as a `SpecifierSet` raises,
  naming the field.
- **AC 5's one mechanically checkable clause:** `rheo_contracts` exposes no
  `ModuleManifest` attribute — `assert not hasattr(rheo_contracts,
  "ModuleManifest")`, with `assert hasattr(rheo_contracts,
  "OperationDeclaration")` beside it as the control, so a mistyped module name
  cannot make the absence look true. `tests/test_contracts.py` pins that
  package's ratified surface in several places but asserts nothing about
  `ModuleManifest`; AC 5's other two clauses (the real home, and the doc naming
  it) are a code fact and a prose fact respectively and have no test.
- The `packaging` declaration check from task 2 above, with its `alembic`
  control.

**7. `docs/architecture/module-contract.md`** — make seven corrections in this one
edit (read the file's manifest section and its version-compatibility section
before editing):
1. The manifest section names `rheo_core.modules.ModuleManifest` as the type's
   real home (not `rheo_contracts.ModuleManifest`), stating the
   handler-travels-beside-declaration reason — mirror
   `packages/contracts/src/rheo_contracts/manifest.py`'s own docstring on why
   `OperationDeclaration.handler` lives outside that package.
2. The `configuration_schema` row records that it is `tuple[KeySpec, ...]`, not a
   pydantic model class (the settings system already declares keys as `KeySpec`
   and registers them through `SettingsRegistry.register(spec, *, origin)`).
3. The `web` row records that release one ships `WebSurface | None`
   (`manifest.py`, consumed by `internal_routes.py`) rather than the ratified
   `WebContribution` shape, and that run 1a3 owns widening or replacing it.
4. The version-compatibility section records PEP 440 specifiers in place of "the
   usual caret and tilde forms" (that ratified sentence describes npm's grammar,
   not Python's).
5. The manifest table gains a 24th row, `audit_sink`, marked optional, naming
   `loader.py`'s `_register` as its installer and `dispatch.py`'s
   `AUDIT_SINK_MISSING` as what refuses every above-`READ` operation without one.
6. The `events` row (`:36`) spells the third field **`data`**, not `data_model` —
   the code's name, per the deviation note above.
7. The `jobs` row (`:38`) gains **`input_model`** to the `JobKind(...)` signature
   it prints, because `JobKindRegistry.register(kind, input_model, handler)`
   (`work/kinds.py:69-71`) cannot register a kind without one. (A later prompt
   adds a separate note to this same row about `max_attempts`/`cancellable` being
   reader-less in release one; do not pre-empt it here.)

## Checkpoint
Seams under test: `ModuleManifest`'s own pydantic construction/validation
contract — every omitted-field case, the `audit_sink` duck-check, and the
present-vs-defaulted distinction are asserted by constructing the model through
its public constructor, exactly the way a module author (and the loader) will,
never through a private helper — plus the loader's `load_modules()` seam for the
two cases that moved out of `validate()` into `_as_manifest`.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff: for each, break the thing it pins (drop one required field's
`= None`-lessness, return `value` unconditionally from `_audit_sink`, delete the
`storage.schema_name == module_id` rule) and confirm the test goes red, then
restore. A seam test that stays green under its own mutation is not a test.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/test_module_manifest.py tests/test_module_loader.py tests/test_contracts.py tests/test_imports.py
make lint && make typecheck && make test && make check
```

Green means: all four files pass, `mypy --strict` and `ruff` are clean over
`packages/core/src/rheo_core/modules/`, and the full suite (`make test`) is green
— this phase must be green standalone, with no test elsewhere in the tree left
red waiting for a later prompt.
````

**Done when:** `ModuleManifest` is a frozen pydantic model with exactly 24 fields
(21 required, 3 optional) and no `arbitrary_types_allowed`; `manifest.validate`
is gone from the module, from `__all__`, and from every caller; every existing
manifest construction in `tests/test_module_loader.py` builds against the model;
the new `tests/test_module_manifest.py` proves AC 2's five omissions and the
`audit_sink` shape; the seven named doc corrections are in `module-contract.md`;
the checkpoint above is green.

---

## Prompt 2 — `modules.installed`, and the three named sets
**Phase:** Plan Phase 2 — `modules.installed`, and the three named sets
**Coder:** The Systemsmith
**Execution-profile:** role-default
**Produces:** `RHEO_MODULES` no longer exists anywhere under `packages/`, `apps/`
or `tests/` (historical doc prose excepted); the module allowlist is a
deployment-scope settings key; on-disk / deployment-loaded / workspace-enabled
are three separately named, independently readable things.
**Depends on:** Prompt 1 (`ModuleManifest` must exist as a pydantic model before
`loaded_manifests()` can hold one).
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/settings/schema.py`,
`packages/core/src/rheo_core/config/defaults.toml`,
`packages/core/src/rheo_core/modules/{loader.py,__init__.py}`, `.env.example`,
`Makefile`, `apps/core/.../startup.py` and `apps/cli/.../openapi.py` (docstring
prose only), `tests/{test_settings.py,test_module_loader.py,test_openapi_document.py,test_production_registration.py,test_absent_behaviour.py}`,
new `tests/test_module_sets.py` and `tests/test_allowlist_variable_absent.py`,
`docs/architecture/module-contract.md`. Expected generated churn — the
`make codegen` artifacts under `apps/web/src/generated/`, which must come back
byte-identical (a diff there is a finding, not churn to commit). Stop boundary:
`discovered()` is not touched; `_register`'s body is not widened (Prompt 3);
`uv.lock` must not change in this phase.

````
This implements spec.md's FR 4 and FR 5, closing AC 6 and AC 8, per plan.md
Phase 2. `ModuleManifest` is now the pydantic model Prompt 1 built.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are.
`apps/core/src/rheo_app_core/startup.py` and
`apps/cli/src/rheo_app_cli/commands/openapi.py` are allowed **only** for removing
`RHEO_MODULES` docstring prose (task 9); no other change there is in scope. A path
in neither list is out of scope: raise it rather than widen the guard.

Today, `packages/core/src/rheo_core/modules/loader.py`'s `allowed_module_ids()`
reads the comma-separated `RHEO_MODULES` process environment variable (constant
name `ALLOWLIST_VARIABLE`), and `_SURFACES` is a bare `dict[str, WebSurface]` that
throws away everything about a loaded manifest except its web surface. Read the
whole file before editing it.

## Task

**1. `packages/core/src/rheo_core/settings/schema.py`** — add a `modules.installed`
`KeySpec` to `PRODUCTION_KEYS`: `type=ValueType.STR_LIST`, `scope=Scope.DEPLOYMENT`,
`floor=None`, `explicit_per_workspace=False`, `default=()`. The empty default is
load-bearing: an unlisted module id is not loaded, with no exception — never
default to "every discovered module." Update this module's own docstring, which
currently states this key is deliberately undeclared; it is being declared now,
for the reason recorded there.

**2. `packages/core/src/rheo_core/config/defaults.toml`** — add the identical
value; `settings/defaults.py` asserts the TOML and the `PRODUCTION_KEYS` default
agree at import, so a mismatch here fails loudly rather than silently.

**3. `tests/test_settings.py`** — add the new key to the `PRODUCTION_KEYS`
dictionary this file maintains for its exact-set assertions; several assertions
in this file compare the full key set and will fail until this key is present.

**4. `packages/core/src/rheo_core/modules/loader.py`** — several changes in this
one file:
- `allowed_module_ids()` becomes
  `frozenset(resolve().get_list("modules.installed"))` (`resolve` from
  `rheo_core.settings`). **Use the typed accessor, not `__getitem__`.**
  `ResolvedSettings.get_list` is the shipped accessor
  (`packages/core/src/rheo_core/settings/resolver.py:124`) and returns
  `list[str]`; a bare subscript returns the union `FrozenValue`
  (`str | int | bool | tuple[str, ...]`), which `mypy --strict` rejects as an
  argument to `frozenset[str]` and which would silently accept a key declared at
  the wrong `ValueType`. `get_list` also raises `SettingTypeMismatch` if the key's
  declared type is ever changed out from under this call. Remove
  `ALLOWLIST_VARIABLE` and the `os` import; nothing else in this file reads
  either.
- Rewrite the module's docstring (today it describes the `RHEO_MODULES`
  mechanism start to finish) to describe the settings key instead.
- Replace `_SURFACES: dict[str, WebSurface]` with `_LOADED: dict[str,
  ModuleManifest]`. Add `loaded_manifests() -> Mapping[str, ModuleManifest]`
  returning a read-only view. `module_surfaces()` keeps its exact signature but
  now derives its answer from `_LOADED` (each manifest's own `.web`, filtered to
  non-`None` and keyed by `web.surface`). `reset_surfaces()` keeps its name and
  clears `_LOADED`.
- `discovered()` — leave its name, signature, and body completely untouched.
  Discovery (host-installed / on-disk, set (a) in FR 5) and the deployment-loaded
  set (b) must stay two separately named things; this function is set (a)'s only
  reader-facing name.

**5. `packages/core/src/rheo_core/modules/__init__.py`** — export
`loaded_manifests`; remove the sentence naming `RHEO_MODULES`.

**6. `.env.example`** — find the `RHEO_MODULES=` block (`:64-78`; it carries a
`# preflight: allow-empty` marker comment at `:76`, immediately above the key).
Replace it with a `RHEO__modules__installed=` block, keeping that exact marker
comment above the new line, plus a short paragraph explaining the empty default.
**Keep the marker verbatim, spelling and all.** Nothing in this repository reads
it — grep the tree and you will find only `.env.example` itself — so no local
gate reds if you drop it; it is read by the run framework's own env-key preflight,
which fails a present-but-empty key without it, and the key's whole point is that
it is empty by default. Do not touch anything else in this file — in particular,
do not touch the port in any DSN example it may carry.

**7. `Makefile`** — the `codegen` target's comment block currently names
`RHEO_MODULES` as part of the canonical generation environment. Update that
sentence to state the canonical generation environment as "no `RHEO_*` variable
**and** no `deployment.toml`" (both now matter, since `modules.installed` is a
deployment-scope settings key reachable through either).

**8. `tests/test_openapi_document.py` and `tests/test_production_registration.py`**
— every reference to `RHEO_MODULES` in these two files switches to the settings
key (set it via the settings-resolution path these tests already use for other
keys, not via `os.environ`).

**9. `apps/core/src/rheo_app_core/startup.py` and
`apps/cli/src/rheo_app_cli/commands/openapi.py`** — **you may touch these two
files only to remove `RHEO_MODULES` docstring prose.** Neither file reads the
variable directly (each only calls `load_modules()`, which is unchanged in this
prompt), but both files' module docstrings name the allowlist as `RHEO_MODULES`
in prose. Rewrite that prose to name the resolved `modules.installed` setting
instead. Make no other change in either file.

**10. `tests/test_module_loader.py`** — delete
`test_the_allowlist_variable_is_not_a_settings_override` (its whole body asserts
`ALLOWLIST_VARIABLE == "RHEO_MODULES"`, and that name is gone). Rewrite
`test_the_process_variable_is_not_set_by_the_suite` (this file's last test)
against the settings key: with `Scope.DEPLOYMENT`, there is no process variable
for a developer's shell to leak, so this case becomes an assertion that no
`RHEO__modules__installed` variable is set in the test environment. Update this
file's own module docstring too.

**11. New file `tests/test_module_sets.py`** — this is AC 8 / FR 5's home. Get
each fixture manifest into `loaded_manifests()` through the fabricated-`EntryPoint`
recipe in this file's standing-constraints section (real `EntryPoint` pointing at a
module-level name, published via the `_publish(monkeypatch, ...)` shape at
`tests/test_module_loader.py:194-198`, `_probe`-suffixed ids, process state reset
either side). Two cases, each proving one set is independently readable without the
other answering for it:
- A module id present on disk (`discovered()` finds it) but **not** in
  `modules.installed` — assert `discovered()` finds it and `load_modules()` does
  not load it.
- A module id loaded this deployment (in `loaded_manifests()`) but never
  installed in a given workspace — assert `list_module_states()` for that
  workspace has no row for it. **Pair this with a positive control in the same
  test**, or a `list_module_states()` that silently returned nothing at all would
  satisfy it: write a row for a *different* module id with
  `enable_harness_module` (`tests/harness/registry.py`) and assert
  `list_module_states()` returns that one. An empty answer is only evidence when
  the same call, over the same workspace, is shown to return something.
The test fails if any single function ever answers more than one of "on disk,"
"loaded this deployment," "workspace-installed" — that is the property FR 5
exists to keep true.

**12. `docs/architecture/module-contract.md`** — discovery step 1 currently says
`modules.installed`'s default is "every discovered module." Rewrite it to state
the empty-list default, and that an unlisted module id is simply not loaded, with
no exception.

**13. The `RHEO_MODULES` deletion census — a new file, deliberately NOT
`tests/test_absent_behaviour.py`.** AC 6's second half wants a repository-wide
search for the literal `RHEO_MODULES` to come back empty outside `docs/`. Build
it as **new file `tests/test_allowlist_variable_absent.py`**, self-contained, and
read the two paragraphs below before you write a line — the obvious placement and
the obvious repair are both wrong, and each breaks a load-bearing gate.

**Why not in `tests/test_absent_behaviour.py`, and why not through
`harness.absence`.** Every helper in `tests/harness/absence.py` records its
`covers` value into a **process-global** `_COVERED` dict keyed by the calling
test's name (`absence.py:62`, `:90-91`, `:169`/`:217`/`:276`).
`test_every_criterion_is_covered` (`test_absent_behaviour.py:142-175`) then
asserts that map is **exactly** one entry — `test_no_spike_remains` paired with
the run-0c criterion label it carries — with the int
partition exactly empty. So a `RHEO_MODULES` probe calling `absent_token(...)` —
in that file or in any other file in the same pytest process — adds a second key
and reds that assertion. `test_absent_behaviour.py:30-34` further records that the
probe names and that map are pinned by a **previous run's** acceptance criterion
(it is that run's numbering in the docstring, not this spec's) and may not be
renamed for house-style reasons, and `:45-52` records that `tests/` is
deliberately **not** a fourth scan root because a trailing-identifier AST match
cannot see a receiver's type. **The repair
that suggests itself — loosening that exact-map assertion to a superset check, or
adding the new probe name to it — damages the instrument the scored 0c1/0c2 trials
are measured with. Do not do it.** Do not import `harness.absence` from the new
file at all; the only change this prompt makes to `tests/test_absent_behaviour.py`
is the prose edit in the next paragraph.

**The one edit to `tests/test_absent_behaviour.py`:** `test_no_spike_remains`'s
docstring (`:102`) cites "the `RHEO_MODULES` value" as a worked example of a token
a five-token census missed. Rewrite that clause so it does not name a variable
that no longer exists (the example still works named as "the allowlist variable's
value"). This is a docstring edit only — do not touch the function name,
`SPIKE_SURVIVORS`, `SCAN_ROOTS`, or any assertion. Note that this edit is also what
stops `tests/test_absent_behaviour.py` from being a live hit in the new census.

**What `tests/test_allowlist_variable_absent.py` contains:**
- The scan: `git ls-files` from the repository root (mirror
  `test_absent_behaviour.py:83-91`'s `subprocess.run(["git", "ls-files"], ...)`
  shape), every tracked file whose path does not start with `docs/`, read as
  UTF-8, matched **case-sensitively** against the literal `RHEO_MODULES`. AC 6
  specifies "the literal"; case-sensitive is both what AC 6 asks for and strictly
  stronger here, and it keeps the check independent of the case-insensitive rule
  `harness.absence` is fixed at.
- **A positive control, because a census with no control is the vacuous pass this
  repository has been bitten by twice.** Assert the walk read a non-trivial number
  of files, and that a control literal genuinely present in the post-change tree —
  use `modules.installed`, which task 1 puts in `settings/schema.py` and task 2 in
  `defaults.toml` — is found by the *same* read over the *same* resolved file set.
  A control that is not found means the walk resolved to nothing and the clean
  `RHEO_MODULES` result proves nothing.
- **A declared exclusion set, asserted both ways.** Exactly one file legitimately
  carries the literal after this prompt: this new test file itself, which has to
  spell the token it hunts for (the same self-exemption
  `tests/test_boundary_scans.py:206-207` takes for the scope privates). Declare it
  explicitly with its reason, assert no undeclared file hits, **and** assert every
  declared file still carries a hit — a stale exclusion is a bug, not a harmless
  leftover.
- An unexpected hit is a real leftover from one of tasks 4–10 and is fixed there.
  It is never added to the exclusion set.

## Checkpoint
Seams under test: the three-set boundary as public, independently-callable
functions — `discovered()` (on-disk), `allowed_module_ids()` /
`loaded_manifests()` (deployment-loaded), and `list_module_states()`
(workspace-installed/enabled) — exercised directly in `tests/test_module_sets.py`
rather than through a shared private cache that could silently conflate them.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff: make `allowed_module_ids()` return every discovered id regardless
of the setting and confirm the on-disk-but-unloaded case reds; make
`loaded_manifests()` answer from `list_module_states()` and confirm the
loaded-but-not-installed case reds. Restore after each. Mutation-verify the new
census the same way: delete its declared exclusion entry and confirm it reds, and
plant the literal in a throwaway tracked-path file and confirm it reds.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/test_settings.py tests/test_module_loader.py tests/test_module_sets.py tests/test_allowlist_variable_absent.py tests/test_absent_behaviour.py tests/test_openapi_document.py
make lint && make typecheck && make test && make check
make codegen && git diff --exit-code apps/web/src/generated/
```

Green means: the six selected files pass; the full suite passes; and
`make codegen`'s regenerated artifacts under `apps/web/src/generated/` come back
byte-identical (a diff there would mean the settings-based loader activated
something the canonical generation environment should not have — see the
technical-risk note about `rheo openapi` gaining a settings dependency).
`apps/web/**` itself stays untouched by you; only its generated artifacts may
change, and only via `make codegen`. Scoping the diff check to
`apps/web/src/generated/` is deliberate: a bare `git diff --exit-code` would red
on your own uncommitted source edits and prove nothing about codegen.
````

**Done when:** `RHEO_MODULES` has zero tracked hits outside `docs/`, proven by
`tests/test_allowlist_variable_absent.py` with a live positive control and a
both-ways exclusion set; `tests/test_absent_behaviour.py`'s exact-map assertion is
unchanged and still green; `modules.installed` is a real `PRODUCTION_KEYS` entry;
`loaded_manifests()` exists and `discovered()` is untouched; the checkpoint above
is green including the `codegen` diff check.

---

## Prompt 3 — The loader registers all six extension points
**Phase:** Plan Phase 3 — The loader registers all six extension points
**Coder:** The Systemsmith
**Produces:** a manifest's tools, job kinds, subscriptions, and
`configuration_schema` keys all reach a live registry after `load_modules()`
runs, beside the two categories (operations, resolvers) it already registers;
declared events are validated and readable; the worker process actually calls
`load_modules()` for the first time.
**Depends on:** Prompt 2 (`loaded_manifests()` and the settings-based allowlist
must exist first; this prompt's `configuration_schema` registration and
dependency/contract-version checks both read through them).
**Execution-profile:** role-default
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/modules/loader.py` (the bulk of the change),
`apps/worker/src/rheo_app_worker/main.py`, `tests/test_worker_job_kinds.py`, new
`tests/test_module_registration.py`, `docs/architecture/module-contract.md`.
Expected generated churn — the `make codegen` artifacts under
`apps/web/src/generated/`, which must come back byte-identical. Stop boundary: do
not widen `JobKindRegistry.register`, `ConsumerRegistry.register`,
`ToolRegistry.register` or `SettingsRegistry.register` — every one of the four is
a shipped signature this prompt calls, not one it changes. No new event registry:
`rheo_core.events` gains nothing. `uv.lock` must not change in this phase.

````
This implements spec.md's FR 6, closing AC 14 and Open Question 2, per plan.md
Phase 3. `packages/core/src/rheo_core/modules/loader.py`'s `load_modules()` and
`_register()` currently handle only operations and resolvers. Read the whole file,
plus `packages/core/src/rheo_core/work/kinds.py`,
`packages/core/src/rheo_core/events/consumers.py`,
`packages/core/src/rheo_core/tokens/sets.py`, and
`packages/core/src/rheo_core/settings/schema.py` for the four registries you are
about to call.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

## Task

**1. `loader.py` — widen `load_modules()`'s signature and `_register()`'s body.**

`load_modules()` gains three new keyword parameters:
- `tools: ToolRegistry = TOOL_REGISTRY` (from `rheo_core.tokens.sets`) — a
  process-global default, like `registry`/`resolvers` already are.
- `kinds: JobKindRegistry | None = None`
- `consumers: ConsumerRegistry | None = None`

`kinds` and `consumers` default to `None`, meaning "do not register that
category" — they are **not** process-global singletons like the other three
registries. `JobKindRegistry` and the consumer registry are composition-root
locals owned by `apps/worker` (built as `JOB_KINDS`/`CONSUMERS` in that app's
`main.py`), so the core process (which never runs a job handler) legitimately
passes neither.

`_register()` grows four branches, each iterating the manifest's own tuples:
- `for decl in manifest.tools: tools.register(decl, origin=manifest.module_id)`
  — `ToolRegistry.register` in `tokens/sets.py`.
- `if kinds is not None: for kind in manifest.jobs: kinds.register(kind.name,
  kind.input_model, kind.handler)` — `JobKindRegistry.register` in
  `work/kinds.py` takes exactly these three positional arguments; `JobKind`'s
  `max_attempts` and `cancellable` fields are carried on the declaration but have
  no reader this run (a later run reads each — do not widen
  `JobKindRegistry.register` to accept them; that is a shipped-signature change
  with no caller in this run).
- `if consumers is not None: for sub in manifest.subscriptions: assert
  sub.module_id == manifest.module_id (or equivalent check); consumers.register(sub)`
  — `ConsumerRegistry.register` in `events/consumers.py` takes the
  `ConsumerSubscription` alone; the module-id agreement check is this prompt's own
  addition, not something the registry enforces for you.
- `for spec in manifest.configuration_schema:
  SettingsRegistry.register(spec, origin=manifest.module_id)` — the shared
  `settings.schema.REGISTRY` instance, whose `register(spec, *, origin)` already
  exists.

  **Name the interaction this creates, in the loader's own docstring, and do not
  try to prevent it here.** `REGISTRY` is process-global, and
  `packages/core/src/rheo_core/storage/provisioning.py`'s
  `_step_write_default_settings` (`:226-238`) iterates
  `REGISTRY.explicit_per_workspace()` and writes a row for every spec it finds.
  So from the moment a module loads, any of its `KeySpec`s marked
  `explicit_per_workspace` is written into **every workspace provisioned
  afterwards**, whether or not that module is installed or enabled there — and
  Prompt 7's enable step writes the same rows for workspaces provisioned before.
  Both writers go through `insert_workspace_setting_if_absent`, so the overlap is
  idempotent rather than a conflict. Record that in the docstring so the next run
  does not rediscover it as a bug; do not add a filter to `provisioning.py`
  (out of scope, and the row is harmless) and do not give modules a second,
  module-local settings registry.

Leave the `audit_sink` branch (installs via `install_sink` under the `is not
None` guard) and the web-surface branch (now writing `_LOADED`) exactly as they
are — they already do the right thing and nothing about them changes.

**2. Contract-version gate, same file.** Before registering anything for a
manifest, refuse to load it (raise `ManifestInvalid`) unless
`rheo_contracts.CONTRACT_VERSION` appears in that manifest's
`core_contract_versions`, naming both versions in the error.

**3. Dependency-range check and topological sort, same file.** For every
*required* `Dependency` (`optional=False`) declared by a manifest in the loaded
set: the dependency's `module_id` must itself be in the loaded set, and that
dependency's `package_version` must satisfy the declaring manifest's
`Dependency.version_range` via
`packaging.specifiers.SpecifierSet(version_range).contains(package_version)`.
Failing either raises `ManifestInvalid` naming both module ids and the range. An
*optional* dependency (`optional=True`) that is absent from the loaded set is
simply skipped — no error. Once every loaded manifest's dependencies are known to
resolve, order the whole loaded set for registration using stdlib
`graphlib.TopologicalSorter` over the dependency edges; a `graphlib.CycleError`
is re-raised as `ManifestInvalid` naming the modules in the cycle. This ordering
is what a later prompt's module migration chains will mean by "the manifest's
dependency-sorted order" — build it once here, do not invent a second order
later.

**4. Event-declaration validation, same file.** For each manifest's declared
`EventDeclaration`s: each `type` string must match
`f"{manifest.module_id}.<record_type>.<verb>"` — i.e. its first dot-separated
segment must equal the manifest's own `module_id`. Across the whole loaded set,
no two modules may declare the same event `type`. Either violation raises
`ManifestInvalid` naming the offending type (and, for the duplicate case, both
modules). There is no event registry to populate — `rheo_core.events` has
nothing that indexes event declarations, and this run does not build one; a
declared event's presence is proven by reading it back from
`loaded_manifests()[module_id].events`, which is why step-4 verification below
reads the manifest directly rather than a registry.

**5. The manifest-to-registration cross-check needs no new code.**
`load_modules()` already calls `_register(manifest, ...)` and `_register`
iterates only the manifest's own tuples — so "an operation registered that the
manifest didn't declare" and "declared but not registered" are both already
unreachable by construction. Do not build a second mechanism for this; add one
assertion to the new test file below that pins the property instead (see task 8).

**6. `apps/worker/src/rheo_app_worker/main.py`** — inside `main()`, immediately
after the existing call to `refuse_misconfigured_login()` and before the
`worker_loop(...)` call, add:

```python
load_modules(kinds=JOB_KINDS, consumers=CONSUMERS)
```

Extract this one call into a small `register_modules()` function (taking no
arguments, reading the module-level `JOB_KINDS`/`CONSUMERS`) so a test can drive
it without spinning up a real backend, and call `register_modules()` from
`main()` in place of the inline call. Do this **inside** `main()` — not at module
level — for the same reason this file already keeps `get_backend()` out of
module scope (read that reasoning in the file's own docstring before you decide
otherwise), and so the existing module-level `JOB_KINDS.names()` assertion in
`tests/test_worker_job_kinds.py` stays true (that assertion runs at import time,
before any function call).

**7. `tests/test_worker_job_kinds.py`** — add a case: with a fixture module
loaded, assert its job kind and its consumer subscription appear in `JOB_KINDS`
and `CONSUMERS` after calling `register_modules()`.

**8. New file `tests/test_module_registration.py`** — this is AC 14's home. Get
every fixture manifest into `loaded_manifests()` through the
fabricated-`EntryPoint` recipe in this file's standing-constraints section (real
`EntryPoint` pointing at a module-level name, published via the
`_publish(monkeypatch, ...)` shape at `tests/test_module_loader.py:194-198`,
`_probe`-suffixed ids, process state reset either side). Cases:
- A fixture manifest declaring exactly one operation, one resolver, one tool, one
  job kind, one event, and one subscription. After `load_modules(...)` (passing
  `kinds=`/`consumers=` fixture registries), assert each of the six categories is
  readable from its own registry — operations from `OperationRegistry`, resolvers
  from `ResolverRegistry`, tools from `ToolRegistry`, the job kind from the
  fixture `JobKindRegistry`, the subscription from the fixture `ConsumerRegistry`,
  and the event from `loaded_manifests()[module_id].events`.
- **AC 14's two event cases, which readability alone does not cover.** AC 14's
  last sentence asks for the declared event to be "namespaced under the module's
  own id, and rejected as a duplicate against another loaded module's event of
  the same type" — two refusals, not one read. Both are built in task 4; both get
  a test here, and neither is provable by reading `loaded_manifests()`:
  - **Namespacing:** a fixture manifest whose `EventDeclaration.type` does not
    start with `f"{module_id}."` — e.g. module `alpha_probe` declaring
    `"beta_probe.note.created"` — makes `load_modules()` raise `ManifestInvalid`
    naming the offending type. Assert the type string appears in the message.
  - **Cross-module duplicate — and there is exactly one shape that reaches it.**
    The namespacing rule forces every declared type's first segment to be the
    declaring manifest's own `module_id`, so **two manifests with *different*
    module ids can never collide on a type**: the second one's type would fail
    namespacing first, and a test built that way asserts the namespacing refusal
    while believing it asserted the duplicate one. The reachable shape is two
    manifests sharing a `module_id`: publish two fabricated `EntryPoint`s **both
    named `alpha_probe`**, pointing at two distinct module-level manifests both
    carrying `module_id="alpha_probe"`, both declaring
    `"alpha_probe.note.created"`. Both clear the entry-point-name check
    (`loader.py:110-116`) and both clear namespacing, so the duplicate check is
    the only thing left to refuse them. Assert `load_modules()` raises
    `ManifestInvalid` naming the duplicated type. Pair it: the same two manifests
    declaring *different* types load cleanly — otherwise the case passes against
    an implementation that refuses any repeated module id.

    This containment is worth a comment in the loader beside the two checks. Both
    are specified (AC 14 names both), the namespacing rule makes the duplicate
    rule unreachable for distinct ids, and the duplicate rule still earns its
    place for the real hazard it covers: two installed distributions both claiming
    the same `module_id`.
- A fixture manifest declaring an `audit_sink`: assert it installs under its own
  module id and the dispatcher's `sink_for(module_id)` (or equivalent lookup)
  returns it by identity.
- A two-module fixture pair whose dependency range is unsatisfied: assert load
  fails, naming both modules and the range.
- A two-module fixture pair forming a dependency cycle: assert load fails, naming
  both modules.
- A contract-version mismatch (task 2): a fixture manifest whose
  `core_contract_versions` omits `rheo_contracts.CONTRACT_VERSION` fails to load,
  naming both versions; the same manifest with the current version loads. Without
  the second half this case passes against a gate that refuses everything.
- The step-4 cross-check, stated as a property rather than built as a mechanism:
  for every item registered whose `origin` is a loaded module id, assert that
  item appears in that module's own manifest (iterate the loaded manifests and
  their registered items and compare).

**9. `docs/architecture/module-contract.md`** — two corrections in this edit: the
`jobs` row records that `max_attempts` and `cancellable` are carried on the
declaration but reader-less in release one, naming the future readers (an
enqueue call site for the former, Disable for the latter); and the discovery
section's step 4 (the manifest-to-registration cross-check) records that it is
satisfied by construction in this tree — there is no separate `register(registry)`
function here for it to check against.

## Checkpoint
Seams under test: `load_modules()`'s full six-category registration contract,
exercised through the same public registries their real consumers read
(`OperationRegistry`, `ResolverRegistry`, `ToolRegistry`, a `JobKindRegistry`, a
`ConsumerRegistry`, and `loaded_manifests()` for events) — not through
`_register`'s internals directly — plus the worker's `register_modules()` as the
production call site that gives job-kind/subscription registration a real
caller.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff. For each of the six categories, delete its `_register` branch and
confirm exactly that category's assertion reds and the other five stay green — a
single assertion that reds for all six is testing `load_modules()` ran, not what
it registered. Do the same for each refusal: remove the namespacing check, the
duplicate check, the contract-version gate, the dependency-range check and the
cycle re-raise in turn, and confirm the matching case reds. Restore after each.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/test_module_registration.py tests/test_module_loader.py tests/test_worker_job_kinds.py tests/test_production_registration.py
make lint && make typecheck && make test && make check
make codegen && git diff --exit-code apps/web/src/generated/
```

Green means: all four selected files pass; the full suite passes; and
`make codegen`'s output diffs clean (this phase, like Prompt 2, changes what
`rheo openapi` loads, so the generated artifacts must still come back
byte-identical in the canonical no-`RHEO_*`/no-`deployment.toml` generation
environment). The diff check is scoped to `apps/web/src/generated/` for the same
reason it is in Prompt 2: a bare `git diff --exit-code` reds on your own
uncommitted source edits and proves nothing about codegen.
````

**Done when:** `load_modules()` registers all six extension points; a manifest
declaring an unsatisfiable dependency or a dependency cycle fails to load, naming
both modules; `apps/worker`'s `main()` calls `register_modules()`; the checkpoint
above is green.

---

## Prompt 4 — The Recallatron skeleton distribution
**Phase:** Plan Phase 4 — The Recallatron skeleton distribution
**Coder:** The Systemsmith
**Produces:** `modules/recallatron` becomes a real, loadable `rheo.modules`
distribution: a packaged entry point, a validating manifest, a minimal Alembic
environment with one schema-only revision, and a contract-test entry CI collects.
Nothing installs or enables it yet — that is Prompts 6 and 7.
**Depends on:** Prompt 1 (`ModuleManifest` must be the real pydantic model before
an instance of it can be constructed here).
**Execution-profile:** role-default
**Reviewability:** primary authored files — everything under
`modules/recallatron/` (`pyproject.toml`, `src/rheo_recallatron/__init__.py`, new
`manifest.py`, new `migrations/{env.py,script.py.mako,versions/0001_schema.py}`,
new `tests/test_contract.py`), plus root `pyproject.toml`'s `testpaths`. Expected
generated churn — `uv.lock`, regenerated by `uv sync` (phases 1 and 4 are the only
two allowed to change it). Stop boundary: no file under `packages/`, `apps/` or
`tests/` is edited by this prompt at all — that is half of what AC 1's "no core
file edited" clause means, and Prompt 8's `tests/test_module_import_graph.py`
proves it. No tables, no operations, no settings keys.

````
This implements spec.md's FR 13 and FR 3's skeleton half, per plan.md Phase 4,
and it is half of AC 1's "lives entirely under `modules/recallatron/`" clause
(the other half — install and enable actually succeeding — is Prompts 6 and 7).

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

`modules/recallatron/` already exists in this tree as a placeholder: its
`pyproject.toml` declares `dependencies = []` and no entry point, and
`src/rheo_recallatron/__init__.py` is a one-line docstring with no code. This
prompt turns it into a real distribution. **There are three other placeholders,
not two: `modules/relationships/`, `modules/leads/` and `modules/current/`
(`rheo-current`, `src/rheo_current/__init__.py`).** Read all three for comparison;
edit none of them. They stay one-line placeholders with `dependencies = []` and no
`rheo.modules` entry point — this run gives an entry point to Recallatron alone.
Also read `packages/core/pyproject.toml`'s `[tool.uv.sources]` block, which is the
pattern to mirror for local workspace dependencies.

## Task

**1. `modules/recallatron/pyproject.toml`** — edit the existing file:
- `dependencies = ["rheo-core", "rheo-contracts"]` (replacing the empty list).
- Add a `[tool.uv.sources]` table naming both as local workspace members:
  `rheo-core = { workspace = true }` and `rheo-contracts = { workspace = true }`
  — the same pattern `packages/core/pyproject.toml` already uses for
  `rheo-contracts`.
- Add `[project.entry-points."rheo.modules"]` with one entry:
  `recallatron = "rheo_recallatron:MANIFEST"`.

**2. New file `modules/recallatron/src/rheo_recallatron/manifest.py`** — the
`MANIFEST` instance of `ModuleManifest`. This is FR 3's skeleton half: every
deferred-consumption field is written out present-and-empty, never omitted.
`module_id="recallatron"`, `package_version` matching the `pyproject.toml`
version, `core_contract_versions=(rheo_contracts.CONTRACT_VERSION,)`,
`dependencies=()`, `record_types=()`, `storage=StorageDeclaration(
schema_name="recallatron", migrations_path="rheo_recallatron.migrations",
required_extensions=())`, `configuration_schema=()`, `operations=()`,
`tools=()`, `events=()`, `subscriptions=()`, `jobs=()`, `schedules=()`,
`resolvers=()`, `deletion_participants=()`, `export=ExportDeclaration(
format_version=1, schema_path="export.schema.json", exporter=..., importer=...)`
— a trivial pair of callables that raise `NotImplementedError` is acceptable here
since nothing calls them in this run — `web=None`, `agent_guidance=None`,
`secret_scopes=()`, `connector_bindings=()`, `health_checks=()`,
`contract_tests="modules/recallatron/tests"`, `sensitivity={}`,
`audit_sink=None`. Put a one-line comment on the `audit_sink=None` line noting
that the skeleton registers no operation so it needs no sink, and that run 1a1
(whose memory operations are `mutate`) is the run that first supplies a real one
(as `audit_sink=CORE_AUDIT_SINK` or a writer of its own) — a 1a1 builder who
copies this file and forgets to change that line gets `AUDIT_SINK_MISSING` at
dispatch, not a load-time failure, so the comment matters.

**3. `modules/recallatron/src/rheo_recallatron/__init__.py`** — replace the
placeholder docstring-only file with a re-export of `MANIFEST`
(`from rheo_recallatron.manifest import MANIFEST`), so the entry point's dotted
target (`rheo_recallatron:MANIFEST`) resolves.

**4. New files under `modules/recallatron/src/rheo_recallatron/migrations/`:**
`env.py`, `script.py.mako`, `versions/0001_schema.py`. Mirror
`packages/core/src/rheo_core/migrations/core/env.py` for `env.py`'s structure —
same refusal when no orchestrator-supplied connection is present — but with
`version_table="alembic_version_recallatron"` and
`version_table_schema="recallatron"`.

**`0001_schema.py` — read this whole item before writing the file. The obvious
spelling fails on the first run, and the obvious repair for *that* breaks the
core chain.**

The revision creates **no tables** (the memory tables are 1a1/1a2's work in a
sibling run; adding one here builds ahead of a requirement no criterion in this
run asks for) and its schema statement is
**`CREATE SCHEMA IF NOT EXISTS recallatron`, never a bare `CREATE SCHEMA
recallatron`.** Here is the sequence, which you can read for yourself in
`packages/core/src/rheo_core/migrations/orchestrator.py:144-170`:

1. `run_chain` takes the advisory lock (`:161`).
2. `run_chain` executes `CREATE SCHEMA IF NOT EXISTS {schema}` (`:162`), where
   `schema` comes from the chain's version-table entry — and Prompt 5's
   `version_table_for()` resolves that, for a module chain, to the module's **own**
   schema. So by the time Alembic runs, `recallatron` already exists.
3. `run_chain` then calls `command.upgrade(...)` (`:170`), which runs this
   revision. A bare `CREATE SCHEMA recallatron` at that point raises
   `DuplicateSchema` and every install fails on a fresh workspace.

**Why the repair you will be tempted by is wrong.** The natural fix — "delete
`orchestrator.py:162` so the migration owns schema creation" — breaks the core
chain outright. `grep -ri "create schema"
packages/core/src/rheo_core/migrations/core/versions/` returns **nothing**: all
six core revisions (`0001_core_schema.py` through `0006_runtime.py`) create tables
inside a `core` schema they never create themselves, because `:162` creates it for
them. `packages/core/src/rheo_core/storage/provisioning.py:216` says so in a
comment on the call site: "The orchestrator creates the `core` schema itself,
under its lock." The same line serves the `control` chain. Removing or
conditioning `:162` would leave both shipped chains applying DDL into a schema
that does not exist. **`orchestrator.py:162` stays exactly as it is; this
revision's `IF NOT EXISTS` is the idempotent backstop, not the owner.**

Put a comment in `0001_schema.py` saying precisely that: `orchestrator.py:162`
creates the schema under the advisory lock before this revision runs, this
statement exists so the chain is also correct if it is ever applied through a
bare `alembic upgrade`, and the `IF NOT EXISTS` is load-bearing rather than
defensive noise. Prompt 5's `tests/postgres/test_module_migrations.py` is the
first thing that executes this chain against a real cluster, and it reds on a
bare `CREATE SCHEMA` — which is the right place for it to red, but not a reason
to write the bare form and wait.

**5. New file `modules/recallatron/tests/test_contract.py`** — the minimal
`contract_tests` entry. This file needs no database and no `conftest.py` — it is
a static smoke check, matching the "trivial smoke assertion" the run brief asks
for, not 1a1/1a2's future behavioural suite. Three assertions:
- **Resolve the manifest through the packaging metadata, not a direct import.**
  `entry_points(group="rheo.modules")`, select the entry named `recallatron`,
  `.load()` it, and assert the loaded object is a `ModuleManifest` and is the
  **same object** as `rheo_recallatron.MANIFEST`. A plain
  `from rheo_recallatron import MANIFEST` passes identically whether or not
  `[project.entry-points."rheo.modules"]` was written correctly, and the entry
  point is the one thing in task 1 that nothing else in this prompt exercises —
  so a direct import here would leave the whole packaging change unverified.
- `MANIFEST.module_id == "recallatron"` (which the entry-point name must also
  equal, or `load_modules()` raises `ManifestInvalid` — the loader gates the
  name).
- The migrations package resolves through `importlib.resources` and contains an
  `env.py`.

**6. Root `pyproject.toml`** — `[tool.pytest.ini_options].testpaths` is
currently `["tests"]` (`:74`); change it to `["tests", "modules"]` so `make test`
and CI actually collect the new `modules/recallatron/tests/test_contract.py`.
The checkpoint below verifies this with a **pathless** `--collect-only`, because
every other command in this prompt names `modules/recallatron` explicitly and
would pass with `testpaths` left alone.

**7. Regenerate `uv.lock`** by running `uv sync` after the `pyproject.toml`
edits above — this is the second (and last) phase in this run allowed to change
`uv.lock`, for Recallatron's new dependency edges.

Nothing installs Recallatron into any workspace in this prompt — no
`modules.installed` entry names it yet, and no operation runs against it. This
prompt only makes it a real, valid, independently-loadable distribution.

## Checkpoint
Seams under test: the packaged `rheo.modules` entry point itself —
`modules/recallatron/tests/test_contract.py` resolves and validates the manifest
the same way the real loader will (through the distribution's own packaging
metadata), not through a shortcut direct import that would pass even if the
entry point were misconfigured.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** that seam before
handoff, because an entry-point test is unusually easy to write vacuously: with
the manifest unchanged, break the `[project.entry-points."rheo.modules"]` value
(point it at a name that does not exist) and confirm `test_contract.py` reds
rather than passing off a direct `from rheo_recallatron import MANIFEST`. Restore
after. A `test_contract.py` that stays green through a broken entry point is
testing the import, not the packaging.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv sync && uv run pytest modules/recallatron tests/test_module_loader.py
uv run pytest --collect-only -q | grep -q 'modules/recallatron/tests/test_contract.py'
make lint && make typecheck && make test && make check
```

The pathless `--collect-only` line is the `testpaths` check from task 6: run from
the worktree root with **no path argument**, pytest collects only what
`testpaths` names, so this grep fails if `["tests", "modules"]` was not written.
Every other command here names `modules/recallatron` explicitly and would pass
either way.

`make check` (`scripts/check_repository.py`) matters here specifically: it
polices which paths are tracked vs. ignored, and the new `migrations/` and
`tests/` directories under `modules/recallatron/` must land on the right side of
that policy. Green means: `uv sync` succeeds with the new entry point resolvable,
the two selected test targets pass, the pathless collection finds the contract
test, and the full suite (now collecting `modules/recallatron/tests/` too) is
green.
````

**Done when:** `modules/recallatron` publishes a real `rheo.modules` entry point
whose manifest validates; its packaged Alembic environment has exactly one
revision, creating the `recallatron` schema idempotently
(`CREATE SCHEMA IF NOT EXISTS`) and nothing else, with
`orchestrator.py:162` untouched; `modules/relationships/`, `modules/leads/` and
`modules/current/` are byte-identical to how this prompt found them;
`modules/recallatron/tests/` collects under `make test`; the checkpoint above is
green.

---

## Prompt 5 — Module migration chains in the orchestrator
**Phase:** Plan Phase 5 — Module migration chains in the orchestrator
**Coder:** The Systemsmith
**Produces:** `run_module_chain` applies a named module's migration chain under
the same advisory lock the core chain uses, writing into its own
`alembic_version_<module_id>` table inside its own schema, and raises to its
caller on failure; `migrate_workspace` keeps refusing a module chain name, now
with a `ValueError` naming the right door instead of `NotImplementedError`.
**Depends on:** Prompt 4 (a real Recallatron migration environment must exist to
exercise `run_module_chain` against).
**Execution-profile:** role-default
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/migrations/orchestrator.py`, optionally new
`packages/core/src/rheo_core/migrations/module_chain.py`,
`packages/core/src/rheo_core/storage/repositories.py`, new
`tests/postgres/test_module_migrations.py`. No generated files, no lockfile
change. Stop boundary: `migrate_workspace`'s `try`/`except Exception` branch and
its `set_workspace_state(..., UNAVAILABLE, ...)` write are read-only in this
prompt — retype the pre-loop guard, change nothing inside the failure branch, and
do not touch either `MigrationResult(chain=CORE_CHAIN)` construction.
`orchestrator.py:161-162` (the advisory lock and the `CREATE SCHEMA IF NOT
EXISTS`) are read-only; see the boxed note in task 1.

````
This implements spec.md's FR 9 and FR 11's structural half, closing AC 10, per
plan.md Phase 5. Read
`packages/core/src/rheo_core/migrations/orchestrator.py` in full before editing
it — in particular `_VERSION_TABLES`, `_check_chain`, `script_location`,
`recorded_revisions`, `run_chain`, and `migrate_workspace`'s failure branch.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

## Task

**1. `orchestrator.py` — a new `version_table_for(chain: str) -> tuple[str, str]`
helper.** For `chain in ("control", "core")` it returns the existing
`_VERSION_TABLES` entry unchanged. For any other `chain` name, it treats `chain`
as a module id and returns `(chain, f"alembic_version_{chain}")` — resolving that
module's manifest through `loaded_manifests()` to confirm it is actually loaded
(raise clearly if it is not).

**This helper has three call sites, not one — all three must be changed, or the
third one below breaks the moment `_check_chain` stops rejecting a module
chain:**
- `_check_chain` itself — its membership test against `_VERSION_TABLES` is
  exactly what is being widened. It must stop unconditionally rejecting any
  chain name outside `("control", "core")`; a name that resolves to a loaded
  module id is now accepted as a valid module chain.
- `recorded_revisions` — its `schema, table =
  _VERSION_TABLES[_check_chain(chain)]` line becomes `schema, table =
  version_table_for(chain)`.
- `run_chain` — its own `schema, _ = _VERSION_TABLES[_check_chain(chain)]` line
  (this is the trap: a bare `_VERSION_TABLES[...]` subscript here would `KeyError`
  on a module chain name the instant `_check_chain` stops rejecting one, because
  `_VERSION_TABLES` itself never gains a module entry). This line must also
  become `schema, _ = version_table_for(chain)`. Everything else in `run_chain` —
  the `advisory_lock(connection, ADVISORY_LOCK_KEY)` call, the
  `CREATE SCHEMA IF NOT EXISTS <schema>` statement (which now creates the
  *module's* schema, because this line resolved it), and the `schema_ahead`
  check — is otherwise unchanged. This is what makes FR 9's "the *same* advisory
  lock" literally true: there is no second lock key anywhere in this file.

  **`orchestrator.py:162` is not yours to remove, condition, or move — not even
  if a module migration appears to duplicate it.** Every one of the six core
  revisions under `packages/core/src/rheo_core/migrations/core/versions/` creates
  tables inside a `core` schema that **no revision creates**: `grep -ri "create
  schema"` over that directory returns nothing, and
  `packages/core/src/rheo_core/storage/provisioning.py:216` records why in a
  comment on the call site — "The orchestrator creates the `core` schema itself,
  under its lock." Line 162 serves the `control` chain the same way. Deleting it
  to "let the module's own migration own schema creation" would leave both
  shipped chains applying DDL into a schema that does not exist. Recallatron's
  `0001_schema.py` (Prompt 4) is `CREATE SCHEMA IF NOT EXISTS` precisely so the
  two statements coexist without a `DuplicateSchema`; if you find yourself
  reaching for `:162` to fix a duplicate-schema error, the bug is a bare
  `CREATE SCHEMA` in the revision, not this line.

**2. `script_location(chain)`** — add a branch for a module chain: resolve
`loaded_manifests()[chain].storage.migrations_path` through
`importlib.resources.files(...)`, keeping the function's existing
"`env.py` must exist" assertion for whatever directory it resolves to.

**3. `migrate_workspace`'s failure guard** — today it raises
`NotImplementedError` for any chain outside `("control", "core")`. Change the
exception type to `ValueError`, and change the message to:

```
module migration chain {chain!r} does not run through migrate_workspace; call
run_module_chain from core.module.install, which surfaces a failure to its
caller instead of marking the workspace unavailable
```

**Do not delete this guard.** `migrate_workspace`'s `try`/`except Exception`
failure branch (further down the function) is already generic over `chains` and
falls through to writing `control.workspace.state = unavailable` on any
exception from any chain in its loop. Deleting the guard — rather than just
retyping it — is exactly the change FR 11 forbids: it would let a single
module's migration failure flip the *entire* workspace's control-plane row
unavailable, taking core and every other module down with it. Leave that
`unavailable`-write branch completely untouched; do not add a chain-kind check
inside it, and do not touch its two `MigrationResult(chain=CORE_CHAIN)`
constructions — none of that changes in this prompt. `ValueError` rather than
`NotImplementedError` because this is a routing rule now, the same class this
function already raises for the control chain on a workspace database — not "not
built yet."

Update this function's module-level docstring paragraph (it currently says
module chains are a later phase's work) to state the two-door rule instead:
`migrate_workspace` is for the unattended startup pass over `core`;
`run_module_chain` is for a caller-initiated module install.

**4. Promote `_error_text` to a public `error_text`** (same body, same file) —
a later prompt's job handler needs to render a chain's own exception the same
way `migrate_workspace` already renders `state_detail` (type name first, then
message, with `DBAPIError.orig` unwrapped).

**5. New file (either `packages/core/src/rheo_core/migrations/module_chain.py`
or a function placed beside `run_chain` in `orchestrator.py` — your call, but
name it clearly)** —

```python
def run_module_chain(
    connection: Connection,
    manifest: ModuleManifest,
    *,
    expected_database: str,
    core_version: str,
) -> None: ...
```

It calls `run_chain(connection, manifest.module_id, expected_database=...)` on
the caller's own connection — no new connection, no new engine. Because
Alembic's version table records only the current head, not the applied history,
enumerate what was *newly* applied by snapshotting `recorded_revisions(connection,
manifest.module_id)` **before** calling `run_chain` and again **after**, then
walking `ScriptDirectory.from_config(build_config(manifest.module_id))
.iterate_revisions(<the after-heads>, <the before-heads, or "base" if the table
did not exist before>)`. Every revision that walk yields is newly applied; write
one `insert_module_schema_version` row per revision, oldest first, with
`schema_version` set to the Alembic revision id, `applied_at` set to now, and
`core_version_at_apply` set to the `core_version` argument (a caller passes
`storage.provisioning.core_version()` for this — the same value the export
restore path already writes to the same column). This function has **no `try`,
no `MigrationResult`, and no access to a control-plane engine** — it raises the
chain's own exception unchanged, so it can never itself reach the
`unavailable`-write branch; only `migrate_workspace` can reach that, and per task
3 above it no longer accepts a module chain at all.

**6. `packages/core/src/rheo_core/storage/repositories.py`** — add
`insert_module_schema_version(conn, *, module_id, schema_version, applied_at,
core_version_at_apply)`. This prompt is the row's only writer for now (the
sole-writer *scan* that proves nothing else may write it is built in the next
prompt, once the second table's writer lands too).

**7. New file `tests/postgres/test_module_migrations.py`** — this is AC 10's
home:
- Against a fresh workspace, call `run_module_chain` for `"recallatron"`.
  Assert: `alembic_version_recallatron` exists **inside the `recallatron`
  schema** (not the public schema, not the core schema); the core's own version
  table is completely untouched; exactly one `core.module_schema_version` row
  exists for `recallatron` afterward.
- **Then call `run_module_chain` for `"recallatron"` a second time on the same
  workspace and assert it is a clean no-op**: no exception, the version table
  still at the same head, and still exactly one `core.module_schema_version` row
  (the before/after revision snapshot in task 5 yields nothing new, so nothing is
  inserted). This is the assertion that keeps Prompt 4's
  `CREATE SCHEMA IF NOT EXISTS` honest and pins the "write one row per *newly*
  applied revision" rule — an implementation that inserted a row per `run_chain`
  call, or that re-created the schema unconditionally, reds here and passes the
  first case.
- FR 11's positive control: `migrate_workspace(backend, workspace_id,
  chains=("recallatron",))` raises `ValueError` whose message names
  `run_module_chain`, and the `control.workspace` row for that workspace is
  unchanged by the attempt. Assert the raised type and message content — a test
  that only checks "no `NotImplementedError` was raised" would pass against an
  implementation that lets the module chain straight through to the
  `unavailable` write, which is precisely the FR 11 violation this control
  exists to catch.

## Checkpoint
Seams under test: `run_module_chain` and `migrate_workspace`'s routing rule, both
exercised at the level of a real Postgres migration run against the test
cluster and a real `control.workspace` row read before/after — not by mocking
`run_chain` or by asserting on a caught exception's type alone without also
reading the row it must not have touched.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff: revert `run_chain`'s `:153` to the bare
`_VERSION_TABLES[_check_chain(chain)]` subscript and confirm the first case reds
with a `KeyError` rather than passing; make `run_module_chain` insert a row
unconditionally instead of per newly-applied revision and confirm the second
(idempotency) case reds; delete `migrate_workspace`'s pre-loop guard entirely and
confirm the FR 11 control reds on the `control.workspace` row read, not only on
the exception type. Restore after each.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/postgres/test_module_migrations.py tests/postgres/test_migrations.py tests/postgres/test_provisioning.py -m postgres
make lint && make typecheck && make test && make check
```

Green means: the three selected Postgres-marked files pass against the real
5433 cluster, and the full suite is green.
````

**Done when:** `run_module_chain` applies Recallatron's one migration under the
same advisory lock as the core chain, writing into its own version table and
appending one `core.module_schema_version` row; `migrate_workspace` raises
`ValueError` naming `run_module_chain` for any module chain and never touches
`control.workspace.state` doing so; the checkpoint above is green.

---

## Prompt 6 — `core.module.install`
**Phase:** Plan Phase 6 — `core.module.install`
**Coder:** The Systemsmith
**Produces:** a workspace can install Recallatron through one dispatched
`core.module.install` operation; every one of its refusals is named exactly per
the spec's refusal vocabulary; a forced migration failure is a failed operation
outcome that leaves `control.workspace.state` untouched.
**Depends on:** Prompt 5 (`run_module_chain` must exist for install's job handler
to call).
**Execution-profile:** role-default
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/storage/repositories.py`, new
`packages/core/src/rheo_core/modules/operations.py`,
`packages/core/src/rheo_core/operations/core_ops.py`,
`packages/core/src/rheo_core/exports/artifact.py`,
`apps/worker/src/rheo_app_worker/main.py`, `tests/harness/registry.py`, new
`tests/harness/modules.py`, new `tests/test_module_state_writers.py`, new
`tests/postgres/test_module_install.py`, `tests/postgres/test_audit_dispatch.py`,
`tests/postgres/test_export_restore.py`. No generated files, no lockfile change.
This is the largest prompt in the run: if the authored diff stops being
reviewable in one sitting, say so rather than absorbing it. Stop boundary:
`work/loop.py`'s `JOB_FAILED` is read-only (see the `error_code` note below);
`enable_harness_module` is rewritten in place, never deleted or renamed;
`THE_SEVENTEEN` and its test are widened, never renamed; `docs/acceptance/phase-1-matrix.md`
is read-only.

````
This implements spec.md's FR 7, FR 10, and FR 11, closing AC 1's install half,
AC 7, AC 11, and AC 12, per plan.md Phase 6. This is the largest prompt in this
run — read all of it before you start, and read
`packages/core/src/rheo_core/operations/dispatch.py`,
`packages/core/src/rheo_core/work/loop.py`, and
`packages/core/src/rheo_core/work/jobs.py` for how a `long_running` operation's
pre-flight/job split and the worker loop's single `job_failed` error code
actually behave today, before writing the handler.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

## Task

**1. `packages/core/src/rheo_core/storage/repositories.py`** — add
`insert_module_state(conn, *, module_id, package_version, state, installed_at,
enabled_at=None, state_detail=None)` and `set_module_state(conn, *, module_id,
state, enabled_at)`. Update this file's module docstring, which currently notes
these writers "arrive with module install" — they are arriving now.

**2. New file `packages/core/src/rheo_core/modules/operations.py`** — the
`core.module.install` operation, split across a synchronous pre-flight handler
and an asynchronous job handler, exactly as `module-contract.md`'s install
section and spec.md's Decision G describe. Do not collapse the two — each
refusal that criteria 11/12/13 ask to see *by name* has to reach the caller from
the layer where it can still be synchronous, or it loses its name inside the
job's one error code.

`ModuleInstallInput`, `ModuleInstallScheduled` — pydantic models
(`ModuleInstallInput` carries at least `module_id: str`; `ModuleInstallScheduled`
carries the minted `operation_id`). The declaration: `safety_class=MUTATE`,
`roles={owner, operator}`, `idempotency=Idempotency.NONE` (a repeat install is
**refused**, never folded into an existing row — `NATURAL` would be wrong here),
`audit=AuditSpec(subject_field=None)`, `long_running=True`.

**The dispatch (pre-flight) handler runs synchronously, in the dispatcher's own
transaction. Five steps, in this exact order — the order matters, because a
refusal reachable without touching the workspace database must arrive before
anything that does:**

1. **`module_unavailable`**, naming the module, when `module_id` is not a key of
   `loaded_manifests()`.
2. **`module_already_installed`**, naming the module's **current state**, when a
   `core.module_state` row already exists for this module in this workspace —
   in **any** state (`installed`, `enabled`, or anything else). This is never a
   no-op and never an upgrade: a silent no-op would swallow the case where the
   deployment-loaded package is a *different* version from the recorded one and
   wrongly report success, and an upgrade path is out of scope for this run.
3. **`dependency_missing`** / **`dependency_version`**, naming the dependency,
   for a *required* `Dependency` of the manifest that is not `installed` (at
   minimum) in this workspace at a version satisfying its range. An *optional*
   dependency that is absent from this workspace is recorded as absent, not
   refused.
4. **`contract_tests_missing`**, naming the resolved path — but **only when the
   resolved profile is `test`**. Resolve the manifest's `contract_tests` string
   against `rheo_core.storage.data_root.find_checkout_root()` and refuse if the
   resolved path does not exist. This check reads the manifest and the
   filesystem only — no workspace database — which is exactly why it belongs at
   pre-flight rather than in the job: `exports/operations.py`'s
   `artifact_missing` refusal is the same shape in an already-shipped
   `long_running` handler. **Install validates that the declared path exists; it
   does not execute the suite.** Running `contract_tests` is CI's job (via the
   `testpaths` widening from Prompt 4) — spawning a test runner from inside a
   mutate operation's own transaction would be re-entrant, since the suite that
   drives install is itself pytest.
5. `enqueue_job(core.module.install, ModuleInstallPayload(workspace_id,
   module_id), max_attempts=1)`; return `ModuleInstallScheduled(operation_id=...)`.
   **`max_attempts=1`, explicitly** — `enqueue_job` makes it a required keyword
   and no requirement names a value, and every failure reachable inside the job
   below is deterministic (a role without extension privilege, a bad revision,
   a failing health check): the shipped default of 8 (via
   `resolve().get_int("work.max_attempts")`, what the three existing
   `long_running` operations pass) would make an operator wait through seven
   pointless retries under up to an hour of backoff for an answer that cannot
   change.

**The job handler (`run_module_install_job`) runs inside the worker's one
transaction. It carries the remaining steps — the ones that genuinely need the
workspace database:**

6. `CREATE EXTENSION IF NOT EXISTS <name>` for every name in the manifest's
   `storage.required_extensions`, **before any migration statement**. A failure
   raises `ModuleInstallFailed("extension_failed", <the extension name>)`, and
   no migration step must have run.
7. `run_module_chain(uow.connection, manifest, expected_database=...,
   core_version=core_version())`, wrapped in exactly one `try`. Any exception
   from it is re-raised as `ModuleInstallFailed("migration_failed",
   orchestrator.error_text(exc)) from exc` and left to propagate — the
   transaction rolls back, and `control.workspace` is untouched (that property
   is Prompt 5's; this step is what calls the function that preserves it).
   **`migration_failed` is never spelled `extension_failed`** — by this step the
   extensions step has already succeeded.
8. `insert_module_state(..., state="installed", package_version=
   manifest.package_version)` — this is FR 7 step 5, and it is deliberately
   ordered *after* the migration so a crash before this line leaves no row and a
   repeat install can proceed cleanly.
9. Under any profile **other than `test`**: run every callable in
   `manifest.health_checks`; a failure raises `ModuleInstallFailed(
   "health_check_failed", <the failing check's name>)`.

`ModuleInstallFailed`'s `__str__()` is `f"{code}: {detail}"`. **Every one of
these three in-job failures arrives to the caller as `error_code="job_failed"`
(`work/loop.py`'s single `JOB_FAILED` — the loop's one code for every route to
`failed`) with `error_text` equal to that `f"{code}: {detail}"` string.** Do
**not** widen `JOB_FAILED` into a per-kind code — that is a shipped write path
shared by every job kind in this system, and this operation gets no special
case. FR 7 step 7 ("configuration keys become settable") needs no code here —
the module's `KeySpec`s were already registered at load time in Prompt 3.

**3. `packages/core/src/rheo_core/operations/core_ops.py`** — register the new
declaration inside `register_core_operations()`. If importing
`rheo_core.modules` from `rheo_core.operations` would close an import cycle,
follow the deferred-import pattern the existing token and approval operations
already use inside that same function — do not put the new declaration in the
module-level `CORE_OPERATIONS` tuple if that pattern is why those two aren't
there either.

**4. `apps/worker/src/rheo_app_worker/main.py`** — register the
`core.module.install` job kind on `JOB_KINDS` beside the four existing
registrations, using the same `JOB_KINDS.register(...)` call shape they use.

**5. `tests/harness/registry.py` — rewrite `enable_harness_module`, do not
delete it.** It currently hand-builds `pg_insert(core_tables.module_state)`
directly. Rewrite its body to call `insert_module_state` instead, keeping its
own signature, its idempotent "insert or do nothing" behavior, and its name
exactly as they are. **Eighteen files under `tests/` depend on this function
existing** — fifteen under `tests/postgres/` plus `tests/harness/consumers.py`,
`tests/harness/registry.py` itself, and `tests/harness/runtime_matrix.py` — for
coverage of `core.workspace.status`, context routing, approvals, and the
`module_disabled` branch. Deleting it would silently drop that coverage, which
this run does not own.

**6. `packages/core/src/rheo_core/exports/artifact.py`** — `_install_modules`
(the export-restore path) currently hand-builds both writes directly:
`insert(core_tables.module_state)` and, further down the same function,
`insert(core_tables.module_schema_version)`. Route **both** through
`insert_module_state` / `insert_module_schema_version` instead, passing the same
columns this call site already supplies — no signature on either writer needs
to widen to accommodate it. This makes export-restore the **third** caller of
the two writers, beside install (this prompt) and enable (the next prompt); the
*writer* clause stays absolute — `repositories.py` remains the only code that
ever inserts into either table. (No migration inserts into either one; the core
chain only creates them. Task 7 relies on that.)

**7. New file `tests/test_module_state_writers.py`** — FR 10's exclusivity
clause and AC 7's second clause: a static scan (AST-based, over `packages/`,
`apps/`, `modules/`, `scripts/` and `tests/`) finding **zero** `insert(...)` /
`pg_insert(...)` constructions against `core_tables.module_state` or
`core_tables.module_schema_version` outside its declared exception set.

**The exception set is `packages/core/src/rheo_core/storage/repositories.py`,
and nothing else — do NOT except "the migrations' own DDL files."** AC 7 words
it as "outside `repositories.py` itself and its own unit tests," and on this tree
no migration carries such a construction at all: `grep -rn
"module_state\|module_schema_version"
packages/core/src/rheo_core/migrations/core/versions/` returns exactly one hit,
a docstring line in `0001_core_schema.py:6` naming the tables it **creates**.
Because this scan is asserted **both ways** (see below), a declared exception
with no matching hit fails the test — so excepting the migrations would red the
scan on its own declared set from the first run. Add a unit-test path to the
exception set only if you actually write a unit test of these two writers that
constructs a raw insert; if you do not, do not declare one.

Assert the set **both ways**, the way `tests/test_absent_behaviour.py:112-136`
already does for `SPIKE_SURVIVORS`: no undeclared file carries a construction,
**and** every declared file still carries one — so a stale exclusion that no
longer needs to exist also fails. Give the scan a positive control too: assert it
finds the constructions inside `repositories.py`, so a scan that has silently
stopped matching (a renamed table attribute, a changed call shape) cannot pass
by finding nothing anywhere.

**Four known live construction sites, in three files, all fixed in this same diff
rather than excepted:**
`packages/core/src/rheo_core/exports/artifact.py:558` and `:571` (task 6 routes
both through the new writers), `tests/harness/registry.py:643` (task 5 rewrites
it), and `tests/postgres/test_export_restore.py:221`, which builds a raw
`insert(core_tables.module_schema_version)` to seed a source workspace — rewrite
that call to use `insert_module_schema_version` instead. A scan whose first real
hits get waived rather than fixed is worth little.

**8. New file `tests/harness/modules.py`** — fixture manifests for the tests in
this and the next task: one declaring a real Postgres extension name in
`storage.required_extensions` (proves the `CREATE EXTENSION IF NOT EXISTS`
mechanism), one declaring an extension name the test database role cannot
create (proves the extension-failure path, distinct from "extension does not
exist"), and a two-module pair with a required dependency between them (for
Prompt 7's enable-dependency case).

These manifests reach `loaded_manifests()` — which is what `core.module.install`'s
first pre-flight step reads — only through the fabricated-`EntryPoint` recipe in
this file's standing-constraints section: a real `EntryPoint` whose `value` points
at a module-level name in this harness module, published via the
`_publish(monkeypatch, ...)` shape at `tests/test_module_loader.py:194-198`, with
`_probe`-suffixed ids and the loader's process-wide tables reset either side.
Export a fixture or helper from `tests/harness/modules.py` that does the
publishing, so Prompts 6 and 7 both drive it the same way and neither invents a
second mechanism. `recallatron` is a real distribution from Prompt 4 on and is
never used as a fixture id — the install/enable tests that need Recallatron use
the real entry point, not a fabricated one.

**9. `tests/postgres/test_audit_dispatch.py` — three edits, all forced by
registering a new mutating operation:**
- Add `"core.module.install"` to `THE_SEVENTEEN` (the module-level frozenset;
  find it near the top of the file).
- The separate `assert len(mutating) == 17` becomes `18` (the next prompt makes
  it `19`).
- `test_one_dispatch_of_every_mutating_kind_leaves_a_matching_audit_record`
  builds a `dispatched` map with one real dispatch per mutating operation and
  asserts none of them refused with one of four specific codes
  (`OPERATION_UNKNOWN`, `ROLE_NOT_PERMITTED`, `MODULE_DISABLED`,
  `AUDIT_SINK_MISSING`) — it needs a `core.module.install` entry now.
  Dispatching with `module_id="no_such_module"` refuses `module_unavailable` at
  pre-flight, which is a refused-but-audited record and needs no worker to
  produce.

**Do not rename `THE_SEVENTEEN` or its test, even though the name is now
stale-sounding at nineteen (after the next prompt).**
`test_the_mutating_set_derived_from_the_registry_is_the_declared_seventeen` is
one of criterion 14's demonstrator pytest node ids, listed verbatim in
`docs/acceptance/phase-1-matrix.md` (a **denied**, read-only file for this run —
you may read it, you may never edit it). `tests/test_acceptance_matrix.py`
resolves every demonstrator node id against `pytest --collect-only`; renaming
this test reds `main` on that check and silently shrinks the absence proof's own
selection in Prompt 8. Instead, widen the frozenset in place and extend
`THE_SEVENTEEN`'s own docstring to record that its name is pinned by an
acceptance record — the same pattern `tests/test_absent_behaviour.py` already
uses for its own probe names.

**10. New file `tests/postgres/test_module_install.py`** — this is AC 11 and
AC 12's home, plus Edge Cases 1, 2, 3, 5, and 7:
- **AC 11, both extension cases:** a fixture manifest with a real
  `required_extensions` entry installs successfully, with the extension present
  afterward; a fixture manifest naming an extension the test role cannot create
  fails install, naming that extension, **and no migration step for the module
  ran** (assert the module's schema/version table do not exist afterward).
- **AC 12, the forced migration failure — read this shape carefully, it is easy
  to get wrong in three specific ways:**
  - Force it by, before dispatching install: creating the `recallatron` schema
    and its `alembic_version_recallatron` table directly in the workspace
    database, and inserting a `version_num` this codebase's revision history
    does not know about. `run_chain`'s existing `schema_ahead` check then raises
    `StorageRefusal(SCHEMA_AHEAD, ...)` from inside `run_module_chain` — you need
    no fixture module for this, just a fresh Recallatron install attempt against
    a workspace pre-seeded this way.
  - **This is a *negative* assertion, not just an outcome check.** Read the
    `control.workspace` row **before and after** the failed install and assert
    it is byte-identical. An implementation that flips the row to `unavailable`
    still fails the install operation itself, so a test that only checks "the
    install failed" would pass against that wrong implementation too — the whole
    point of AC 12 is that the *row* stays untouched.
  - **Assert on `error_text`, never on `error_code`.** All three in-job failures
    arrive as `error_code="job_failed"`; the discriminating name lives only in
    `error_text`. The correct assertion is
    `error_text.startswith("migration_failed: ")` and, for the forced case above,
    `"schema_ahead"` appearing in that text. Asserting `error_code ==
    "extension_failed"` (or any of the three names) will fail, and "fixing" that
    by widening `JOB_FAILED` into a per-kind code is the wrong repair — do not do
    it.
  - **The name is `migration_failed`, never `extension_failed`**, for this
    failure — by the time the chain runs, the extensions step has already
    succeeded, so labeling this failure `extension_failed` would describe the
    wrong event even if some other assertion happened to still pass.
- **Edge Case 1:** install called for a module not in `modules.installed` —
  refused `module_unavailable`, naming it.
- **Edge Case 2:** install called against a module already `installed` or
  `enabled` — refused `module_already_installed`, naming that current state.
- **Edge Case 3:** the extension-cannot-be-created case above, asserted with a
  test role explicitly denied the privilege (distinct from an extension name
  that plain does not exist).
- **Edge Case 5:** exercised by the two-module dependency fixture from task 8 —
  install itself does not need a dependency to be *enabled* (that is enable's
  concern in the next prompt), only *installed*, so this case is really about
  `dependency_missing`/`dependency_version` naming the right dependency and
  range when it is absent or the wrong version.
- **Edge Case 7:** a `modules.installed` entry naming a module id nothing on
  disk provides is not an install-time failure at all — it is simply never
  loaded, so `core.module.install` never even sees it as a candidate; a fixture
  proving this belongs in `tests/test_module_loader.py`-style coverage from
  earlier prompts, not a new assertion here, but note it in this file's module
  docstring so a reader knows it was not missed.

## Checkpoint
Seams under test: `core.module.install` dispatched end-to-end through the real
operation registry and the real worker job loop — pre-flight refusals read as
`OperationRefused` outcomes, in-job failures read as the operation record's
terminal `error_code`/`error_text` — never by calling the handler function
directly, so the exact caller-visible split between the five named
`error_code`s and the three `error_text` prefixes is pinned at the seam a real
caller actually sees.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** every one of these
seam tests before handoff — this is the largest prompt in the run and the one
where a green-but-empty test costs the most. Concretely: for each of the five
pre-flight refusals, delete its step and confirm **only** that refusal's case
reds (a mutation that reds all five means the cases are asserting "it refused,"
not "it refused with this name"); move the `insert_module_state` call from step 8
to before the migration and confirm the AC 12 case reds; make the job handler
swallow the chain exception instead of re-raising and confirm the AC 12
`control.workspace` before/after comparison reds; delete one declared entry from
`tests/test_module_state_writers.py`'s exception set and confirm the both-ways
assertion reds. Restore after each.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/postgres/test_module_install.py tests/test_module_state_writers.py tests/postgres/test_audit_dispatch.py tests/postgres/test_workspace_status.py tests/postgres/test_export_restore.py tests/postgres/test_worker_loop.py -m postgres
make lint && make typecheck && make test && make check
```

Green means: all six selected files pass against the real 5433 cluster,
including the widened `THE_SEVENTEEN` assertions and the rewritten export-restore
insert; the full suite is green.
````

**Done when:** `core.module.install` installs Recallatron into a fresh
workspace, naming every refusal exactly per the vocabulary above; the module
migration failure never touches `control.workspace.state`; `enable_harness_module`
still exists and now calls `insert_module_state`; `THE_SEVENTEEN` is widened, not
renamed; the checkpoint above is green.

---

## Prompt 7 — `core.module.enable`, and criterion 24 end to end
**Phase:** Plan Phase 7 — `core.module.enable`, and criterion 24 end to end
**Coder:** The Systemsmith
**Produces:** a workspace can enable an installed Recallatron through one
dispatched `core.module.enable` operation; installing then enabling Recallatron
through those two operations alone makes `core.workspace.status` report its
version, state, and schema version.
**Depends on:** Prompt 6 (`core.module.install` must exist — enable refuses
unless a module is already `installed`, so nothing can enable until something
can install).
**Execution-profile:** role-default
**Reviewability:** primary authored files —
`packages/core/src/rheo_core/storage/repositories.py`,
`packages/core/src/rheo_core/modules/operations.py`,
`packages/core/src/rheo_core/operations/core_ops.py`,
`tests/postgres/test_audit_dispatch.py`, new
`tests/postgres/test_module_enable.py`, new
`tests/postgres/test_module_lifecycle.py`,
`docs/architecture/module-contract.md`. No generated files, no lockfile change.
Stop boundary: `packages/core/src/rheo_core/boundary/factories.py` is read-only —
it already derives `enabled_modules` from `core.module_state`, and this prompt
gives it a row to read, not a code change. `THE_SEVENTEEN` and its test are
widened, never renamed. `docs/acceptance/phase-1-matrix.md` is read-only.

````
This implements spec.md's FR 8, closing AC 1 (except its carried
interface-contribution half) and AC 13, per plan.md Phase 7.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

## Task

**1. `packages/core/src/rheo_core/storage/repositories.py`** — add
`insert_schedule_if_absent`, mirroring the shape of the one `core.schedule`
insert that already exists today, inside
`packages/core/src/rheo_core/migrations/core/versions/0006_runtime.py` (read
that migration's insert statement for the exact columns a schedule row needs).

**2. `packages/core/src/rheo_core/modules/operations.py`** — add
`ModuleEnableInput`, `ModuleEnabled` (pydantic models), and the
`core.module.enable` declaration: `safety_class=MUTATE`, `roles={owner,
operator}`, **`audit=AuditSpec(subject_field=None)`**, `idempotency=Idempotency.NONE`,
**not** `long_running` (unlike install, every step here is fast and needs no job).

**`audit=` is not optional here and the omission fails loudly, not silently.**
`OperationRegistry.register` refuses any declaration whose `safety_class` is not
`SafetyClass.READ` and whose `audit` is `None`
(`packages/core/src/rheo_core/operations/registry.py:206-212`,
`RegistrationRefused`: "declares no audit spec … above read, and an operation
above read cannot be registered without one"). `subject_field=None` because
enable acts on a module, not on a record: `dispatch`'s `_subject_ref` reads the
named field off the validated input, and registry.py checks any non-`None`
`subject_field` against the input model's `model_fields`, so naming a field the
model does not declare is itself a registration refusal. Prompt 6 states the same
for install; both operations carry it.

The handler runs entirely inside the dispatcher's own
transaction, four steps, **each independently refusable and each its own named
outcome:**

1. Refuse **`module_state_invalid`**, naming the module's **actual current
   state**, unless that state is `installed` or `disabled`.
2. Refuse **`dependency_not_enabled`**, naming the dependency, unless every
   *required* `Dependency` of the manifest is `enabled` (not merely `installed`)
   in this workspace.
3. For every `KeySpec` in the manifest's `configuration_schema` marked
   `explicit_per_workspace`, write its row from the package default — call
   `insert_workspace_setting_if_absent`
   (`packages/core/src/rheo_core/storage/repositories.py`) with
   `settings.encode_text(spec, spec.default)` for the value, **exactly** the way
   `packages/core/src/rheo_core/storage/provisioning.py`'s
   `_step_write_default_settings` (`:226-238`) already calls them — read that
   function before writing this one, and match its call shape rather than
   inventing a new one.

   **Expect an overlap with provisioning, and do not try to remove it.** Prompt 3
   registers a module's `KeySpec`s into the process-global settings `REGISTRY` at
   load time, and `_step_write_default_settings` iterates
   `REGISTRY.explicit_per_workspace()` for every workspace it provisions — so for
   any workspace provisioned *after* the module loaded, those rows already exist
   before enable runs. `insert_workspace_setting_if_absent` is exactly the
   `if_absent` writer that makes both paths safe together: enable covers
   workspaces provisioned before the module loaded, provisioning covers the rest,
   and neither overwrites the other. Do not add a filter to `provisioning.py`
   (out of scope) and do not skip the write here on the assumption provisioning
   did it.

   For every `Schedule` in the manifest marked `enabled_by_default`, insert a
   `core.schedule` row via `insert_schedule_if_absent` (task 1 above).
4. `set_module_state(module_id, state="enabled", enabled_at=now)`. Nothing else
   is needed to make "from the next request, `enabled_modules` includes it"
   true: `packages/core/src/rheo_core/boundary/factories.py` already derives that
   set from `core.module_state` rows where `state == "enabled"` — read it to
   confirm, but do not change it.

**3. `packages/core/src/rheo_core/operations/core_ops.py`** — register the
declaration inside `register_core_operations()`, beside install (follow whatever
pattern Prompt 6 used for install's registration).

**4. `tests/postgres/test_audit_dispatch.py`** — `core.module.enable` joins the
inventory. `THE_SEVENTEEN` now holds **nineteen** names: widen the frozenset;
the length assertion that Prompt 6 made `18` becomes `19`; and add a
`dispatched` entry for `core.module.enable`: dispatching with
`module_id="no_such_module"` refuses `module_state_invalid` naming state
`absent`, which is another refused-but-audited record outside the four codes
that test excludes. **Again: do not rename `THE_SEVENTEEN` or its test** — same
pinned-demonstrator reason as Prompt 6 (`docs/acceptance/phase-1-matrix.md`
line 909, denied and read-only). Widen the frozenset and its docstring only.

**5. New file `tests/postgres/test_module_enable.py`** — AC 13's four steps,
**each tested independently** (not only as a chain):
- State `absent` (never installed) refuses `module_state_invalid`, naming
  `absent`.
- A required dependency that is `installed` but not `enabled` refuses
  `dependency_not_enabled`, naming that dependency — use the two-module fixture
  pair from Prompt 6's `tests/harness/modules.py`.
- `explicit_per_workspace` settings rows and `enabled_by_default` schedule rows
  land correctly from a fixture manifest declaring at least one of each.
- After a successful enable, `WorkspaceContext.enabled_modules` includes the
  module on the *next* request — read this through
  `boundary/factories.py`'s existing derivation (task 2, step 4), which needs no
  code change, only a real row for it to read.

**6. New file `tests/postgres/test_module_lifecycle.py`** — AC 1's end-to-end
proof: against a fresh workspace, dispatch `core.module.install` for
`"recallatron"`, drive the worker loop to completion, then dispatch
`core.module.enable`. Assert `core.workspace.status`'s response lists
`recallatron` under `modules` with a non-null `package_version`, `state ==
"enabled"`, and a non-null `schema_version`. Note in this file's docstring that
AC 1's other clause — "no file under `packages/core/` or `packages/contracts/`
is edited to make this pass" — is proved separately, by Prompt 8's
`tests/test_module_import_graph.py`, not by anything in this test.

**7. `docs/architecture/module-contract.md`** — three corrections in this edit:

1. Beside the install section's step 6 (`:226`, "The module's `contract_tests` run
   when the profile is `test`…"), record that `contract_tests` is *resolved and
   validated* by install and *run* by CI (not executed by install itself), and
   that this resolution happens at pre-flight so `contract_tests_missing` is the
   caller's own refusal code.
2. **The manifest table's `contract_tests` row (`:48`) says the same superseded
   thing in one line — "The module's behavioural suite entry, run by the install
   path in test profile and by CI" — and correction 1 does not reach it.** Fix
   that row too, in the same edit: the path is validated by install, and the
   suite is run by CI (via the `testpaths` widening from Prompt 4). Leaving `:48`
   live would leave the document asserting, in its most-read table, exactly the
   behaviour this run decided against.
3. Beside the criterion-24 paragraph, write the
partial-closure sentence in plain terms: **criterion 24 is only partially closed
by this run** (the module-contract half — install, enable, and the workspace
row's version/schema-version fields), and its interface-contribution half (the
`WebContribution` shape, the `apps/web/src/modules.generated.ts` composition
step, and the test asserting a module's screens appear) belongs to run 1a3. A
reader who marks criterion 24 fully closed after this run alone is reading a
wrong record — say so in the document, not only in this run's own artifacts.

## Checkpoint
Seams under test: `core.module.enable` dispatched through the real operation
registry, and the full install-then-enable lifecycle read back through
`core.workspace.status` — the same seam an operator or CLI caller uses, not a
direct call into either handler function.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff: delete step 1's state check and confirm only the
`module_state_invalid` case reds; relax step 2 from `enabled` to `installed` and
confirm only the `dependency_not_enabled` case reds; drop the
`insert_workspace_setting_if_absent` call and then the `insert_schedule_if_absent`
call in turn and confirm each reds its own assertion (a single "rows landed"
assertion that reds for both is not testing two steps); set state to something
other than `enabled` in step 4 and confirm the `enabled_modules` case reds.
Restore after each.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/postgres/test_module_lifecycle.py tests/postgres/test_module_enable.py tests/postgres/test_workspace_status.py -m postgres
make lint && make typecheck && make test && make check
```

Green means: all three selected files pass against the real 5433 cluster,
including the widened `THE_SEVENTEEN` (now nineteen names); the full suite is
green.
````

**Done when:** `core.module.enable` is registered with an `audit=` spec and
enables an installed Recallatron, naming
`module_state_invalid`/`dependency_not_enabled` exactly where each applies;
installing then enabling Recallatron through the two public operations alone
makes `core.workspace.status` report it as `enabled` with real version numbers;
`module-contract.md` records criterion 24 as partially closed **and** carries the
corrected `contract_tests` wording in both places (`:48` and the install
section's step 6); the checkpoint above is green.

---

## Prompt 8 — The two boundary mechanisms and the absence proof
**Phase:** Plan Phase 8 — The two boundary mechanisms and the absence proof
**Coder:** The Systemsmith
**Produces:** the construction-gate scans cover `modules/*/src`; a separate,
independent mechanism proves Recallatron's storage stays inside its own schema;
and a two-configuration diff proves criterion 33 — that neither `rheo_core` nor
`rheo_contracts` depends on the Recallatron distribution, by configuration and by
absence-on-disk alike.
**Depends on:** Prompt 7 (the absence proof's own phase-two selection needs the
full install-then-enable lifecycle to exist so it can name real test node ids to
exclude from the phase-one comparison; the storage-ownership test needs a real
migration chain to snapshot).
**Execution-profile:** role-default
**Reviewability:** primary authored files —
`tests/test_boundary.py`, `tests/test_boundary_scans.py`, new
`tests/postgres/test_module_storage_ownership.py`, new
`scripts/absence_proof.py`, new `tests/test_absence_proof.py`, new
`tests/test_module_import_graph.py`, `Makefile`,
`.github/workflows/repository-checks.yml`,
`docs/architecture/module-contract.md`. No generated files, no lockfile change.
Stop boundary: `docs/acceptance/phase-1-matrix.md` is read, never written — its
`parse()` is reused from `tests/test_acceptance_matrix.py`, not reimplemented;
`test_no_module_distribution_imports_the_identity_boundary` is a pinned
demonstrator (`docs/acceptance/phase-1-matrix.md:401`) and must not be renamed.
`--mode checkout`'s throwaway worktree is removed unconditionally; the
developer's own tree is never mutated.

````
This implements spec.md's FR 12 and FR 14, closing AC 3, AC 4, and AC 9, per
plan.md Phase 8 — the last phase in this run.

**Scope is `state.json#scope`, and it is authoritative.** Read `RUN_DIR/state.json`'s
`scope.allowed_paths` / `scope.denied_paths` before your first edit. Denied for the
whole run: `apps/web/**`, `apps/mcp/**`, `modules/relationships/**`,
`modules/leads/**`, `docs/requirements/build-plan.md`,
`docs/requirements/requirements-and-scope.md`,
`docs/acceptance/phase-1-matrix.md`, `deploy/**`, `tests/conftest.py`.
`modules/current/**` is the fourth placeholder distribution and appears in neither
list — out of scope exactly as the other placeholders are. A path in neither list
is out of scope: raise it rather than widen the guard.

Specific to this phase: read `phase-1-matrix.md` freely (the absence proof's
`selected` set is built from its own `parse()` function, reused, not rewritten),
but never edit it or `build-plan.md`. `modules/relationships/`, `modules/leads/`
and `modules/current/` all stay one-line placeholders with no entry point; this
run gives an entry point to Recallatron only.

## Task

**1. `tests/test_boundary.py`** — `_SCAN_DIRS` (`:83`, currently `("packages",
"apps", "scripts", "tests")`) gains `"modules"`. This is the B15
`WorkspaceContext`-construction gate; it has never walked `modules/` before
because there was no real code under `modules/*/src` for it to catch anything in.

**2. `tests/test_boundary_scans.py`** — the same `_SCAN_DIRS` tuple in this file
(`:30`) also gains `"modules"` (this is the B9 `SecretScope`-construction gate).
**In the same edit**, `test_scope_privates_are_named_only_inside_the_secrets_package`
(`:204`) currently walks `(*_SCAN_DIRS, "modules")` explicitly — simplify it to
plain `_SCAN_DIRS`, since `"modules"` is now in the tuple and the old explicit
addition would double-walk the same directory. (This one edit is cosmetic and
**no test reds if you skip it**: the walk collects into a dict keyed by path, so
visiting `modules/` twice yields the same result. Do it anyway — a leftover
explicit `"modules"` is the next reader's evidence that the tuple does not
already contain it.) That `:204` line is also the
evidence for what this task is actually fixing: the privates scan already walked
`modules/`; `test_workspace_context_is_constructed_only_in_the_boundary_package`
(`test_boundary.py:182`) and
`test_secret_scope_is_constructed_only_inside_the_secrets_package`
(`test_boundary_scans.py:77`) did not. **That asymmetry is the gap**, and closing
it is AC 9.

Do not rename `test_no_module_distribution_imports_the_identity_boundary` — it is
a pinned demonstrator at `docs/acceptance/phase-1-matrix.md:401`.

**3. A positive control that genuinely reds when `"modules"` is absent from
`_SCAN_DIRS`. Read this whole item: the obvious control cannot fail.**

`_SCAN_DIRS` already contains `"tests"` in **both** files (`test_boundary.py:83`,
`test_boundary_scans.py:30`). So a fixture placed under `tests/fixtures/modules/`
is walked whether or not `"modules"` was ever added, and a control asserting "the
scan catches it" passes identically before and after the change — it proves the
scan runs, not that it was widened. Do not build that. For the same reason,
`harness.absence`'s helpers are the wrong instrument here: they are the
`present=`/`missing=` shape for a *token census*, they record into the global
`_COVERED` map that `tests/test_absent_behaviour.py:173-175` asserts as an exact
one-entry dict, and they cannot express "this root is walked."

Build it as a **root-parameterised re-run of the real scan**, in both files:

- Extract each file's scan loop into a small helper that takes the walk root and
  the scan-dirs tuple and returns **both** counters it currently computes — e.g.
  `_scan(root: Path, scan_dirs: Sequence[str]) -> tuple[int, dict[str, list[str]]]`
  returning `(scanned, violations)` — in `test_boundary.py` (the body of
  `:182-189`) and the equivalent in `test_boundary_scans.py` (`:77-84`). The
  existing tests then call it with `(_REPO_ROOT, _SCAN_DIRS)` and are otherwise
  unchanged: they keep their own `assert scanned, "no Python files scanned"` and
  their existing not-vacuous tails (`assert inside == ["factories.py"]`,
  `assert sites == ["scope.py"]`) exactly as they are. Returning `scanned` rather
  than asserting on it inside the helper matters — under the control's `tmp_path`
  root the other four scan dirs do not exist, so a helper that asserted
  `scanned` internally would raise there for the wrong reason.
- The control test writes a fixture source file at
  `tmp_path / "modules" / "probe_pkg" / "src" / "probe.py"` that really does
  construct the class the gate polices — a `WorkspaceContext(...)` call in
  `test_boundary.py`'s control, a `SecretScope(...)` call in
  `test_boundary_scans.py`'s — then asserts **two** things:
  1. `_scan(tmp_path, _SCAN_DIRS)`'s violations contain it. This is the assertion
     that reds when `"modules"` is missing from the tuple: with the un-widened
     tuple the walk visits `tmp_path/packages`, `tmp_path/apps`,
     `tmp_path/scripts` and `tmp_path/tests`, none of which exist, finds nothing,
     and the control fails.
  2. `_scan(tmp_path, tuple(d for d in _SCAN_DIRS if d != "modules"))`'s
     violations are **empty** — the negative half, which pins that the hit in (1)
     came from the `"modules"` entry specifically and not from some other root
     incidentally covering the path.
- No file is written under the real repository tree, so `make check`'s
  tracked-vs-ignored policy is untouched and a crashed run leaves nothing behind.
- Assert `"modules" in _SCAN_DIRS` in both files as well. It is a one-line
  tautology on its own — AC 9 offers it as the weaker of two options — so it is
  the cheap belt beside the braces above, not a substitute for them.

**4. New file `tests/postgres/test_module_storage_ownership.py`** — this is
AC 3's home, and it is a **deliberately separate mechanism** from tasks 1–3
above: those check that a module must not *mint* a `WorkspaceContext` or
`SecretScope` (an AST scan for named-class construction); this checks that a
module's storage stays inside its own schema and reaches storage only through
the core (a live-database schema diff plus two more scans). The two properties
fail independently of each other — a module can own its schema perfectly and
still mint a `WorkspaceContext` it should not, and a module can respect both
construction gates and still write into someone else's schema — so keep them in
different files, as this task list does. Three separate assertions:
1. Snapshot Postgres's own `pg_class`/`information_schema` before and after a
   fresh Recallatron migration chain run; every relation in the after-minus-before
   difference must be inside the `recallatron` schema.

   **This one needs a control, or it is near-vacuous.** Recallatron's skeleton
   creates no tables, so the difference is a single relation —
   `recallatron.alembic_version_recallatron`, created by Alembic itself — and on
   an empty difference the "all new relations are in `recallatron`" assertion is
   vacuously true and would pass against a chain that did nothing at all. Add
   both halves:
   - **A non-emptiness assertion:** the difference is not empty and contains
     exactly `recallatron.alembic_version_recallatron`. Naming it exactly, rather
     than asserting a count, also pins that the skeleton ships no tables — the
     thing 1a1 will deliberately change.
   - **A positive control that goes red:** run the same before/after snapshot
     around a throwaway revision (or a raw `CREATE TABLE public.probe_leak (...)`
     executed inside the same transaction as the chain, rolled back afterwards)
     that creates one relation **outside** `recallatron`, and assert the check
     reports it. A schema-isolation test that has never seen a leak is a test
     that has never been shown to detect one.
2. No file under `modules/recallatron/src` names another module's
   schema-qualified table name or another module's storage-layer symbols (a
   text/AST scan for cross-module storage references).
3. No file under `modules/recallatron/src` constructs a raw database connection,
   engine, or DSN directly (storage access must happen only through a
   `UnitOfWork`/repository the dispatcher supplies) — another AST scan, this
   time for driver/engine/DSN construction.

**Assertions 2 and 3 need controls too, for the same reason as 1.**
`modules/recallatron/src` holds three small files after Prompt 4 — an
`__init__.py`, a `manifest.py` and a migrations `env.py` — so a scan that
silently resolved to nothing, or whose matcher stopped matching, reports a clean
boundary and looks identical to a real pass. For each: assert the walk parsed a
non-zero number of files, and point the same scan function at a `tmp_path`
fixture file that deliberately does the forbidden thing (names
`core.module_state` for 2; calls `create_engine(...)` / builds a
`postgresql://…` DSN string for 3) and assert it is reported. A scan that has
never been shown to catch its own violation has not been shown to work.

**5. New file `scripts/absence_proof.py`** — four modes, all sharing one inner
step:

- **`--mode run --out <summary.json>`** — the step both configurations share,
  always executed by *that configuration's own interpreter*. Compute
  `selected`, collect, run in-process, write the summary. Run pytest
  **in-process** (`pytest.main([...], plugins=[recorder])` with a small recorder
  object implementing `pytest_collection_finish` and `pytest_runtest_logreport`)
  rather than as a subprocess plugin — a subprocess `-p` plugin would have to be
  importable from the throwaway `--mode checkout` worktree too, which defeats
  the point of that mode. Load `parse` from `tests/test_acceptance_matrix.py` by
  path (`importlib.util.spec_from_file_location`, since `tests/` is not an
  installed package) rather than importing it as a package.
- **`--mode config`** — Recallatron on disk and in the environment,
  `modules.installed` resolved to a value that never names it (the empty
  default), then run the shared inner step.
- **`--mode checkout`** — `git worktree add --detach <tmp> HEAD`; delete
  `modules/recallatron/` inside that copy; `uv sync` **without `--frozen`** (the
  committed lock names `rheo-recallatron`, but the workspace root's `modules/*`
  member glob simply stops matching once the directory is gone — no file needs
  editing for this to resolve cleanly); then `uv run --directory <tmp> python
  scripts/absence_proof.py --mode run` inside that copy; `git worktree remove
  --force` afterward regardless of outcome. The developer's own tree is never
  mutated by this mode.

  **This mode builds from `HEAD`, so it cannot see uncommitted work — and a
  silent comparison of a dirty tree against the last commit is the whole failure
  this proof exists to avoid.** `--mode config` runs in the working tree and
  `--mode checkout` runs in a worktree of `HEAD`, so any uncommitted change makes
  the two configurations differ for a reason that has nothing to do with the
  module boundary. Make the mode **refuse**, non-zero, when `git status
  --porcelain` (run from the repository root, including untracked files) is
  non-empty, printing the offending paths and the single instruction "commit
  before running the absence proof." Commit your own work before running
  `make absence-proof` — the refusal is the reminder, not an obstacle to route
  around with `git stash`.
- **`--mode compare <a.json> <b.json>`** — non-zero on any difference between
  the two summaries.

**The selection, exactly:**

```python
selected = {
    demo.removeprefix("pytest:")
    for row in parse(matrix) for demo in row.demonstrators
    if demo.startswith("pytest:")
}
```

— the phase-one acceptance matrix's **own** `pytest:` demonstrators (102 unique
node ids on this tree), prefix-stripped and deduplicated. **Never the whole
`tests/` tree** — this run's own phase-two tests (AC 1, AC 10 through AC 13) need
Recallatron on disk to pass at all, and would turn a whole-tree diff under the
`--mode checkout` configuration red for a reason that proves nothing about the
boundary. The matrix's `ci:` demonstrators (7 unique) are GitHub Actions
`job / step` pairs, not pytest items — pytest cannot collect them. Record them
under a `ci_excluded` key in the summary and never attempt to run them.

**A summary is `{selected, collected, outcomes, ci_excluded}` — never a bare
pass/fail count.** `collected` is every collected node id that resolves to a
selected id, using the exact-match-or-`<id>[`-parametrised-expansion rule
`tests/test_acceptance_matrix.py`'s own resolver already implements (set
equality is the wrong test here — several base ids expand into multiple
parametrised items). `outcomes` maps each collected node id to
`passed`/`failed`/`error`/`skipped`.

**Five controls, without which this proof cannot fail (build all five; a
comparison of two nothings is otherwise clean):**
1. Every mode exits non-zero when `selected` is empty.
2. Every mode exits non-zero unless every selected id resolves into `collected`
   **and** every collected id maps back to a selected id, naming the
   difference — this is what would catch Prompt 6/7's `THE_SEVENTEEN` test if it
   had been renamed instead of widened.
3. Every mode exits non-zero unless `set(outcomes) == collected` and no outcome
   is `skipped`.
4. `--mode compare` exits non-zero unless both summaries carry the same
   non-empty `selected` (and that set equals what `parse` yields again at
   compare time), both `collected` sets are equal, and the two `outcomes` maps
   are identical key by key. Print every non-`passed` outcome for a human
   reader. A `failed` outcome that is *identical on both sides* is **not** by
   itself a boundary violation and must not by itself red the compare — that
   property ("the phase-one matrix is green") belongs to `make test` and CI's
   `python` job, already enforced on every push; this proof's only claim is
   about the *difference* between the two configurations.
5. `--mode checkout` exits non-zero on a dirty working tree, as above. Without
   it, the two configurations can differ by uncommitted source and the compare
   reports a boundary violation that is not one — or, worse, agrees for the wrong
   reason.

**6. New file `tests/test_absence_proof.py`** — the mutation that proves the
five controls above are load-bearing, in the exact shape
`tests/test_absent_behaviour.py`'s `test_the_positive_controls_are_live`
(`:178-222`) already uses: hand-built summary dictionaries, **no database and no
subprocess pytest run**, one pair per control — a pair differing in a single node
id's outcome, a summary with an empty `selected`, a summary where a selected id
is missing from `collected`, a summary missing an outcome for a collected id, a
summary with one outcome set to `skipped`, and a fake `git status --porcelain`
result that is non-empty — and assert each triggers a non-zero exit from the
relevant mode/control check.

**Each of these is a *pair*, not a single case:** for every control, assert the
bad input exits non-zero **and** that the otherwise-identical good input exits
zero. Without the second half, a control function that returned non-zero
unconditionally would pass all five.

Do **not** import `harness.absence` here. Its helpers record into a process-global
`_COVERED` map that `tests/test_absent_behaviour.py:173-175` asserts as an exact
one-entry dict; a call from this file would red that assertion, and loosening it
would damage the instrument. Copy the *shape* of
`test_the_positive_controls_are_live`, not its imports.

**7. `Makefile`** — add an `absence-proof` target running `config`, then
`checkout`, then `compare`, in that order, failing the whole target if any step
does. Add it to `.PHONY` beside the existing targets. Put a one-line comment
above the target saying the tree must be committed first, because `checkout`
builds from `HEAD` — the refusal in `--mode checkout` enforces it, the comment
saves a cycle. Each step is its own recipe line so the target fails on the first
non-zero; do not chain them with `;`.

**8. `.github/workflows/repository-checks.yml`** — add an `absence-proof` job
beside the existing `repository-checks`, `docker`, `python`, and `web` jobs.
**Copy the `python` job's `services: postgres` block and its
`RHEO_TEST_CLUSTER_DSN` environment variable verbatim** — without a reachable
cluster, 82 of the 102 selected node ids `pytest.exit` in *both* configurations,
which control 3 above correctly reds, but for the wrong reason. Also copy the
same checkout and `uv sync --frozen` steps, then run `make absence-proof`.

**Nothing in the local suite reds if you skip this job**, so assert it. In
`tests/test_absence_proof.py`, reuse `parse_ci_steps`
(`tests/test_acceptance_matrix.py:68`, which already maps each workflow job key
to its `- name:` steps and is read by the acceptance-matrix resolver) and assert
that the `absence-proof` job exists and carries a step running
`make absence-proof`. Its positive control: the same parse finds the shipped
`python` job, so a parser pointed at the wrong file or a renamed workflow cannot
report a clean pass. Import `parse_ci_steps` the way `test_acceptance_matrix.py`
is imported elsewhere in the suite, or load it by path if `tests/` is not an
installed package — do not copy the parser.

**9. New file `tests/test_module_import_graph.py`** — FR 14's third, cheap
check, run in the ordinary suite alongside the two heavier configurations: a
static scan asserting no file under `packages/`, `apps/`, **or `tests/`** names
`rheo_recallatron` or `modules/recallatron`, **except** the declared set of
phase-two test files that exist specifically to drive it (this prompt's own new
files, plus every earlier prompt's new test files that reference Recallatron
directly). Declare that exception set explicitly and assert it **both ways**,
same as task 7 in Prompt 6 — a stale exclusion here must also fail. `tests/` is
scanned deliberately: an exception set of test files means nothing if the root
those files live under is never walked.

Give it a positive control too, for the same reason the census in Prompt 2 needs
one: assert the scan read a non-trivial number of files and that it **does** find
`rheo_recallatron` in at least one declared exception file by the same mechanism.
A scan that resolved to nothing would otherwise report a clean import graph and a
clean exception set simultaneously.

**10. `docs/architecture/module-contract.md`** — the stale sentence "the
no-memory run's test profile sets `modules.installed` to `relationships, leads`"
is replaced with a short description of the two-configuration proof this prompt
builds. None of `modules/relationships/`, `modules/leads/` or `modules/current/`
is a real distribution (each has a placeholder `__init__.py`, `dependencies = []`
and no entry point), so naming those ids and naming none is already the same
configuration on this tree — the corrected text should say so plainly rather
than describing a setup that cannot exist.

## Checkpoint
Seams under test: the absence proof's own `--mode compare` mechanism, mutation-
verified against its five controls in `tests/test_absence_proof.py`, plus the
two boundary mechanisms from tasks 1–4 — each proven to catch its own positive
control before being trusted to pass clean on the real tree.

Load `docs/conventions/tdd-seams.md` and **mutation-verify** these seam tests
before handoff. This phase's whole job is controls, so the mutation pass is the
deliverable, not a formality: remove `"modules"` from `_SCAN_DIRS` in each file
in turn and confirm **that file's** root-parameterised control reds (and its
existing repository-wide test stays green — the two are different claims); make
the AC 3 snapshot ignore the schema qualifier and confirm the leak control reds;
make each of the five absence-proof controls return zero unconditionally and
confirm its pair in `tests/test_absence_proof.py` reds. Restore after each.

**Commit your work before running `make absence-proof`** — `--mode checkout`
builds from `HEAD` and refuses a dirty tree by design.

```
export RHEO_TEST_CLUSTER_DSN=postgresql://rheo:rheo_dev_only@localhost:5433/postgres
uv run pytest tests/test_boundary.py tests/test_boundary_scans.py tests/test_module_import_graph.py tests/test_absence_proof.py tests/postgres/test_module_storage_ownership.py
make absence-proof
make lint && make typecheck && make test && make check
```

Both `--mode config` and `--mode checkout` inside `make absence-proof` need the
Postgres cluster reachable (82 of the 102 selected node ids are `postgres`-marked
and this repo's tests never skip on an unreachable cluster, they `pytest.exit`)
— keep the exported DSN in the same shell for this whole checkpoint. Green
means: the five selected files pass; `make absence-proof` exits 0 with a clean
compare; the full suite is green.
````

**Done when:** `_SCAN_DIRS` in both boundary-scan files walks `modules/`, each
proven against a control that reds when the `"modules"` entry is removed (not one
that a `tests/`-rooted fixture satisfies either way);
`tests/postgres/test_module_storage_ownership.py`
proves Recallatron's schema isolation independently of the construction-gate
scans, with a leak control that has been seen to fail;
`make absence-proof` passes with an identical per-node-id outcome map
between "Recallatron on disk but unloaded" and "Recallatron absent from the
checkout entirely"; the checkpoint above is green. This is the last prompt in
the run — a full run of `make test` from a clean `uv sync` at this point is the
final proof that nothing earlier was left half-migrated.
