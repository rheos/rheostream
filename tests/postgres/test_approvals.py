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

**Three things here are pinned by concurrency, by injection, or not at all**, and each
says so at its own test: exactly-once is a property of three compare-and-set
predicates that a *sequential* test cannot see (a read-then-check refuses the second
approve on its own), so it is driven from two threads; the guard seam's *call* cannot
be reached from outside `execute_approved`, so it is pinned by putting a real guard in
`CORE_GUARDS`; and the window clause is exercised both directly on `binding_detail`
and end to end against a row whose window has passed.
"""

import hashlib
import json
import threading
from datetime import UTC, datetime, timedelta
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
from pydantic import BaseModel
from rheo_contracts import Role, WorkspaceContext
from rheo_core.approvals import APPROVAL_APPROVE, APPROVAL_REFUSE, APPROVAL_STATE
from rheo_core.approvals import guards as guards_module
from rheo_core.approvals.binding import INVALID_APPROVAL, binding_detail
from rheo_core.approvals.gate import GUARD_REFUSED, PAYLOAD_TOO_LARGE
from rheo_core.approvals.records import ApprovalRow
from rheo_core.approvals.tables import approval as approval_table
from rheo_core.approvals.tables import approval_payload
from rheo_core.audit import AUDIT_LIST
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.operations import (
    OPERATION_GET,
    SETTINGS_SET,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.postgres import PostgresBackend
from sqlalchemy import Engine, update

pytestmark = pytest.mark.postgres

WINDOW_KEY = "approvals.max_window_seconds"
"""The one approvals key a workspace may override, written as the literal the
operation takes rather than imported from the schema: a test that asked the registry
for the key name would still pass if the key were renamed out from under its
readers."""


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


def test_a_tampered_subject_column_never_reaches_the_audit_row(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The audit row's subject comes from the **verified input**, never from
    ``core.approval.subject_ref``.

    That column is a second stored copy of the subject and the payload digest does not
    cover it, so it is reachable by exactly the tamper the digest check exists to
    catch, one column to the left. When the executed row was built from the column,
    this tamper produced a ``succeeded`` audit record naming a record the destructive
    operation had never touched — the criterion 14 failure the acceptance matrix calls
    "the failure that would be hardest to notice in production", reached through a
    criterion 19 path.

    **The last assertion is the control**: the published approval record still shows
    the tampered value, which proves the tamper landed. Without it, a green here would
    also be what an `UPDATE` that quietly matched no rows produces.
    """
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    planted = note_ref(UUID("018f0000-0000-7000-8000-0000000000aa"))
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            update(approval_table)
            .where(approval_table.c.id == approval_id)
            .values(subject_ref=planted)
        )
        uow.commit()

    record = _record(_approve(owner, approval_id))

    # The effect ran against the payload's subject, which is what the digest binds.
    assert _acts(engine, database) == (note,)
    listing: Any = _record(dispatch(owner, AUDIT_LIST, {"limit": 50}))
    executed = [
        row
        for row in listing.records
        if row.operation_name == FIXTURE_ACT and row.outcome == "succeeded"
    ]
    assert [row.subject_ref for row in executed] == [note]
    assert planted not in {row.subject_ref for row in listing.records}
    assert record.subject_ref == planted


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


# --- exactly once, under two threads -------------------------------------------------

CONCURRENT_ROUNDS = 25
"""How many approvals the concurrency test races two approvers over.

**Not one round.** Two threads racing one approval interleave differently every time;
a single round catches a dropped predicate only some of the time, and a test that
fails intermittently is a test that gets deleted. Twenty-five rounds cost about two
seconds against a local cluster, and with all three predicates removed the Challenger's
own probe recorded 49 executions for 25 approvals — every extra round is another
chance to catch a relaxation that one round would let through."""


def _race_two_approvals(ctx: WorkspaceContext, approval_id: UUID) -> list[str]:
    """Approve ``approval_id`` from two threads released together; return both states.

    A barrier rather than a sleep: both approvers are inside
    ``core.approval.approve`` at the same instant by construction, which is the
    interleaving the compare-and-set predicates exist for. A helper rather than an
    inline closure so nothing captures a loop variable.
    """
    start = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def approve() -> None:
        start.wait()
        outcome = _approve(ctx, approval_id)
        with lock:
            results.append(outcome.state)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_two_concurrent_approvals_execute_the_destructive_fixture_once(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """Criterion 19 sentence 3, the half a sequential test cannot reach: approval makes
    the fixture execute **once**, under two approvers racing the same approval.

    **Why this exists as a separate test, in one sentence: without it the suite cannot
    tell this code from code that double-executes a destructive operation.**
    ``test_a_second_approval_does_not_execute_the_fixture_again`` above is satisfied by
    ``_must_be_pending``'s read-then-check, which is exactly the layer concurrency
    defeats — a second approver that read the row *before* the first one wrote it walks
    straight past it. Three compare-and-set predicates are what actually hold the line
    (``mark_approved`` on ``pending``, ``mark_executed`` on ``approved``,
    ``finish_held_succeeded`` on ``approval_required``), any two of them suffice, and
    **all three can be deleted with every other test in this file still green.**

    So the assertion is a count of the fixture's recorded acts across many races, not a
    return value and not a state: a double execution writes a second harness row and
    changes nothing else a caller can see. `succeeded` approvals are counted too, so a
    run in which *no* approval executed cannot pass by writing no rows either.
    """
    states: list[str] = []
    for _ in range(CONCURRENT_ROUNDS):
        approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
        states.extend(_race_two_approvals(owner, approval_id))

    acts = _acts(engine, database)
    assert len(acts) == CONCURRENT_ROUNDS, (
        f"the destructive fixture ran {len(acts)} times for {CONCURRENT_ROUNDS} "
        "approvals; one approval must execute it exactly once"
    )
    # The other half of the same property, and the control that stops a green from
    # meaning "nothing ran at all": one approver of each pair succeeded, and the
    # loser's refusal is a state, not an exception.
    assert states.count("succeeded") == CONCURRENT_ROUNDS
    assert set(states) <= {"succeeded", APPROVAL_STATE, INVALID_APPROVAL}, sorted(
        set(states)
    )
    assert set(acts) == {note}


# --- the guard seam: proving the call happens, and where ----------------------------


class _RecordingGuard:
    """A guard that records every call and answers what it was told to answer.

    Stands in for C7's real guards. It exists to prove two things a `None`-returning
    stub could not: that the seam is **called**, and that its answer **decides**
    whether the effect lands.
    """

    def __init__(self, detail: str | None) -> None:
        self._detail = detail
        self.calls: list[tuple[UUID, str]] = []
        self.instants: list[datetime] = []

    @property
    def name(self) -> str:
        return "RecordingGuard"

    def check(
        self,
        ctx: WorkspaceContext,
        uow: UnitOfWork,
        *,
        approval: ApprovalRow,
        model_input: BaseModel,
        now: datetime,
    ) -> str | None:
        self.calls.append((approval.id, approval.state))
        self.instants.append(now)
        return self._detail


def test_a_refusing_guard_stops_the_effect_and_a_permitting_one_does_not(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard seam is **called**, at the ratified position, and its answer decides.

    **This is the chunk's own named deliverable and nothing else pins it.** The
    prompt's "Done when" asks that the guard-check seam exist as an explicit, callable
    step, and ``guards.py``'s docstring says the call *site* — after the binding
    check, before the effect — is the part that is easy to put in the wrong place.
    With no test, replacing the whole ``run_guards`` call with ``pass`` leaves every
    gate green, and C7 would attach its guards to a step that could have been deleted.

    Both halves are one test because neither half means anything alone: a refusing
    guard that stops the effect proves the call happens; a permitting guard that lets
    the *same* call through proves the first result came from the guard rather than
    from an unrelated refusal. The recorded call is the third leg — it shows the seam
    saw the approval in state ``approved``, which is what "after the binding check,
    before the effect" means in terms a test can assert.
    """
    refusing = _RecordingGuard("the recording guard refused this execution")
    monkeypatch.setattr(guards_module, "CORE_GUARDS", (refusing,))
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))

    outcome = _approve(owner, approval_id)

    assert outcome.state == GUARD_REFUSED, outcome
    assert outcome.error is not None
    assert "RecordingGuard" in outcome.error.error_text
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"
    # The seam ran, once, and saw the approval already moved to ``approved`` — the
    # position the ratified flow fixes. The whole transaction then rolled back, which
    # is why the row it saw is not the row that survives.
    assert refusing.calls == [(approval_id, "approved")]
    assert refusing.instants[0].tzinfo is not None

    # The control. Same seam, same approval, a guard that permits: now the effect
    # lands. Without this, a green above would also be produced by a dispatcher that
    # refused every approval for any reason at all.
    permitting = _RecordingGuard(None)
    monkeypatch.setattr(guards_module, "CORE_GUARDS", (permitting,))

    record = _record(_approve(owner, approval_id))

    assert record.state == "executed"
    assert _acts(engine, database) == (note,)
    assert permitting.calls == [(approval_id, "approved")]


# --- the window clause of the binding tuple -----------------------------------------


def test_binding_detail_refuses_an_instant_outside_the_window() -> None:
    """The window comparison itself, at both edges and inside.

    Called directly rather than through a dispatch, which is what ``binding_detail``'s
    ``now`` parameter exists for — "a caller that chooses the instant can test the
    window's edges without sleeping" — and until now no caller did. Inclusive at both
    ends is the assertion that would otherwise drift silently: an approval must be
    executable at the instant its window opens and at the instant it closes.
    """
    digest = b"\x01" * 32
    start = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    end = start + timedelta(minutes=15)

    def at(now: datetime) -> str | None:
        return binding_detail(
            approved_digest=digest,
            digest=digest,
            window_start=start,
            window_end=end,
            now=now,
        )

    assert at(start) is None
    assert at(start + timedelta(minutes=7)) is None
    assert at(end) is None
    assert at(start - timedelta(seconds=1)) == (
        "the approval's execution window has passed"
    )
    assert at(end + timedelta(seconds=1)) == (
        "the approval's execution window has passed"
    )
    # The digest clause still answers first when both are wrong, which is what makes
    # the two Cost lines of this tuple distinguishable.
    assert (
        binding_detail(
            approved_digest=digest,
            digest=b"\x02" * 32,
            window_start=start,
            window_end=end,
            now=end + timedelta(days=1),
        )
        == "the payload differs from the one that was approved"
    )


def test_an_approval_whose_window_has_passed_refuses_and_runs_nothing(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The same clause end to end: an expired approval executes nothing.

    ``window_end`` is moved into the past by a direct write, for the same reason the
    payload tamper below uses one — the window is minted from the settings at hold
    time, so the only way to reach an expired approval inside a test is to age the row
    or to wait fifteen minutes. The refusal state is the same ``invalid_approval``
    criterion 60 names for every binding failure; the detail is what distinguishes
    them, and a caller learns only the state.
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            approval_table.update()
            .where(approval_table.c.id == approval_id)
            .values(
                window_start=datetime.now(UTC) - timedelta(days=2),
                window_end=datetime.now(UTC) - timedelta(days=1),
            )
        )
        uow.commit()

    outcome = _approve(owner, approval_id)

    assert outcome.state == INVALID_APPROVAL, outcome
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"


def test_a_workspace_may_shorten_its_own_execution_window(
    owner: WorkspaceContext, note: str
) -> None:
    """``approvals.max_window_seconds`` is workspace-scope with a ``min`` floor, and
    the hold reads it **with the workspace's own rows**.

    Two assertions, and the second is the floor: a workspace override below the
    deployment maximum shortens the window an approval is minted with, and an override
    *above* it is refused outright. Without the first, the key would be a floored
    setting no reader resolves per workspace — a floor declared and never applied,
    which is the armed-but-unused shape this run rejects elsewhere.

    **The floor bites at the write, not at the read**, which is stronger than the
    clamp this test first assumed: ``validate_override`` refuses
    ``setting_floor_violation`` rather than storing a looser value for the resolver to
    narrow later, so a workspace cannot even record a longer window. The assertion was
    corrected to what the code does after it was observed doing it.

    The window is read back through ``core.approval.refuse``'s published record rather
    than off the table, like every other read in this file.
    """
    assert dispatch(owner, SETTINGS_SET, {"key": WINDOW_KEY, "value": 60}).ok
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    record = _record(_refuse(owner, approval_id))
    assert record.window_end - record.window_start == timedelta(seconds=60)

    looser = dispatch(owner, SETTINGS_SET, {"key": WINDOW_KEY, "value": 999999})
    assert looser.state == "setting_floor_violation", looser
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    record = _record(_refuse(owner, approval_id))
    assert record.window_end - record.window_start == timedelta(seconds=60)


# --- the payload bound --------------------------------------------------------------


def test_an_oversized_payload_is_refused_before_anything_is_written(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """``approvals.max_payload_bytes``: an input whose snapshot would exceed the bound
    is refused, and the refusal happens before the hold opens a transaction.

    A real oversized input rather than a lowered bound, so what is exercised is the
    shipped 65536 and not a number a test chose. The refusal carries no approval id
    and no operation id — there is nothing to approve, because nothing was minted —
    which is the assertion that distinguishes "refused" from "held and then failed".
    """
    payload: dict[str, object] = {
        "destination": "party:one",
        "message": "x" * (64 * 1024 + 1),
    }

    outcome = dispatch(owner, SINK_SEND, payload)

    assert outcome.state == PAYLOAD_TOO_LARGE, outcome
    assert outcome.approval_id is None
    assert outcome.operation_id is None
    assert outcome.error is not None
    assert "approvals.max_payload_bytes" in outcome.error.error_text
    assert _sends(engine, database) == ()
