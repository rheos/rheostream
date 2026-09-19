"""``core.module.install``: the five pre-flight refusals and the four job steps.

FR 7's install, split across a synchronous dispatch handler and an asynchronous job
handler exactly as ``module-contract.md`` § Install and Decision G describe. The split
is not an implementation detail, it is the whole reason the refusals have names.

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

from datetime import UTC, datetime
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
from rheo_core.settings import current_profile
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.storage.control_plane import get_workspace
from rheo_core.storage.data_root import find_checkout_root
from rheo_core.storage.postgres import get_backend
from rheo_core.storage.provisioning import core_version
from rheo_core.storage.repositories import insert_module_state, list_module_states
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

MODULE_INSTALL: Final = "core.module.install"
"""The operation name, and the job kind's name too.

One string for both deliberately: the job is this operation's own continuation and
nothing else enqueues it, so a second spelling would be a second thing to keep in
step. ``apps/worker``'s ``JOB_KINDS.register(MODULE_INSTALL, ...)`` reads this name.
"""

MODULE_UNAVAILABLE: Final = "module_unavailable"
MODULE_ALREADY_INSTALLED: Final = "module_already_installed"
DEPENDENCY_MISSING: Final = "dependency_missing"
DEPENDENCY_VERSION: Final = "dependency_version"
CONTRACT_TESTS_MISSING: Final = "contract_tests_missing"

EXTENSION_FAILED: Final = "extension_failed"
MIGRATION_FAILED: Final = "migration_failed"
HEALTH_CHECK_FAILED: Final = "health_check_failed"

INSTALLED_STATE: Final = "installed"
TEST_PROFILE: Final = "test"

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
    """

    workspace_id: UUID
    module_id: str


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
            workspace_id=ctx.workspace_id, module_id=module_id
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
    #    rather than a row claiming a schema that is not there.
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
