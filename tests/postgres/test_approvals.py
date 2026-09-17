"""Criterion 19, first half / AC 8 and AC 10's operation half: the approval record,
the dispatcher's upper-class gate, and approve/refuse.

Seams: ``dispatch()``'s ``DESTRUCTIVE``/``EXTERNAL`` branch (mint the approval, hold
the operation record, **never** enter the handler); ``core.approval.approve`` and
``.refuse``'s state transitions and their effect on the held record; the payload
digest recomputed at execution and the refusal when it does not match.

**Nothing here reads ``core.approval`` with a ``SELECT``** (AC 8). The durable row —
its state, its ``payload_digest``, the subject revision captured when it was created —
is read back off ``core.approval.approve``/``.refuse``, which publish the row **after**
the transition by reading it again rather than by returning what the handler meant to
write. The one direct write to ``core.approval_payload`` below is a deliberate tamper,
not a read: it is how a stored payload comes to differ from the digest that binds it,
which is the only way criterion 60's "a differing byte" can arise once the snapshot is
taken.

**The expected digest is computed here from literals**, with ``hashlib`` and
``json.dumps``, and never by calling ``rheo_core.approvals.binding``: a test that
computed its expectation with the code under test would agree with any canonicalisation
that function happened to use, including a wrong one.

**The control that keeps the gate's assertions honest** is
``test_a_mutate_operation_is_not_held``: the same dispatcher, the same workspace, a
``MUTATE`` operation that runs immediately. Without it, every "nothing happened"
assertion below would also pass on a dispatcher that refused everything.
"""

import hashlib
import json
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import (
    FIXTURE_ACT,
    NOTE_WRITE,
    SINK_SEND,
    act_payload,
    add_member,
    enable_harness_module,
    note_ref,
    recorded_acts,
    register_harness,
    sent_messages,
)
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE, APPROVAL_REFUSE, APPROVAL_STATE
from rheo_core.approvals.binding import INVALID_APPROVAL
from rheo_core.approvals.tables import approval_payload
from rheo_core.audit import AUDIT_LIST
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import (
    OPERATION_GET,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.postgres import PostgresBackend
from sqlalchemy import Engine, update

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

    The module row is what puts ``harness`` in the context's ``enabled_modules``;
    without it every ``harness.*`` dispatch refuses ``module_disabled`` before its
    handler, and each assertion below would pass for the wrong reason.
    """
    with UnitOfWork(engine, database) as uow:
        enable_harness_module(uow.connection)
        uow.commit()
    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return ctx


@pytest.fixture
def note(owner: WorkspaceContext) -> str:
    """A real ``harness.note`` record, so the gated call has a subject that
    resolves."""
    outcome = dispatch(owner, NOTE_WRITE, {"body": "the record the fixture acts on"})
    assert outcome.ok, outcome
    result: Any = outcome.result
    return str(result.ref)


def _expected_digest(payload: dict[str, object]) -> str:
    """The digest the approval must carry, computed with stdlib only.

    Sorted keys and no whitespace at every level — the canonical form
    ``confirmation-and-safety.md`` fixes for the column — and ``hexdigest`` because
    that is how the record publishes it.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _acts(engine: Engine, database: str) -> tuple[str, ...]:
    """Every request ``harness.fixture.act`` has recorded in this workspace."""
    with UnitOfWork(engine, database) as uow:
        return recorded_acts(uow.connection)


def _sends(engine: Engine, database: str) -> tuple[str, ...]:
    with UnitOfWork(engine, database) as uow:
        return sent_messages(uow.connection)


def _held(
    ctx: WorkspaceContext, name: str, payload: dict[str, object]
) -> tuple[UUID, UUID]:
    """Dispatch a gated call and assert it was held; return ``(approval, operation)``.

    Asserts the dispatcher's own half of criterion 19 on the way through: the state is
    ``approval_required`` — distinct from ``succeeded`` and from ``failed`` — and it
    carries both identifiers rather than one and a sentence.
    """
    outcome = dispatch(ctx, name, payload)
    assert outcome.state == "approval_required", outcome
    assert not outcome.ok
    assert outcome.approval_id is not None, outcome
    assert outcome.operation_id is not None, outcome
    assert outcome.result is None, outcome
    return outcome.approval_id, outcome.operation_id


def _approve(ctx: WorkspaceContext, approval_id: UUID) -> OperationOutcome:
    return dispatch(ctx, APPROVAL_APPROVE, {"approval_id": str(approval_id)})


def _refuse(ctx: WorkspaceContext, approval_id: UUID) -> OperationOutcome:
    return dispatch(ctx, APPROVAL_REFUSE, {"approval_id": str(approval_id)})


def _operation_state(ctx: WorkspaceContext, operation_id: UUID) -> str:
    """The held record's state, through the supported read (AC 19)."""
    outcome = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
    assert outcome.ok, outcome
    record: Any = outcome.result
    return str(record.state)


def _record(outcome: OperationOutcome) -> Any:
    assert outcome.ok, outcome
    assert outcome.result is not None
    return outcome.result


# --- the gate: an upper-class call is held and nothing runs ---------------------------


def test_a_destructive_dispatch_is_held_and_the_fixture_records_nothing(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """Criterion 19, sentence 2: the call gets an approval-required state carrying an
    approval identifier, the fixture recorded no request, and no operation record
    reports success.

    The three assertions are about three different places — the outcome, the harness
    table and the operation record — because a gate that answered
    ``approval_required`` while still running the handler would pass any one of them
    alone.
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))

    assert approval_id != operation_id
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"


def test_an_external_dispatch_is_held_and_the_sink_sends_nothing(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """The same gate for the external class, against the recording sink.

    Both classes, not one: ``_APPROVAL_CLASSES`` is a set of three and a branch keyed
    on ``DESTRUCTIVE`` alone would pass the destructive case above while letting every
    external send run unapproved.
    """
    payload: dict[str, object] = {"destination": "party:one", "message": "not sent"}
    _held(owner, SINK_SEND, payload)

    assert _sends(engine, database) == ()


def test_a_mutate_operation_is_not_held(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """The control: a ``MUTATE`` dispatch still runs immediately and mints no
    approval.

    This is what makes every "nothing ran" assertion in this module evidence about
    the *class branch* rather than about a dispatcher that stopped running anything.
    """
    outcome = dispatch(owner, NOTE_WRITE, {"body": "a mutate operation is not gated"})

    assert outcome.ok, outcome
    assert outcome.approval_id is None
    assert outcome.operation_id is None


# --- approve: the operation runs exactly once, and the row is readable ----------------


def test_approving_runs_the_fixture_once_and_the_record_carries_the_digest(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """Criterion 19, sentence 3 / AC 8: approval executes the fixture exactly once and
    a durable approval row carrying the payload digest is readable through a supported
    operation.

    The count is the assertion, not the return value: a handler invoked twice inside
    one transaction returns exactly what a handler invoked once returns, and only the
    rows it wrote tell the two apart.

    The digest is compared against one computed here from literals, so this also pins
    the canonical form rather than only the fact that *some* digest was stored.
    """
    payload = act_payload(note)
    approval_id, operation_id = _held(owner, FIXTURE_ACT, payload)

    record = _record(_approve(owner, approval_id))

    assert _acts(engine, database) == (note,)
    assert record.approval_id == approval_id
    assert record.state == "executed"
    assert record.operation_name == FIXTURE_ACT
    assert record.operation_id == operation_id
    assert record.payload_digest == _expected_digest(payload)
    # Captured when the approval was created, from the live record, which is what
    # ``RecordStateGuard`` compares at execution. ``harness.note`` reports the
    # constant 1.
    assert record.subject_ref == note
    assert record.subject_revision == 1
    assert record.approved_by_id == owner.actor.id
    assert record.approved_entry == owner.entry.value
    assert record.executed_at is not None
    # The held record is terminal, with the check that was actually made: the handler
    # returned. Left at ``approval_required`` it would be the one state criterion 13
    # forbids — a caller polling a record for work that has already happened.
    assert _operation_state(owner, operation_id) == "succeeded"


def test_a_second_approval_does_not_execute_the_fixture_again(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The second approve refuses ``approval_state`` and nothing runs a second time.

    The predicate on ``mark_approved`` is what this pins: a read-then-write would let
    two approvers of one approval both reach the handler.
    """
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, approval_id))

    second = _approve(owner, approval_id)

    assert second.state == APPROVAL_STATE, second
    assert _acts(engine, database) == (note,)


def test_refusing_moves_the_approval_to_refused_and_runs_nothing(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """``core.approval.refuse`` ends the approval and the held record without any
    effect."""
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))

    record = _record(_refuse(owner, approval_id))

    assert record.state == "refused"
    assert record.executed_at is None
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "cancelled"


def test_approving_a_refused_approval_is_refused(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """A refused approval cannot be approved afterwards."""
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_refuse(owner, approval_id))

    outcome = _approve(owner, approval_id)

    assert outcome.state == APPROVAL_STATE, outcome
    assert _acts(engine, database) == ()


# --- the binding tuple: a differing payload refuses -----------------------------------


def test_a_differing_payload_at_execution_refuses_invalid_approval(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    cluster: ClusterSession,
    workspace: UUID,
) -> None:
    """Criterion 60: execution recomputes the digest from the payload it is about to
    run; a differing byte refuses ``invalid_approval`` and records nothing.

    **The stored snapshot is tampered with directly, and that is the point.** Once the
    approval is minted the payload and its digest are written together, so the only
    way they can disagree is if one of them is changed afterwards — which is exactly
    the attack the recomputation exists to catch, and the only thing a test can stage.
    The row is rewritten to name a *different* record; the digest column is left
    alone, so the recomputation is the one thing standing between that row and an
    execution against the wrong subject.

    The approval is still ``pending`` afterwards — proved by refusing it, which only a
    ``pending`` approval accepts — because the whole approve transaction rolled back,
    including the ``approved`` transition it had already written.
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    other = note_ref(UUID("018f0000-0000-7000-8000-0000000000ff"))
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == approval_id)
            .values(body=act_payload(other))
        )
        uow.commit()

    outcome = _approve(owner, approval_id)

    assert outcome.state == INVALID_APPROVAL, outcome
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"
    still_pending = _record(_refuse(owner, approval_id))
    assert still_pending.state == "refused"


# --- the audit trail of a gated call --------------------------------------------------


def test_the_held_call_and_its_execution_are_both_audited(
    owner: WorkspaceContext, note: str
) -> None:
    """Criterion 14 over the new path: the held call writes a ``refused`` row and the
    execution writes a ``succeeded`` row, both naming the gated operation and both
    readable through ``core.audit.list``.

    A held call is not an unaudited call. ``refused`` is the outcome because the
    dispatcher declined to run the handler — the audit vocabulary's three members are
    fixed by the ``audit_record_outcome`` constraint, and this is the one that means
    "it did not run".
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, approval_id))

    outcome = dispatch(owner, AUDIT_LIST, {"limit": 50})
    listing: Any = _record(outcome)
    rows = [row for row in listing.records if row.operation_name == FIXTURE_ACT]

    assert [row.outcome for row in rows] == ["succeeded", "refused"]
    assert {row.operation_id for row in rows} == {operation_id}
    assert {row.safety_class for row in rows} == {"destructive"}
    assert {row.subject_ref for row in rows} == {note}
    # The approving operation is audited in its own right, under its own name.
    approvals = [
        row for row in listing.records if row.operation_name == APPROVAL_APPROVE
    ]
    assert [row.outcome for row in approvals] == ["succeeded"]


# --- who may approve ------------------------------------------------------------------


def test_a_member_may_approve_and_an_operator_may_not(
    owner: WorkspaceContext,
    workspace: UUID,
    cluster: ClusterSession,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """``module-contract.md``'s row for the pair: ``owner, member``.

    The operator half is the assertion that matters — ``core.operation.resolve`` and
    the token operations *do* carry ``operator``, so "every core mutate operation lets
    an operator in" is a live wrong belief this refuses.
    """
    backend: PostgresBackend = cluster.backend
    member_id = add_member(backend, workspace, Role.MEMBER, display_name="approver")
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext), member
    # Built through the real operator factory, not ``add_member``: ``operator`` is a
    # boundary role and never a ``control.membership`` row
    # (``storage/control_plane.py``), so the CLI's own bootstrap is the only thing
    # that produces one of these contexts.
    operator = context_for_operator(workspace)
    assert isinstance(operator, WorkspaceContext), operator

    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    refused = _approve(operator, approval_id)
    assert refused.state == "role_not_permitted", refused
    assert _acts(engine, database) == ()

    record = _record(_approve(member, approval_id))
    assert record.state == "executed"
    assert record.approved_by_id == member_id
    assert _acts(engine, database) == (note,)
