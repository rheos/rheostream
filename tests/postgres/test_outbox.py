"""AC 1, AC 2, AC 3 and the repository half of AC 4, AC 5 and AC 9: the outbox write,
its fan-out, and the ordered delivery lease, against a real Postgres.

Seams: :func:`rheo_core.events.publish.publish` (the outbox write and its fan-out) and
:func:`rheo_core.events.deliveries.lease_delivery` (the ordered, blocking lease), plus
the repository writes an attempt ends with. Every assertion reads the rows back in a
fresh transaction rather than trusting a return value, because the defects this file
exists to catch — a fan-out that ignored the enabled-module set, a lease that took a
row it should have left alone — show up in the rows and not in the call.

**Time is chosen, never slept.** ``now`` is a parameter of every call under test, so a
lease expires because a later ``now`` is passed and not because the suite waited sixty
seconds. That is AC 1's demonstration and not merely a convenience.

**The context is built, never hand-made.** ``WorkspaceContext`` is constructible only
inside ``rheo_core.boundary`` (``tests/test_boundary.py``'s AST scan), so the
enabled-module cases are driven by enabling a real ``core.module_state`` row and
building an operator context over that workspace — which is also the only honest way
to prove the fan-out reads the set a real boundary produced.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import enable_harness_module
from rheo_contracts import EventEnvelope, WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import (
    ConsumerRegistry,
    ConsumerSubscription,
    NewEvent,
    already_processed,
    fail_delivery,
    lease_delivery,
    mark_delivered,
    publish,
    record_processed,
    requeue_delivery,
    skip_delivery,
)
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_tables import (
    consumer_processed,
    event_delivery,
    outbox_event,
)
from sqlalchemy import Engine, Row, func, select
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

WRITTEN = "harness.note.written"
ARCHIVED = "harness.note.archived"
SUBJECT = "harness.note:one"
OTHER_SUBJECT = "harness.note:two"
CORE_CONSUMER = "core.recorder"
HARNESS_CONSUMER = "harness.recorder"
PAYLOAD: dict[str, object] = {"body": "one", "count": 2}
OWNER_A = "worker-a"
OWNER_B = "worker-b"
LEASE = 60


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """The workspace database this file's events live in."""
    return cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from, anchored to the wall clock."""
    return datetime.now(UTC)


def _context(workspace: UUID) -> WorkspaceContext:
    built = context_for_operator(workspace)
    assert isinstance(built, WorkspaceContext), built
    return built


@pytest.fixture
def ctx(workspace: UUID) -> WorkspaceContext:
    """An operator context over a workspace with **no** module enabled."""
    built = _context(workspace)
    assert built.enabled_modules == frozenset()
    return built


@pytest.fixture
def ctx_with_harness(engine: Engine, workspace: UUID) -> WorkspaceContext:
    """The same, over a workspace where ``harness`` is enabled."""
    with engine.begin() as conn:
        enable_harness_module(conn)
    built = _context(workspace)
    assert built.enabled_modules == frozenset({"harness"})
    return built


def _noop(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """No delivery happens in this file; the handler is never called."""


def _subscription(
    consumer_id: str, module_id: str, *, event_type: str = WRITTEN
) -> ConsumerSubscription:
    return ConsumerSubscription(
        consumer_id=consumer_id,
        event_type=event_type,
        module_id=module_id,
        replay_safe=True,
        handler=_noop,
    )


def _registry(*subscriptions: ConsumerSubscription) -> ConsumerRegistry:
    registry = ConsumerRegistry()
    for subscription in subscriptions:
        registry.register(subscription)
    return registry


def _event(*, subject_ref: str = SUBJECT, revision: int = 1) -> NewEvent:
    return NewEvent(
        type=WRITTEN,
        schema_version=1,
        subject_ref=subject_ref,
        subject_revision=revision,
        data=PAYLOAD,
    )


def _publish(
    ctx: WorkspaceContext,
    registry: ConsumerRegistry,
    *,
    now: datetime,
    event: NewEvent | None = None,
) -> UUID:
    """One published event, committed, through the real unit of work."""
    with open_unit_of_work(ctx) as uow:
        event_id = publish(
            ctx, uow, _event() if event is None else event, now=now, consumers=registry
        )
        uow.commit()
    return event_id


def _events(engine: Engine) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(select(outbox_event).order_by(outbox_event.c.position))
        )


def _deliveries(engine: Engine) -> list[Row[Any]]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(event_delivery).order_by(
                    event_delivery.c.consumer_id, event_delivery.c.position
                )
            )
        )


# --- AC 1 and AC 2: the write and the fan-out -----------------------------------------


def test_publish_writes_one_outbox_row_and_one_delivery_per_enabled_consumer(
    engine: Engine, ctx_with_harness: WorkspaceContext, now: datetime
) -> None:
    """AC 2: one row per subscribed, enabled consumer, and no row for anyone else.

    Three subscriptions, two of which must produce a row: the third is subscribed to
    another event type, so a fan-out that fanned to every registered consumer rather
    than to ``for_type``'s answer would be caught here too.
    """
    registry = _registry(
        _subscription(CORE_CONSUMER, "core"),
        _subscription(HARNESS_CONSUMER, "harness"),
        _subscription("harness.archivist", "harness", event_type=ARCHIVED),
    )

    event_id = _publish(ctx_with_harness, registry, now=now)

    events = _events(engine)
    assert len(events) == 1
    event = events[0]
    assert event.id == event_id
    assert event.type == WRITTEN
    assert event.schema_version == 1
    assert event.source == "urn:rheo:module:harness"
    assert event.subject_ref == SUBJECT
    assert event.subject_revision == 1
    assert event.correlation_id == ctx_with_harness.request_id
    assert event.causation_id is None
    assert event.actor_kind == "operator"
    assert event.actor_id is None
    assert event.occurred_at == now
    assert event.data == PAYLOAD

    deliveries = _deliveries(engine)
    assert [row.consumer_id for row in deliveries] == [CORE_CONSUMER, HARNESS_CONSUMER]
    for row in deliveries:
        assert row.event_id == event_id
        assert row.state == "pending"
        assert row.attempts == 0
        assert row.next_attempt_at == now
        assert row.subject_ref == SUBJECT
        assert row.position == event.position
        assert row.lease_owner is None and row.lease_until is None
        assert row.last_error is None and row.completed_at is None


def test_a_consumer_whose_module_is_not_enabled_gets_no_delivery_row(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """AC 2's disabled-module case, and M11a''s target.

    The same two subscriptions as above, over a workspace where ``harness`` is not
    enabled. The core segment is always enabled and the other is not, so a fan-out
    that ignored ``ctx.enabled_modules`` would write two rows here instead of one.
    """
    registry = _registry(
        _subscription(CORE_CONSUMER, "core"),
        _subscription(HARNESS_CONSUMER, "harness"),
    )

    _publish(ctx, registry, now=now)

    assert len(_events(engine)) == 1
    assert [row.consumer_id for row in _deliveries(engine)] == [CORE_CONSUMER]


def test_an_event_nobody_consumes_still_writes_its_outbox_row(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """Zero enabled, subscribed consumers is a complete publish, not an error.

    The outbox is the record of what happened, and what happened does not depend on
    who was listening.
    """
    event_id = _publish(ctx, _registry(), now=now)

    events = _events(engine)
    assert [row.id for row in events] == [event_id]
    assert _deliveries(engine) == []


def test_a_rolled_back_publish_leaves_neither_the_event_nor_its_deliveries(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """The property exactly-once delivery rests on: both writes are the caller's.

    A rollback after ``publish`` leaves nothing behind, and the committed publish that
    follows proves the rolled-back case is not passing vacuously against a function
    that never wrote anything.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))

    with open_unit_of_work(ctx) as uow:
        publish(ctx, uow, _event(), now=now, consumers=registry)
        uow.rollback()

    assert _events(engine) == []
    assert _deliveries(engine) == []

    event_id = _publish(ctx, registry, now=now)

    assert [row.id for row in _events(engine)] == [event_id]
    assert [row.consumer_id for row in _deliveries(engine)] == [CORE_CONSUMER]


# --- AC 4 (repository half): the ordered, blocking lease ------------------------------


def test_the_lease_is_ordered_and_blocks_behind_an_unsettled_earlier_delivery(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """AC 4, repository half, and M11d's target.

    Three events on one subject, one consumer. The head is leased, and nothing behind
    it may be taken while it is in any state but ``delivered`` or ``skipped`` — which
    is asserted for **both** of the states that do not release it (``leased``, then
    ``failed``) and for both that do. A delivery for a different subject is leasable
    throughout: across subjects there is no ordering promise.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))
    for revision in (1, 2, 3):
        _publish(ctx, registry, now=now, event=_event(revision=revision))
    positions = [row.position for row in _deliveries(engine)]
    assert positions == sorted(positions) and len(positions) == 3

    with engine.begin() as conn:
        head = lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    assert head is not None and head.position == positions[0]
    assert head.attempts == 1

    with engine.begin() as conn:
        assert (
            lease_delivery(conn, owner=OWNER_B, now=now, lease_seconds=LEASE) is None
        ), "a later delivery for the same subject must not be taken behind a leased one"

    with engine.begin() as conn:
        assert (
            mark_delivered(
                conn,
                event_id=head.event_id,
                consumer_id=head.consumer_id,
                owner=OWNER_A,
                now=now,
            )
            is True
        )
        second = lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    assert second is not None and second.position == positions[1]

    with engine.begin() as conn:
        assert (
            fail_delivery(
                conn,
                event_id=second.event_id,
                consumer_id=second.consumer_id,
                owner=OWNER_A,
                now=now,
                error="handler raised",
            )
            is True
        )
    with engine.begin() as conn:
        assert (
            lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE) is None
        ), "a failed head blocks its queue until a person acts"

    _publish(
        ctx, registry, now=now, event=_event(subject_ref=OTHER_SUBJECT, revision=1)
    )
    with engine.begin() as conn:
        elsewhere = lease_delivery(conn, owner=OWNER_B, now=now, lease_seconds=LEASE)
    assert elsewhere is not None and elsewhere.subject_ref == OTHER_SUBJECT

    with engine.begin() as conn:
        assert (
            skip_delivery(
                conn,
                event_id=second.event_id,
                consumer_id=second.consumer_id,
                now=now,
            )
            is True
        )
        third = lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    assert third is not None and third.position == positions[2]


# --- AC 5 (repository half): the expired lease ----------------------------------------


def test_an_expired_delivery_lease_is_reacquirable_and_counts_the_attempt(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """The crash-recovery arm: a lease nobody released is taken by the next worker.

    No release path is called, standing in for a killed worker. The attempt counts,
    because the retry schedule is indexed by the attempt being made.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))
    event_id = _publish(ctx, registry, now=now)

    with engine.begin() as conn:
        first = lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    assert first is not None and first.attempts == 1

    lapsed = now + timedelta(seconds=LEASE + 1)
    with engine.begin() as conn:
        again = lease_delivery(conn, owner=OWNER_B, now=lapsed, lease_seconds=LEASE)
    assert again is not None and again.event_id == event_id
    assert again.attempts == 2

    row = _deliveries(engine)[0]
    assert row.lease_owner == OWNER_B
    assert row.lease_until == lapsed + timedelta(seconds=LEASE)


# --- the ownership half of the three writes that end an attempt -----------------------


def test_a_worker_whose_lease_was_stolen_cannot_end_the_delivery(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """All three finish writes refuse a worker that no longer holds the lease.

    The steal is a second ``lease_delivery`` after the clock passes ``lease_until``, so
    the row is left **``leased`` under owner B**. A steal that ended the row terminally
    would let the surviving ``state = 'leased'`` predicate reject the loser on its own,
    and an ownership defect would go unseen.

    ``fail_delivery`` is the one that makes this a correctness bug rather than a
    transient window: ``failed`` is not in the lease's candidate set and nothing in the
    module moves a row out of it, so a lapsed owner's failure write would strand this
    ``(consumer_id, subject_ref)`` queue behind a head that was processed exactly once,
    with no remedy shipped this run. The other two are asserted beside it because one
    predicate serves all three and a later edit could drop it from any of them.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))
    event_id = _publish(ctx, registry, now=now)

    with engine.begin() as conn:
        assert lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    stolen_at = now + timedelta(seconds=LEASE + 1)
    with engine.begin() as conn:
        assert lease_delivery(conn, owner=OWNER_B, now=stolen_at, lease_seconds=LEASE)

    zombie_at = stolen_at + timedelta(seconds=1)
    with engine.begin() as conn:
        assert (
            mark_delivered(
                conn,
                event_id=event_id,
                consumer_id=CORE_CONSUMER,
                owner=OWNER_A,
                now=zombie_at,
            )
            is False
        )
        assert (
            fail_delivery(
                conn,
                event_id=event_id,
                consumer_id=CORE_CONSUMER,
                owner=OWNER_A,
                now=zombie_at,
                error="zombie",
            )
            is False
        )
        assert (
            requeue_delivery(
                conn,
                event_id=event_id,
                consumer_id=CORE_CONSUMER,
                owner=OWNER_A,
                now=zombie_at,
                error="zombie",
                jitter=None,
            )
            is False
        )

    row = _deliveries(engine)[0]
    assert row.state == "leased"
    assert row.lease_owner == OWNER_B
    assert row.lease_until == stolen_at + timedelta(seconds=LEASE)
    assert row.attempts == 2
    assert row.last_error is None
    assert row.completed_at is None


def test_skip_delivery_touches_nothing_that_is_not_already_failed(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """The ``failed`` predicate, isolated — the reason this write is not unconditional.

    An unconditional ``UPDATE ... SET state = 'skipped'`` passes every other test in
    this file, because nothing else asks it about a row it should leave alone. Both
    states a worker can be mid-flight on are asserted: ``pending`` (due, about to be
    leased) and ``leased`` (running now). The positive case closes the test so a
    mutation that simply made the function always return ``False`` cannot pass either.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))
    event_id = _publish(ctx, registry, now=now)

    with engine.begin() as conn:
        assert (
            skip_delivery(conn, event_id=event_id, consumer_id=CORE_CONSUMER, now=now)
            is False
        )
    pending = _deliveries(engine)[0]
    assert pending.state == "pending"
    assert pending.next_attempt_at == now
    assert pending.completed_at is None

    with engine.begin() as conn:
        assert lease_delivery(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)
    with engine.begin() as conn:
        assert (
            skip_delivery(conn, event_id=event_id, consumer_id=CORE_CONSUMER, now=now)
            is False
        )
    leased = _deliveries(engine)[0]
    assert leased.state == "leased"
    assert leased.lease_owner == OWNER_A
    assert leased.completed_at is None

    with engine.begin() as conn:
        assert (
            fail_delivery(
                conn,
                event_id=event_id,
                consumer_id=CORE_CONSUMER,
                owner=OWNER_A,
                now=now,
                error="handler raised",
            )
            is True
        )
        assert (
            skip_delivery(conn, event_id=event_id, consumer_id=CORE_CONSUMER, now=now)
            is True
        )
    assert _deliveries(engine)[0].state == "skipped"


# --- AC 1: the chosen instant ---------------------------------------------------------


def test_the_publishers_instant_is_the_one_that_lands_in_both_tables(
    engine: Engine, ctx_with_harness: WorkspaceContext
) -> None:
    """AC 1's demonstration: a controlled ``now``, and no sleep anywhere.

    The instant is deliberately far from the wall clock, so a function that read the
    process clock instead could not pass by coincidence.
    """
    chosen = datetime(2019, 7, 4, 12, 30, 15, tzinfo=UTC)
    registry = _registry(
        _subscription(CORE_CONSUMER, "core"),
        _subscription(HARNESS_CONSUMER, "harness"),
    )

    _publish(ctx_with_harness, registry, now=chosen)

    assert [row.occurred_at for row in _events(engine)] == [chosen]
    deliveries = _deliveries(engine)
    assert len(deliveries) == 2
    assert [row.next_attempt_at for row in deliveries] == [chosen, chosen]


# --- criterion 11's backstop ----------------------------------------------------------


def test_a_second_record_processed_for_the_same_pair_raises(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """The dedup ledger's primary key is a real refusal, not merely documented.

    This is what stops ``record_processed`` from being written ``ON CONFLICT DO
    NOTHING``: that spelling leaves every correct-path test green while the backstop
    exactly-once delivery depends on is gone. The second insert must raise, and the
    ledger must still hold exactly one row for the pair.
    """
    event_id = _publish(ctx, _registry(_subscription(CORE_CONSUMER, "core")), now=now)

    with engine.connect() as conn:
        assert (
            already_processed(conn, consumer_id=CORE_CONSUMER, event_id=event_id)
            is False
        )

    with engine.begin() as conn:
        record_processed(conn, consumer_id=CORE_CONSUMER, event_id=event_id, now=now)

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            record_processed(
                conn,
                consumer_id=CORE_CONSUMER,
                event_id=event_id,
                now=now + timedelta(seconds=1),
            )

    with engine.connect() as conn:
        assert (
            already_processed(conn, consumer_id=CORE_CONSUMER, event_id=event_id)
            is True
        )
        rows = conn.execute(
            select(func.count())
            .select_from(consumer_processed)
            .where(
                consumer_processed.c.consumer_id == CORE_CONSUMER,
                consumer_processed.c.event_id == event_id,
            )
        ).scalar_one()
    assert rows == 1


# --- AC 9 (repository half): the retry schedule ---------------------------------------


def test_requeue_delivery_walks_the_backoff_schedule(
    engine: Engine, ctx: WorkspaceContext, now: datetime
) -> None:
    """AC 9, repository half, and M11e's target.

    Three consecutive failures of the same delivery, each driven with ``jitter=None``
    so the values are exact: the schedule's first three steps are 5 s, 20 s and 80 s,
    and all three are reachable because a delivery's budget is ``work.max_attempts``
    (default 8) rather than anything this repository enforces.

    **All three steps are asserted, not only the first.** A single-step assertion
    survives replacing the schedule call with a literal five-second delay, which is
    precisely the substitution this test exists to refuse.
    """
    registry = _registry(_subscription(CORE_CONSUMER, "core"))
    _publish(ctx, registry, now=now)

    landed: list[datetime] = []
    attempted: list[int] = []
    at = now
    for step in (5, 20, 80):
        with engine.begin() as conn:
            leased = lease_delivery(conn, owner=OWNER_A, now=at, lease_seconds=LEASE)
            assert leased is not None
            attempted.append(leased.attempts)
            assert (
                requeue_delivery(
                    conn,
                    event_id=leased.event_id,
                    consumer_id=leased.consumer_id,
                    owner=OWNER_A,
                    now=at,
                    error=f"attempt {leased.attempts}",
                    jitter=None,
                )
                is True
            )
        row = _deliveries(engine)[0]
        assert row.state == "pending"
        assert row.lease_owner is None and row.lease_until is None
        assert row.last_error == f"attempt {leased.attempts}"
        landed.append(row.next_attempt_at)
        assert row.next_attempt_at == at + timedelta(seconds=step)
        at = row.next_attempt_at

    assert attempted == [1, 2, 3]
    assert landed == [
        now + timedelta(seconds=5),
        now + timedelta(seconds=25),
        now + timedelta(seconds=105),
    ]
