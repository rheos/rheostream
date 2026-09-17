"""Criterion 19's last sentence / AC 9: a standing grant naming an upper-class
operation is refused at grant time, naming it.

Seams: ``core.standing_grant.create``'s class check against the live registry, run
before anything is written; ``core.standing_grant.revoke``'s durable revocation and
what it does to the grant's coverage; and the declared roles of both.

**Nothing here reads ``core.standing_grant`` with a ``SELECT``.** The grant's stored
shape — who it is for, what it covers, whether it still covers it — is read back off
``core.standing_grant.create`` and ``.revoke``, which publish the row *after* the
write by reading it again rather than returning what the handler meant to store. The
one exception is the row-count control in
``test_a_refused_grant_writes_no_row_at_all``, which is not a read of a grant: it is
how "no row at all" is asserted, and it has to look where a row would have been.

**The refusal is asserted for all three upper classes**, not only for the destructive
one the criterion names. ``harness.fixture.act`` is ``DESTRUCTIVE`` and
``harness.sink.send`` is ``EXTERNAL``, and both are refused through a real dispatch.
**Nothing anywhere in the tree is ``FINANCIAL``**, so that third class gets a
different instrument and says so at its own test: the set the handler tests membership
in is asserted directly. That is weaker than a dispatch and it is what is available —
stated rather than papered over.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from conftest import ClusterSession
from harness.registry import (
    FIXTURE_ACT,
    NOTE_WRITE,
    SINK_SEND,
    add_member,
    enable_harness_module,
    register_harness,
)
from rheo_contracts import Role, SafetyClass, WorkspaceContext
from rheo_core.approvals import (
    GUARDED_CLASSES,
    STANDING_GRANT_CLASS_REFUSED,
    STANDING_GRANT_CREATE,
    STANDING_GRANT_REVOKE,
    STANDING_GRANT_STATE,
)
from rheo_core.approvals.grant_tables import standing_grant, standing_grant_operation
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import (
    WORKSPACE_STATUS,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.refusals import OPERATION_UNKNOWN
from rheo_core.storage.backend import UnitOfWork
from sqlalchemy import Engine, func, select

pytestmark = pytest.mark.postgres


# --- fixtures and helpers -------------------------------------------------------------


@pytest.fixture(autouse=True)
def registrations(harness_keys: None) -> None:
    """The shipped core operations and the suite's harness module, on the
    process-wide registries. Both are idempotent."""
    register_core_operations()
    register_harness()


@pytest.fixture
def database(cluster: ClusterSession, workspace: UUID) -> str:
    return cluster.registry_row(workspace).database_name


@pytest.fixture
def engine(cluster: ClusterSession, database: str) -> Engine:
    return cluster.backend.pools.engine_for(database)


@pytest.fixture
def owner(
    workspace: UUID, owner_account_id: UUID, engine: Engine, database: str
) -> WorkspaceContext:
    """An owner context over a workspace whose ``harness`` module is enabled.

    The module row is what puts ``harness`` in the context's ``enabled_modules``.
    ``core.standing_grant.create`` does not need it — the class check reads the
    registry, not the workspace — but the two harness operations this file names have
    to be registered for the check to find them, and registration is what
    ``register_harness`` does.
    """
    with UnitOfWork(engine, database) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


def _create(ctx: WorkspaceContext, account_id: UUID, *names: str) -> OperationOutcome:
    return dispatch(
        ctx,
        STANDING_GRANT_CREATE,
        {"account_id": str(account_id), "operation_names": list(names)},
    )


def _record(outcome: OperationOutcome) -> Any:
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


def _row_counts(engine: Engine, database: str) -> tuple[int, int]:
    """``(grants, grant operations)`` in this workspace.

    The only direct table read in this file, and it is a count rather than a row: a
    refused grant has no id to read it back by, so "nothing was written" can only be
    asserted where the rows would have been.
    """
    with UnitOfWork(engine, database) as uow:
        grants = uow.connection.execute(
            select(func.count()).select_from(standing_grant)
        ).scalar_one()
        operations = uow.connection.execute(
            select(func.count()).select_from(standing_grant_operation)
        ).scalar_one()
    return int(grants), int(operations)


# --- the grant-time class refusal -----------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "safety_class"),
    [
        pytest.param(FIXTURE_ACT, "destructive", id="destructive"),
        pytest.param(SINK_SEND, "external", id="external"),
    ],
)
def test_a_grant_naming_an_upper_class_operation_is_refused_naming_it(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    operation: str,
    safety_class: str,
) -> None:
    """AC 9's refusal half, for the two upper classes this tree has examples of.

    The refusal **names the operation**, which is the part the criterion asks for and
    the part a caller who listed twenty operations needs: a state alone would leave
    them to guess which one. The class is named too, so the message says why rather
    than only that.
    """
    outcome = _create(owner, owner_account_id, operation)

    assert outcome.state == STANDING_GRANT_CLASS_REFUSED, outcome
    assert outcome.error is not None
    assert operation in outcome.error.error_text
    assert safety_class in outcome.error.error_text


def test_the_financial_class_is_refused_too(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """The third upper class, which no registered operation carries.

    ``GUARDED_CLASSES`` is the set the handler tests membership in, and the two tests
    above exercise two of its three members. A predicate narrowed to those two would
    pass every other test in this file while letting a financial operation into a
    grant, so the third member is asserted directly on the set the code reads — the
    same instrument the tree uses elsewhere for a clause with no shipped example.

    Asserting the *set* rather than registering a throwaway ``FINANCIAL`` operation is
    deliberate: a registration on the process-wide registry would leak into every
    other test in the session, and one on a private registry would not be the registry
    the handler consults, so the test would prove nothing about the real path.
    """
    assert SafetyClass.FINANCIAL in GUARDED_CLASSES
    assert GUARDED_CLASSES == frozenset(
        {SafetyClass.DESTRUCTIVE, SafetyClass.EXTERNAL, SafetyClass.FINANCIAL}
    )


def test_a_refused_grant_writes_no_row_at_all(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    engine: Engine,
    database: str,
) -> None:
    """The refusal happens before the write, so no partial grant survives it.

    The list names an acceptable operation **first** and the destructive one second,
    on purpose: a handler that wrote rows as it validated would leave the first one
    behind, and a test that named only the offender could not tell that apart from
    refusing before anything ran.

    The row counts are taken before and after, so this cannot pass on an empty
    workspace by accident — and the successful grant afterwards is the control that
    proves the counter counts.
    """
    before = _row_counts(engine, database)

    refused = _create(owner, owner_account_id, WORKSPACE_STATUS, FIXTURE_ACT)

    assert refused.state == STANDING_GRANT_CLASS_REFUSED, refused
    assert _row_counts(engine, database) == before
    # The control: the same call without the offender writes exactly one grant and
    # one operation row.
    _record(_create(owner, owner_account_id, WORKSPACE_STATUS))
    assert _row_counts(engine, database) == (before[0] + 1, before[1] + 1)


def test_a_grant_naming_an_unregistered_operation_is_refused(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """A name the registry does not know cannot have its class checked, so it refuses.

    Without this the class check would be trivially passable: an operation nobody has
    registered would fall through it, and the grant would cover a name that could later
    be registered as destructive.
    """
    outcome = _create(owner, owner_account_id, "core.nothing.here")

    assert outcome.state == OPERATION_UNKNOWN, outcome
    assert outcome.error is not None
    assert "core.nothing.here" in outcome.error.error_text


# --- what a grant that is accepted actually records -----------------------------------


def test_a_grant_naming_only_lower_class_operations_is_accepted(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """AC 9's acceptance half: ``read``, ``draft`` and ``mutate`` are all fine.

    ``core.workspace.status`` is ``read`` and ``harness.note.write`` is ``mutate``, so
    this covers both ends of the permitted range rather than one. The published record
    is asserted field by field, because a grant that stored the *caller's* account id
    instead of the named one, or dropped half the list, would still answer ``ok``.
    """
    record = _record(_create(owner, owner_account_id, WORKSPACE_STATUS, NOTE_WRITE))

    assert record.actor_kind == "account"
    assert record.actor_id == owner_account_id
    assert record.granted_by_id == owner.actor.id
    assert record.revoked_at is None
    assert record.operation_names == sorted([WORKSPACE_STATUS, NOTE_WRITE])
    assert record.covers == sorted([WORKSPACE_STATUS, NOTE_WRITE])


def test_a_grant_may_name_another_account(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
) -> None:
    """The grant is for the named account, not for whoever created it.

    This is the case ``account_id`` exists for — an owner granting standing permission
    to a member — and the assertion is that the two ids on the record differ in the
    right direction.
    """
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="granted-member"
    )

    record = _record(_create(owner, member_id, WORKSPACE_STATUS))

    assert record.actor_id == member_id
    assert record.granted_by_id == owner.actor.id
    assert record.actor_id != record.granted_by_id


def test_a_repeated_operation_name_is_stored_once(
    owner: WorkspaceContext, owner_account_id: UUID, engine: Engine, database: str
) -> None:
    """The operation table's primary key is ``(grant_id, operation_name)``, so a
    caller who lists a name twice must not meet a unique violation.

    The row count is the assertion, not the published list: de-duplicating only in the
    published record would leave the database to reject the insert.
    """
    before = _row_counts(engine, database)

    record = _record(
        _create(owner, owner_account_id, WORKSPACE_STATUS, WORKSPACE_STATUS, NOTE_WRITE)
    )

    assert record.operation_names == sorted([WORKSPACE_STATUS, NOTE_WRITE])
    assert _row_counts(engine, database) == (before[0] + 1, before[1] + 2)


def test_an_empty_operation_list_is_input_invalid(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """A grant covering nothing is refused by the input model, so it needs no refusal
    state of its own."""
    outcome = _create(owner, owner_account_id)

    assert outcome.state == "input_invalid", outcome


# --- revocation -----------------------------------------------------------------------


def test_revoking_records_the_revocation_and_ends_the_coverage(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """``revoke`` stamps ``revoked_at`` durably, and the grant covers nothing after it.

    Two assertions, and the second is the one with meaning: ``revoked_at`` alone is a
    timestamp, and what a reader needs to know is whether the grant still grants
    anything. ``covers`` is that answer, read off the supported operation.

    ``operation_names`` is asserted **unchanged**, because a revocation must not erase
    the record of what was granted — a grant whose list was deleted would read as a
    grant that never covered anything.
    """
    created = _record(_create(owner, owner_account_id, WORKSPACE_STATUS, NOTE_WRITE))
    assert created.covers == sorted([WORKSPACE_STATUS, NOTE_WRITE])

    revoked = _record(
        dispatch(owner, STANDING_GRANT_REVOKE, {"grant_id": str(created.grant_id)})
    )

    assert revoked.grant_id == created.grant_id
    assert revoked.revoked_at is not None
    assert revoked.covers == []
    assert revoked.operation_names == sorted([WORKSPACE_STATUS, NOTE_WRITE])


def test_revoking_twice_is_refused_rather_than_silently_repeated(
    owner: WorkspaceContext, owner_account_id: UUID
) -> None:
    """The second revoke refuses ``standing_grant_state`` and does not move the
    instant.

    Moving ``revoked_at`` forward on a second call would rewrite when the permission
    ended, which is the one fact the row exists to hold.
    """
    created = _record(_create(owner, owner_account_id, WORKSPACE_STATUS))
    first = _record(
        dispatch(owner, STANDING_GRANT_REVOKE, {"grant_id": str(created.grant_id)})
    )

    second = dispatch(owner, STANDING_GRANT_REVOKE, {"grant_id": str(created.grant_id)})

    assert second.state == STANDING_GRANT_STATE, second
    assert second.error is not None
    assert first.revoked_at.isoformat() in second.error.error_text


def test_revoking_a_grant_that_does_not_exist_is_not_found(
    owner: WorkspaceContext,
) -> None:
    """An id from another workspace and an id that never existed get the same answer,
    which is what keeps a grant's existence from being probeable."""
    outcome = dispatch(owner, STANDING_GRANT_REVOKE, {"grant_id": str(uuid4())})

    assert outcome.state == "not_found", outcome


# --- roles ----------------------------------------------------------------------------


def test_only_an_owner_may_grant_or_revoke(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    cluster: ClusterSession,
    workspace: UUID,
) -> None:
    """``module-contract.md``'s row for the pair: ``owner``, and nothing else.

    The member half is the assertion that matters — ``core.approval.approve`` next
    door *does* carry ``member``, so "the approval operations and the grant operations
    have the same roles" is a live wrong belief this refuses. The operator half is the
    other direction: several core mutate operations carry ``operator`` and these two
    do not.
    """
    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="not-an-owner"
    )
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    operator = context_for_operator(workspace)
    assert isinstance(operator, WorkspaceContext), operator

    assert _create(member, owner_account_id, WORKSPACE_STATUS).state == (
        "role_not_permitted"
    )
    assert _create(operator, owner_account_id, WORKSPACE_STATUS).state == (
        "role_not_permitted"
    )

    # The control: the same call under an owner succeeds, so the two refusals are
    # about the role and not about the payload.
    created = _record(_create(owner, owner_account_id, WORKSPACE_STATUS))
    assert (
        dispatch(
            member, STANDING_GRANT_REVOKE, {"grant_id": str(created.grant_id)}
        ).state
        == "role_not_permitted"
    )
    assert (
        _record(
            dispatch(owner, STANDING_GRANT_REVOKE, {"grant_id": str(created.grant_id)})
        ).revoked_at
        is not None
    )
