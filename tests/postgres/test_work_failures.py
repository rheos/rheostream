"""AC 9 and AC 10: ``core.work.failures`` through ``dispatch``, against a real
Postgres — the failed jobs, and the failed deliveries beside them.

Seam: the registered operation itself — ``context_for_operator`` /
``context_for_harness`` -> ``registry.authorize`` -> ``dispatch`` -> the handler ->
C1's ``list_failed_jobs`` -> the response model. Never the handler called directly
and never a ``SELECT`` written here: AC 9's own words are "through ``dispatch``, not
by querying the table", so a test that read ``core.job`` itself would prove the
repository (C1 already does that) and nothing about the operation.

**No worker loop runs.** A job is put into ``failed`` the way the repository does it
— ``enqueue``, ``acquire_lease``, ``finish_failed`` — because ``finish_failed`` is
predicated on a live lease held by the calling owner, so the lease is a step of the
setup rather than a worker being started. A delivery is put into ``failed`` the same
way, through ``publish``, ``lease_delivery`` and ``fail_delivery``: this file is
about the **read**, and the drain that exhausts a real budget is
``tests/postgres/test_event_delivery.py``'s.

**Time is chosen, never slept**, matching ``test_job_repository.py``: every ``now``
is a parameter, anchored to the wall clock only because ``enqueue``'s control-plane
mark stamps ``updated_at`` from the process clock.

The role assertions are the ratified pair ``{owner, operator}``
(``docs/architecture/module-contract.md:105``), asserted from both sides: the two
permitted roles each get a real successful dispatch, and a ``member`` context — the
role the declaration's own default would have admitted — is **refused**, not handed
an empty list.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness.registry import add_member
from rheo_contracts import EventEnvelope, Role, WorkspaceContext
from rheo_core.boundary import context_for_harness, context_for_operator
from rheo_core.events import (
    ConsumerRegistry,
    ConsumerSubscription,
    NewEvent,
    fail_delivery,
    lease_delivery,
    publish,
)
from rheo_core.operations import (
    INPUT_INVALID,
    ROLE_NOT_PERMITTED,
    WORK_FAILURES,
    dispatch,
    register_core_operations,
)
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.routing import open_unit_of_work
from rheo_core.work.jobs import acquire_lease, enqueue, finish_failed
from rheo_core.work.operations import FailureList
from sqlalchemy import Engine

pytestmark = pytest.mark.postgres

KIND = "harness.job"
OWNER = "worker-a"
LEASE = 60
EVENT_TYPE = "harness.note.written"
CONSUMER = "core.recorder"


def _never_called(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """No delivery is drained in this file; the handler is never entered."""


def _consumers() -> ConsumerRegistry:
    """One consumer, owned by the reserved core segment so the fan-out reaches it
    without a module having to be enabled — this file proves the *read*, and the
    enabled-module half of the fan-out is ``tests/postgres/test_outbox.py``'s.
    """
    registry = ConsumerRegistry()
    registry.register(
        ConsumerSubscription(
            consumer_id=CONSUMER,
            event_type=EVENT_TYPE,
            module_id="core",
            replay_safe=True,
            handler=_never_called,
        )
    )
    return registry


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()


@pytest.fixture
def engine(cluster: ClusterSession, workspace: UUID) -> Engine:
    """The workspace database this file's jobs live in."""
    return cluster.backend.pools.engine_for(
        cluster.registry_row(workspace).database_name
    )


@pytest.fixture
def now() -> datetime:
    """One instant every test builds its timeline from."""
    return datetime.now(UTC)


def _fail(
    workspace: UUID, engine: Engine, *, at: datetime, error: str, budget: int = 1
) -> UUID:
    """One job, enqueued and driven to ``failed`` through C1's own functions."""
    job_id = enqueue(
        workspace,
        kind=KIND,
        payload={"note": error},
        now=at,
        max_attempts=budget,
    )
    with engine.begin() as conn:
        leased = acquire_lease(conn, owner=OWNER, now=at, lease_seconds=LEASE)
    assert leased is not None and leased.id == job_id
    with engine.begin() as conn:
        assert finish_failed(conn, job_id=job_id, owner=OWNER, now=at, error=error)
    return job_id


def _fail_delivery_of(
    ctx: WorkspaceContext, engine: Engine, *, at: datetime, error: str, subject: str
) -> UUID:
    """One published event whose single delivery is driven to ``failed`` through the
    repository's own functions.

    Published and failed one at a time, so the unqualified ``lease_delivery`` below
    has exactly one candidate to take and the test never depends on which row it
    would otherwise have picked.
    """
    with open_unit_of_work(ctx) as uow:
        event_id = publish(
            ctx,
            uow,
            NewEvent(
                type=EVENT_TYPE,
                schema_version=1,
                subject_ref=subject,
                subject_revision=1,
                data={"note": error},
            ),
            now=at,
            consumers=_consumers(),
        )
        uow.commit()
    with engine.begin() as conn:
        leased = lease_delivery(conn, owner=OWNER, now=at, lease_seconds=LEASE)
    assert leased is not None and leased.event_id == event_id
    with engine.begin() as conn:
        assert fail_delivery(
            conn,
            event_id=event_id,
            consumer_id=CONSUMER,
            owner=OWNER,
            now=at,
            error=error,
        )
    return event_id


def _listing(ctx: WorkspaceContext, **payload: object) -> FailureList:
    outcome = dispatch(ctx, WORK_FAILURES, payload)
    assert outcome.ok, outcome
    assert isinstance(outcome.result, FailureList)
    return outcome.result


# --- AC 9: the read itself ------------------------------------------------------------


def test_an_operator_reads_the_failed_jobs_with_their_errors(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 9's whole sentence: registered, readable by ``operator``, and every
    failed job comes back carrying its ``last_error``.

    The survivor is the control. A queued job that never failed must not appear,
    or "the failed jobs" is really "the jobs".
    """
    older = _fail(workspace, engine, at=now - timedelta(hours=1), error="first")
    newest = _fail(workspace, engine, at=now, error="second")
    survivor = enqueue(workspace, kind=KIND, payload={"note": "still queued"}, now=now)

    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    listing = _listing(ctx)

    assert [job.job_id for job in listing.jobs] == [newest, older]
    assert [job.last_error for job in listing.jobs] == ["second", "first"]
    assert survivor not in {job.job_id for job in listing.jobs}

    first = listing.jobs[0]
    assert first.kind == KIND
    assert first.attempts == 1
    assert first.max_attempts == 1
    assert first.finished_at == now


def test_an_owner_reads_the_same_list(
    cluster: ClusterSession,
    workspace: UUID,
    engine: Engine,
    now: datetime,
    owner_account_id: UUID,
) -> None:
    """The other half of the ratified pair, under a real ``owner`` context."""
    job_id = _fail(workspace, engine, at=now, error="owner sees this")

    ctx = context_for_harness(workspace, owner_account_id, Role.OWNER)
    assert isinstance(ctx, WorkspaceContext)
    listing = _listing(ctx)

    assert [job.job_id for job in listing.jobs] == [job_id]
    assert listing.jobs[0].last_error == "owner sees this"


def test_a_member_is_refused_rather_than_shown_an_empty_list(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """``member`` is exactly the role ``OperationDeclaration``'s default would have
    admitted, so this is the assertion that the roles were set explicitly.

    A refusal, not an empty result: the failure is a wrong *answer* only if the
    caller is told nothing went wrong. ``outcome.result`` is asserted ``None`` for
    that reason — an implementation that authorized the member and returned an
    empty list would pass a bare "not ok" check on some other refusal state.
    """
    _fail(workspace, engine, at=now, error="not for a member")

    member_id = add_member(
        cluster.backend, workspace, Role.MEMBER, display_name="member-two"
    )
    ctx = context_for_harness(workspace, member_id, Role.MEMBER)
    assert isinstance(ctx, WorkspaceContext)

    outcome = dispatch(ctx, WORK_FAILURES, {})
    assert not outcome.ok
    assert outcome.state == ROLE_NOT_PERMITTED
    assert outcome.error is not None
    assert outcome.error.error_code == ROLE_NOT_PERMITTED
    assert outcome.result is None


# --- the bounded limit ----------------------------------------------------------------


def test_the_limit_caps_the_listing_newest_first(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    older = _fail(workspace, engine, at=now - timedelta(hours=1), error="first")
    newest = _fail(workspace, engine, at=now, error="second")

    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)

    assert [job.job_id for job in _listing(ctx, limit=1).jobs] == [newest]
    assert [job.job_id for job in _listing(ctx, limit=50).jobs] == [newest, older]


@pytest.mark.parametrize("limit", [-1, 0, 501])
def test_a_limit_outside_its_bounds_is_input_invalid_not_handler_failed(
    cluster: ClusterSession, workspace: UUID, limit: int
) -> None:
    """The ``Field`` bound's whole purpose.

    Without ``ge``/``le`` a negative ``limit`` reaches the ``SELECT``, Postgres
    raises *LIMIT must not be negative*, and ``dispatch`` reports
    ``handler_failed`` carrying only an exception class name. The refusal a caller
    can act on is ``input_invalid``, and the detail names the failing field.
    """
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)

    outcome = dispatch(ctx, WORK_FAILURES, {"limit": limit})
    assert outcome.state == INPUT_INVALID, outcome
    assert outcome.error is not None
    assert "limit" in outcome.error.error_text, outcome.error


def test_an_extra_payload_key_is_ignored_and_the_context_decides(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """``extra = "ignore"``: a payload naming another workspace is not a refusal,
    and the answer is still this context's workspace."""
    job_id = _fail(workspace, engine, at=now, error="this workspace")

    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    listing = _listing(ctx, workspace_id="not-consulted")

    assert [job.job_id for job in listing.jobs] == [job_id]


# --- AC 10: the deliveries collection, beside the jobs one ----------------------------


def test_the_failed_deliveries_come_back_beside_the_failed_jobs(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """AC 10: ``deliveries`` is a second collection on the same response, and adding it
    left ``jobs`` exactly as it was.

    Both collections are asserted in one call because "beside" is the claim: a
    delivery that displaced the job list, or a job list that swallowed the deliveries,
    would satisfy either assertion alone. The pending delivery is the control — a
    delivery that has not failed must not appear, or "the failed deliveries" is really
    "the deliveries".
    """
    job_id = _fail(workspace, engine, at=now, error="the job failed")
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    event_id = _fail_delivery_of(
        ctx, engine, at=now, error="the delivery failed", subject="harness.note:one"
    )
    with open_unit_of_work(ctx) as uow:
        publish(
            ctx,
            uow,
            NewEvent(
                type=EVENT_TYPE,
                schema_version=1,
                subject_ref="harness.note:two",
                subject_revision=1,
                data={"note": "still pending"},
            ),
            now=now,
            consumers=_consumers(),
        )
        uow.commit()

    listing = _listing(ctx)

    assert [job.job_id for job in listing.jobs] == [job_id]
    assert [job.last_error for job in listing.jobs] == ["the job failed"]

    assert [delivery.event_id for delivery in listing.deliveries] == [event_id]
    delivery = listing.deliveries[0]
    assert delivery.consumer_id == CONSUMER
    assert delivery.attempts == 1
    assert delivery.last_error == "the delivery failed"
    assert delivery.completed_at == now


def test_the_limit_caps_the_deliveries_newest_first(
    cluster: ClusterSession, workspace: UUID, engine: Engine, now: datetime
) -> None:
    """The ``deliveries`` collection is ordered and capped on its own terms, by the
    same ``limit`` the jobs collection uses.

    ``limit`` bounds each collection rather than their sum: a burst of failed jobs must
    not be able to push every failed delivery out of the answer.
    """
    ctx = context_for_operator(workspace)
    assert isinstance(ctx, WorkspaceContext)
    older = _fail_delivery_of(
        ctx,
        engine,
        at=now - timedelta(hours=1),
        error="first",
        subject="harness.note:one",
    )
    newest = _fail_delivery_of(
        ctx, engine, at=now, error="second", subject="harness.note:two"
    )
    _fail(workspace, engine, at=now, error="a job, to spend the jobs budget")

    assert [delivery.event_id for delivery in _listing(ctx, limit=1).deliveries] == [
        newest
    ]
    assert [delivery.event_id for delivery in _listing(ctx, limit=50).deliveries] == [
        newest,
        older,
    ]
    assert len(_listing(ctx, limit=1).jobs) == 1, "the limit bounds each collection"
