"""FR 3 / AC 10: the sealed view a handler receives, and its refusal through the real
dispatch path.

Seams under test: ``HandlerUnitOfWork``'s ``commit``/``rollback``/``__enter__``
refusals, and ``dispatch()`` handing the handler that view rather than the unit of work
it commits itself. The second half is driven through ``dispatch()`` against a real
workspace, not by calling a handler directly: the claim is about what the *dispatcher*
passes, and a direct call would prove only that the subclass raises.

Each test registers its own handler under ``core.settings.set`` on a **local**
``OperationRegistry``, so the process-wide registry is never touched. No ``spike.*``
name is registered anywhere here: ``modules/spike`` does not exist until C2, and this
chunk's checkpoint runs before it.

What is deliberately **not** asserted: that a handler cannot write at all.
``view.connection`` is inherited and hands back a live ``Connection``, so
``view.connection.commit()`` still ends the transaction. That gap is run 0v's finding
F1, and ``test_the_connection_is_not_sealed_and_that_is_the_known_gap`` pins it as a
fact rather than leaving it to be rediscovered.
"""

from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_contracts import (
    AuditSpec,
    Idempotency,
    OperationDeclaration,
    Role,
    SafetyClass,
    WorkspaceContext,
)
from rheo_core.boundary import context_for_harness
from rheo_core.operations import (
    FAILED,
    HANDLER_FAILED,
    HANDLER_MAY_NOT_COMMIT,
    OperationRegistry,
    dispatch,
)
from rheo_core.operations.core_ops import SETTINGS_SET, SettingWrite, SettingWritten
from rheo_core.settings import CORE_ORIGIN, ValueType
from rheo_core.storage.backend import (
    UNIT_OF_WORK_CLOSED,
    HandlerUnitOfWork,
    StorageRefusal,
    UnitOfWork,
)
from rheo_core.storage.repositories import upsert_workspace_setting, workspace_settings
from rheo_core.storage.routing import open_unit_of_work

pytestmark = pytest.mark.postgres

PROBE_KEY = "identity.token_max_days.cli"
"""A real, workspace-scope, writable key. ``tests/postgres/test_settings_floor_e2e.py``
writes 30 against a package default of 90 and it is accepted, so nothing here depends
on a harness key or on the floor engine."""

PROBE_PAYLOAD = {"key": PROBE_KEY, "value": 30}


@pytest.fixture
def owner(workspace: UUID, owner_account_id: UUID) -> WorkspaceContext:
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


DECLARATION = OperationDeclaration(
    name=SETTINGS_SET,
    safety_class=SafetyClass.MUTATE,
    roles=frozenset({Role.OWNER}),
    input_model=SettingWrite,
    output=SettingWritten,
    idempotency=Idempotency.NONE,
    audit=AuditSpec(subject_field=None),
)


def _write(ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite) -> None:
    upsert_workspace_setting(
        uow.connection,
        key=model_input.key,
        value=str(model_input.value),
        value_type=ValueType.INT,
        updated_by=ctx.actor.id,
    )


def _written(model_input: SettingWrite) -> SettingWritten:
    return SettingWritten(
        key=model_input.key, value=model_input.value, scope="workspace"
    )


def _rows(cluster: ClusterSession, workspace: UUID) -> dict[str, str]:
    row = cluster.registry_row(workspace)
    engine = cluster.backend.pools.engine_for(row.database_name)
    with UnitOfWork(engine, row.database_name) as uow:
        return workspace_settings(uow.connection)


# --- the view itself ------------------------------------------------------------------


def test_the_view_shares_the_connection_and_refuses_to_end_the_transaction(
    owner: WorkspaceContext,
) -> None:
    uow = open_unit_of_work(owner)
    with uow:
        view = HandlerUnitOfWork(uow)
        # The same connection, so a handler can still do its work.
        assert view.connection is uow.connection
        for refused in (view.commit, view.rollback):
            with pytest.raises(StorageRefusal) as excinfo:
                refused()
            assert excinfo.value.state == HANDLER_MAY_NOT_COMMIT
        with pytest.raises(StorageRefusal) as entered, view:
            pass
        assert entered.value.state == HANDLER_MAY_NOT_COMMIT
        # The real unit of work is untouched by any of that.
        uow.commit()


def test_the_view_adds_nothing_to_the_pinned_unit_of_work_surface() -> None:
    """D-2's whole reason for a subclass: ``UnitOfWork`` is a pinned 0c-boundary shape
    (``tests/postgres/test_isolation.py``), and this adds nothing to it."""
    assert HandlerUnitOfWork.__slots__ == ()
    assert issubclass(HandlerUnitOfWork, UnitOfWork)
    assert {name for name in dir(UnitOfWork) if not name.startswith("_")} == {
        "commit",
        "connection",
        "rollback",
        "verify_database",
    }


def test_the_view_wraps_only_a_unit_of_work() -> None:
    with pytest.raises(TypeError):
        HandlerUnitOfWork(object())  # type: ignore[arg-type]


# --- through the real dispatch path ---------------------------------------------------


def test_a_handler_may_not_end_the_transaction(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext
) -> None:
    """AC 10: a handler that commits is refused ``handler_may_not_commit`` and nothing
    it wrote persists."""

    def _commit_early(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
    ) -> SettingWritten:
        _write(ctx, uow, model_input)
        uow.commit()
        return _written(model_input)

    registry = OperationRegistry()
    registry.register(DECLARATION, _commit_early, origin=CORE_ORIGIN)
    before = _rows(cluster, workspace)

    outcome = dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD, registry=registry)

    assert outcome.state == HANDLER_MAY_NOT_COMMIT, outcome
    assert outcome.result is None
    assert outcome.error is not None
    assert outcome.error.error_code == HANDLER_MAY_NOT_COMMIT
    # The write is gone: the refusal rolled the one transaction back.
    assert _rows(cluster, workspace) == before


def test_a_handler_may_not_roll_back_either(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext
) -> None:
    def _roll_back(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
    ) -> SettingWritten:
        uow.rollback()
        return _written(model_input)

    registry = OperationRegistry()
    registry.register(DECLARATION, _roll_back, origin=CORE_ORIGIN)
    outcome = dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD, registry=registry)
    assert outcome.state == HANDLER_MAY_NOT_COMMIT, outcome


def test_the_handler_receives_the_sealed_view_not_the_dispatchers_own(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext
) -> None:
    """The mutant this kills: passing ``uow`` straight through, which is exactly the
    state ``main`` is in today (F1). Type identity, not behaviour, because a handler
    that never calls ``commit`` cannot otherwise tell the two apart."""
    seen: list[UnitOfWork] = []

    def _observe(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
    ) -> SettingWritten:
        seen.append(uow)
        _write(ctx, uow, model_input)
        return _written(model_input)

    registry = OperationRegistry()
    registry.register(DECLARATION, _observe, origin=CORE_ORIGIN)

    assert dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD, registry=registry).ok
    assert len(seen) == 1
    assert type(seen[0]) is HandlerUnitOfWork
    # And the dispatcher's own commit still lands the handler's write.
    assert _rows(cluster, workspace)[PROBE_KEY] == "30"


def test_the_connection_is_not_sealed_and_that_is_the_known_gap(
    cluster: ClusterSession, workspace: UUID, owner: WorkspaceContext
) -> None:
    """Finding F1, pinned rather than left to be rediscovered.

    ``overview.md``'s "A handler cannot commit on its own" is true of ``commit`` and
    ``rollback`` after this run and still false of ``view.connection``, which the
    subclass inherits and cannot close without narrowing the handler protocol. If a
    later run closes that path, this is the test that should go red, and it should be
    deleted along with the finding.

    It also corrects F1 on one point of fact: the connection path does **not** end in
    ``unit_of_work_closed``. See the comment on the assertions below.
    """

    def _commit_through_the_connection(
        ctx: WorkspaceContext, uow: UnitOfWork, model_input: SettingWrite
    ) -> SettingWritten:
        _write(ctx, uow, model_input)
        uow.connection.commit()
        return _written(model_input)

    registry = OperationRegistry()
    registry.register(DECLARATION, _commit_through_the_connection, origin=CORE_ORIGIN)

    outcome = dispatch(owner, SETTINGS_SET, PROBE_PAYLOAD, registry=registry)

    # The write is already durable, and the caller is told the operation FAILED.
    assert _rows(cluster, workspace)[PROBE_KEY] == "30"
    assert outcome.state == FAILED, outcome
    assert outcome.error is not None
    assert outcome.error.error_code == HANDLER_FAILED

    # Worth stating precisely, because F1 predicts the other refusal. Two escape
    # hatches, two different endings:
    #   uow.commit()            -> UnitOfWork.commit() clears ``_transaction``, so
    #                              the dispatcher's own commit raises
    #                              ``unit_of_work_closed`` (what F1 describes).
    #   uow.connection.commit() -> the unit of work never learns, so its
    #                              ``transaction.commit()`` raises SQLAlchemy's
    #                              InvalidRequestError, which the dispatcher folds
    #                              into ``failed``/``handler_failed``.
    # After this run the first is closed and the second is not, and the second is
    # the worse of the two: a committed write reported as a failure.
    assert UNIT_OF_WORK_CLOSED not in (outcome.state, outcome.error.error_code)
