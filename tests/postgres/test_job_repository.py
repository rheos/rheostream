"""AC 2, 3, 4, 6 (repository half), 7, 8, 10, 11 (predicate half), 12 and 17: the job
repository, the retry schedule and the cancellation checkpoint, against a real
Postgres.

Seams: the public functions of ``rheo_core.work.jobs`` — ``enqueue``/``enqueue_job``,
``acquire_lease``, ``extend_lease``, the four finishes, ``request_cancellation``,
``list_failed_jobs`` and ``earliest_due_at`` — plus ``backoff_for``'s bound and
``CancellationToken.checkpoint``'s throttle. Every assertion reads the row back in a
fresh transaction rather than trusting a return value, because the defects this file
exists to catch (a write that applied when it should not have, an attempt counted
against a cancelled job) show up in the row and not in the call.

**Time is chosen, never slept.** Nothing here sleeps: ``now`` is a parameter of every
call under test, so a lease expires because a later ``now`` is passed and not because
the suite waited sixty seconds. The one place a real clock still matters is
``enqueue``'s control-plane mark, which stamps ``updated_at`` from the process clock —
so the ``now`` these tests use is anchored to the wall clock rather than to an
arbitrary instant.

**Two assertions need a real, nonzero clock delta, not a shared ``now``.** Both
checkpoint cases below give their second call a ``now`` strictly later than the first:
``lease_until = :now + :lease_seconds`` computes to the identical value when two calls
share one ``now``, so "unchanged" and "extended" would both hold on correct code and on
a token with no throttle at all.
"""

import random
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from conftest import ClusterSession
from rheo_core.storage.work_index_tables import workspace_work_due
from rheo_core.storage.work_tables import job
from rheo_core.work.backoff import BACKOFF_CAP_SECONDS, backoff_for
from rheo_core.work.cancellation import (
    CancellationToken,
    JobCancelled,
    JobLeaseLost,
)
from rheo_core.work.jobs import (
    acquire_lease,
    earliest_due_at,
    enqueue,
    enqueue_job,
    extend_lease,
    finish_cancelled,
    finish_failed,
    finish_succeeded,
    list_failed_jobs,
    request_cancellation,
    requeue_for_retry,
)
from sqlalchemy import Connection, Engine, Row, select, text

pytestmark = pytest.mark.postgres

KIND = "harness.job"
PAYLOAD: dict[str, object] = {"note": "one", "count": 2}
OWNER_A = "worker-a"
OWNER_B = "worker-b"
LEASE = 60
HEARTBEAT = 20


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """The workspace database this file's jobs live in."""
    return cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from, anchored to the wall clock."""
    return datetime.now(UTC)


def _put(
    engine: Engine,
    *,
    now: datetime,
    max_attempts: int = 3,
    next_run_at: datetime | None = None,
) -> UUID:
    """One queued job, committed, without going through the control plane."""
    with engine.begin() as conn:
        return enqueue_job(
            conn,
            kind=KIND,
            payload=PAYLOAD,
            now=now,
            max_attempts=max_attempts,
            next_run_at=next_run_at,
        )


def _read(engine: Engine, job_id: UUID) -> Row[Any]:
    """The whole row, read in its own transaction."""
    with engine.connect() as conn:
        return conn.execute(select(job).where(job.c.id == job_id)).one()


def _lease(engine: Engine, job_id: UUID, *, owner: str, at: datetime) -> None:
    """Acquire and assert the acquire took the job this test meant."""
    with engine.begin() as conn:
        leased = acquire_lease(conn, owner=owner, now=at, lease_seconds=LEASE)
    assert leased is not None and leased.id == job_id


# --- AC 2 and AC 17: the enqueue path and the settings key's production reader --------


def test_enqueue_writes_a_queued_row_and_marks_the_workspace_due(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 2, both halves, and AC 17's reader.

    ``max_attempts`` is deliberately **not** passed: the ``8`` asserted below can only
    come from ``work.max_attempts``, which makes the key's only production reader the
    thing under test rather than a fixture value handed in.
    """
    job_id = enqueue(workspace, kind=KIND, payload=PAYLOAD, now=now)

    row = _read(engine, job_id)
    assert row.state == "queued"
    assert row.attempts == 0
    assert row.cancel_requested is False
    assert row.next_run_at == now
    assert row.created_at == now
    assert row.input == PAYLOAD
    assert row.max_attempts == 8
    assert row.lease_owner is None and row.lease_until is None

    with cluster.backend.control_engine.connect() as conn:
        due_at = conn.execute(
            select(workspace_work_due.c.due_at).where(
                workspace_work_due.c.workspace_id == workspace
            )
        ).scalar_one()
    assert due_at == now, "the mark must carry the row's own next_run_at"


def test_an_explicit_budget_beats_the_setting(
    workspace: UUID, engine: Engine, now: datetime
) -> None:
    job_id = enqueue(workspace, kind=KIND, payload=PAYLOAD, now=now, max_attempts=2)
    assert _read(engine, job_id).max_attempts == 2


# --- AC 3: the lease query ------------------------------------------------------------


def test_acquire_lease_takes_one_due_job_oldest_first_and_counts_the_attempt(
    engine: Engine, now: datetime
) -> None:
    """AC 3: one job per call, ``ORDER BY next_run_at``, ``attempts`` incremented."""
    oldest = _put(engine, now=now - timedelta(minutes=5))
    newer = _put(engine, now=now - timedelta(minutes=1))

    with engine.begin() as conn:
        leased = acquire_lease(conn, owner=OWNER_A, now=now, lease_seconds=LEASE)

    assert leased is not None
    assert leased.id == oldest
    assert leased.kind == KIND
    assert leased.payload == PAYLOAD
    assert leased.attempts == 1
    assert leased.max_attempts == 3
    assert leased.cancel_requested is False

    row = _read(engine, oldest)
    assert row.state == "leased"
    assert row.lease_owner == OWNER_A
    assert row.lease_until == now + timedelta(seconds=LEASE)
    assert row.attempts == 1
    assert _read(engine, newer).state == "queued", "one job per call, not both"


def test_a_job_that_is_not_due_yet_is_not_acquired(
    engine: Engine, now: datetime
) -> None:
    _put(engine, now=now, next_run_at=now + timedelta(minutes=5))
    with engine.begin() as conn:
        assert acquire_lease(conn, owner=OWNER_A, now=now, lease_seconds=LEASE) is None


def test_an_expired_lease_is_reacquired_with_the_attempt_counted(
    engine: Engine, now: datetime
) -> None:
    """AC 3's increment clause, on the crash-recovery arm.

    Cited to AC 3 rather than AC 5: AC 5 is the loop's, and proves the job runs to
    completion afterwards. What this asserts is the statement's own behaviour — the
    second ``WHERE`` arm matches an expired lease, and re-acquiring counts the attempt.
    No release path is called, standing in for a killed worker.
    """
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    lapsed = now + timedelta(seconds=LEASE + 1)
    with engine.begin() as conn:
        again = acquire_lease(conn, owner=OWNER_B, now=lapsed, lease_seconds=LEASE)

    assert again is not None and again.id == job_id
    assert again.attempts == 2
    row = _read(engine, job_id)
    assert row.lease_owner == OWNER_B
    assert row.lease_until == lapsed + timedelta(seconds=LEASE)


def test_a_live_lease_held_by_another_worker_is_not_stolen(
    engine: Engine, now: datetime
) -> None:
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    with engine.begin() as conn:
        assert (
            acquire_lease(
                conn, owner=OWNER_B, now=now + timedelta(seconds=1), lease_seconds=LEASE
            )
            is None
        )


def test_the_case_spares_an_attempt_on_a_job_flagged_for_cancellation(
    engine: Engine, now: datetime
) -> None:
    """Deviation 2's own scenario, distinct from the plain expired-lease case above.

    A leased job is flagged while its owner is running, the owner then dies and the
    lease lapses. The re-acquire must **not** charge an attempt: this row is about to
    be cancelled rather than retried, and counting the attempt would leave the data
    contradicting the terminal state.
    """
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "leased"
    before = _read(engine, job_id).attempts

    with engine.begin() as conn:
        again = acquire_lease(
            conn,
            owner=OWNER_B,
            now=now + timedelta(seconds=LEASE + 1),
            lease_seconds=LEASE,
        )

    assert again is not None and again.id == job_id
    assert again.cancel_requested is True
    assert again.attempts == before, "a job about to be cancelled is charged no attempt"
    assert _read(engine, job_id).attempts == before


# --- AC 4: two workers, with SKIP LOCKED actually exercised ---------------------------


@pytest.fixture
def contender(engine: Engine) -> Iterator[Connection]:
    """A second connection that fails fast rather than blocking.

    ``SET LOCAL statement_timeout`` is what makes the ``SKIP LOCKED`` mutation fail in
    seconds instead of hanging the suite: without the clause this connection blocks on
    the first worker's open transaction, and a blocked test is indistinguishable from a
    slow one.
    """
    with engine.connect() as conn:
        with conn.begin():
            conn.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            yield conn


def test_two_concurrent_workers_never_lease_the_same_job(
    engine: Engine, contender: Connection, now: datetime
) -> None:
    """AC 4: A's acquire transaction is **held open** while B acquires.

    A sequential acquire-commit-acquire would prove nothing here — under plain
    ``FOR UPDATE`` B blocks, then re-evaluates after A commits and returns ``None``,
    which is the identical observable.
    """
    _put(engine, now=now - timedelta(minutes=2))
    _put(engine, now=now - timedelta(minutes=1))

    with engine.connect() as holder:
        with holder.begin():
            first = acquire_lease(holder, owner=OWNER_A, now=now, lease_seconds=LEASE)
            assert first is not None
            second = acquire_lease(
                contender, owner=OWNER_B, now=now, lease_seconds=LEASE
            )
            assert second is not None, "B must not block behind A's open transaction"
            assert second.id != first.id


def test_a_second_worker_gets_nothing_when_only_one_job_is_due(
    engine: Engine, contender: Connection, now: datetime
) -> None:
    _put(engine, now=now)
    with engine.connect() as holder:
        with holder.begin():
            assert (
                acquire_lease(holder, owner=OWNER_A, now=now, lease_seconds=LEASE)
                is not None
            )
            assert (
                acquire_lease(contender, owner=OWNER_B, now=now, lease_seconds=LEASE)
                is None
            )


# --- AC 6 (repository half): the ownership predicate ----------------------------------


def test_a_worker_whose_lease_was_stolen_cannot_finish_the_job(
    engine: Engine, now: datetime
) -> None:
    """AC 6, repository half.

    The steal is a second ``acquire_lease`` after the clock passes ``lease_until``, so
    the row is left **``leased`` under owner B**. A steal that ended the row terminally
    would let the surviving ``state = 'leased'`` predicate reject the loser on its own,
    and an ownership defect would go unseen.
    """
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    stolen_at = now + timedelta(seconds=LEASE + 1)
    _lease(engine, job_id, owner=OWNER_B, at=stolen_at)

    with engine.begin() as conn:
        applied = finish_succeeded(
            conn, job_id=job_id, owner=OWNER_A, now=stolen_at + timedelta(seconds=1)
        )

    assert applied is False
    row = _read(engine, job_id)
    assert row.state == "leased"
    assert row.lease_owner == OWNER_B
    assert row.finished_at is None


def test_every_finish_refuses_a_worker_that_does_not_hold_the_lease(
    engine: Engine, now: datetime
) -> None:
    """The shared predicate, on all four writes rather than only the one above."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    with engine.begin() as conn:
        assert finish_succeeded(conn, job_id=job_id, owner=OWNER_B, now=now) is False
        assert (
            finish_failed(conn, job_id=job_id, owner=OWNER_B, now=now, error="x")
            is False
        )
        assert finish_cancelled(conn, job_id=job_id, owner=OWNER_B, now=now) is False
        assert (
            requeue_for_retry(
                conn,
                job_id=job_id,
                owner=OWNER_B,
                now=now,
                next_run_at=now + timedelta(seconds=5),
                error="x",
            )
            is False
        )

    row = _read(engine, job_id)
    assert row.state == "leased"
    assert row.lease_owner == OWNER_A


# --- AC 7 and AC 8: the retry budget's two repository writes --------------------------


def test_requeue_for_retry_puts_the_job_back_under_its_backoff(
    engine: Engine, now: datetime
) -> None:
    """AC 7, repository half: requeued with ``last_error`` and the lease released."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    next_run_at = now + backoff_for(1, jitter=None)

    with engine.begin() as conn:
        applied = requeue_for_retry(
            conn,
            job_id=job_id,
            owner=OWNER_A,
            now=now,
            next_run_at=next_run_at,
            error="handler raised",
        )

    assert applied is True
    row = _read(engine, job_id)
    assert row.state == "queued"
    assert row.next_run_at == next_run_at
    assert row.last_error == "handler raised"
    assert row.lease_owner is None and row.lease_until is None
    assert row.finished_at is None
    assert row.attempts == 1, "no failure path increments attempts"


def test_finish_failed_ends_the_job_with_its_error(
    engine: Engine, now: datetime
) -> None:
    """AC 8, repository half: the row is written ``failed``, never deleted."""
    job_id = _put(engine, now=now, max_attempts=1)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    with engine.begin() as conn:
        applied = finish_failed(
            conn, job_id=job_id, owner=OWNER_A, now=now, error="budget spent"
        )

    assert applied is True
    row = _read(engine, job_id)
    assert row.state == "failed"
    assert row.finished_at == now
    assert row.last_error == "budget spent"
    assert row.lease_owner is None and row.lease_until is None


def test_finish_succeeded_clears_the_lease_and_the_last_error(
    engine: Engine, now: datetime
) -> None:
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    with engine.begin() as conn:
        assert (
            requeue_for_retry(
                conn,
                job_id=job_id,
                owner=OWNER_A,
                now=now,
                next_run_at=now,
                error="first try",
            )
            is True
        )
    _lease(engine, job_id, owner=OWNER_A, at=now)

    with engine.begin() as conn:
        assert finish_succeeded(conn, job_id=job_id, owner=OWNER_A, now=now) is True

    row = _read(engine, job_id)
    assert row.state == "succeeded"
    assert row.finished_at == now
    assert row.last_error is None, "a success clears the error a retry left behind"
    assert row.lease_owner is None and row.lease_until is None


# --- the failure list -----------------------------------------------------------------


def test_the_failure_list_is_newest_first_and_honours_its_limit(
    engine: Engine, now: datetime
) -> None:
    def _fail(at: datetime, error: str) -> UUID:
        job_id = _put(engine, now=at, max_attempts=1)
        _lease(engine, job_id, owner=OWNER_A, at=at)
        with engine.begin() as conn:
            assert (
                finish_failed(conn, job_id=job_id, owner=OWNER_A, now=at, error=error)
                is True
            )
        return job_id

    oldest = _fail(now - timedelta(hours=2), "first")
    middle = _fail(now - timedelta(hours=1), "second")
    newest = _fail(now, "third")
    survivor = _put(engine, now=now)

    with engine.connect() as conn:
        rows = list_failed_jobs(conn, limit=10)
        capped = list_failed_jobs(conn, limit=2)

    assert [row.id for row in rows] == [newest, middle, oldest]
    assert [row.id for row in capped] == [newest, middle]
    assert survivor not in {row.id for row in rows}

    first = rows[0]
    assert first.kind == KIND
    assert first.attempts == 1
    assert first.max_attempts == 1
    assert first.last_error == "third"
    assert first.finished_at == now


# --- AC 10: cancelling a queued job ---------------------------------------------------


def test_cancelling_a_queued_job_terminalises_it_in_one_write(
    engine: Engine, now: datetime
) -> None:
    """AC 10: ``cancelled`` immediately, with the handler never entered.

    Setting the flag and waiting for a worker to notice would leave a job that had been
    requeued under a full backoff carrying the flag for up to an hour before reaching a
    terminal state — the recorded terminal status would be that late.
    """
    job_id = _put(engine, now=now)

    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "cancelled"

    row = _read(engine, job_id)
    assert row.state == "cancelled"
    assert row.finished_at == now
    assert row.cancel_requested is True
    assert row.attempts == 0, "the job was never entered"

    with engine.begin() as conn:
        assert (
            acquire_lease(
                conn, owner=OWNER_A, now=now + timedelta(hours=2), lease_seconds=LEASE
            )
            is None
        )


def test_a_job_under_a_long_backoff_is_terminal_at_once_not_at_its_next_run_at(
    engine: Engine, now: datetime
) -> None:
    """AC 10's second case: a full-backoff row is terminal now, not in an hour."""
    next_run_at = now + timedelta(seconds=BACKOFF_CAP_SECONDS)
    job_id = _put(engine, now=now, next_run_at=next_run_at)

    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "cancelled"

    row = _read(engine, job_id)
    assert row.state == "cancelled"
    assert row.finished_at == now


def test_cancelling_a_leased_job_only_raises_the_flag(
    engine: Engine, now: datetime
) -> None:
    """A worker owns the row and is the only thing that may end it."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "leased"

    row = _read(engine, job_id)
    assert row.state == "leased"
    assert row.cancel_requested is True
    assert row.lease_owner == OWNER_A
    assert row.finished_at is None


def test_a_request_against_a_terminal_job_changes_nothing(
    engine: Engine, now: datetime
) -> None:
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    with engine.begin() as conn:
        assert finish_succeeded(conn, job_id=job_id, owner=OWNER_A, now=now) is True

    with engine.begin() as conn:
        assert (
            request_cancellation(conn, job_id, now=now + timedelta(minutes=1)) is None
        )

    row = _read(engine, job_id)
    assert row.state == "succeeded"
    assert row.cancel_requested is False
    assert row.finished_at == now


# --- AC 11 (predicate half): the three writes cancellation must beat ------------------


def _leased_and_flagged(
    engine: Engine, now: datetime, *, max_attempts: int = 3
) -> UUID:
    """A job leased by A and then cancelled — flag only, because it is ``leased``."""
    job_id = _put(engine, now=now, max_attempts=max_attempts)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    with engine.begin() as conn:
        assert request_cancellation(conn, job_id, now=now) == "leased"
    return job_id


def _still_leased_and_flagged(engine: Engine, job_id: UUID) -> None:
    row = _read(engine, job_id)
    assert row.state == "leased"
    assert row.cancel_requested is True
    assert row.lease_owner == OWNER_A
    assert row.finished_at is None


def test_finish_succeeded_is_refused_once_a_cancellation_has_landed(
    engine: Engine, now: datetime
) -> None:
    """The commit-window predicate, isolated, with no loop involved.

    Without it a cancellation landing between the handler's last checkpoint and the
    terminal write still commits ``succeeded`` with the flag set.
    """
    job_id = _leased_and_flagged(engine, now)
    with engine.begin() as conn:
        applied = finish_succeeded(
            conn, job_id=job_id, owner=OWNER_A, now=now + timedelta(seconds=1)
        )
    assert applied is False
    _still_leased_and_flagged(engine, job_id)


def test_requeue_for_retry_is_refused_once_a_cancellation_has_landed(
    engine: Engine, now: datetime
) -> None:
    """The "or a further retry" predicate, isolated.

    Without it a cancelled running job whose handler raises goes back to ``queued``
    with the flag still set, under a backoff of up to an hour.
    """
    job_id = _leased_and_flagged(engine, now)
    with engine.begin() as conn:
        applied = requeue_for_retry(
            conn,
            job_id=job_id,
            owner=OWNER_A,
            now=now,
            next_run_at=now + timedelta(seconds=BACKOFF_CAP_SECONDS),
            error="handler raised",
        )
    assert applied is False
    _still_leased_and_flagged(engine, job_id)


def test_finish_failed_is_refused_once_a_cancellation_has_landed(
    engine: Engine, now: datetime
) -> None:
    """The same clause on the arm that reaches exhaustion instead of a checkpoint.

    At ``max_attempts = 1`` the next terminal write would otherwise be budget-exhausted,
    so without this predicate the job ends ``failed`` rather than ``cancelled`` —
    cancellation is supposed to win over exhaustion.
    """
    job_id = _leased_and_flagged(engine, now, max_attempts=1)
    with engine.begin() as conn:
        applied = finish_failed(
            conn, job_id=job_id, owner=OWNER_A, now=now, error="budget spent"
        )
    assert applied is False
    _still_leased_and_flagged(engine, job_id)


def test_finish_cancelled_is_the_write_the_other_three_defer_to(
    engine: Engine, now: datetime
) -> None:
    """The fourth finish carries no flag predicate, so the re-read branch can land."""
    job_id = _leased_and_flagged(engine, now)
    with engine.begin() as conn:
        assert finish_cancelled(conn, job_id=job_id, owner=OWNER_A, now=now) is True
    row = _read(engine, job_id)
    assert row.state == "cancelled"
    assert row.finished_at == now
    assert row.lease_owner is None and row.lease_until is None


# --- AC 12: the heartbeat, the throttle, and the two exceptions ----------------------


def _token(
    engine: Engine, job_id: UUID, *, owner: str, clock_at: datetime
) -> CancellationToken:
    return CancellationToken(
        engine,
        job_id=job_id,
        owner=owner,
        lease_seconds=LEASE,
        heartbeat_seconds=HEARTBEAT,
        clock=lambda: clock_at,
    )


def test_the_first_checkpoint_extends_the_lease(engine: Engine, now: datetime) -> None:
    """AC 12's extension half.

    The checkpoint's ``now`` is strictly later than the acquire's, so the assertion has
    a value to distinguish from the acquire's own write. With a shared ``now`` it would
    hold whether or not anything round-tripped.
    """
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    assert _read(engine, job_id).lease_until == now + timedelta(seconds=LEASE)

    at = now + timedelta(seconds=5)
    _token(engine, job_id, owner=OWNER_A, clock_at=now).checkpoint(now=at)

    assert _read(engine, job_id).lease_until == at + timedelta(seconds=LEASE)


def test_a_checkpoint_inside_the_heartbeat_window_does_not_round_trip(
    engine: Engine, now: datetime
) -> None:
    """The throttle's second case, with a real nonzero delta between the two calls."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    token = _token(engine, job_id, owner=OWNER_A, clock_at=now)

    first_at = now + timedelta(seconds=5)
    token.checkpoint(now=first_at)
    extended = _read(engine, job_id).lease_until
    assert extended == first_at + timedelta(seconds=LEASE)

    token.checkpoint(now=first_at + timedelta(seconds=5))
    assert _read(engine, job_id).lease_until == extended, (
        "a checkpoint less than heartbeat_seconds after the last extension must not "
        "touch the database"
    )

    past_the_window = first_at + timedelta(seconds=HEARTBEAT)
    token.checkpoint(now=past_the_window)
    assert _read(engine, job_id).lease_until == past_the_window + timedelta(
        seconds=LEASE
    )


def test_a_checkpoint_with_no_now_takes_it_from_the_injected_clock(
    engine: Engine, now: datetime
) -> None:
    """What lets a handler signature carry no clock of its own."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    at = now + timedelta(seconds=7)
    _token(engine, job_id, owner=OWNER_A, clock_at=at).checkpoint()

    assert _read(engine, job_id).lease_until == at + timedelta(seconds=LEASE)


def test_a_checkpoint_raises_job_cancelled_once_the_flag_is_set(
    engine: Engine, now: datetime
) -> None:
    job_id = _leased_and_flagged(engine, now)
    at = now + timedelta(seconds=5)
    with pytest.raises(JobCancelled):
        _token(engine, job_id, owner=OWNER_A, clock_at=now).checkpoint(now=at)
    assert _read(engine, job_id).lease_until == at + timedelta(seconds=LEASE), (
        "the same round trip that read the flag also extended the lease"
    )


def test_a_checkpoint_raises_job_lease_lost_once_the_lease_is_gone(
    engine: Engine, now: datetime
) -> None:
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)
    stolen_at = now + timedelta(seconds=LEASE + 1)
    _lease(engine, job_id, owner=OWNER_B, at=stolen_at)

    with pytest.raises(JobLeaseLost):
        _token(engine, job_id, owner=OWNER_A, clock_at=now).checkpoint(
            now=stolen_at + timedelta(seconds=1)
        )
    assert _read(engine, job_id).lease_owner == OWNER_B


def test_extend_lease_reports_the_flag_and_the_lost_lease(
    engine: Engine, now: datetime
) -> None:
    """The statement the token is a thin policy over, driven directly."""
    job_id = _put(engine, now=now)
    _lease(engine, job_id, owner=OWNER_A, at=now)

    with engine.begin() as conn:
        assert (
            extend_lease(
                conn,
                job_id=job_id,
                owner=OWNER_B,
                now=now,
                lease_seconds=LEASE,
            )
            is None
        )
        assert (
            extend_lease(
                conn, job_id=job_id, owner=OWNER_A, now=now, lease_seconds=LEASE
            )
            is False
        )
        assert request_cancellation(conn, job_id, now=now) == "leased"
        assert (
            extend_lease(
                conn, job_id=job_id, owner=OWNER_A, now=now, lease_seconds=LEASE
            )
            is True
        )


# --- earliest_due_at ------------------------------------------------------------------


def test_earliest_due_at_is_the_soonest_of_queued_work_and_lapsing_leases(
    engine: Engine, now: datetime
) -> None:
    with engine.connect() as conn:
        assert earliest_due_at(conn) is None

    waiting = _put(engine, now=now, next_run_at=now + timedelta(seconds=300))
    running = _put(engine, now=now - timedelta(seconds=10))
    with engine.connect() as conn:
        assert earliest_due_at(conn) == now - timedelta(seconds=10)

    _lease(engine, running, owner=OWNER_A, at=now)
    with engine.connect() as conn:
        assert earliest_due_at(conn) == now + timedelta(seconds=LEASE), (
            "a leased row's lapse time replaces its next_run_at"
        )

    with engine.begin() as conn:
        assert finish_succeeded(conn, job_id=running, owner=OWNER_A, now=now) is True
    with engine.connect() as conn:
        assert earliest_due_at(conn) == now + timedelta(seconds=300)

    with engine.begin() as conn:
        assert request_cancellation(conn, waiting, now=now) == "cancelled"
    with engine.connect() as conn:
        assert earliest_due_at(conn) is None, "terminal rows are neither"


# --- the retry schedule ---------------------------------------------------------------


def test_the_backoff_schedule_runs_out_into_its_cap() -> None:
    steps = [backoff_for(attempts, jitter=None) for attempts in range(1, 8)]
    assert steps == [
        timedelta(seconds=seconds) for seconds in (5, 20, 80, 320, 1280, 3600, 3600)
    ]
    assert backoff_for(500, jitter=None) == timedelta(seconds=BACKOFF_CAP_SECONDS)


def test_the_jitter_is_additive_and_never_lowers_the_floor() -> None:
    """The bound, not the type: a subtractive jitter must fail this."""
    source = random.Random(20260914)
    base = timedelta(seconds=5)
    draws = [backoff_for(1, jitter=source) for _ in range(200)]

    assert all(draw >= base for draw in draws), "the backoff floor must always hold"
    assert all(draw < base * 1.1 for draw in draws)
    assert any(draw > base for draw in draws), "the source must actually be consulted"
