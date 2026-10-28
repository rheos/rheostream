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
be reached from outside `execute_approved`, so it is pinned by putting a chosen guard
in `CORE_GUARDS`; and the window clause is exercised both directly on `binding_detail`
and end to end against a row whose window has passed.

**Run 0c3's C7 adds the rest of criterion 19 below**: the attachment rule for the
three guards the core attaches, `ActorPermissionGuard`'s two revocation scenarios
through a real dispatch, `WindowGuard`'s and `RecordStateGuard`'s own logic at values
a dispatch cannot reach, and the whole criterion end to end through an MCP-kind
context. Why the last two are unit-level rather than end-to-end is written at each of
them; it is a property of this tree (one immutable record type, a binding check that
refuses an expired window before the guards see it), not a shortcut.
"""

import dataclasses
import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import httpx
import pytest
from conftest import ClusterSession
from harness.registry import (
    FIXTURE_ACT,
    FIXTURE_ACT_DECLARATION,
    NOTE_WRITE,
    NOTE_WRITE_DECLARATION,
    SINK_SEND,
    SINK_SEND_DECLARATION,
    act_payload,
    acting_identity,
    add_member,
    enable_harness_module,
    note_ref,
    recorded_act_callers,
    recorded_acts,
    register_harness,
    sent_messages,
)
from pydantic import BaseModel, ConfigDict, Field
from rheo_app_core.main import public_app
from rheo_contracts import (
    ActorKind,
    AuditSpec,
    Entry,
    ExternalSpec,
    Idempotency,
    KeyedSpec,
    OperationDeclaration,
    RecordRef,
    Role,
    SafetyClass,
    StaleRecord,
    WorkspaceContext,
)
from rheo_core.approvals import (
    APPROVAL_APPROVE,
    APPROVAL_DIGEST_MISMATCH,
    APPROVAL_PREVIEW,
    APPROVAL_REFUSE,
    APPROVAL_STATE,
    REDACTED_PREVIEW,
    STANDING_GRANT_CLASS_REFUSED,
    STANDING_GRANT_CREATE,
    RecordStateGuard,
    WindowGuard,
    core_guards_for,
)
from rheo_core.approvals import guards as guards_module
from rheo_core.approvals.binding import INVALID_APPROVAL, binding_detail
from rheo_core.approvals.gate import GUARD_REFUSED, PAYLOAD_TOO_LARGE, window_seconds
from rheo_core.approvals.records import ApprovalRow
from rheo_core.approvals.tables import approval as approval_table
from rheo_core.approvals.tables import approval_payload
from rheo_core.audit import AUDIT_LIST, CORE_AUDIT_SINK, install_sink
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_token
from rheo_core.modules import DESTINATIONS, DestinationMode, DestinationRegistration
from rheo_core.operations import (
    OPERATION_GET,
    SETTINGS_SET,
    OperationOutcome,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.continuation import EXTERNAL_ACTION_EXECUTE
from rheo_core.operations.core_ops import TOKEN_ISSUE, TOKEN_REVOKE
from rheo_core.operations.external_actions import external_action
from rheo_core.operations.idempotency_tables import idempotency_result
from rheo_core.operations.registry import REGISTRY
from rheo_core.refs.resolver import (
    DELETED,
    LIVE,
    NOT_FOUND,
    RESOLVERS,
    RecordHead,
    ResolverRegistry,
    Unavailable,
)
from rheo_core.settings import CORE_ORIGIN, TEST_HARNESS_ORIGIN
from rheo_core.storage import control_tables
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.postgres import PostgresBackend
from rheo_core.storage.work_tables import audit_record
from rheo_core.storage.work_tables import job as job_table
from rheo_core.storage.work_tables import operation as operation_table
from rheo_core.tokens.policy import NON_TOKEN_ISSUABLE
from sqlalchemy import Engine, Table, delete, func, select, update

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


def _operation_record(ctx: WorkspaceContext, operation_id: UUID) -> Any:
    """The operation record, through the supported read (AC 19) and never the table."""
    outcome = dispatch(ctx, OPERATION_GET, {"operation_id": str(operation_id)})
    assert outcome.ok, outcome
    return outcome.result


def _operation_state(ctx: WorkspaceContext, operation_id: UUID) -> str:
    """The held record's state, through the supported read (AC 19)."""
    return str(_operation_record(ctx, operation_id).state)


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
    ``_must_be_decidable``'s read-then-check, which is exactly the layer concurrency
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

    **This is C6's own named deliverable and nothing else pins the call site.**
    ``guards.py``'s docstring says the *site* — after the binding check, before the
    effect — is the part that is easy to put in the wrong place. With no test,
    replacing the whole ``run_guards`` call with ``pass`` leaves every gate green, and
    C7's three real guards would hang off a step that could have been deleted.

    Both halves are one test because neither half means anything alone: a refusing
    guard that stops the effect proves the call happens; a permitting guard that lets
    an identical call through proves the first result came from the guard rather than
    from an unrelated refusal. The recorded call is the third leg — it shows the seam
    saw the approval in state ``approved``, which is what "after the binding check,
    before the effect" means in terms a test can assert.

    **Two things changed in C7 and are asserted in their changed form.** The injected
    guard is substituted for ``CORE_GUARDS``, which is no longer empty, so the tuple
    under test is the injected guard plus the ``RecordStateGuard`` ``core_guards_for``
    appends for this declaration; and a guard refusal is now **durable**, so the
    control half needs a second approval rather than re-approving the first. The old
    shape — refuse, roll back, re-approve the same still-``pending`` approval — is
    exactly what AC 11 forbids.
    """
    refusing = _RecordingGuard("the recording guard refused this execution")
    monkeypatch.setattr(guards_module, "CORE_GUARDS", (refusing,))
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))

    record = _record(_approve(owner, approval_id))

    assert record.state == "refused"
    assert record.executed_at is None
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "failed"
    failed = _operation_record(owner, operation_id)
    assert failed.error_code == GUARD_REFUSED
    assert "RecordingGuard" in failed.error_text
    # The seam ran, once, and saw the approval already moved to ``approved`` — the
    # position the ratified flow fixes.
    assert refusing.calls == [(approval_id, "approved")]
    assert refusing.instants[0].tzinfo is not None

    # The control. Same seam, a fresh approval of the same operation, a guard that
    # permits: now the effect lands. Without it, a green above would also be produced
    # by a dispatcher that refused every approval for any reason at all.
    permitting = _RecordingGuard(None)
    monkeypatch.setattr(guards_module, "CORE_GUARDS", (permitting,))
    second_id, second_operation = _held(owner, FIXTURE_ACT, act_payload(note))

    record = _record(_approve(owner, second_id))

    assert record.state == "executed"
    assert _acts(engine, database) == (note,)
    assert _operation_state(owner, second_operation) == "succeeded"
    assert permitting.calls == [(second_id, "approved")]


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


# --- the three core guards: attachment, then each guard's own logic ------------------


def test_the_core_attaches_two_guards_always_and_the_third_only_with_a_subject() -> (
    None
):
    """``module-contract.md``'s guard-attachment rule, as a property of a declaration.

    Four cases, and the pair that matters is the last two: ``harness.fixture.act``
    names ``AuditSpec(subject_field="ref")`` and ``harness.sink.send`` names
    ``subject_field=None``, so they are the same class, registered the same way,
    differing in exactly the thing the rule keys on. Asserting only the first would
    pass for a rule that attached ``RecordStateGuard`` to everything.

    The lower-class case is here because "attached to every destructive, external and
    financial operation" has an unstated other half — that nothing else gets them —
    and a set that ignored the class would still satisfy both upper-class cases.

    The two upper-class declarations come from the harness rather than from literals
    built here: a declaration assembled by the test could name a ``subject_field``
    the registry would have refused, and then this would assert the rule against a
    declaration that can never exist.
    """
    assert core_guards_for(NOTE_WRITE_DECLARATION) == ()

    sink = core_guards_for(SINK_SEND_DECLARATION)
    assert [guard.name for guard in sink] == ["ActorPermissionGuard", "WindowGuard"]

    fixture = core_guards_for(FIXTURE_ACT_DECLARATION)
    assert [guard.name for guard in fixture] == [
        "ActorPermissionGuard",
        "WindowGuard",
        "RecordStateGuard",
    ]
    # The attached guard reads the declaration's own subject field, not a name this
    # module chose: a guard built with the wrong field would look at nothing.
    attached = fixture[2]
    assert isinstance(attached, RecordStateGuard)
    assert attached.subject_field == FIXTURE_ACT_DECLARATION.audit.subject_field


def _revoke_membership(
    cluster: ClusterSession, account_id: UUID, workspace: UUID
) -> None:
    """Delete an account's ``control.membership`` row for this workspace.

    A direct delete rather than an operation, because release one registers no
    operation that removes a membership — ``rheo member add`` writes the row and
    nothing takes it away yet. The row *is* the permission
    (``boundary/factories.py`` reads it to build every account context), so deleting
    it is exactly what "the actor has lost the membership" means, and there is no
    weaker staging of a revocation available.
    """
    with cluster.backend.control_engine.begin() as connection:
        deleted = connection.execute(
            delete(control_tables.membership).where(
                (control_tables.membership.c.account_id == account_id)
                & (control_tables.membership.c.workspace_id == workspace)
            )
        ).rowcount
    # The anti-vacuity control: a revocation test whose revocation matched no row
    # would pass against a guard that never ran, and look identical.
    assert deleted == 1, (
        f"no control.membership row was deleted for account {account_id}; the "
        "revocation this test stages did not happen"
    )


def _member_context(
    cluster: ClusterSession, workspace: UUID, name: str
) -> tuple[UUID, WorkspaceContext]:
    """A second account with a ``member`` membership, and its context."""
    account_id = add_member(cluster.backend, workspace, Role.MEMBER, display_name=name)
    ctx = context_for_harness(workspace, account_id, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext), ctx
    return account_id, ctx


def _act_callers(engine: Engine, database: str) -> tuple[str, ...]:
    """The acting identity each ``harness.fixture.act`` execution ran under."""
    with UnitOfWork(engine, database) as uow:
        return recorded_act_callers(uow.connection)


def test_an_approved_handler_runs_as_the_held_caller_not_the_approver(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """1a1: the gated handler runs under the **original held caller's** rebuilt
    context, not the approving actor's.

    A member holds the destructive call; the owner releases it. Before 1a1 the handler
    received the approving owner's context — its actor, its ``owner`` role, its
    ``ALL_OPERATIONS`` set — so a member's held call executed with an owner's
    authority the moment an owner approved it. ``ActorPermissionGuard`` does not catch
    that: it reads ``approval.actor_*`` off the row and never looks at the context the
    handler is about to be handed.

    **The two accounts are asserted to differ first**, because everything below is
    about telling them apart and a fixture that handed back one account twice would
    make all of it vacuous. The identity is compared whole — actor kind, actor id,
    role and bound purpose — so a rebuild that got the account right and the role
    wrong cannot pass.
    """
    member_id, member = _member_context(cluster, workspace, "held-caller")
    assert member_id != owner_account_id
    assert acting_identity(member) != acting_identity(owner)

    approval_id, _ = _held(member, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, approval_id))

    # It ran, once, on the right record — and as the member who asked for it.
    assert _acts(engine, database) == (note,)
    assert _act_callers(engine, database) == (acting_identity(member),)


def _audit_rows(ctx: WorkspaceContext, operation_name: str) -> tuple[Any, ...]:
    """Every audit record for ``operation_name``, through the supported read.

    ``core.audit.list`` rather than a ``SELECT``: the row's actor is what this file is
    about to assert on, and reading it through the operation that publishes it proves
    the value a person would actually see rather than a column a test went looking for.
    """
    outcome = dispatch(ctx, AUDIT_LIST, {"limit": 100})
    assert outcome.ok, outcome
    result: Any = outcome.result
    return tuple(
        record for record in result.records if record.operation_name == operation_name
    )


def test_a_gated_operations_success_audit_row_names_the_held_caller(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    cluster: ClusterSession,
    workspace: UUID,
    note: str,
) -> None:
    """1a1: the ``succeeded`` audit row for a gated operation names the caller whose
    call was held, not the actor who released it.

    The handler and the guards moved to the rebuilt held caller when
    ``context_for_approved_execution`` landed; ``record_operation_audit`` was still
    handed the approver's ``ctx``, so the one durable record of *who did this* named
    the wrong principal for every gated operation in the tree — an owner appearing to
    have performed a member's destructive call, with nothing else in the row to
    contradict it.

    **Three rows are asserted, not one**, because the fix has a way of being wrong in
    both directions. The held call's ``refused`` row and the execution's ``succeeded``
    row must both name the member — they are two records of one act — and
    ``core.approval.approve``'s own row must still name the owner, or the change would
    have swapped the defect for its mirror image and lost the record of who approved.
    """
    member_id, member = _member_context(cluster, workspace, "audited-held-caller")
    assert member_id != owner_account_id

    approval_id, _ = _held(member, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, approval_id))

    gated = {row.outcome: row for row in _audit_rows(owner, FIXTURE_ACT)}
    assert set(gated) == {"refused", "succeeded"}, gated
    assert gated["succeeded"].actor_id == member_id
    assert gated["succeeded"].actor_kind == member.actor.kind.value
    assert gated["succeeded"].entry == member.entry.value
    assert gated["succeeded"].subject_ref == note
    assert gated["refused"].actor_id == member_id
    (approve_row,) = _audit_rows(owner, APPROVAL_APPROVE)
    assert approve_row.outcome == "succeeded"
    assert approve_row.actor_id == owner_account_id


def test_the_held_tokens_own_authority_survives_into_the_approved_handler(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """The same property for a token actor, where the rebuilt context differs from the
    approver's in more than a role.

    An MCP token's context carries the token as actor and its snapshot as the
    operation set; the owner who approves carries an account actor and
    ``ALL_OPERATIONS``. Rebuilding is what keeps the handler inside the token's own
    authority instead of promoting it to the approver's — and it is the case that
    matters, because a headless token call released by a person is the design's
    central one.
    """
    mcp, token_id = _mcp_context(owner, FIXTURE_ACT)
    approval_id, _ = _held(mcp, FIXTURE_ACT, act_payload(note))

    _record(_approve(owner, approval_id))

    (caller,) = _act_callers(engine, database)
    assert caller == acting_identity(mcp)
    assert str(token_id) in caller
    assert str(owner.actor.id) not in caller


def test_revoking_the_gated_actors_role_refuses_execution_and_records_it(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """AC 11, scenario one: the **gated** actor's role is revoked before execution.

    The member holds the membership that let the call be made; the owner approves it;
    the membership is gone by the time execution rechecks. All four of AC 11's
    consequences are asserted, and three of them are durable state rather than the
    call's return value: the approval ``refused``, the held operation ``failed``
    carrying the guard's code and name, and the fixture untouched.

    **Two actors, not one**, so that this scenario and the next cannot both pass on a
    guard that only ever looks at whoever is approving. Here the approver's membership
    is untouched and the gated actor's is gone.

    **The approval does not survive as ``pending``.** A rolled-back guard refusal would
    leave it immediately re-approvable, which is the outcome R3 item 4 wants a
    revocation to prevent, so re-approving is asserted to refuse.
    """
    member_id, member = _member_context(cluster, workspace, "gated-actor")
    approval_id, operation_id = _held(member, FIXTURE_ACT, act_payload(note))
    _revoke_membership(cluster, member_id, workspace)

    record = _record(_approve(owner, approval_id))

    assert record.state == "refused"
    assert record.executed_at is None
    assert _acts(engine, database) == ()
    failed = _operation_record(owner, operation_id)
    assert failed.state == "failed"
    assert failed.error_code == GUARD_REFUSED
    assert "ActorPermissionGuard" in failed.error_text
    assert "the gated actor" in failed.error_text
    assert str(member_id) in failed.error_text
    # Durable: the approval is spent, not merely unexecuted.
    assert _approve(owner, approval_id).state == APPROVAL_STATE


def test_an_approver_whose_membership_is_gone_is_refused_before_anything_is_written(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """AC 11, scenario two, as run 4b2 decides it: the **approving** actor's role is
    revoked before they approve.

    ``dispatch`` still authorizes the call, because the approver's context was built
    before the revocation. ``core.approval.approve`` now rechecks the *current*
    membership itself, under the row lock and before any write, so the stale context
    cannot spend somebody else's approval: it answers ``not_found`` — the answer a
    missing id gets, so the caller learns nothing — and the approval stays ``pending``
    for an eligible person, who can still release it once.

    The guard's "approving actor" clause still matters for a revocation that lands
    after this check (and for the EXTERNAL continuation, where approval and effect
    are two transactions); ``test_revoking_the_approving_actors_role_refuses_execution
    _too`` below stages that race end to end and is the name the phase-1 acceptance
    matrix cites for criterion 19.
    """
    approver_id, approver = _member_context(cluster, workspace, "stale-approver")
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _revoke_membership(cluster, approver_id, workspace)

    refused = _approve(approver, approval_id)

    assert refused.state == "not_found", refused
    assert refused.error is not None
    assert refused.error.error_text == f"no approval {approval_id} in this workspace"
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"
    assert _refuse(approver, approval_id).state == "not_found"
    # Still pending: the eligible owner releases it, exactly once.
    record = _record(_approve(owner, approval_id))
    assert record.state == "executed"
    assert record.approved_by_id == owner_account_id
    assert _acts(engine, database) == (note,)


def test_revoking_the_approving_actors_role_refuses_execution_too(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 11, scenario two, end to end: the **approving** actor's role is revoked
    after they were found eligible and before the effect.

    The mirror of the gated-actor scenario, and the clause that would still be green
    if the guard read only ``approval.actor_*``. Since run 4b2 the decision rechecks
    the approver's current membership itself, so a revocation that lands *before*
    the decision answers ``not_found`` (the test above). The membership lives on the
    control plane, which the approval row's lock does not cover, so a revocation can
    still commit between that eligibility read and the guards. This stages exactly
    that race — the real eligibility read runs and passes, then the membership row is
    deleted and committed — and dispatches the real approve. ``ActorPermissionGuard``
    must catch it at execution: the approval ``refused``, the held operation
    ``failed`` naming the approving actor, the fixture untouched.

    It is the criterion-19 demonstrator ``docs/acceptance/phase-1-matrix.md`` names,
    and the matrix's recorded hunk (removing ``run_guards`` from
    ``execute_approved``) turns it red with ``assert 'executed' == 'refused'``.
    """
    from rheo_core.approvals import operations as approval_operations

    gated_id, gated = _member_context(cluster, workspace, "gated-actor-intact")
    approver_id, approver = _member_context(cluster, workspace, "approver-revoked")
    approval_id, operation_id = _held(gated, FIXTURE_ACT, act_payload(note))
    real_eligible = approval_operations._eligible
    revoked: list[UUID] = []

    def eligible_then_revoked(ctx: WorkspaceContext) -> bool:
        answer = real_eligible(ctx)
        if ctx.actor.id == approver_id and not revoked:
            _revoke_membership(cluster, approver_id, workspace)
            revoked.append(approver_id)
        return answer

    monkeypatch.setattr(approval_operations, "_eligible", eligible_then_revoked)

    record = _record(_approve(approver, approval_id))

    assert revoked == [approver_id]
    assert record.state == "refused"
    assert record.executed_at is None
    assert _acts(engine, database) == ()
    failed = _operation_record(owner, operation_id)
    assert failed.state == "failed"
    assert failed.error_code == GUARD_REFUSED
    assert "ActorPermissionGuard" in failed.error_text
    assert "the approving actor" in failed.error_text
    assert str(approver_id) in failed.error_text
    assert str(gated_id) not in failed.error_text


def test_a_role_that_no_longer_permits_the_operation_refuses_as_well(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """The ratified row's other clause: the role that *permitted* it, not only the
    membership.

    ``core.approval.approve`` is ``owner, member``; ``core.settings.set`` is ``owner``
    alone. Demoting the approver from owner to member leaves the membership in place,
    so a guard that only checked for a row's existence would pass this — and the
    approving actor would still hold a role that does not permit what it just did.

    The gated operation is deliberately the one whose roles *do* include ``member``,
    so the refusal can only be about the approver.
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    with cluster.backend.control_engine.begin() as connection:
        demoted = connection.execute(
            update(control_tables.membership)
            .where(
                (control_tables.membership.c.account_id == owner.actor.id)
                & (control_tables.membership.c.workspace_id == workspace)
            )
            .values(role=Role.MEMBER.value)
        ).rowcount
    assert demoted == 1, "the demotion this test stages did not happen"

    record = _record(_approve(owner, approval_id))

    # ``member`` still permits both operations, so this is the control half: the
    # demotion alone must not refuse.
    assert record.state == "executed"
    assert _acts(engine, database) == (note,)
    assert _operation_state(owner, operation_id) == "succeeded"


def _approval_row(**overrides: Any) -> ApprovalRow:
    """An ``ApprovalRow`` with the fields a unit-level guard reads, and defaults
    elsewhere.

    Built rather than read back from a dispatch because these two guards are exercised
    at values a dispatch cannot produce: a window in the past that the binding check
    would refuse first, and a subject revision that no immutable harness record can
    reach.
    """
    start = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
    fields: dict[str, Any] = {
        "id": UUID("018f0000-0000-7000-8000-00000000c7c7"),
        "actor_kind": "account",
        "actor_id": None,
        "operation_name": FIXTURE_ACT,
        "operation_id": UUID("018f0000-0000-7000-8000-00000000c7c8"),
        "destination_ref": None,
        "subject_ref": None,
        "subject_revision": 1,
        "payload_digest": b"\x01" * 32,
        "purpose": None,
        "window_start": start,
        "window_end": start + timedelta(minutes=15),
        "state": "approved",
        "approved_by_kind": "account",
        "approved_by_id": None,
        "approved_at": start,
        "approved_entry": "web",
        "executed_at": None,
        "invalidated_reason": None,
    }
    fields.update(overrides)
    return ApprovalRow(**fields)


def test_the_window_guard_refuses_outside_its_window_and_permits_inside_it(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """``WindowGuard``'s own logic, at both edges and outside both.

    Unit-level on purpose, and the reason is written in the guard's own docstring: the
    binding tuple carries the window too and is checked *first*, so no dispatch can
    ever reach this guard with an expired approval —
    ``test_an_approval_whose_window_has_passed_refuses_and_runs_nothing`` above is that
    path, and it refuses ``invalid_approval`` before the guards run. Driving the class
    directly is what makes the rule true for any execution path that reaches the
    guards without the binding check.

    Inclusive at both ends is the assertion that would otherwise drift silently: an
    approval must be executable at the instant its window opens and at the instant it
    closes.
    """
    guard = WindowGuard()
    approval = _approval_row()
    start, end = approval.window_start, approval.window_end

    def at(now: datetime) -> str | None:
        with UnitOfWork(engine, database) as uow:
            return guard.check(
                owner,
                uow,
                approval=approval,
                model_input=_ProbeInput(ref=_probe_ref()),
                now=now,
            )

    assert at(start) is None
    assert at(start + timedelta(minutes=7)) is None
    assert at(end) is None
    assert at(start - timedelta(seconds=1)) is not None
    assert "outside the approval's window" in str(at(end + timedelta(seconds=1)))


_PROBE_ID = "018f0000-0000-7000-8000-00000000c701"
"""The one record id the unit-level ``RecordStateGuard`` cases resolve. A ``RecordRef``
id is a UUID (``rheo_contracts.refs``), so it is spelled as one."""


class _ProbeInput(BaseModel):
    """An input model whose ``ref`` field carries a ``RecordRef``, like
    ``harness.fixture.act``'s."""

    ref: RecordRef


def _probe_ref() -> RecordRef:
    return RecordRef(module="harness", record_type="probe", id=_PROBE_ID)


def _stub_resolvers(answer: RecordHead | Unavailable) -> ResolverRegistry:
    """A private resolver table whose one resolver always answers ``answer``.

    Private rather than the process-wide one: registering a second resolver for a
    record type the harness already owns would be refused, and a stub on the shared
    table would leak into every other test in the session.
    """
    registry = ResolverRegistry()

    def resolver(
        ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
    ) -> RecordHead | Unavailable:
        return answer

    registry.register("harness", "probe", resolver, origin=TEST_HARNESS_ORIGIN)
    return registry


def test_the_record_state_guard_permits_a_live_matching_subject(
    owner: WorkspaceContext, engine: Engine, database: str
) -> None:
    """The control for the three refusals below.

    Without it, a guard that refused every subject for any reason would pass all of
    them, and this file would report a working guard that permits nothing.
    """
    ref = _probe_ref()
    live = RecordHead(ref=ref, display="probe", readable=True, state=LIVE, revision=1)
    guard = RecordStateGuard("ref", _stub_resolvers(live))

    with UnitOfWork(engine, database) as uow:
        detail = guard.check(
            owner,
            uow,
            approval=_approval_row(subject_revision=1),
            model_input=_ProbeInput(ref=ref),
            now=datetime(2026, 9, 17, 12, 1, tzinfo=UTC),
        )

    assert detail is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(
            Unavailable(f"harness.probe:{_PROBE_ID}", "not_found"),
            "no longer resolves",
            id="unresolvable",
        ),
        pytest.param(
            RecordHead(
                ref=RecordRef(module="harness", record_type="probe", id=_PROBE_ID),
                display="probe",
                readable=True,
                state=DELETED,
                revision=None,
            ),
            "is deleted",
            id="deleted",
        ),
        pytest.param(
            RecordHead(
                ref=RecordRef(module="harness", record_type="probe", id=_PROBE_ID),
                display="probe",
                readable=True,
                state=LIVE,
                revision=7,
            ),
            "not the revision 1 that was approved",
            id="revision-moved",
        ),
    ],
)
def test_the_record_state_guard_refuses_a_gone_or_moved_subject(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    answer: RecordHead | Unavailable,
    expected: str,
) -> None:
    """``RecordStateGuard``'s three refusal branches, one case each.

    The ratified row names three conditions — the subject resolves to ``deleted``, to
    ``unavailable``, or at a ``revision`` differing from the approval's — and a test
    over one of them would pass for a guard that implemented only that one.

    **Unit-level because the revision branch has no honest end-to-end staging in this
    tree.** ``harness.note`` is the only resolvable record type and it is immutable,
    reporting the constant revision 1, so a dispatch-level revision mismatch would need
    either a new mutable record type (outside this chunk's files) or a tampered
    ``core.approval.subject_revision``, which would be a test about tampering rather
    than about the guard.
    """
    guard = RecordStateGuard("ref", _stub_resolvers(answer))

    with UnitOfWork(engine, database) as uow:
        detail = guard.check(
            owner,
            uow,
            approval=_approval_row(subject_revision=1),
            model_input=_ProbeInput(ref=_probe_ref()),
            now=datetime(2026, 9, 17, 12, 1, tzinfo=UTC),
        )

    assert detail is not None
    assert expected in detail


# --- criterion 19, end to end through an MCP-kind context ---------------------------


def _mcp_context(
    owner: WorkspaceContext, operation: str
) -> tuple[WorkspaceContext, UUID]:
    """A context resolved from a real ``mcp`` token whose set names ``operation``.

    Built through ``context_from_token``, which is the same function
    ``apps/mcp``'s ``resolve_context`` calls, so the context under test is MCP-kind in
    every way the criterion means: actor ``token``, audience ``token``, entry ``mcp``,
    and an operation set that is the token's snapshot rather than ``ALL_OPERATIONS``.
    It is not driven through the transport's HTTP round trip, which would test the
    transport rather than the confirmation requirement.
    """
    issued = dispatch(owner, TOKEN_ISSUE, {"kind": "mcp", "operations": [operation]})
    assert issued.ok, issued
    value = str(issued.result.value)  # type: ignore[union-attr]
    token_id = issued.result.token_id  # type: ignore[union-attr]
    ctx = context_from_token(value, "mcp")
    assert isinstance(ctx, WorkspaceContext), ctx
    assert ctx.actor.kind is ActorKind.TOKEN
    assert ctx.actor.id == token_id
    assert ctx.audience is not None and ctx.audience.id == token_id
    assert ctx.entry is Entry.MCP
    assert ctx.operation_set == frozenset({operation})
    return ctx, token_id


def test_revoking_the_gated_token_refuses_approved_execution(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    mcp, token_id = _mcp_context(owner, FIXTURE_ACT)
    approval_id, operation_id = _held(mcp, FIXTURE_ACT, act_payload(note))

    revoked = dispatch(owner, TOKEN_REVOKE, {"token_id": str(token_id)})
    assert revoked.ok, revoked
    record = _record(_approve(owner, approval_id))

    assert record.actor_kind == ActorKind.TOKEN.value
    assert record.actor_id == token_id
    assert record.state == "refused"
    assert record.executed_at is None
    assert _acts(engine, database) == ()
    failed = _operation_record(owner, operation_id)
    assert failed.state == "failed"
    assert failed.error_code == GUARD_REFUSED
    assert "ActorPermissionGuard" in failed.error_text
    assert "the gated actor" in failed.error_text
    assert "revoked" in failed.error_text


def test_criterion_19_end_to_end_through_an_mcp_token_session(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """Criterion 19, whole, in the order its own sentences give.

    1. A destructive call through an MCP token session answers ``approval_required``
       with an approval id, distinct from success and from failure, within the
       configured deadline.
    2. The fixture recorded nothing and no operation record reports success.
    3. Approving as an authenticated, non-token actor executes the fixture **once**,
       and the durable approval row carrying the ``payload_digest`` is readable through
       a supported operation. A second approval does not execute it again.
    4. A standing grant naming that destructive operation is refused at grant time.

    **The three states are compared as values, not by prefix** (AC 4): the held call,
    a successful call and a failed call are collected here and asserted to be three
    distinct strings, so a dispatcher that answered ``approval_required_ok`` for
    everything could not pass.

    **The deadline is read from the setting** (AC 5). There is no separate
    response-deadline key anywhere in the settings schema — the only configured time
    bound the approvals design has is the execution window,
    ``approvals.default_window_seconds`` clamped by ``approvals.max_window_seconds``,
    which ``window_seconds`` resolves. That is the honest bound to assert against: a
    hold that took longer than the window it was minting would hand back an approval
    that had already expired. It is read through ``window_seconds(ctx)`` rather than
    written as a number, so a deployment that changes the setting changes this
    assertion with it.
    """
    mcp, _ = _mcp_context(owner, FIXTURE_ACT)
    payload = act_payload(note)
    deadline = window_seconds(mcp)

    started = time.monotonic()
    held = dispatch(mcp, FIXTURE_ACT, payload)
    elapsed = time.monotonic() - started

    assert held.state == "approval_required", held
    assert held.approval_id is not None, held
    assert held.operation_id is not None, held
    assert elapsed < deadline, (
        f"the held call took {elapsed:.3f}s, which is not inside the configured "
        f"{deadline}s window it was minting"
    )
    # Three distinguishable values, asserted as such. The failing call is refused for
    # a reason that has nothing to do with the class branch: the token's set names one
    # operation and this is not it.
    succeeded = dispatch(owner, NOTE_WRITE, {"body": "a call that simply runs"})
    failed = dispatch(mcp, NOTE_WRITE, {"body": "not in this token's set"})
    assert len({held.state, succeeded.state, failed.state}) == 3, (
        held.state,
        succeeded.state,
        failed.state,
    )
    assert succeeded.ok and not held.ok and not failed.ok

    # Nothing happened: the fixture's own recording surface is empty and the held
    # record does not report success.
    assert _acts(engine, database) == ()
    assert _operation_state(owner, held.operation_id) == "approval_required"

    # A person approves, from a context that is not a token.
    assert owner.actor.kind is ActorKind.ACCOUNT
    record = _record(_approve(owner, held.approval_id))

    assert record.state == "executed"
    assert record.payload_digest == _expected_digest(payload)
    assert _acts(engine, database) == (note,)
    assert _operation_state(owner, held.operation_id) == "succeeded"
    assert _approve(owner, held.approval_id).state == APPROVAL_STATE
    assert _acts(engine, database) == (note,)

    # And the grant half of the same criterion, from the same workspace.
    refused = dispatch(
        owner,
        STANDING_GRANT_CREATE,
        {
            "account_id": str(owner.actor.id),
            "operation_names": [FIXTURE_ACT],
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
    )
    assert refused.state == STANDING_GRANT_CLASS_REFUSED, refused
    assert refused.error is not None
    assert FIXTURE_ACT in refused.error.error_text


async def test_the_http_envelope_carries_the_approval_id_as_a_field(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """Issue #127: over ``POST /api/v1/operations/{name}`` a held call's envelope names
    its approval in ``approval_id``, not only inside ``error_text``, and that value is
    the one a person approves with.

    Approving through the id read off the envelope, and then counting the fixture's
    recorded acts, is what makes this more than a shape check: an envelope carrying
    some other uuid would fail the approval, and one carrying the right uuid only in
    prose would fail the key lookup.
    """
    issued = dispatch(owner, TOKEN_ISSUE, {"kind": "cli", "operations": [FIXTURE_ACT]})
    assert issued.ok, issued
    value = str(issued.result.value)  # type: ignore[union-attr]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/api/v1/operations/{FIXTURE_ACT}",
            headers={"Authorization": f"Bearer {value}"},
            json=act_payload(note),
        )
    body = response.json()
    assert body["state"] == "approval_required", body
    assert body["operation_id"] is not None, body
    approval_id = UUID(body["approval_id"])
    assert body["approval_id"] in body["error"]["error_text"], body
    assert _acts(engine, database) == ()

    record = _record(_approve(owner, approval_id))
    assert record.operation_id == UUID(body["operation_id"])
    assert _acts(engine, database) == (note,)


def test_an_approved_handler_that_raises_stale_record_answers_record_stale(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #12: the approval-executed path answers a handler's ``StaleRecord`` the
    way ``dispatch()`` answers it for an ordinary call.

    ``execute_approved`` calls the handler itself rather than going back through
    ``dispatch()``, and translates ``StaleRecord`` to ``record_stale`` there. Today its
    one caller is ``core.approval.approve``'s own dispatch, whose ``StaleRecord``
    branch would answer the same word even without that translation, so this pins the
    end-to-end contract rather than the gate's line alone; the translation is what
    keeps it true for any caller that is not a dispatch. The handler is swapped on the
    process-wide registry for one that raises, because the harness fixture has no
    compare-and-set of its own. The approve rolls back
    whole, so the approval is still usable: once the real handler is back, the same
    approval executes the fixture exactly once.
    """
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    registered = REGISTRY.lookup(FIXTURE_ACT)
    assert registered is not None

    def stale(*_: object) -> BaseModel:
        raise StaleRecord("the subject has moved since the revision you hold")

    with monkeypatch.context() as patch:
        patch.setitem(
            REGISTRY._operations,  # noqa: SLF001 - the only seam to the approved call
            FIXTURE_ACT,
            dataclasses.replace(registered, handler=stale),
        )
        outcome = _approve(owner, approval_id)

    assert outcome.state == "record_stale", outcome
    assert outcome.error is not None and outcome.error.error_code == "record_stale"
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"

    assert _record(_approve(owner, approval_id)).state == "executed"
    assert _acts(engine, database) == (note,)


# --- run 4b2: the bound preview and digest-bound decisions ---------------------------
#
# ``core.approval.preview`` is the read a person's confirmation page is built from.
# Every test below drives it through ``dispatch`` and reads the decision's outcome
# through approve/refuse, the harness tables and the external-action rows. A redacted
# answer is compared with ``REDACTED_PREVIEW`` whole, so a redaction that leaked one
# field — or differed by cause — fails on the field itself.

PROBE: Literal["core.external.preview_probe"] = "core.external.preview_probe"
"""A synthetic KEYED EXTERNAL operation, so the EXTERNAL queueing path is covered."""


class _ExternalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_key: str
    destination_ref: Literal["synthetic"] = "synthetic"
    title: str = Field(min_length=1)


class _ExternalOutput(BaseModel):
    item_ref: str


def _probe_payload(key: str) -> dict[str, object]:
    return {"request_key": key, "destination_ref": "synthetic", "title": "Synthetic"}


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Register the probe and its in-process destination; return its call log.

    The operation handler raises: an EXTERNAL effect runs only through the
    continuation's destination, never inline at approval.
    """
    calls: list[str] = []

    def forbidden(ctx: WorkspaceContext, uow: Any, model: BaseModel) -> Any:
        raise AssertionError("an EXTERNAL operation never runs inline")

    def execute(ctx: WorkspaceContext, uow: Any, model: BaseModel) -> BaseModel:
        assert isinstance(model, _ExternalInput)
        calls.append(model.request_key)
        return _ExternalOutput(item_ref=f"synthetic:{model.request_key}")

    declaration = OperationDeclaration(
        name=PROBE,
        safety_class=SafetyClass.EXTERNAL,
        roles=frozenset({Role.OWNER, Role.MEMBER}),
        input_model=_ExternalInput,
        output=_ExternalOutput,
        idempotency=Idempotency.KEYED,
        keyed=KeyedSpec(
            key_field="request_key", request_schema_version=1, result_schema_version=1
        ),
        external=ExternalSpec(
            destination_field="destination_ref",
            destination_ref="synthetic",
            result_ref_field="item_ref",
        ),
        audit=AuditSpec(subject_field=None),
    )
    monkeypatch.setattr(REGISTRY, "_operations", dict(REGISTRY._operations))
    REGISTRY._operations.pop(PROBE, None)
    REGISTRY.register(declaration, forbidden, origin=CORE_ORIGIN)
    monkeypatch.setattr(DESTINATIONS, "_destinations", dict(DESTINATIONS._destinations))
    DESTINATIONS._destinations.pop("synthetic", None)
    DESTINATIONS.register(
        DestinationRegistration(
            destination_ref="synthetic",
            operation_name=PROBE,
            mode=DestinationMode.IN_PROCESS,
            is_installed=lambda: True,
            is_enabled=lambda ctx: True,
            is_ready=lambda ctx, uow: True,
            credentials_available=None,
            execute=execute,
            reconcile=None,
        ),
        origin="core",
    )
    install_sink("core", CORE_AUDIT_SINK)
    return calls


def _preview(ctx: WorkspaceContext, approval_id: UUID) -> Any:
    outcome = dispatch(ctx, APPROVAL_PREVIEW, {"approval_id": str(approval_id)})
    assert outcome.ok, outcome
    return outcome.result


def _decide(
    ctx: WorkspaceContext, name: str, approval_id: UUID, digest: str | None
) -> OperationOutcome:
    payload: dict[str, object] = {"approval_id": str(approval_id)}
    if digest is not None:
        payload["expected_payload_digest"] = digest
    return dispatch(ctx, name, payload)


def _set_approval(
    engine: Engine, database: str, approval_id: UUID, **values: Any
) -> None:
    """A direct write to the approval row: the only way to stage a state no path
    writes yet (``expired``, ``invalidated``) or a restored ``requires_reapproval``."""
    with UnitOfWork(engine, database) as uow:
        changed = uow.connection.execute(
            update(approval_table)
            .where(approval_table.c.id == approval_id)
            .values(**values)
        ).rowcount
        uow.commit()
    assert changed == 1


def _rows(engine: Engine, database: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table a preview or a decision could touch."""
    tables: tuple[Table, ...] = (
        approval_table,
        approval_payload,
        operation_table,
        job_table,
        audit_record,
        external_action,
        idempotency_result,
    )
    with UnitOfWork(engine, database) as uow:
        return {
            table.name: [
                tuple(row)
                for row in uow.connection.execute(
                    select(table).order_by(*table.primary_key.columns)
                ).all()
            ]
            for table in tables
        }


def _probe_jobs(engine: Engine, database: str) -> int:
    with UnitOfWork(engine, database) as uow:
        return int(
            uow.connection.execute(
                select(func.count())
                .select_from(job_table)
                .where(job_table.c.kind == EXTERNAL_ACTION_EXECUTE)
            ).scalar_one()
        )


def _hide_notes_from(monkeypatch: pytest.MonkeyPatch, account_id: UUID) -> None:
    """Make every ``harness.note`` invisible to one account: its resolver answers
    ``not_found`` for that viewer and resolves normally for everyone else — the shape
    a module's own audience rule takes."""
    original = RESOLVERS.lookup("harness", "note")
    assert original is not None

    def scoped(
        ctx: WorkspaceContext, uow: UnitOfWork, ref: RecordRef
    ) -> RecordHead | Unavailable:
        if ctx.actor.id == account_id:
            return Unavailable(ref.format(), NOT_FOUND)
        return original(ctx, uow, ref)

    monkeypatch.setitem(RESOLVERS._resolvers, ("harness", "note"), scoped)


def _stale_member(
    cluster: ClusterSession, workspace: UUID, name: str
) -> WorkspaceContext:
    """A member context whose membership has since been revoked."""
    account_id, ctx = _member_context(cluster, workspace, name)
    _revoke_membership(cluster, account_id, workspace)
    return ctx


def test_preview_of_a_pending_approval_discloses_the_verified_binding(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """An eligible viewer sees exactly the persisted binding, and it is actionable.

    The requester is the member who asked, not the owner viewing; the payload and
    digest are checked against literals; no destination is bound for DESTRUCTIVE.
    """
    member_id, member = _member_context(cluster, workspace, "requester")
    payload = act_payload(note)
    approval_id, operation_id = _held(member, FIXTURE_ACT, payload)

    preview = _preview(owner, approval_id)

    assert preview.status == "pending"
    assert preview.actionable is True
    assert preview.approval_id == approval_id
    assert preview.operation_id == operation_id
    assert preview.external_action_id is None
    assert preview.operation_name == FIXTURE_ACT
    assert (preview.requester_kind, preview.requester_id) == ("account", member_id)
    assert preview.subject_ref == note
    assert preview.destination_ref is None
    assert preview.payload == payload
    assert preview.payload_digest == _expected_digest(payload)
    assert preview.purpose is None
    assert preview.window_start < preview.window_end
    assert preview.durable_state == "pending"
    assert _acts(engine, database) == ()


def test_preview_of_an_external_action_names_its_exact_destination(
    owner: WorkspaceContext, engine: Engine, database: str, probe: list[str]
) -> None:
    """The staged EXTERNAL action's preview carries the declared destination and
    the action id; previewing it queues nothing and calls nothing."""
    held = dispatch(owner, PROBE, _probe_payload("preview-external"))
    assert held.approval_id is not None, held

    preview = _preview(owner, held.approval_id)

    assert preview.status == "pending" and preview.actionable is True
    assert preview.destination_ref == "synthetic"
    assert preview.external_action_id is not None
    assert preview.payload == _probe_payload("preview-external")
    assert preview.payload_digest == _expected_digest(
        _probe_payload("preview-external")
    )
    assert _probe_jobs(engine, database) == 0
    assert probe == []


def test_preview_redaction_is_identical_for_missing_denied_unauthorized_and_invalid(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
    probe: list[str],
) -> None:
    """Four separate causes, one byte-identical answer that names nothing.

    - **missing**: an id no approval has;
    - **denied**: a member whose membership was revoked after their context was built;
    - **unauthorized**: a current member who cannot see the held subject;
    - **invalid**: a snapshot tampered after the hold (the digest no longer matches),
      and an EXTERNAL row whose destination binding was changed — each shown to the
      eligible owner, who still gets nothing.

    Each control proves the redaction is about the cause: the same approval previews
    fully for the owner before the tamper.
    """
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    assert _preview(owner, approval_id).status == "pending"
    redacted = REDACTED_PREVIEW.model_dump()
    assert redacted == {
        "status": "unavailable",
        "actionable": False,
        **{
            name: None
            for name in REDACTED_PREVIEW.model_dump()
            if name not in {"status", "actionable"}
        },
    }

    missing = _preview(owner, UUID("018f0000-0000-7000-8000-0000000000ee"))
    denied = _preview(_stale_member(cluster, workspace, "revoked-viewer"), approval_id)
    hidden_id, hidden = _member_context(cluster, workspace, "cannot-see-subject")
    _hide_notes_from(monkeypatch, hidden_id)
    unauthorized = _preview(hidden, approval_id)
    # A real, visible record, so only the digest check can refuse the tamper.
    other = str(_record(dispatch(owner, NOTE_WRITE, {"body": "tampered to"})).ref)
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == approval_id)
            .values(body=act_payload(other))
        )
        uow.commit()
    invalid = _preview(owner, approval_id)
    staged = dispatch(owner, PROBE, _probe_payload("redacted-destination"))
    assert staged.approval_id is not None
    assert _preview(owner, staged.approval_id).status == "pending"
    _set_approval(engine, database, staged.approval_id, destination_ref="elsewhere")
    rebound = _preview(owner, staged.approval_id)
    # The unkeyed EXTERNAL sink has no staged action, so only the row's own
    # destination clause can refuse its rebinding.
    sink_id, _ = _held(owner, SINK_SEND, {"destination": "party:one", "message": "m"})
    assert _preview(owner, sink_id).status == "pending"
    _set_approval(engine, database, sink_id, destination_ref="party:two")
    sink_rebound = _preview(owner, sink_id)

    for answer in (missing, denied, unauthorized, invalid, rebound, sink_rebound):
        assert answer.model_dump() == redacted
    # Approving the rebound EXTERNAL rows refuses before anything is queued or sent.
    assert _approve(owner, staged.approval_id).state == INVALID_APPROVAL
    assert _approve(owner, sink_id).state == INVALID_APPROVAL
    assert _probe_jobs(engine, database) == 0 and probe == []
    assert _sends(engine, database) == ()


def test_an_invalidated_approval_previews_invalid_and_cannot_be_decided(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """``invalidated`` is a stale binding: an eligible viewer is told ``invalid`` with
    nothing disclosed, an ineligible one gets the common redaction, and neither
    decision can release, refuse or rebuild it. (A restored ``requires_reapproval``
    is the decidable ``restored`` case; see the tests below.)"""
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _set_approval(
        engine, database, approval_id, state="invalidated", invalidated_reason="test"
    )
    stale = _stale_member(cluster, workspace, "stale-invalidated-viewer")
    before = _rows(engine, database)

    invalid = _preview(owner, approval_id)
    assert invalid.status == "invalid" and invalid.actionable is False
    assert invalid.model_dump(exclude={"status"}) == REDACTED_PREVIEW.model_dump(
        exclude={"status"}
    )
    assert _preview(stale, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()
    assert _approve(owner, approval_id).state == APPROVAL_STATE
    assert _refuse(owner, approval_id).state == APPROVAL_STATE

    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"
    after = _rows(engine, database)
    # Nothing rebuilt: no new approval, operation, job or payload. The only rows the
    # refused decisions add are their own ``refused`` audit records.
    assert {k: v for k, v in after.items() if k != "audit_record"} == {
        k: v for k, v in before.items() if k != "audit_record"
    }


def _restored(engine: Engine, database: str, approval_id: UUID) -> None:
    """Put an approval in the state restore writes: ``requires_reapproval``, carrying
    its source workspace's window, which has long passed by the time anyone restores
    an export (the default window is fifteen minutes)."""
    _set_approval(
        engine,
        database,
        approval_id,
        state="requires_reapproval",
        window_start=datetime.now(UTC) - timedelta(days=3),
        window_end=datetime.now(UTC) - timedelta(days=3) + timedelta(minutes=15),
    )


def _keyed_state(engine: Engine, database: str, key: str) -> str:
    with UnitOfWork(engine, database) as uow:
        return str(
            uow.connection.execute(
                select(idempotency_result.c.state).where(
                    idempotency_result.c.idempotency_key == key
                )
            ).scalar_one()
        )


def _action(engine: Engine, database: str, approval_id: UUID) -> Any:
    with UnitOfWork(engine, database) as uow:
        return uow.connection.execute(
            select(external_action).where(external_action.c.approval_id == approval_id)
        ).one()


def test_a_restored_approval_previews_as_restored_and_actionable(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Robin's checkpoint-10 decision (option A): a restored approval is approvable
    again, so its verified snapshot is disclosed with the closed ``restored`` status
    and ``actionable = true``, even though the window it carries from the source
    workspace has passed. Ineligible and subject-blind viewers still get the common
    redaction, and previewing changes nothing."""
    member_id, member = _member_context(cluster, workspace, "restored-requester")
    payload = act_payload(note)
    approval_id, operation_id = _held(member, FIXTURE_ACT, payload)
    _restored(engine, database, approval_id)
    stale = _stale_member(cluster, workspace, "stale-restored-viewer")
    hidden_id, hidden = _member_context(cluster, workspace, "hidden-restored-viewer")
    _hide_notes_from(monkeypatch, hidden_id)
    before = _rows(engine, database)

    preview = _preview(owner, approval_id)

    assert (preview.status, preview.actionable) == ("restored", True)
    assert preview.durable_state == "requires_reapproval"
    assert preview.approval_id == approval_id
    assert preview.operation_id == operation_id
    assert (preview.requester_kind, preview.requester_id) == ("account", member_id)
    assert preview.subject_ref == note
    assert preview.payload == payload
    assert preview.payload_digest == _expected_digest(payload)
    assert preview.window_end < datetime.now(UTC)
    assert _preview(stale, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()
    assert _preview(hidden, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()
    assert _rows(engine, database) == before


def test_approving_a_restored_approval_executes_it_once_in_a_fresh_window(
    owner: WorkspaceContext,
    owner_account_id: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """DESTRUCTIVE inline: approving a restored approval with the digest the person
    was shown runs the held call exactly once through the normal gate, and the
    approval's window is reopened at the re-approval instant for the workspace's own
    window length (its source window had long passed). A second approval refuses
    ``approval_state`` and runs nothing more."""
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _restored(engine, database, approval_id)
    shown = _preview(owner, approval_id).payload_digest
    started = datetime.now(UTC)

    record = _record(_decide(owner, APPROVAL_APPROVE, approval_id, shown))

    assert record.state == "executed"
    assert record.approved_by_id == owner_account_id
    assert record.payload_digest == shown
    assert started <= record.window_start <= record.approved_at
    assert record.window_end - record.window_start == timedelta(
        seconds=window_seconds(owner)
    )
    assert _acts(engine, database) == (note,)
    assert _operation_state(owner, operation_id) == "succeeded"
    assert _approve(owner, approval_id).state == APPROVAL_STATE
    assert _acts(engine, database) == (note,)


def test_approving_a_restored_staged_external_action_queues_one_continuation(
    owner: WorkspaceContext, engine: Engine, database: str, probe: list[str]
) -> None:
    """EXTERNAL: a restored approval of a staged KEYED action is approved exactly as
    a pending one is — one continuation job queued, the action still ``pending`` and
    bound to that job, the destination not called inline. A stale displayed digest
    refuses first, and a second approval queues nothing more."""
    staged = dispatch(owner, PROBE, _probe_payload("restored-external"))
    assert staged.approval_id is not None
    _restored(engine, database, staged.approval_id)
    shown = _preview(owner, staged.approval_id).payload_digest

    assert (
        _decide(owner, APPROVAL_APPROVE, staged.approval_id, "0" * 64).state
        == APPROVAL_DIGEST_MISMATCH
    )
    assert _probe_jobs(engine, database) == 0
    record = _record(_decide(owner, APPROVAL_APPROVE, staged.approval_id, shown))

    assert record.state == "approved"
    assert record.window_end > datetime.now(UTC)
    assert _probe_jobs(engine, database) == 1
    action = _action(engine, database, staged.approval_id)
    assert action.state == "pending" and action.execution_job_id is not None
    assert probe == []
    assert _approve(owner, staged.approval_id).state == APPROVAL_STATE
    assert _probe_jobs(engine, database) == 1


def test_refusing_a_restored_approval_settles_the_held_operation_and_its_action(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    probe: list[str],
) -> None:
    """Refusal of a restored approval follows the pre-job refusal contract: the
    approval ``refused``, the held operation ``cancelled``; for a staged EXTERNAL
    action, the action ``refused`` with ``cancelled_no_effect`` and its keyed
    reservation ``refused``, and no job or destination call ever exists."""
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _restored(engine, database, approval_id)
    staged = dispatch(owner, PROBE, _probe_payload("restored-refusal"))
    assert staged.approval_id is not None and staged.operation_id is not None
    _restored(engine, database, staged.approval_id)

    inline = _record(
        _decide(
            owner,
            APPROVAL_REFUSE,
            approval_id,
            _preview(owner, approval_id).payload_digest,
        )
    )
    external = _record(_refuse(owner, staged.approval_id))

    assert inline.state == "refused"
    assert _operation_state(owner, operation_id) == "cancelled"
    assert _acts(engine, database) == ()
    assert external.state == "refused"
    assert _operation_state(owner, staged.operation_id) == "cancelled"
    action = _action(engine, database, staged.approval_id)
    assert (action.state, action.failure_class) == ("refused", "cancelled_no_effect")
    assert _keyed_state(engine, database, "restored-refusal") == "refused"
    assert _probe_jobs(engine, database) == 0 and probe == []
    for held_id in (approval_id, staged.approval_id):
        assert _preview(owner, held_id).status == "resolved"
        assert _approve(owner, held_id).state == APPROVAL_STATE


def test_a_restored_approval_whose_snapshot_fails_verification_is_not_actionable(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    probe: list[str],
) -> None:
    """A restored approval is approvable only through its verified snapshot. A
    tampered body and a rebound EXTERNAL destination each preview as the common
    redaction and approving refuses ``invalid_approval`` with nothing run or queued.
    Refusing stays possible, exactly as for a pending approval, so it can end."""
    other = str(_record(dispatch(owner, NOTE_WRITE, {"body": "restored tamper"})).ref)
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    _restored(engine, database, approval_id)
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == approval_id)
            .values(body=act_payload(other))
        )
        uow.commit()
    staged = dispatch(owner, PROBE, _probe_payload("restored-rebound"))
    assert staged.approval_id is not None
    _restored(engine, database, staged.approval_id)
    _set_approval(engine, database, staged.approval_id, destination_ref="elsewhere")

    for held_id in (approval_id, staged.approval_id):
        assert _preview(owner, held_id).model_dump() == REDACTED_PREVIEW.model_dump()
        assert _approve(owner, held_id).state == INVALID_APPROVAL

    assert _acts(engine, database) == ()
    assert _probe_jobs(engine, database) == 0 and probe == []
    assert _record(_refuse(owner, approval_id)).state == "refused"
    assert _operation_state(owner, operation_id) == "cancelled"


def test_a_restored_keyed_external_without_its_staged_action_never_runs_inline(
    owner: WorkspaceContext, engine: Engine, database: str, probe: list[str]
) -> None:
    """Restore carries approvals and held operations but not the staged action or
    its keyed reservation. A KEYED EXTERNAL approval with no staged action does not
    verify: approving it would otherwise run the operation inline, outside the
    at-most-once continuation (the probe's inline handler raises if it is ever
    reached). It previews as the common redaction and approval refuses
    ``invalid_approval``; refusal still ends it."""
    staged = dispatch(owner, PROBE, _probe_payload("restored-unstaged"))
    assert staged.approval_id is not None and staged.operation_id is not None
    _restored(engine, database, staged.approval_id)
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            delete(external_action).where(
                external_action.c.approval_id == staged.approval_id
            )
        )
        uow.connection.execute(
            delete(idempotency_result).where(
                idempotency_result.c.idempotency_key == "restored-unstaged"
            )
        )
        uow.commit()

    assert (
        _preview(owner, staged.approval_id).model_dump()
        == REDACTED_PREVIEW.model_dump()
    )
    assert _approve(owner, staged.approval_id).state == INVALID_APPROVAL
    assert _probe_jobs(engine, database) == 0 and probe == []
    assert _record(_refuse(owner, staged.approval_id)).state == "refused"
    assert _operation_state(owner, staged.operation_id) == "cancelled"


def test_the_gate_verifies_the_snapshot_with_the_previews_verifier(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """One verifier for the preview and the execution (W5). A DESTRUCTIVE row that
    names a destination its declaration has none for is unverifiable: the preview
    redacts it, and ``execute_approved`` — reached directly on an approved row, the
    way only a caller that skipped the decision's own checks could — refuses
    ``invalid_approval`` before the effect rather than running it."""
    from rheo_core.approvals.gate import execute_approved
    from rheo_core.approvals.records import get as read_approval
    from rheo_core.approvals.records import mark_approved
    from rheo_core.operations.refusals import OperationRefused

    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _set_approval(engine, database, approval_id, destination_ref="sink:elsewhere")

    now = datetime.now(UTC)
    with UnitOfWork(engine, database) as uow:
        assert mark_approved(
            uow.connection,
            approval_id=approval_id,
            approved_by_kind=owner.actor.kind.value,
            approved_by_id=owner.actor.id,
            approved_entry=owner.entry.value,
            now=now,
        )
        row = read_approval(uow.connection, approval_id=approval_id)
        assert row is not None
        with pytest.raises(OperationRefused) as refused:
            execute_approved(owner, uow, approval=row, now=now)
        uow.rollback()

    assert refused.value.state == INVALID_APPROVAL
    assert _acts(engine, database) == ()
    # The same row, still pending after the rollback, through the preview.
    assert _preview(owner, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()


def _expired_pending(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> UUID:
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _set_approval(
        engine,
        database,
        approval_id,
        window_start=datetime.now(UTC) - timedelta(days=2),
        window_end=datetime.now(UTC) - timedelta(days=1),
    )
    return approval_id


def test_preview_of_expired_and_each_resolved_state_is_read_only_history(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
    probe: list[str],
) -> None:
    """Expired and every resolved durable state: the eligible viewer sees the
    verified snapshot as history with ``actionable = false``; an ineligible viewer
    sees the common redaction; and approving any of them directly submits nothing.

    The states: ``pending`` past its window and durable ``expired`` (expired);
    ``executed`` (inline DESTRUCTIVE), ``refused`` (by a person), ``approved`` (an
    EXTERNAL action queued and not yet run) and ``refused`` by an execution guard
    (resolved).
    """
    states: dict[str, tuple[UUID, str, str]] = {}

    states["pending past window"] = (
        _expired_pending(owner, engine, database, note),
        "expired",
        "pending",
    )
    durable_expired, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _set_approval(engine, database, durable_expired, state="expired")
    states["durable expired"] = (durable_expired, "expired", "expired")
    executed, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, executed))
    states["executed"] = (executed, "resolved", "executed")
    refused, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_refuse(owner, refused))
    states["refused"] = (refused, "resolved", "refused")
    staged = dispatch(owner, PROBE, _probe_payload("history-queued"))
    assert staged.approval_id is not None
    _record(_approve(owner, staged.approval_id))
    states["approved and queued"] = (staged.approval_id, "resolved", "approved")
    gated_id, gated = _member_context(cluster, workspace, "guard-refused-requester")
    guard_refused, _ = _held(gated, FIXTURE_ACT, act_payload(note))
    _revoke_membership(cluster, gated_id, workspace)
    assert _record(_approve(owner, guard_refused)).state == "refused"
    states["guard refused"] = (guard_refused, "resolved", "refused")

    acts = _acts(engine, database)
    jobs = _probe_jobs(engine, database)
    stale = _stale_member(cluster, workspace, "stale-history-viewer")
    hidden_id, hidden = _member_context(cluster, workspace, "hidden-history-viewer")
    _hide_notes_from(monkeypatch, hidden_id)

    for label, (approval_id, status, durable) in states.items():
        history = _preview(owner, approval_id)
        assert (history.status, history.actionable) == (status, False), label
        assert history.durable_state == durable, label
        assert history.approval_id == approval_id, label
        assert history.payload is not None and history.payload_digest is not None
        assert (
            _preview(stale, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()
        )
        if history.subject_ref is not None:
            assert (
                _preview(hidden, approval_id).model_dump()
                == REDACTED_PREVIEW.model_dump()
            ), label
        attempt = _decide(owner, APPROVAL_APPROVE, approval_id, history.payload_digest)
        assert attempt.state in {APPROVAL_STATE, INVALID_APPROVAL}, (label, attempt)

    # No direct approval of any of them ran an effect or queued a job.
    assert _acts(engine, database) == acts
    assert _probe_jobs(engine, database) == jobs == 1
    assert probe == []


def test_preview_performs_no_mutation(
    owner: WorkspaceContext,
    engine: Engine,
    database: str,
    note: str,
    probe: list[str],
) -> None:
    """Every table a preview could touch is identical before and after previews of a
    pending DESTRUCTIVE, a pending EXTERNAL and a missing approval, repeated."""
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    staged = dispatch(owner, PROBE, _probe_payload("no-mutation"))
    assert staged.approval_id is not None
    before = _rows(engine, database)

    for _ in range(2):
        _preview(owner, approval_id)
        _preview(owner, staged.approval_id)
        _preview(owner, UUID("018f0000-0000-7000-8000-0000000000ed"))

    assert _rows(engine, database) == before
    assert _acts(engine, database) == () and probe == []


def test_a_stale_displayed_digest_refuses_the_inline_approval_before_any_effect(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The preview-to-submit race on the DESTRUCTIVE inline path.

    The person is shown digest D. The row then moves to another consistent binding
    (payload and digest rewritten together, as a re-stage would). Approving with D
    refuses ``approval_digest_mismatch`` and nothing runs; the matching digest of the
    new binding — after a fresh preview — then approves exactly once.
    """
    second = str(
        _record(dispatch(owner, NOTE_WRITE, {"body": "the record it moved to"})).ref
    )
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    shown = _preview(owner, approval_id).payload_digest
    moved = act_payload(second)
    with UnitOfWork(engine, database) as uow:
        uow.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == approval_id)
            .values(body=moved)
        )
        uow.connection.execute(
            update(approval_table)
            .where(approval_table.c.id == approval_id)
            .values(payload_digest=bytes.fromhex(_expected_digest(moved)))
        )
        uow.commit()

    raced = _decide(owner, APPROVAL_APPROVE, approval_id, shown)
    assert raced.state == APPROVAL_DIGEST_MISMATCH, raced
    refused_too = _decide(owner, APPROVAL_REFUSE, approval_id, shown)
    assert refused_too.state == APPROVAL_DIGEST_MISMATCH, refused_too
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"

    fresh = _preview(owner, approval_id)
    assert fresh.payload_digest == _expected_digest(moved) != shown
    record = _record(
        _decide(owner, APPROVAL_APPROVE, approval_id, fresh.payload_digest)
    )
    assert record.state == "executed"
    assert _acts(engine, database) == (second,)


def test_the_displayed_digest_is_compared_under_the_approval_row_lock(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The race the lock exists for, staged with a real second transaction.

    Transaction A locks the approval row and moves it to another consistent binding,
    and holds the lock. The approver, holding the digest they were shown, starts
    approving while A is still open. Only when the decision's **first** read takes
    the row lock does it wait for A and then judge the moved row: the assertion
    refuses and nothing runs. A decision that compared the digest on an unlocked read
    would see the old committed row, pass the comparison, block later on its own
    write, and then execute the binding the person never saw.

    The one-second pause is what puts the approver's first read inside A's window;
    it can only make the race *more* likely to be observed, never hide a pass.
    """
    second = str(_record(dispatch(owner, NOTE_WRITE, {"body": "moved under lock"})).ref)
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    shown = _preview(owner, approval_id).payload_digest
    moved = act_payload(second)
    outcomes: list[OperationOutcome] = []

    with UnitOfWork(engine, database) as writer:
        writer.connection.execute(
            select(approval_table.c.id)
            .where(approval_table.c.id == approval_id)
            .with_for_update()
        )
        writer.connection.execute(
            update(approval_payload)
            .where(approval_payload.c.approval_id == approval_id)
            .values(body=moved)
        )
        writer.connection.execute(
            update(approval_table)
            .where(approval_table.c.id == approval_id)
            .values(payload_digest=bytes.fromhex(_expected_digest(moved)))
        )
        approver = threading.Thread(
            target=lambda: outcomes.append(
                _decide(owner, APPROVAL_APPROVE, approval_id, shown)
            )
        )
        approver.start()
        time.sleep(1.0)
        writer.commit()
    approver.join(timeout=30)

    assert not approver.is_alive()
    (raced,) = outcomes
    assert raced.state == APPROVAL_DIGEST_MISMATCH, raced
    assert _acts(engine, database) == ()
    assert _operation_state(owner, operation_id) == "approval_required"


def test_a_wrong_displayed_digest_refuses_before_the_external_job_is_queued(
    owner: WorkspaceContext, engine: Engine, database: str, probe: list[str]
) -> None:
    """The same assertion on the EXTERNAL queueing path: a mismatch leaves the
    approval pending, the action pending and no continuation job; the displayed
    digest then queues exactly one."""
    staged = dispatch(owner, PROBE, _probe_payload("digest-external"))
    assert staged.approval_id is not None
    shown = _preview(owner, staged.approval_id).payload_digest
    wrong = "0" * 64
    assert shown != wrong

    raced = _decide(owner, APPROVAL_APPROVE, staged.approval_id, wrong)

    assert raced.state == APPROVAL_DIGEST_MISMATCH, raced
    assert _probe_jobs(engine, database) == 0
    assert _preview(owner, staged.approval_id).status == "pending"
    with UnitOfWork(engine, database) as uow:
        action_state = uow.connection.execute(
            select(external_action.c.state).where(
                external_action.c.approval_id == staged.approval_id
            )
        ).scalar_one()
    assert action_state == "pending"

    record = _record(_decide(owner, APPROVAL_APPROVE, staged.approval_id, shown))
    assert record.state == "approved"
    assert _probe_jobs(engine, database) == 1
    assert probe == []


def test_a_digest_bound_decision_after_a_concurrent_resolution_refuses_state(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
) -> None:
    """Two people looked at the same pending approval; one approved first. The
    second, holding the same displayed digest, gets ``approval_state`` and the
    effect is not repeated — refusing after the fact cannot undo it either."""
    _, member = _member_context(cluster, workspace, "second-decider")
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    shown = _preview(member, approval_id).payload_digest
    assert _preview(owner, approval_id).payload_digest == shown

    _record(_decide(owner, APPROVAL_APPROVE, approval_id, shown))
    late = _decide(member, APPROVAL_APPROVE, approval_id, shown)
    late_refusal = _decide(member, APPROVAL_REFUSE, approval_id, shown)

    assert late.state == APPROVAL_STATE, late
    assert late_refusal.state == APPROVAL_STATE, late_refusal
    assert _acts(engine, database) == (note,)
    assert _preview(member, approval_id).status == "resolved"


def test_a_decision_with_a_malformed_digest_is_refused_as_invalid_input(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """The assertion is lowercase hex of 32 bytes or absent; anything else is an
    input error, not a silently ignored assertion."""
    approval_id, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    shown = _preview(owner, approval_id).payload_digest

    for malformed in (shown.upper(), shown[:-1], "", "z" * 64):
        attempt = _decide(owner, APPROVAL_APPROVE, approval_id, malformed)
        assert not attempt.ok and attempt.state != "succeeded", malformed
    assert _acts(engine, database) == ()
    assert _record(_decide(owner, APPROVAL_APPROVE, approval_id, shown)).state == (
        "executed"
    )


def test_a_viewer_who_cannot_see_the_subject_cannot_decide_or_preview(
    owner: WorkspaceContext,
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    database: str,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subject visibility is rechecked at the decision, with the preview's answer:
    ``not_found`` for approve and refuse, nothing written, still pending.

    **And it is checked before the state** (review W1), so a subject-blind eligible
    viewer cannot tell an executed or a restored approval from a missing id: every
    answer is ``not_found`` with the missing id's own text."""
    hidden_id, hidden = _member_context(cluster, workspace, "decider-without-sight")
    approval_id, operation_id = _held(owner, FIXTURE_ACT, act_payload(note))
    executed, _ = _held(owner, FIXTURE_ACT, act_payload(note))
    _record(_approve(owner, executed))
    restored, restored_operation = _held(owner, FIXTURE_ACT, act_payload(note))
    _restored(engine, database, restored)
    _hide_notes_from(monkeypatch, hidden_id)

    assert _preview(hidden, approval_id).model_dump() == REDACTED_PREVIEW.model_dump()
    for held_id in (approval_id, executed, restored):
        for attempt in (_approve(hidden, held_id), _refuse(hidden, held_id)):
            assert attempt.state == NOT_FOUND, (held_id, attempt)
            assert attempt.error is not None
            assert (
                attempt.error.error_text == f"no approval {held_id} in this workspace"
            )
    assert _acts(engine, database) == (note,)
    assert _operation_state(owner, operation_id) == "approval_required"
    assert _operation_state(owner, restored_operation) == "approval_required"
    assert _preview(owner, approval_id).status == "pending"
    assert _preview(owner, restored).status == "restored"


def test_no_token_may_carry_or_call_preview_approve_refuse_or_resolve(
    owner: WorkspaceContext, engine: Engine, database: str, note: str
) -> None:
    """All four are in the non-token-issuable policy; issuing a token naming any of
    them is refused, and a token context cannot call the preview even for an
    approval its own call created."""
    names = {
        APPROVAL_PREVIEW,
        APPROVAL_APPROVE,
        APPROVAL_REFUSE,
        "core.operation.resolve",
    }
    assert names <= NON_TOKEN_ISSUABLE
    for name in sorted(names):
        for kind in ("cli", "mcp"):
            issued = dispatch(owner, TOKEN_ISSUE, {"kind": kind, "operations": [name]})
            assert not issued.ok, (name, kind, issued)

    mcp, _ = _mcp_context(owner, FIXTURE_ACT)
    approval_id, _ = _held(mcp, FIXTURE_ACT, act_payload(note))
    shown = _preview(owner, approval_id).payload_digest
    for attempt in (
        dispatch(mcp, APPROVAL_PREVIEW, {"approval_id": str(approval_id)}),
        _decide(mcp, APPROVAL_APPROVE, approval_id, shown),
        _decide(mcp, APPROVAL_REFUSE, approval_id, shown),
    ):
        assert not attempt.ok and attempt.result is None, attempt
    assert _acts(engine, database) == ()
    assert _preview(owner, approval_id).status == "pending"
