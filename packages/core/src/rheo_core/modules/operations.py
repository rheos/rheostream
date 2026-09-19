"""``core.module.install`` and ``core.module.enable``: the two operations FR 7 and FR 8
give a workspace for taking a host-loaded module and making it live in that workspace.

FR 7's install, split across a synchronous dispatch handler and an asynchronous job
handler exactly as ``module-contract.md`` § Install and Decision G describe. The split
is not an implementation detail, it is the whole reason the refusals have names.

**Enable is not split, and the asymmetry is the point.** ``module-contract.md`` §
Enable is four steps — a state check, a dependency check, two ``if_absent`` row
writes, and the ``core.module_state`` update — and every one of them is a bounded
statement against the workspace database the dispatcher has already opened. Nothing
here creates an extension, runs a migration chain or calls a module's code, which is
what made install long-running; so enable declares no ``long_running``, mints no job,
and each of its two refusals reaches the caller under its own ``error_code`` the way
install's pre-flight five do. Its handler runs inside the dispatcher's own
transaction, so a refusal at step 2 leaves nothing of step 3 behind.

**Everything reachable without the workspace database is refused synchronously, and
everything that needs it runs in the job.** A refusal raised from the dispatch handler
reaches the caller as ``OperationRefused`` — its own ``error_code``, its own
``error_text``, an audit record, and a terminal ``core.operation`` row. A failure
raised from the job handler reaches the caller as ``error_code = "job_failed"``
(``work/loop.py``'s single :data:`~rheo_core.work.loop.JOB_FAILED`, the loop's one
code for every route to ``failed``) with the failure's own ``str()`` as
``error_text``. So the five pre-flight refusals keep their names and the three in-job
failures carry theirs in the text instead:

===============================  ===================  ==========================
failure                          ``error_code``       ``error_text``
===============================  ===================  ==========================
``module_unavailable``           itself               the refusal's detail
``module_already_installed``     itself               the refusal's detail
``dependency_missing``           itself               the refusal's detail
``dependency_version``           itself               the refusal's detail
``contract_tests_missing``       itself               the refusal's detail
extension step                   ``job_failed``       ``extension_failed: <name>``
migration step                   ``job_failed``       ``migration_failed: <text>``
health-check step                ``job_failed``       ``health_check_failed: <name>``
===============================  ===================  ==========================

Widening ``JOB_FAILED`` into a per-kind code is not the repair for a test that wanted
the third column in the second: that constant is a shipped write path shared by every
job kind in this system, and this operation gets no special case.

**Install validates ``contract_tests``; it does not execute them.** Running the suite
is CI's job, through the ``testpaths`` widening that puts ``modules/`` in ``make
test``. Spawning a test runner from inside a mutate operation's own transaction would
be re-entrant — the suite that drives install *is* pytest — and it would put an
arbitrary subprocess inside a database transaction holding the workspace's advisory
lock. A named deviation from ``module-contract.md``'s install step 6, which still says
the tests "run".

**The declared path is resolved against the checkout, and only under ``profile =
test``.** A ``contract_tests`` value is repo-relative (Recallatron's is
``modules/recallatron/tests``) and that directory is **absent from the built wheel** —
a distribution packages its source, not its test suite. So the check is meaningful
only in a checkout, the ``profile == "test"`` guard is what excludes a module installed
from a wheel into a deployment that has none, and the refusal says the declared path is
absent from the checkout rather than that the module is broken.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from packaging.specifiers import SpecifierSet
from pydantic import BaseModel, ConfigDict
from rheo_contracts import WorkspaceContext
from sqlalchemy import text

from rheo_core.migrations.module_chain import run_module_chain
from rheo_core.migrations.orchestrator import error_text
from rheo_core.modules.loader import loaded_manifests
from rheo_core.modules.manifest import ModuleManifest
from rheo_core.operations.refusals import OperationRefused
from rheo_core.settings import current_profile, encode_text
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import get_workspace
from rheo_core.storage.data_root import find_checkout_root
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import (
    ModuleStateRow,
    insert_module_state,
    insert_schedule_if_absent,
    insert_workspace_setting_if_absent,
    list_module_states,
    set_module_state,
)
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

MODULE_INSTALL: Final = "core.module.install"
"""The operation name, and the job kind's name too.

One string for both deliberately: the job is this operation's own continuation and
nothing else enqueues it, so a second spelling would be a second thing to keep in
step. ``apps/worker``'s ``JOB_KINDS.register(MODULE_INSTALL, ...)`` reads this name.
"""

MODULE_ENABLE: Final = "core.module.enable"
"""The operation name. Unlike :data:`MODULE_INSTALL` it names no job kind.

Enable enqueues nothing: every one of its four steps is a bounded statement inside the
dispatcher's transaction, so there is no continuation for a job kind to carry.
"""

MODULE_UNAVAILABLE: Final = "module_unavailable"
MODULE_ALREADY_INSTALLED: Final = "module_already_installed"
DEPENDENCY_MISSING: Final = "dependency_missing"
DEPENDENCY_VERSION: Final = "dependency_version"
CONTRACT_TESTS_MISSING: Final = "contract_tests_missing"

MODULE_STATE_INVALID: Final = "module_state_invalid"
DEPENDENCY_NOT_ENABLED: Final = "dependency_not_enabled"

EXTENSION_FAILED: Final = "extension_failed"
MIGRATION_FAILED: Final = "migration_failed"
HEALTH_CHECK_FAILED: Final = "health_check_failed"

INSTALLED_STATE: Final = "installed"
ENABLED_STATE: Final = "enabled"
TEST_PROFILE: Final = "test"

ABSENT_STATE: Final = "absent"
"""What :data:`MODULE_STATE_INVALID` names for a module with no row at all.

Not one of ``core_tables.MODULE_STATES``, deliberately: ``absent`` is the first state
of ``module-contract.md`` § Lifecycle's diagram and the one with no row to record it,
so it is a name this refusal supplies rather than a value it read. A module nothing
ever installed and a module whose row says ``removed`` are different answers and the
refusal gives each its own.
"""

_ENABLEABLE_STATES: Final = frozenset({INSTALLED_STATE, "disabled"})
"""The two ``core.module_state.state`` values enable accepts (§ Enable step 1).

``disabled`` is in the set because re-enable is the same operation: § Re-enable's
contract is "same checks as enable", and a disabled module's schema and rows are
already there. ``enabled`` is **not** in it — enable is not idempotent and says so,
the same call Decision D makes for a repeat install — and neither is ``removed``,
whose bindings are revoked and whose way back is restore.
"""

_FIRST_SCHEDULE_RUN: Final = timedelta(days=1)
"""How far ahead a schedule created at enable first becomes due.

The same offset ``migrations/core/versions/0006_runtime.py`` stamps on the core's own
``core.retention_sweep`` row, and the same one ``work/schedules.py``'s
``run_due_schedules`` advances by on every tick. A schedule due the instant it is
created would fire on the next worker visit, which is not what "created at enable"
means anywhere in the contract.
"""

INSTALL_ATTEMPTS: Final = 1
"""``max_attempts`` for the enqueued job, explicitly rather than by the setting.

``enqueue_job`` makes it a required keyword and no requirement names a value. Every
failure reachable inside the job is deterministic — a role without extension
privilege, a revision this code does not know, a health check that says no — so the
shipped default of 8 (``work.max_attempts``, what the three existing ``long_running``
operations pass) would make an operator wait through seven pointless retries under up
to an hour of backoff for an answer that cannot change.
"""

_INSTALLED_AT_MINIMUM: Final = frozenset({"installed", "enabled", "disabled"})
"""The ``core.module_state.state`` values that satisfy a required dependency here.

Install needs its dependency's *schema* to be present in this workspace, which is true
of all three: ``disabled`` blocks the module's operations, not its tables. Requiring
``enabled`` is ``core.module.enable``'s own check (``module-contract.md`` § Enable
step 2) and duplicating it here would refuse an install that enable would then have
refused anyway, with the wrong name on it. ``removed`` is excluded — the module is on
its way out of this workspace and its bindings are revoked — and so is a module with no
row at all.
"""


class ModuleInstallInput(BaseModel):
    """The module to install. The workspace comes from the context."""

    model_config = ConfigDict(extra="ignore")

    module_id: str


class ModuleInstallScheduled(BaseModel):
    """What the dispatch returns: the minted record the worker will terminalise."""

    model_config = ConfigDict(frozen=True)

    operation_id: UUID


class ModuleInstallPayload(BaseModel):
    """The job's stored input.

    ``workspace_id`` is carried even though the worker already opened a unit of work on
    that workspace's database: it is what :func:`run_module_install_job` reads the
    registry row with, so ``run_module_chain``'s ``expected_database`` is the name the
    *control plane* holds for this workspace rather than the name of whatever database
    the connection happens to be on. Deriving it from the connection would make the
    orchestrator's wrong-pool assertion compare a value with itself.

    ``package_version`` is the version the **dispatcher** validated against, carried so
    the worker can refuse a version that moved underneath it. ``_LOADED``
    (``modules/loader.py``) is a module-level table and the dispatcher runs in
    ``apps/core`` while the job runs in ``apps/worker``, so the two answer
    independently: a rolling deploy or a worker restart between the enqueue and the
    lease can leave the handler holding a different version of the same module id.
    Without this field the chain that ran would be version B's while the dependency
    ranges and the ``contract_tests`` path that cleared pre-flight were version A's.
    """

    workspace_id: UUID
    module_id: str
    package_version: str


class ModuleInstallFailed(Exception):
    """One of the three in-job failures, rendered the way the caller reads it.

    ``str()`` is ``f"{code}: {detail}"``, and that is a caller-visible contract rather
    than a log line: ``work/loop.py``'s exception arm writes ``str(failure)`` into the
    job's ``last_error`` and into the operation record's ``error_text`` verbatim. The
    three codes are the only things that distinguish these failures from each other
    downstream, because all three arrive under ``error_code = "job_failed"``.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ModuleEnableInput(BaseModel):
    """The module to enable. The workspace comes from the context.

    The same one field ``ModuleInstallInput`` carries, declared separately rather than
    shared: the input model is half of what the OpenAPI document publishes for an
    operation, and two operations pointed at one model would make a later change to
    either one a change to both.
    """

    model_config = ConfigDict(extra="ignore")

    module_id: str


class ModuleEnabled(BaseModel):
    """What a successful enable returns: the module, and when the row says so.

    ``enabled_at`` rather than a bare acknowledgement because it is the one fact the
    caller cannot read back from ``core.workspace.status``, which reports a module's
    version, state and schema version and not its timestamps.
    """

    model_config = ConfigDict(frozen=True)

    module_id: str
    enabled_at: datetime


def _minted_operation(uow: UnitOfWork) -> UUID:
    """The record ``dispatch()`` minted for this ``long_running`` call.

    ``core.runtime.run``'s handler makes the same check for the same reason: a
    ``long_running`` declaration promises the caller an operation id, and a handler
    reached without one has nothing to stamp on the job it is about to enqueue.
    """
    if not isinstance(uow, HandlerUnitOfWork) or uow.operation_id is None:
        raise OperationRefused(
            "output_invalid",
            f"{MODULE_INSTALL} is long-running and needs a minted operation",
        )
    return uow.operation_id


def _refuse_unless_loaded(module_id: str) -> ModuleManifest:
    """Step 1. The cheapest refusal, and the only one that reads no database at all."""
    manifests = loaded_manifests()
    manifest = manifests.get(module_id)
    if manifest is None:
        raise OperationRefused(
            MODULE_UNAVAILABLE,
            f"module {module_id!r} is not loaded by this deployment "
            f"(loaded: {sorted(manifests)})",
        )
    return manifest


def resolved_contract_tests(manifest: ModuleManifest) -> Path:
    """Where ``manifest.contract_tests`` resolves, as install's pre-flight resolves it.

    Public so a test, and a module author's own contract suite, can ask the question
    install asks rather than rebuilding the join and drifting from it.
    """
    return find_checkout_root() / manifest.contract_tests


def module_install_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ModuleInstallInput
) -> ModuleInstallScheduled:
    """The dispatch (pre-flight) handler: five refusals, then the enqueue.

    Runs synchronously in the dispatcher's own transaction, so each refusal reaches
    the caller under its own ``error_code``. **The order matters**: a refusal reachable
    without touching the workspace database comes before anything that does, so a
    module nothing on this host provides is refused without a query, and the
    filesystem check that cannot vary per workspace is the last thing before the
    enqueue.
    """
    minted = _minted_operation(uow)
    module_id = model_input.module_id

    # 1. module_unavailable
    manifest = _refuse_unless_loaded(module_id)

    rows = {row.module_id: row for row in list_module_states(uow.connection)}

    # 2. module_already_installed — in ANY state, naming the state it is in, and never
    #    a no-op. A silent success for a module that already has a row would swallow
    #    the case that matters: the package this deployment loaded being a *different
    #    version* from the one the row records, reported as an install that happened.
    #    Upgrade is out of scope for this run (``module-contract.md`` § Upgrade, on
    #    paper), so the honest answer names the current state and lets the caller act.
    existing = rows.get(module_id)
    if existing is not None:
        raise OperationRefused(
            MODULE_ALREADY_INSTALLED,
            f"module {module_id!r} is already {existing.state!r} in this workspace "
            f"at version {existing.package_version}; install is not an upgrade and "
            "not a no-op",
        )

    # 3. dependency_missing / dependency_version, naming the dependency and the range.
    for dependency in manifest.dependencies:
        present = rows.get(dependency.module_id)
        if present is None or present.state not in _INSTALLED_AT_MINIMUM:
            if dependency.optional:
                # ``optional = True`` means the dependency may be absent from this
                # workspace. Nothing records the absence — there is no column for it —
                # so it is skipped, and the module installs beside whatever is here.
                continue
            found = "is not installed" if present is None else f"is {present.state!r}"
            raise OperationRefused(
                DEPENDENCY_MISSING,
                f"module {module_id!r} requires {dependency.module_id!r} at "
                f"{dependency.version_range!r}, which {found} in this workspace",
            )
        if not SpecifierSet(dependency.version_range).contains(present.package_version):
            raise OperationRefused(
                DEPENDENCY_VERSION,
                f"module {module_id!r} requires {dependency.module_id!r} at "
                f"{dependency.version_range!r}, but this workspace has "
                f"{dependency.module_id!r} at {present.package_version}",
            )

    # 4. contract_tests_missing — under profile ``test`` only; see the module docstring
    #    for why a wheel-installed module is deliberately not covered.
    if current_profile() == TEST_PROFILE:
        resolved = resolved_contract_tests(manifest)
        if not resolved.exists():
            raise OperationRefused(
                CONTRACT_TESTS_MISSING,
                f"module {module_id!r} declares contract_tests "
                f"{manifest.contract_tests!r}, which is absent from this checkout "
                f"at {resolved}",
            )

    # 5. The job, and the id the caller gets back.
    enqueue_job(
        uow.connection,
        kind=MODULE_INSTALL,
        payload=ModuleInstallPayload(
            workspace_id=ctx.workspace_id,
            module_id=module_id,
            # The version every check above was made against, so the worker can tell
            # whether it is installing the same thing that cleared pre-flight.
            package_version=manifest.package_version,
        ).model_dump(mode="json"),
        now=datetime.now(UTC),
        max_attempts=INSTALL_ATTEMPTS,
        operation_id=minted,
    )
    return ModuleInstallScheduled(operation_id=minted)


def _expected_database(workspace_id: UUID) -> str:
    """The database name the control plane holds for this workspace.

    Read on the control engine rather than derived from the worker's own connection,
    so ``run_chain``'s wrong-pool assertion compares two independently obtained
    answers. ``exports/operations.py``'s ``_owner_account_id`` reaches the control
    plane from inside a handler the same way.
    """
    with get_backend().control_engine.connect() as control:
        row = get_workspace(control, workspace_id)
    if row is None:
        raise RuntimeError(f"workspace {workspace_id} has no control-plane row")
    return row.database_name


def _create_extensions(uow: HandlerUnitOfWork, manifest: ModuleManifest) -> None:
    """Step 6. Every declared extension, before any migration statement.

    The name is quoted through the dialect's own preparer rather than interpolated
    bare: it arrives from a third-party distribution's manifest, and an identifier
    from a package is still an identifier from outside this repository.

    A failure aborts the surrounding transaction as well as raising, which is exactly
    why this step is first: nothing of the migration can have run.
    """
    preparer = uow.connection.dialect.identifier_preparer
    for extension in manifest.storage.required_extensions:
        try:
            uow.connection.execute(
                text(f"CREATE EXTENSION IF NOT EXISTS {preparer.quote(extension)}")
            )
        except Exception as exc:
            raise ModuleInstallFailed(EXTENSION_FAILED, extension) from exc


def run_module_install_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """The job handler: extensions, the chain, the row, then the health checks.

    Runs inside the worker's one transaction, so every step commits together or none
    of them does. Nothing here touches ``control.workspace``: a module's bad revision
    is this operation's failure and not the workspace's, which is the property
    ``run_module_chain`` exists to preserve (FR 11) and which this function is simply
    the caller of.
    """
    assert isinstance(payload, ModuleInstallPayload)
    token.checkpoint()
    manifest = loaded_manifests().get(payload.module_id)
    if manifest is None:
        # Deliberately not a ``ModuleInstallFailed``: the three in-job codes name
        # install's own failure modes, and a worker whose loaded set disagrees with
        # the dispatcher's is a deployment fault — the same class as an unreachable
        # database — rather than an outcome of installing this module.
        raise RuntimeError(
            f"this worker has not loaded module {payload.module_id!r}, which the "
            "dispatcher that enqueued this job had loaded; check that every process "
            "resolves the same modules.installed"
        )
    if manifest.package_version != payload.package_version:
        # **The same fault as above, one notch finer, closed the same way.** The check
        # above catches a worker that has not loaded the module at all; this catches
        # one that has loaded a *different version* of it. Both are the dispatcher's
        # loaded set and this worker's disagreeing, both are a deployment fault rather
        # than an outcome of installing this module, and both are therefore a bare
        # ``RuntimeError`` — no fourth ``ModuleInstallFailed`` code, so the ratified
        # vocabulary of five pre-flight refusals and three in-job prefixes is
        # untouched.
        #
        # Refused rather than allowed-and-recorded, because the pre-flight verdict is
        # what goes stale: the dependency ranges and the ``contract_tests`` path that
        # cleared belong to the version the dispatcher held, and nothing re-checks
        # them here. The row would have recorded the version whose chain really ran,
        # so the *record* was never the problem — the unchecked install was.
        raise RuntimeError(
            f"module {payload.module_id!r} was {payload.package_version} when this "
            f"install was dispatched and is {manifest.package_version} in this "
            "worker; the dependency and contract-test checks were made against the "
            "version that is no longer loaded, so nothing is installed"
        )

    # 6. Extensions first, so a failure here cannot have run a migration statement.
    _create_extensions(uow, manifest)

    # 7. The chain. One try, and the exception is re-raised under its own name rather
    #    than caught: the transaction rolls back and the caller reads why.
    try:
        run_module_chain(
            uow.connection,
            manifest,
            expected_database=_expected_database(payload.workspace_id),
            core_version=core_version(),
        )
    except Exception as exc:
        raise ModuleInstallFailed(MIGRATION_FAILED, error_text(exc)) from exc

    # 8. The row, *after* the migration — FR 7 step 5. Ordered last of the two so a
    #    crash between them leaves no row and a repeat install can proceed cleanly,
    #    rather than a row claiming a schema that is not there. The ordering guards a
    #    *crash*; running both in one transaction is what guards a *failure*, and that
    #    half is pinned — the trailing checkpoint below is a real failure after this
    #    line, and ``tests/postgres/test_module_install.py::test_a_cancellation_after
    #    _the_last_write_rolls_the_module_state_row_back`` uses it to prove this row
    #    goes down with the rollback. The ordering itself no test can observe, because
    #    two writes in one transaction land or vanish together.
    insert_module_state(
        uow.connection,
        module_id=manifest.module_id,
        package_version=manifest.package_version,
        state=INSTALLED_STATE,
        installed_at=datetime.now(UTC),
    )

    # 9. The health checks, in every profile but ``test`` — where the declared
    #    contract tests are the check, and CI runs them.
    if current_profile() != TEST_PROFILE:
        for check in manifest.health_checks:
            try:
                check(uow)
            except Exception as exc:
                raise ModuleInstallFailed(
                    HEALTH_CHECK_FAILED, getattr(check, "__name__", repr(check))
                ) from exc
    token.checkpoint()


# --- core.module.enable ---------------------------------------------------------------


def _enable_refuse_unless_state(rows: dict[str, ModuleStateRow], module_id: str) -> str:
    """Step 1. The module's actual state, or the refusal naming it.

    First, and before the loaded-manifest lookup, because the state is the question a
    caller asking "can this be enabled" is asking: a module id nothing ever installed
    is ``absent`` here, not ``module_unavailable``, and an operator who mistyped the id
    reads the same answer as one who forgot to install it.
    """
    row = rows.get(module_id)
    current = ABSENT_STATE if row is None else row.state
    if current not in _ENABLEABLE_STATES:
        raise OperationRefused(
            MODULE_STATE_INVALID,
            f"module {module_id!r} is {current!r} in this workspace; "
            f"enable needs {sorted(_ENABLEABLE_STATES)}",
        )
    return current


def _refuse_unless_dependencies_enabled(
    rows: dict[str, ModuleStateRow], manifest: ModuleManifest
) -> None:
    """Step 2. Every **required** dependency ``enabled`` in this workspace.

    ``enabled`` and not merely ``installed``, which is the whole difference from
    install's own dependency check: install needs the dependency's *schema* to exist
    (``_INSTALLED_AT_MINIMUM``), enable needs its *behaviour* to be live, because from
    the next request this module's code may call it.

    An optional dependency is skipped entirely, matching install and matching
    ``module-contract.md`` § Enable step 2's "required dependencies". An optional
    dependency that is present but disabled is therefore not a refusal: ``optional``
    says this module runs without it, and a module that cannot is declaring the wrong
    flag.
    """
    for dependency in manifest.dependencies:
        if dependency.optional:
            continue
        present = rows.get(dependency.module_id)
        if present is None or present.state != ENABLED_STATE:
            found = f"is {ABSENT_STATE}" if present is None else f"is {present.state!r}"
            raise OperationRefused(
                DEPENDENCY_NOT_ENABLED,
                f"module {manifest.module_id!r} requires {dependency.module_id!r} to "
                f"be {ENABLED_STATE!r} in this workspace, which {found}",
            )


def _write_enable_rows(
    uow: UnitOfWork, manifest: ModuleManifest, *, now: datetime
) -> None:
    """Step 3. The ``explicit_per_workspace`` settings rows and the default schedules.

    **The settings write overlaps ``storage/provisioning.py``'s step 4 on purpose.**
    ``modules/loader.py``'s ``_register`` puts a module's ``KeySpec``s into the
    process-global settings registry at load time, and ``_step_write_default_settings``
    iterates ``REGISTRY.explicit_per_workspace()`` for every workspace it provisions —
    so a workspace provisioned *after* the module loaded already has these rows before
    enable runs, and a workspace provisioned before it does not. Both writers go
    through :func:`~rheo_core.storage.repositories.insert_workspace_setting_if_absent`,
    which is what makes the two safe together: neither overwrites a value the workspace
    has since set, and neither may assume the other ran.

    The specs are read off **this manifest** rather than out of that registry, which is
    the narrower and the correct source: the registry holds every loaded module's keys,
    and enabling one module must not write another's.
    """
    for spec in manifest.configuration_schema:
        if not spec.explicit_per_workspace:
            continue
        insert_workspace_setting_if_absent(
            uow.connection,
            key=spec.key,
            value=encode_text(spec, spec.default),
            value_type=spec.type,
            updated_by=None,
        )
    for schedule in manifest.schedules:
        if not schedule.enabled_by_default:
            continue
        insert_schedule_if_absent(
            uow.connection,
            module_id=manifest.module_id,
            name=schedule.name,
            job_kind=schedule.job_kind,
            cron=schedule.cron,
            enabled=True,
            next_run_at=now + _FIRST_SCHEDULE_RUN,
        )


def module_enable_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ModuleEnableInput
) -> ModuleEnabled:
    """§ Enable's four steps, in the dispatcher's own transaction.

    Each step is independently refusable and none of them is reached out of order: a
    module in the wrong state never has its dependencies read, and a module whose
    dependency is disabled never has a settings row or a schedule written. That is what
    running inside one transaction buys — a refusal at any step leaves the workspace
    exactly as it found it, including the rows step 3 would have written.

    ``ctx`` is unused beyond the routing the dispatcher already did with it: the
    connection ``uow`` carries **is** this workspace's, so a second read of
    ``ctx.workspace_id`` would be asking the same question twice.
    """
    module_id = model_input.module_id
    rows = {row.module_id: row for row in list_module_states(uow.connection)}

    # 1. module_state_invalid, naming the state this workspace actually holds.
    _enable_refuse_unless_state(rows, module_id)

    # The manifest, needed from step 2 on. Reached *after* step 1 so the state answer
    # wins for an id this deployment does not load; a module whose row says
    # ``installed`` while the deployment has stopped loading it is the deployment
    # fault ``module_unavailable`` already names, and it gets that name here too
    # rather than a second one meaning the same thing.
    manifest = _refuse_unless_loaded(module_id)

    # 2. dependency_not_enabled, naming the dependency and what it is instead.
    _refuse_unless_dependencies_enabled(rows, manifest)

    # 3. The declared rows, before the state moves: a failure writing either of them
    #    must not leave a module reported enabled without the configuration and the
    #    schedules enabling it is defined to create.
    now = datetime.now(UTC)
    _write_enable_rows(uow, manifest, now=now)

    # 4. The row. ``set_module_state`` answers whether one matched, and the answer is
    #    read rather than discarded: step 1 established the row exists, so a ``False``
    #    here means it stopped existing between that read and this write — a
    #    concurrent remove, the one thing this transaction cannot see. Raised as a
    #    plain error rather than a refusal because it is not the caller's mistake and
    #    carries no vocabulary of its own; the dispatcher rolls the transaction back
    #    and reports ``handler_failed``, which is the honest answer for a state that
    #    is supposed to be unreachable. Nothing else is needed to make "from the next
    #    request, ``enabled_modules`` includes it" true: ``boundary/factories.py``
    #    derives that set from these rows on every context it builds.
    if not set_module_state(
        uow.connection, module_id=module_id, state=ENABLED_STATE, enabled_at=now
    ):
        raise RuntimeError(
            f"module {module_id!r} had a core.module_state row when this enable "
            "began and has none now; nothing is enabled"
        )
    return ModuleEnabled(module_id=module_id, enabled_at=now)
