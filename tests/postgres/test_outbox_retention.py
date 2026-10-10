"""Issue #335: the outbox retention sweep and the replay gap it opens.

Seams: ``core.retention_sweep``'s handler (``run_retention_sweep``) for what the daily
sweep deletes and keeps, ``rheo_core.events.retention.purge_outbox`` directly for its
batch bounds and its cancellation, and ``core.work.replay`` through ``dispatch`` for the
gap gate, exactly as the CLI reaches it.

**Setup writes delivery states directly.** Every event is published through the real
``publish`` and delivered by the real worker drain, so its ledger row is the real one.
A test that needs a ``failed``, ``pending``, ``leased`` or ``skipped`` row then sets
that state on the row, because what is under test is what the sweep does with each
state, not how a delivery reaches it (``test_work_recovery.py`` covers that).

**Time.** Events are published with an explicit instant, which is what
``occurred_at`` records, so "older than the window" is a chosen past instant rather
than a wait.
"""

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.consumers import ensure_consumer_tables, subscription
from harness.evidence import handler_uow
from harness.registry import enable_harness_module
from rheo_contracts import WorkspaceContext
from rheo_core.boundary import context_for_operator
from rheo_core.events import (
    FAILED,
    LEASED,
    PENDING,
    SKIPPED,
    ConsumerRegistry,
    NewEvent,
    publish,
)
from rheo_core.events.retention import (
    OutboxPurge,
    purge_outbox,
    swept_through_position,
)
from rheo_core.operations import WORK_REPLAY, dispatch, register_core_operations
from rheo_core.settings.schema import ValueType
from rheo_core.storage.backend import UnitOfWork
from rheo_core.storage.outbox_retention_tables import outbox_retention
from rheo_core.storage.repositories import upsert_workspace_setting
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.storage.work_tables import (
    consumer_processed,
    event_delivery,
    outbox_event,
)
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import visit_workspace
from rheo_core.work.operations import REPLAY_GAP, ReplayResult
from rheo_core.work.schedules import RetentionSweepPayload, run_retention_sweep
from sqlalchemy import Engine, insert, select, text, update
from sqlalchemy.exc import IntegrityError

pytestmark = pytest.mark.postgres

OWNER = "outbox-retention-test"
OLD = timedelta(days=31)
RECENT = timedelta(days=1)


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    built = cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )
    with built.begin() as conn:
        enable_harness_module(conn)
        ensure_consumer_tables(conn)
    return built


@pytest.fixture
def ctx(workspace: UUID, engine: Engine) -> WorkspaceContext:
    built = context_for_operator(workspace)
    assert isinstance(built, WorkspaceContext), built
    return built


@pytest.fixture
def consumers() -> ConsumerRegistry:
    built = ConsumerRegistry()
    built.register(subscription())
    return built


class _NeverCancelled:
    def checkpoint(self) -> None:
        return None


def _publish(
    ctx: WorkspaceContext, consumers: ConsumerRegistry, *, at: datetime, subject: str
) -> UUID:
    with open_unit_of_work(ctx) as uow:
        event_id = publish(
            ctx,
            uow,
            NewEvent(
                type="harness.note.written",
                schema_version=1,
                subject_ref=subject,
                subject_revision=1,
                data={"body": "a synthetic note"},
            ),
            now=at,
            consumers=consumers,
        )
        uow.commit()
    return event_id


def _deliver_all(
    cluster: ClusterSession, workspace: UUID, consumers: ConsumerRegistry
) -> None:
    visit_workspace(
        DueWorkspace(workspace_id=workspace, observed_due_at=None),
        kinds=JobKindRegistry(),
        consumers=consumers,
        backend=cluster.backend,
        owner=OWNER,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
        jitter=None,
    )


def _set_state(engine: Engine, event_id: UUID, state: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(event_delivery)
            .where(event_delivery.c.event_id == event_id)
            .values(state=state)
        )


def _position(engine: Engine, event_id: UUID) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(outbox_event.c.position).where(outbox_event.c.id == event_id)
            ).scalar_one()
        )


def _present(engine: Engine) -> tuple[set[UUID], set[UUID], set[UUID]]:
    """Event ids in the outbox, with a delivery, and with a ledger row."""
    with engine.connect() as conn:
        events = set(conn.execute(select(outbox_event.c.id)).scalars())
        deliveries = set(conn.execute(select(event_delivery.c.event_id)).scalars())
        ledger = set(conn.execute(select(consumer_processed.c.event_id)).scalars())
    return events, deliveries, ledger


def _recorded(engine: Engine) -> tuple[int | None, datetime | None]:
    with engine.connect() as conn:
        row = conn.execute(select(outbox_retention)).one()
    return row.swept_through_position, row.swept_at


def _sweep(engine: Engine, workspace: UUID) -> None:
    """One real ``run_retention_sweep`` call, committed. Bounded so a lock wait reds
    the test instead of hanging it."""
    database_name = engine.url.database
    assert database_name is not None
    with UnitOfWork(engine, database_name) as uow:
        uow.connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        run_retention_sweep(
            handler_uow(uow, None),
            RetentionSweepPayload(workspace_id=workspace),
            _NeverCancelled(),  # type: ignore[arg-type]
        )
        uow.commit()


def _purge(
    engine: Engine,
    *,
    checkpoint: Callable[[], None] = lambda: None,
    batch_events: int = 1000,
    max_batches: int = 20,
) -> OutboxPurge:
    database_name = engine.url.database
    assert database_name is not None
    now = datetime.now(UTC)
    with UnitOfWork(engine, database_name) as uow:
        result = purge_outbox(
            uow.connection,
            now=now,
            occurred_before=now - timedelta(days=30),
            checkpoint=checkpoint,
            batch_events=batch_events,
            max_batches=max_batches,
        )
        uow.commit()
    return result


def _replay(
    ctx: WorkspaceContext, consumers: ConsumerRegistry, from_position: int
) -> Any:
    return dispatch(
        ctx,
        WORK_REPLAY,
        {"consumer_id": "harness.recorder", "from_position": from_position},
        consumers=consumers,
    )


# --- the migration's one row ----------------------------------------------------------


def test_the_record_starts_as_one_null_row_and_a_second_row_is_refused(
    engine: Engine,
) -> None:
    assert _recorded(engine) == (None, None)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(insert(outbox_retention).values(id=2))


# --- what the sweep deletes and what it keeps -----------------------------------------


def test_the_sweep_deletes_old_settled_events_and_keeps_open_and_recent_ones(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """Old ``delivered``, old ``skipped`` and an old event nobody consumed go, with
    their deliveries and ledger rows. Old ``failed``, ``pending`` and ``leased`` stay,
    their deliveries and ledger rows with them, and so does a recent ``delivered``
    one. The record names the highest position deleted."""
    old = datetime.now(UTC) - OLD
    delivered = _publish(ctx, consumers, at=old, subject="harness.note:a")
    skipped = _publish(ctx, consumers, at=old, subject="harness.note:b")
    unconsumed = _publish(ctx, ConsumerRegistry(), at=old, subject="harness.note:c")
    failed = _publish(ctx, consumers, at=old, subject="harness.note:d")
    pending = _publish(ctx, consumers, at=old, subject="harness.note:e")
    leased = _publish(ctx, consumers, at=old, subject="harness.note:f")
    recent = _publish(
        ctx, consumers, at=datetime.now(UTC) - RECENT, subject="harness.note:g"
    )
    _deliver_all(cluster, workspace, consumers)
    _set_state(engine, skipped, SKIPPED)
    _set_state(engine, failed, FAILED)
    _set_state(engine, pending, PENDING)
    _set_state(engine, leased, LEASED)
    # The highest deleted is the unconsumed event, the third published.
    highest_gone = _position(engine, unconsumed)

    _sweep(engine, workspace)

    events, deliveries, ledger = _present(engine)
    gone = {delivered, skipped, unconsumed}
    kept = {failed, pending, leased, recent}
    assert events == kept
    assert deliveries == kept
    assert ledger == kept
    assert gone.isdisjoint(events)
    swept, swept_at = _recorded(engine)
    assert swept == highest_gone and swept_at is not None


def test_the_window_is_the_workspace_setting(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """A workspace that shortens ``work.outbox_retention_days`` to one day sees a
    two-day-old settled event go that the default 30 days would keep."""
    two_days = _publish(
        ctx,
        consumers,
        at=datetime.now(UTC) - timedelta(days=2),
        subject="harness.note:a",
    )
    _deliver_all(cluster, workspace, consumers)

    _sweep(engine, workspace)
    assert two_days in _present(engine)[0]

    database_name = engine.url.database
    assert database_name is not None
    with UnitOfWork(engine, database_name) as uow:
        upsert_workspace_setting(
            uow.connection,
            key="work.outbox_retention_days",
            value="1",
            value_type=ValueType.INT,
            updated_by=None,
        )
        uow.commit()
    _sweep(engine, workspace)
    assert two_days not in _present(engine)[0]


def test_the_record_only_moves_forward(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """A later sweep that deletes an event below the recorded position, because its
    stuck delivery was finally skipped, does not pull the record back down."""
    old = datetime.now(UTC) - OLD
    stuck = _publish(ctx, consumers, at=old, subject="harness.note:a")
    later = _publish(ctx, consumers, at=old, subject="harness.note:b")
    _deliver_all(cluster, workspace, consumers)
    _set_state(engine, stuck, FAILED)
    high = _position(engine, later)

    assert _purge(engine).swept_through == high
    _set_state(engine, stuck, SKIPPED)
    result = _purge(engine)
    assert result.events == 1 and result.swept_through == high
    assert _recorded(engine)[0] == high


# --- the bounds -----------------------------------------------------------------------


def test_one_sweep_deletes_at_most_its_batches_oldest_first(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    old = datetime.now(UTC) - OLD
    events = [
        _publish(ctx, consumers, at=old, subject=f"harness.note:{n}") for n in range(5)
    ]
    _deliver_all(cluster, workspace, consumers)
    positions = [_position(engine, e) for e in events]

    first = _purge(engine, batch_events=2, max_batches=2)
    assert (first.events, first.deliveries, first.ledger_rows) == (4, 4, 4)
    assert first.swept_through == positions[3]
    assert _present(engine)[0] == {events[4]}

    second = _purge(engine, batch_events=2, max_batches=2)
    assert second.events == 1 and second.swept_through == positions[4]
    assert _present(engine) == (set(), set(), set())


def test_a_cancelled_sweep_deletes_nothing(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """Cancellation fires at the second batch's checkpoint; the first batch's deletes
    and the record roll back with the transaction."""
    old = datetime.now(UTC) - OLD
    events = {
        _publish(ctx, consumers, at=old, subject=f"harness.note:{n}") for n in range(3)
    }
    _deliver_all(cluster, workspace, consumers)
    calls: list[int] = []

    def checkpoint() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        _purge(engine, checkpoint=checkpoint, batch_events=1, max_batches=5)
    assert _present(engine)[0] == events
    assert _recorded(engine) == (None, None)


# --- the replay gap -------------------------------------------------------------------


def test_replay_refuses_a_position_inside_a_gap_a_stuck_event_hides(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """The gap shape ``intake-and-events.md`` § Retention names. The oldest event's
    delivery is stuck ``failed``, so the sweep keeps it and deletes the three settled
    events above it. The smallest position still present is the stuck one, so a gate
    on ``min(position)`` would let a replay start at the first deleted position and
    rebuild from a hole. Gated on the recorded position, it is refused, naming the
    first position after the deleted ones, and from there it runs."""
    old = datetime.now(UTC) - OLD
    stuck = _publish(ctx, consumers, at=old, subject="harness.note:a")
    swept = [
        _publish(ctx, consumers, at=old, subject=f"harness.note:{n}") for n in "bcd"
    ]
    survivor = _publish(
        ctx, consumers, at=datetime.now(UTC) - RECENT, subject="harness.note:e"
    )
    _deliver_all(cluster, workspace, consumers)
    _set_state(engine, stuck, FAILED)
    stuck_at = _position(engine, stuck)
    swept_at = [_position(engine, e) for e in swept]
    survivor_at = _position(engine, survivor)

    _sweep(engine, workspace)
    assert _present(engine)[0] == {stuck, survivor}
    assert _recorded(engine)[0] == swept_at[-1]

    for inside in (stuck_at, swept_at[0], swept_at[-1]):
        outcome = _replay(ctx, consumers, inside)
        assert outcome.state == REPLAY_GAP, (inside, outcome)
        assert outcome.error is not None
        assert f"can start from is {swept_at[-1] + 1}" in outcome.error.error_text
    with engine.connect() as conn:
        assert (
            conn.execute(
                select(event_delivery.c.state).where(event_delivery.c.event_id == stuck)
            ).scalar_one()
            == FAILED
        )

    fine = _replay(ctx, consumers, swept_at[-1] + 1)
    assert fine.ok, fine
    assert isinstance(fine.result, ReplayResult) and fine.result.reset == 1
    assert survivor_at > swept_at[-1]


def test_a_replay_waits_for_an_open_sweep_and_then_sees_its_gap(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """The sweep has deleted but not committed. A replay from a position it is about
    to make a gap of must not pass its check against the old horizon: it waits on the
    record's row, and once the sweep commits it is refused ``replay_gap``."""
    old = datetime.now(UTC) - OLD
    first = _publish(ctx, consumers, at=old, subject="harness.note:a")
    _publish(ctx, consumers, at=datetime.now(UTC) - RECENT, subject="harness.note:b")
    _deliver_all(cluster, workspace, consumers)
    first_at = _position(engine, first)

    database_name = engine.url.database
    assert database_name is not None
    outcomes: dict[str, Any] = {}
    sweep = UnitOfWork(engine, database_name)
    with sweep as uow:
        now = datetime.now(UTC)
        purged = purge_outbox(
            uow.connection,
            now=now,
            occurred_before=now - timedelta(days=30),
            checkpoint=lambda: None,
        )
        assert purged.events == 1

        def run_replay() -> None:
            outcomes["replay"] = _replay(ctx, consumers, first_at)

        replay = threading.Thread(target=run_replay)
        replay.start()
        deadline = time.monotonic() + 15
        while replay.is_alive() and not _waiting(engine):
            assert time.monotonic() < deadline, "the replay never waited"
            time.sleep(0.05)
        assert replay.is_alive(), outcomes.get("replay")
        uow.commit()
    replay.join(30)
    assert outcomes["replay"].state == REPLAY_GAP, outcomes["replay"]


def test_a_sweep_waits_for_an_open_replay(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    ctx: WorkspaceContext,
    consumers: ConsumerRegistry,
) -> None:
    """The other order: a replay holds the record's row ``FOR SHARE`` to its commit,
    so a sweep cannot delete under a replay that has passed its gap check."""
    old = datetime.now(UTC) - OLD
    _publish(ctx, consumers, at=old, subject="harness.note:a")
    _deliver_all(cluster, workspace, consumers)

    database_name = engine.url.database
    assert database_name is not None
    with UnitOfWork(engine, database_name) as replay:
        assert swept_through_position(replay.connection) is None
        with UnitOfWork(engine, database_name) as sweep:
            sweep.connection.execute(text("SET LOCAL lock_timeout = '500ms'"))
            now = datetime.now(UTC)
            with pytest.raises(Exception, match="lock timeout"):
                purge_outbox(
                    sweep.connection,
                    now=now,
                    occurred_before=now - timedelta(days=30),
                    checkpoint=lambda: None,
                )
    assert _purge(engine).events == 1


def _waiting(engine: Engine) -> bool:
    with engine.connect() as conn:
        return bool(
            conn.exec_driver_sql(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ).scalar_one()
        )
