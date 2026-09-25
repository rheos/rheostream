"""Issue #131: the ways out of a failed delivery head, through ``dispatch``, against a
real Postgres. ``core.work.retry``, ``.skip`` and ``.replay`` move a delivery out of
``failed`` (or re-run a replay-safe consumer), and ``core.work.failure_summary`` counts
what is stuck.

Seam: the registered operations, reached through ``dispatch`` exactly as the API and
the CLI reach them, and the worker's own ``visit_workspace`` drain for the deliveries
they release. The consumer is ``tests/harness/consumers.py``'s recorder, with its
handler swapped per test; the worker's production ``CONSUMERS`` is empty until Leads
ships, so a harness consumer is the only kind there is to test with.

**Two kinds of setup, and each test says which it uses.** The headline test drives a
delivery to ``failed`` through the real drain and a real budget, because "a failing
consumer blocks its subject queue" is a claim about the worker. The others put a
delivery into ``failed`` through the repository's own ``lease_delivery`` and
``fail_delivery``, as ``test_work_failures.py`` does, because what they test is the
operation and eight visits per case would test the budget again. One test deletes an
outbox row directly, standing in for the retention sweep release one does not have;
it says so where it does it.

**Time.** The operations stamp the moment a person acted with the wall clock, as
``core.operation.resolve`` does, so the visits that follow them use a clock just past
the wall clock rather than a chosen far-off instant.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.consumers import (
    CONSUMER_ID,
    EVENT_TYPE,
    consumer_invocation,
    ensure_consumer_tables,
    invocation_count,
    record_delivery,
    subscription,
)
from harness.registry import add_member, enable_harness_module
from rheo_contracts import EventEnvelope, Role, WorkspaceContext
from rheo_core.audit import AUDIT_LIST, AuditList
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.boundary.factories import context_from_session, context_from_token
from rheo_core.events import (
    DELIVERED,
    FAILED,
    PENDING,
    SKIPPED,
    ConsumerRegistry,
    ConsumerSubscription,
    NewEvent,
    fail_delivery,
    lease_delivery,
    publish,
)
from rheo_core.operations import (
    INPUT_INVALID,
    OPERATION_NOT_PERMITTED,
    ROLE_NOT_PERMITTED,
    WORK_FAILURE_SUMMARY,
    WORK_FAILURES,
    WORK_REPLAY,
    WORK_RETRY,
    WORK_SKIP,
    dispatch,
    register_core_operations,
)
from rheo_core.operations.core_ops import TOKEN_ISSUE
from rheo_core.operations.dispatch import CONSUMERS_MISSING
from rheo_core.operations.records import mark_unresolved, mint
from rheo_core.sessions import create_session, mint_host_secret, switch_workspace
from rheo_core.settings import resolve
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_index_tables import workspace_work_due
from rheo_core.storage.work_tables import (
    consumer_processed,
    event_delivery,
    outbox_event,
)
from rheo_core.tokens.issue import issue_runtime_token
from rheo_core.tokens.policy import PERSON_HELD_TOKEN_KINDS
from rheo_core.work import operations as work_operations
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_core.work.operations import (
    CONSUMER_NOT_ENABLED,
    CONSUMER_UNKNOWN,
    DELIVERY_IN_FLIGHT,
    DELIVERY_NOT_FOUND,
    DELIVERY_STATE,
    REPLAY_GAP,
    REPLAY_UNSAFE,
    DeliveryChanged,
    FailureList,
    FailureSummary,
    ReplayResult,
)
from rheo_core.work.operations import CONSUMERS_MISSING as WORK_CONSUMERS_MISSING
from sqlalchemy import Engine, Row, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

pytestmark = pytest.mark.postgres

OWNER = "worker-under-test"
SUBJECT = "harness.note:one"
OTHER_SUBJECT = "harness.note:two"
HOST = "example.test"
LEASE = 60


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """This test's workspace database, with ``harness`` enabled and the recorder's
    tables created, as ``test_event_delivery.py`` builds it."""
    built = cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )
    with built.begin() as conn:
        enable_harness_module(conn)
        ensure_consumer_tables(conn)
    return built


@pytest.fixture
def ctx(workspace: UUID, engine: Engine) -> WorkspaceContext:
    """An operator context, the role the operator CLI carries."""
    built = context_for_operator(workspace)
    assert isinstance(built, WorkspaceContext), built
    return built


def _failing_for(
    failing: set[UUID],
) -> Callable[[HandlerUnitOfWork, EventEnvelope], None]:
    """The recorder, raising for any event whose id is in ``failing`` at call time.

    The set is read on every call, so a test "fixes" the consumer by removing an id,
    which is the situation a retry exists for.
    """

    def handler(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
        record_delivery(uow, envelope)
        if envelope.id in failing:
            raise RuntimeError(f"this consumer cannot take {envelope.id} yet")

    return handler


def _registry(*subscriptions: ConsumerSubscription) -> ConsumerRegistry:
    built = ConsumerRegistry()
    for one in subscriptions:
        built.register(one)
    return built


def _publish(
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
    *,
    at: datetime,
    subject_ref: str = SUBJECT,
) -> UUID:
    with open_unit_of_work(ctx) as uow:
        event_id = publish(
            ctx,
            uow,
            NewEvent(
                type=EVENT_TYPE,
                schema_version=1,
                subject_ref=subject_ref,
                subject_revision=1,
                data={"body": "a note"},
            ),
            now=at,
            consumers=consumers,
        )
        uow.commit()
    return event_id


def _visit(
    workspace_id: UUID,
    cluster: ClusterSession,
    consumers: ConsumerRegistry,
    at: datetime,
) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace_id, observed_due_at=None),
        kinds=JobKindRegistry(),
        consumers=consumers,
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: at,
        jitter=None,
    )


def _soon() -> datetime:
    """Just past the wall clock, which is what the operations stamp their rows with."""
    return datetime.now(UTC) + timedelta(seconds=1)


def _delivery(
    engine: Engine, event_id: UUID, consumer_id: str = CONSUMER_ID
) -> Row[Any]:
    with engine.connect() as conn:
        return conn.execute(
            select(event_delivery).where(
                event_delivery.c.event_id == event_id,
                event_delivery.c.consumer_id == consumer_id,
            )
        ).one()


def _delivery_count(engine: Engine, event_id: UUID, consumer_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count())
                .select_from(event_delivery)
                .where(
                    event_delivery.c.event_id == event_id,
                    event_delivery.c.consumer_id == consumer_id,
                )
            ).scalar_one()
        )


def _invocations(engine: Engine, event_id: UUID) -> int:
    with engine.connect() as conn:
        return invocation_count(conn, event_id=event_id)


def _fail_head(engine: Engine, event_id: UUID, *, at: datetime, error: str) -> None:
    """Setup: lease the next due delivery, which must be ``event_id``, and fail it."""
    with engine.begin() as conn:
        leased = lease_delivery(conn, owner=OWNER, now=at, lease_seconds=LEASE)
    assert leased is not None and leased.event_id == event_id, leased
    with engine.begin() as conn:
        assert fail_delivery(
            conn,
            event_id=event_id,
            consumer_id=leased.consumer_id,
            owner=OWNER,
            now=at,
            error=error,
        )


def _ref(event_id: UUID, consumer_id: str = CONSUMER_ID) -> dict[str, object]:
    return {"event_id": str(event_id), "consumer_id": consumer_id}


def _audit_names(ctx: WorkspaceContext) -> list[tuple[str, str]]:
    outcome = dispatch(ctx, AUDIT_LIST, {"limit": 500})
    assert outcome.ok, outcome
    assert isinstance(outcome.result, AuditList)
    return [(row.operation_name, row.outcome) for row in outcome.result.records]


def _due_at(cluster: ClusterSession, workspace_id: UUID) -> datetime | None:
    with cluster.backend.control_engine.connect() as conn:
        value = conn.execute(
            select(workspace_work_due.c.due_at).where(
                workspace_work_due.c.workspace_id == workspace_id
            )
        ).scalar_one_or_none()
    return value if isinstance(value, datetime) else None


# --- retry, end to end through the worker ---------------------------------------------


def test_a_failed_head_blocks_its_subject_until_retried_and_then_delivers_in_order(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """The whole story of a blocked queue, driven through the real drain.

    A consumer that cannot take the first event of a subject exhausts its budget and
    ends ``failed``; the second event of that subject waits ``pending`` behind it,
    while the other subject is delivered. Retry resets the head's attempts, the
    consumer is fixed, and the next visit delivers the head and then the event behind
    it, in position order. The due mark is checked too: without it the retried row
    would wait for the reconcile floor rather than the next pass.
    """
    now = datetime.now(UTC)
    failing: set[UUID] = set()
    consumers = _registry(subscription(_failing_for(failing)))
    head = _publish(ctx, consumers, at=now)
    other = _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT)
    behind = _publish(ctx, consumers, at=now)
    failing.add(head)

    budget = resolve().get_int("work.max_attempts")
    at = now
    for _ in range(budget + 1):
        _visit(workspace, cluster, consumers, at)
        row = _delivery(engine, head)
        if row.state == FAILED:
            break
        at = row.next_attempt_at
    assert _delivery(engine, head).state == FAILED
    assert _delivery(engine, head).attempts == budget
    assert _delivery(engine, other).state == DELIVERED, "another subject is not held"
    assert _delivery(engine, behind).state == PENDING, "the same subject waits"
    assert _invocations(engine, behind) == 0

    listing = dispatch(ctx, WORK_FAILURES, {})
    assert isinstance(listing.result, FailureList)
    assert [(d.event_id, d.blocked_count) for d in listing.result.deliveries] == [
        (head, 1)
    ]

    # The last visit pushed the index out; a retry must pull it back to now.
    with cluster.backend.control_engine.begin() as conn:
        conn.execute(
            workspace_work_due.update()
            .where(workspace_work_due.c.workspace_id == workspace)
            .values(due_at=datetime.now(UTC) + timedelta(hours=1))
        )
    before_retry = datetime.now(UTC)
    outcome = dispatch(ctx, WORK_RETRY, _ref(head))
    assert outcome.ok, outcome
    assert outcome.result == DeliveryChanged(
        event_id=head, consumer_id=CONSUMER_ID, state=PENDING
    )
    retried = _delivery(engine, head)
    assert retried.state == PENDING
    assert retried.attempts == 0
    assert retried.completed_at is None
    assert retried.next_attempt_at >= before_retry
    assert retried.last_error is not None, "the evidence stays until a success"
    due = _due_at(cluster, workspace)
    assert due is not None and due <= datetime.now(UTC), due

    failing.discard(head)
    _visit(workspace, cluster, consumers, _soon())
    delivered = _delivery(engine, head)
    assert delivered.state == DELIVERED
    assert delivered.attempts == 1, "the budget started again"
    assert delivered.last_error is None
    assert _delivery(engine, behind).state == DELIVERED, "the queue is released"
    assert _invocations(engine, behind) == 1
    assert (WORK_RETRY, "succeeded") in _audit_names(ctx)


def test_a_retry_that_fails_again_walks_the_backoff_from_the_start(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """Setup through the repository. The retried row is leased as attempt one and,
    still failing, requeued ``pending`` rather than ended ``failed`` at once: a retry
    that kept the spent count would put it straight back where it was."""
    now = datetime.now(UTC)
    failing: set[UUID] = set()
    consumers = _registry(subscription(_failing_for(failing)))
    head = _publish(ctx, consumers, at=now)
    failing.add(head)
    _fail_head(engine, head, at=now, error="first failure")

    assert dispatch(ctx, WORK_RETRY, _ref(head)).ok
    at = _soon()
    _visit(workspace, cluster, consumers, at)
    row = _delivery(engine, head)
    assert row.state == PENDING
    assert row.attempts == 1
    assert row.next_attempt_at > at
    assert row.last_error is not None and "cannot take" in row.last_error


@pytest.mark.parametrize("act", [WORK_RETRY, WORK_SKIP])
def test_retry_and_skip_refuse_a_delivery_that_is_not_failed(
    engine: Engine, ctx: WorkspaceContext, act: str
) -> None:
    """``pending`` is refused naming its state, and an unknown pair is ``not_found``.
    Both refusals are audited, like every refused mutate."""
    consumers = _registry(subscription())
    event_id = _publish(ctx, consumers, at=datetime.now(UTC))
    payload = {**_ref(event_id), "note": "not needed"}

    outcome = dispatch(ctx, act, payload)
    assert outcome.state == DELIVERY_STATE, outcome
    assert _delivery(engine, event_id).state == PENDING

    missing = dispatch(ctx, act, {**payload, "consumer_id": "harness.nobody"})
    assert missing.state == DELIVERY_NOT_FOUND, missing
    assert (act, "refused") in _audit_names(ctx)


# --- skip -----------------------------------------------------------------------------


def test_skip_with_a_note_releases_its_queue_only_and_is_audited(
    engine: Engine, ctx: WorkspaceContext
) -> None:
    """Two subjects, each with a failed head and a delivery waiting behind it.

    Skipping one head releases the delivery behind it and nothing else: the other
    subject's head is still failed and its follower still unleasable. The note and the
    original error both survive on the row, and the skip is audited ``succeeded``.
    """
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    first = _publish(ctx, consumers, at=now)
    other_head = _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT)
    behind = _publish(ctx, consumers, at=now)
    other_behind = _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT)
    _fail_head(engine, first, at=now, error="bad payload")
    _fail_head(engine, other_head, at=now, error="also bad")
    with engine.begin() as conn:
        assert lease_delivery(conn, owner=OWNER, now=now, lease_seconds=LEASE) is None

    refused = dispatch(ctx, WORK_SKIP, _ref(first))
    assert refused.state == INPUT_INVALID, "a skip without a note is refused"
    blank = dispatch(ctx, WORK_SKIP, {**_ref(first), "note": ""})
    assert blank.state == INPUT_INVALID

    outcome = dispatch(
        ctx, WORK_SKIP, {**_ref(first), "note": "payload predates the schema"}
    )
    assert outcome.ok, outcome
    skipped = _delivery(engine, first)
    assert skipped.state == SKIPPED
    assert skipped.completed_at is not None
    assert skipped.last_error == (
        "skipped: payload predates the schema\nlast error: bad payload"
    )

    with engine.begin() as conn:
        released = lease_delivery(conn, owner=OWNER, now=_soon(), lease_seconds=LEASE)
        assert released is not None and released.event_id == behind
        assert (
            lease_delivery(conn, owner=OWNER, now=_soon(), lease_seconds=LEASE) is None
        ), "the other subject's queue is still held by its own failed head"
    assert _delivery(engine, other_head).state == FAILED
    assert _delivery(engine, other_behind).state == PENDING
    assert (WORK_SKIP, "succeeded") in _audit_names(ctx)


# --- replay ---------------------------------------------------------------------------


def _delivered_three(
    workspace: UUID,
    cluster: ClusterSession,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> tuple[UUID, UUID, UUID]:
    now = datetime.now(UTC)
    events = (
        _publish(ctx, consumers, at=now),
        _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT),
        _publish(ctx, consumers, at=now),
    )
    _visit(workspace, cluster, consumers, _soon())
    for event_id in events:
        assert _delivery(engine, event_id).state == DELIVERED
        assert _invocations(engine, event_id) == 1
    return events


def test_replay_reruns_a_replay_safe_consumer_from_a_position_without_duplicating(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """From the second event on: two deliveries reset, none created, the ledger rows
    for exactly those two gone, and the next visit runs the handler for those two and
    not for the first. A second replay resets the same rows again and still leaves
    one row per event, which is the primary key's rule stated as behaviour."""
    consumers = _registry(subscription())
    first, second, third = _delivered_three(workspace, cluster, engine, ctx, consumers)
    from_position = int(_delivery(engine, second).position)

    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": from_position},
        consumers=consumers,
    )
    assert outcome.ok, outcome
    assert outcome.result == ReplayResult(
        consumer_id=CONSUMER_ID, from_position=from_position, reset=2, created=0
    )
    assert _delivery(engine, first).state == DELIVERED
    for event_id in (second, third):
        row = _delivery(engine, event_id)
        assert row.state == PENDING and row.attempts == 0
    with engine.connect() as conn:
        ledger = {
            row.event_id
            for row in conn.execute(
                select(consumer_processed.c.event_id).where(
                    consumer_processed.c.consumer_id == CONSUMER_ID
                )
            )
        }
    assert ledger == {first}

    _visit(workspace, cluster, consumers, _soon())
    assert [_invocations(engine, e) for e in (first, second, third)] == [1, 2, 2]
    assert all(_delivery(engine, e).state == DELIVERED for e in (second, third))

    again = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": from_position},
        consumers=consumers,
    )
    assert isinstance(again.result, ReplayResult) and again.result.reset == 2
    assert [_delivery_count(engine, e, CONSUMER_ID) for e in (second, third)] == [1, 1]
    assert (WORK_REPLAY, "succeeded") in _audit_names(ctx)


def test_replay_creates_deliveries_for_a_consumer_that_had_none(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """A read model added after the events were published: the outbox is its history,
    so replay makes a ``pending`` row per retained event of its type, in the event's
    own position and subject, and the worker delivers them."""
    consumers = _registry(subscription())
    events = _delivered_three(workspace, cluster, engine, ctx, consumers)
    late = ConsumerSubscription(
        consumer_id="core.late_index",
        event_type=EVENT_TYPE,
        module_id="core",
        replay_safe=True,
        handler=record_delivery,
    )
    widened = _registry(subscription(), late)
    first_position = int(_delivery(engine, events[0]).position)

    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": late.consumer_id, "from_position": first_position},
        consumers=widened,
    )
    assert outcome.ok, outcome
    assert isinstance(outcome.result, ReplayResult)
    assert (outcome.result.reset, outcome.result.created) == (0, 3)
    for event_id in events:
        row = _delivery(engine, event_id, late.consumer_id)
        assert row.state == PENDING
        assert row.position == _delivery(engine, event_id).position
        assert row.subject_ref == _delivery(engine, event_id).subject_ref

    _visit(workspace, cluster, widened, _soon())
    assert all(
        _delivery(engine, e, late.consumer_id).state == DELIVERED for e in events
    )


def test_replay_refuses_a_consumer_that_is_not_replay_safe(
    workspace: UUID, cluster: ClusterSession, engine: Engine, ctx: WorkspaceContext
) -> None:
    """``replay_safe = False`` is a consumer whose effects must not happen twice, and
    nothing is reset for it."""
    unsafe = ConsumerSubscription(
        consumer_id=CONSUMER_ID,
        event_type=EVENT_TYPE,
        module_id="harness",
        replay_safe=False,
        handler=record_delivery,
    )
    consumers = _registry(unsafe)
    events = _delivered_three(workspace, cluster, engine, ctx, consumers)

    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": 1},
        consumers=consumers,
    )
    assert outcome.state == REPLAY_UNSAFE, outcome
    assert all(_delivery(engine, e).state == DELIVERED for e in events)
    assert (WORK_REPLAY, "refused") in _audit_names(ctx)


def test_replay_refuses_a_position_older_than_the_oldest_retained_event(
    workspace: UUID, cluster: ClusterSession, engine: Engine, ctx: WorkspaceContext
) -> None:
    """The first outbox row is deleted directly here, standing in for the retention
    sweep release one does not ship. A replay from its position would rebuild from a
    hole, so it is refused, naming the oldest position that is still there; from that
    position it runs."""
    consumers = _registry(subscription())
    first, second, _ = _delivered_three(workspace, cluster, engine, ctx, consumers)
    gone = int(_delivery(engine, first).position)
    with engine.begin() as conn:
        conn.execute(delete(outbox_event).where(outbox_event.c.id == first))
    oldest = int(_delivery(engine, second).position)

    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": gone},
        consumers=consumers,
    )
    assert outcome.state == REPLAY_GAP, outcome
    assert outcome.error is not None and str(oldest) in outcome.error.error_text
    assert _delivery(engine, second).state == DELIVERED

    fine = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": oldest},
        consumers=consumers,
    )
    assert fine.ok, fine


def test_replay_refuses_while_a_delivery_of_the_consumer_is_leased(
    engine: Engine, ctx: WorkspaceContext
) -> None:
    consumers = _registry(subscription())
    event_id = _publish(ctx, consumers, at=datetime.now(UTC))
    with engine.begin() as conn:
        assert lease_delivery(
            conn, owner=OWNER, now=datetime.now(UTC), lease_seconds=LEASE
        )
    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": CONSUMER_ID, "from_position": 1},
        consumers=consumers,
    )
    assert outcome.state == DELIVERY_IN_FLIGHT, outcome
    assert _delivery(engine, event_id).lease_owner == OWNER


def test_replay_refuses_an_unknown_a_disabled_or_an_unwired_consumer(
    workspace: UUID, cluster: ClusterSession, engine: Engine, ctx: WorkspaceContext
) -> None:
    consumers = _registry(subscription())
    _delivered_three(workspace, cluster, engine, ctx, consumers)
    payload = {"consumer_id": CONSUMER_ID, "from_position": 1}

    assert WORK_CONSUMERS_MISSING == CONSUMERS_MISSING
    assert dispatch(ctx, WORK_REPLAY, payload).state == CONSUMERS_MISSING
    unknown = dispatch(
        ctx,
        WORK_REPLAY,
        {**payload, "consumer_id": "harness.nobody"},
        consumers=consumers,
    )
    assert unknown.state == CONSUMER_UNKNOWN, unknown
    elsewhere = ConsumerSubscription(
        consumer_id="other.index",
        event_type=EVENT_TYPE,
        module_id="othermodule",
        replay_safe=True,
        handler=record_delivery,
    )
    disabled = dispatch(
        ctx,
        WORK_REPLAY,
        {**payload, "consumer_id": elsewhere.consumer_id},
        consumers=_registry(elsewhere),
    )
    assert disabled.state == CONSUMER_NOT_ENABLED, disabled


# --- who may call them ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "extra"),
    [
        (WORK_RETRY, {}),
        (WORK_SKIP, {"note": "a member tries"}),
        (WORK_REPLAY, None),
        (WORK_FAILURE_SUMMARY, None),
    ],
)
def test_a_member_is_refused_every_work_recovery_operation(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    name: str,
    extra: dict[str, object] | None,
) -> None:
    """``owner, operator`` only: ``member`` is the declaration default's other half,
    so this is the proof the roles were spelled out. The failed row is untouched."""
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    head = _publish(ctx, consumers, at=now)
    _fail_head(engine, head, at=now, error="stuck")
    member_id = add_member(cluster.backend, workspace, Role.MEMBER, display_name="m2")
    member = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(member, WorkspaceContext)

    payload: dict[str, object]
    if name == WORK_REPLAY:
        payload = {"consumer_id": CONSUMER_ID, "from_position": 1}
    elif extra is None:
        payload = {}
    else:
        payload = {**_ref(head), **extra}
    outcome = dispatch(member, name, payload, consumers=consumers)
    assert outcome.state == ROLE_NOT_PERMITTED, outcome
    assert outcome.result is None
    assert _delivery(engine, head).state == FAILED


def test_an_owner_may_retry_but_the_owners_model_may_not(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    owner_account_id: UUID,
) -> None:
    """An ``mcp`` token the owner issued naming the three writes explicitly (they are
    in no named set a model gets), and a run's ``runtime`` token naming them, reach
    the operations and are refused
    ``operation_not_permitted`` inside each: a way out of a failed head is a person's
    act. The rule is an allow-list, so the owner's ``cli`` token and the owner's own
    session both retry."""
    assert PERSON_HELD_TOKEN_KINDS == frozenset({"cli"}), "an allow-list of one"
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    head = _publish(ctx, consumers, at=now)
    _fail_head(engine, head, at=now, error="stuck")

    session_row = create_session(owner_account_id)
    secret = mint_host_secret(session_row.id, HOST)
    assert switch_workspace(session_row.id, workspace) is None
    owner = context_from_session(secret, HOST)
    assert isinstance(owner, WorkspaceContext), owner

    issued = dispatch(
        owner,
        TOKEN_ISSUE,
        {"kind": "mcp", "operations": [WORK_RETRY, WORK_SKIP, WORK_REPLAY]},
    )
    assert issued.ok, issued
    model = context_from_token(issued.result.value, "api")  # type: ignore[union-attr]
    assert isinstance(model, WorkspaceContext), model
    _, run_value = issue_runtime_token(
        account_id=owner_account_id,
        workspace_id=workspace,
        purpose="respond",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        operations=[WORK_RETRY, WORK_SKIP, WORK_REPLAY],
    )
    run = context_from_token(run_value, "mcp")
    assert isinstance(run, WorkspaceContext), run
    for held in (model, run):
        for name, payload in (
            (WORK_RETRY, _ref(head)),
            (WORK_SKIP, {**_ref(head), "note": "a model tries"}),
            (WORK_REPLAY, {"consumer_id": CONSUMER_ID, "from_position": 1}),
        ):
            outcome = dispatch(held, name, payload, consumers=consumers)
            assert outcome.state == OPERATION_NOT_PERMITTED, (name, outcome)
    assert _delivery(engine, head).state == FAILED

    # A ``cli`` token is the person's own, and the allow-list admits it.
    cli = dispatch(owner, TOKEN_ISSUE, {"kind": "cli", "operations": [WORK_RETRY]})
    assert cli.ok, cli
    person = context_from_token(cli.result.value, "api")  # type: ignore[union-attr]
    assert isinstance(person, WorkspaceContext), person
    assert dispatch(person, WORK_RETRY, _ref(head)).ok
    assert _delivery(engine, head).state == PENDING

    _fail_head(engine, head, at=_soon(), error="stuck again")
    assert dispatch(owner, WORK_RETRY, _ref(head)).ok, "and so does a session"


# --- failure_summary ------------------------------------------------------------------


def test_failure_summary_counts_failed_heads_what_they_block_and_unresolved_records(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """Two failed heads, one of them holding two deliveries back, and one delivered
    event that must not count; plus one ``unresolved`` operation record. After a skip
    the counts fall with it."""
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    empty = dispatch(ctx, WORK_FAILURE_SUMMARY, {})
    assert empty.result == FailureSummary(
        failed_count=0, blocked_count=0, oldest_failed_at=None, unresolved_count=0
    )

    delivered = _publish(ctx, consumers, at=now, subject_ref="harness.note:done")
    _visit(workspace, cluster, consumers, _soon())
    assert _delivery(engine, delivered).state == DELIVERED

    published = now - timedelta(minutes=10)
    older = _publish(ctx, consumers, at=published)
    newer = _publish(ctx, consumers, at=published, subject_ref=OTHER_SUBJECT)
    _publish(ctx, consumers, at=published)
    _publish(ctx, consumers, at=published)
    _fail_head(engine, older, at=now - timedelta(minutes=5), error="old")
    _fail_head(engine, newer, at=now, error="new")
    with open_unit_of_work(ctx) as uow:
        operation_id = mint(
            uow.connection,
            name="harness.op",
            safety_class="external",
            actor_kind="system",
            actor_id=None,
            entry="job",
            audience_kind="none",
            audience_id=None,
            now=now,
        )
        assert mark_unresolved(uow.connection, operation_id=operation_id, now=now)
        uow.commit()

    summary = dispatch(ctx, WORK_FAILURE_SUMMARY, {})
    assert summary.ok, summary
    assert summary.result == FailureSummary(
        failed_count=2,
        blocked_count=2,
        oldest_failed_at=now - timedelta(minutes=5),
        unresolved_count=1,
    )

    assert dispatch(ctx, WORK_SKIP, {**_ref(older), "note": "let it go"}).ok
    after = dispatch(ctx, WORK_FAILURE_SUMMARY, {})
    assert isinstance(after.result, FailureSummary)
    assert (after.result.failed_count, after.result.blocked_count) == (1, 0)
    assert after.result.oldest_failed_at == now


# --- cold-review cases: what replay keeps, clears and filters -------------------------

OTHER_EVENT_TYPE = "harness.note.archived"


def _replay(
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
    consumer_id: str,
    from_position: int,
) -> ReplayResult:
    outcome = dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": consumer_id, "from_position": from_position},
        consumers=consumers,
    )
    assert outcome.ok, outcome
    assert isinstance(outcome.result, ReplayResult)
    return outcome.result


def _ledger(engine: Engine, consumer_id: str) -> set[UUID]:
    with engine.connect() as conn:
        return {
            row.event_id
            for row in conn.execute(
                select(consumer_processed.c.event_id).where(
                    consumer_processed.c.consumer_id == consumer_id
                )
            )
        }


def test_replay_keeps_a_skipped_rows_note_until_a_success_clears_it(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """The row is the skip note's only readable home, since the audit keeps a digest.
    A replay that reset ``last_error`` would erase it before anything re-ran."""
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    head = _publish(ctx, consumers, at=now)
    _fail_head(engine, head, at=now, error="bad payload")
    assert dispatch(ctx, WORK_SKIP, {**_ref(head), "note": "known bad import"}).ok
    kept = "skipped: known bad import\nlast error: bad payload"

    _replay(ctx, consumers, CONSUMER_ID, int(_delivery(engine, head).position))
    reset = _delivery(engine, head)
    assert reset.state == PENDING
    assert reset.last_error == kept

    _visit(workspace, cluster, consumers, _soon())
    delivered = _delivery(engine, head)
    assert delivered.state == DELIVERED
    assert delivered.last_error is None


def test_replay_clears_the_ledger_of_in_range_pending_rows_and_no_other_consumers(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """A ``pending`` row left with a ledger row (a requeue after a lost finish) must
    have its handler run by a rebuild, so its ledger row goes too. Another consumer of
    the same events keeps every ledger row it has.

    The pending row's ledger row is written directly: it stands for the state a lost
    finish leaves, which no public path produces on demand.
    """
    other = ConsumerSubscription(
        consumer_id="core.other_index",
        event_type=EVENT_TYPE,
        module_id="core",
        replay_safe=True,
        handler=record_delivery,
    )
    consumers = _registry(subscription(), other)
    now = datetime.now(UTC)
    first = _publish(ctx, consumers, at=now)
    second = _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT)
    third = _publish(ctx, consumers, at=now)
    _visit(workspace, cluster, consumers, _soon())
    assert _ledger(engine, other.consumer_id) == {first, second, third}
    stranded = _publish(ctx, consumers, at=datetime.now(UTC))
    with engine.begin() as conn:
        conn.execute(
            consumer_processed.insert().values(
                consumer_id=CONSUMER_ID,
                event_id=stranded,
                processed_at=datetime.now(UTC),
            )
        )

    result = _replay(
        ctx, consumers, CONSUMER_ID, int(_delivery(engine, second).position)
    )
    assert (result.reset, result.created) == (2, 0), "the pending row is not reset"
    assert _ledger(engine, CONSUMER_ID) == {first}
    assert {first, second, third} <= _ledger(engine, other.consumer_id), (
        "the other consumer's ledger rows survive"
    )

    _visit(workspace, cluster, consumers, _soon())
    assert _delivery(engine, stranded).state == DELIVERED
    with engine.connect() as conn:
        ran_for = [
            row.consumer_id
            for row in conn.execute(
                select(consumer_invocation.c.consumer_id).where(
                    consumer_invocation.c.event_id == stranded
                )
            )
        ]
    assert CONSUMER_ID in ran_for, "the stranded pending row's handler really ran"
    assert _delivery(engine, second, other.consumer_id).attempts == 1


def test_replay_touches_only_its_consumers_event_type_and_only_from_the_position(
    cluster: ClusterSession, workspace: UUID, engine: Engine, ctx: WorkspaceContext
) -> None:
    """Three filters, each held by an event the replay must leave alone.

    * An outbox row of another type in range gets no created row.
    * This consumer's own delivered row of another type (it was subscribed to that type
      when the event was published) is not reset.
    * A late consumer replayed from a middle position gets no row below it.
    """
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    first = _publish(ctx, consumers, at=now)
    second = _publish(ctx, consumers, at=now)
    under_old_type = ConsumerSubscription(
        consumer_id=CONSUMER_ID,
        event_type=OTHER_EVENT_TYPE,
        module_id="harness",
        replay_safe=True,
        handler=record_delivery,
    )
    with open_unit_of_work(ctx) as uow:
        foreign = publish(
            ctx,
            uow,
            NewEvent(
                type=OTHER_EVENT_TYPE,
                schema_version=1,
                subject_ref=SUBJECT,
                subject_revision=2,
                data={"body": "another type"},
            ),
            now=now,
            consumers=_registry(under_old_type),
        )
        uow.commit()
    third = _publish(ctx, consumers, at=now, subject_ref=OTHER_SUBJECT)
    _visit(workspace, cluster, _registry(under_old_type), _soon())
    _visit(workspace, cluster, consumers, _soon())
    assert _delivery(engine, foreign).state == DELIVERED

    first_position = int(_delivery(engine, first).position)
    result = _replay(ctx, consumers, CONSUMER_ID, first_position)
    assert result.reset == 3
    assert _delivery(engine, foreign).state == DELIVERED, "another type is not reset"

    late = ConsumerSubscription(
        consumer_id="core.late_index",
        event_type=EVENT_TYPE,
        module_id="core",
        replay_safe=True,
        handler=record_delivery,
    )
    widened = _registry(subscription(), late)
    created = _replay(
        ctx, widened, late.consumer_id, int(_delivery(engine, second).position)
    )
    assert (created.reset, created.created) == (0, 2)
    assert _delivery_count(engine, first, late.consumer_id) == 0, "below the position"
    assert _delivery_count(engine, foreign, late.consumer_id) == 0, "another type"
    assert _delivery_count(engine, second, late.consumer_id) == 1
    assert _delivery_count(engine, third, late.consumer_id) == 1


def _push_due_out(cluster: ClusterSession, workspace_id: UUID) -> datetime:
    """Set the due index an hour out, as a visit that found nothing leaves it."""
    later = datetime.now(UTC) + timedelta(hours=1)
    with cluster.backend.control_engine.begin() as conn:
        statement = pg_insert(workspace_work_due).values(
            workspace_id=workspace_id, due_at=later, updated_at=datetime.now(UTC)
        )
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[workspace_work_due.c.workspace_id],
                set_={"due_at": later},
            )
        )
    due = _due_at(cluster, workspace_id)
    assert due is not None
    return due


@pytest.mark.parametrize("act", [WORK_SKIP, WORK_REPLAY])
def test_skip_and_replay_mark_the_workspace_due_and_a_refusal_marks_nothing(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    act: str,
) -> None:
    now = datetime.now(UTC)
    consumers = _registry(subscription())
    head = _publish(ctx, consumers, at=now)
    _visit(workspace, cluster, consumers, _soon())
    payload: dict[str, object] = (
        {**_ref(head), "note": "let it go"}
        if act == WORK_SKIP
        else {"consumer_id": CONSUMER_ID, "from_position": 1}
    )

    pushed = _push_due_out(cluster, workspace)
    refused = dispatch(
        ctx, act, {**payload, "consumer_id": "harness.nobody"}, consumers=consumers
    )
    assert not refused.ok
    assert _due_at(cluster, workspace) == pushed, "a refused call writes no mark"

    if act == WORK_SKIP:
        second = _publish(ctx, consumers, at=now)
        _fail_head(engine, second, at=_soon(), error="stuck")
        payload = {**_ref(second), "note": "let it go"}
    else:
        payload = {
            "consumer_id": CONSUMER_ID,
            "from_position": int(_delivery(engine, head).position),
        }
    pushed = _push_due_out(cluster, workspace)
    assert dispatch(ctx, act, payload, consumers=consumers).ok
    due = _due_at(cluster, workspace)
    assert due is not None and due < pushed and due <= datetime.now(UTC), due


# --- cold-review blocker: a publish racing an open replay -----------------------------


def _waiting_on_locks(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).scalar_one()
        )


def _until(predicate: Callable[[], bool], *, seconds: float = 15.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.05)


def test_a_publish_and_a_lease_racing_an_open_replay_cannot_break_subject_order(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cold review's reproduction, as a test.

    The replay is held open after its writes and before its commit, by a patched
    ``replay_deliveries``. From other threads a later event of the same subject is
    published and a worker tries to lease. With the table lock both wait for the
    replay's commit; once it lands, the earlier (replayed) event is the one leased,
    the later one waits behind it, and a second worker leases nothing. Without the
    lock the publish commits under the open replay, the worker still sees the earlier
    row ``delivered`` and leases the later one, and after the commit a second worker
    leases the earlier one: two in flight on one subject, out of order.
    """
    consumers = _registry(subscription())
    earlier = _publish(ctx, consumers, at=datetime.now(UTC))
    _visit(workspace, cluster, consumers, _soon())
    assert _delivery(engine, earlier).state == DELIVERED

    written = threading.Event()
    release = threading.Event()
    original = work_operations.replay_deliveries

    def held_open(conn: Any, **kwargs: Any) -> Any:
        counts = original(conn, **kwargs)
        written.set()
        assert release.wait(30), "the test never released the replay"
        return counts

    monkeypatch.setattr(work_operations, "replay_deliveries", held_open)
    outcomes: dict[str, Any] = {}

    def run_replay() -> None:
        outcomes["replay"] = dispatch(
            ctx,
            WORK_REPLAY,
            {
                "consumer_id": CONSUMER_ID,
                "from_position": int(_delivery(engine, earlier).position),
            },
            consumers=consumers,
        )

    def run_publish() -> None:
        outcomes["later"] = _publish(ctx, consumers, at=datetime.now(UTC))

    def run_lease() -> None:
        with engine.begin() as conn:
            outcomes["first_lease"] = lease_delivery(
                conn, owner=OWNER, now=_soon(), lease_seconds=LEASE
            )

    replay = threading.Thread(target=run_replay)
    replay.start()
    try:
        assert written.wait(15), "the replay never reached its writes"
        baseline = _waiting_on_locks(engine)
        publisher = threading.Thread(target=run_publish)
        publisher.start()
        _until(lambda: not publisher.is_alive() or _waiting_on_locks(engine) > baseline)
        leaser = threading.Thread(target=run_lease)
        leaser.start()
        _until(
            lambda: not leaser.is_alive() or _waiting_on_locks(engine) > baseline + 1
        )
    finally:
        release.set()
        replay.join(30)
    publisher.join(30)
    leaser.join(30)
    assert outcomes["replay"].ok, outcomes["replay"]

    later = outcomes["later"]
    first_lease = outcomes["first_lease"]
    with engine.begin() as conn:
        second_lease = lease_delivery(
            conn, owner="worker-two", now=_soon(), lease_seconds=LEASE
        )
    leased = {
        lease.event_id for lease in (first_lease, second_lease) if lease is not None
    }
    assert later not in leased, "the later event was leased ahead of the earlier one"
    assert leased == {earlier}, leased
    assert _delivery(engine, later).state == PENDING
