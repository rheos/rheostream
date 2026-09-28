"""Recallatron's automatic-memory consumer and drain job (spec § Architecture, System
Components items 11 and 12; FR 3, FR 7, FR 11).

Seams under test:

- **the consumer enqueues exactly one drain job per delivery**, payload ``{}``, one due
  mark, and reads nothing from the envelope;
- **the follow-up enqueue timing**: the drain writes one more drain job exactly at the
  claim's ``follow_up_at``, now or later, and none when it is ``None``;
- **an extraction failure leaves the drain job ``succeeded``**, once, with no error;
  only the row's own ``extraction_attempts`` moves;
- **an unresolved workspace is a job retry**: the drain raises, retries under
  ``DRAIN_MAX_ATTEMPTS`` and fails terminally, and never touches an evidence row.

Two tests run the whole chain on purpose. One pins where the drain is published and
consumed (#187): a job that records evidence, the delivery the same visit drains, and
the drain job that delivery enqueues, all at one pinned instant. The other shows that
with the pipeline live a recording failure still leaves a user's run succeeding (#197).

Kinds and subscriptions are taken off ``MANIFEST``, never registered by hand, so a
kind the manifest stops declaring fails here as unknown. Every text is synthetic.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import pytest
from conftest import ClusterSession
from harness import runtime_matrix
from harness.evidence import (
    EvidenceWorkspace,
    enable_recording,
    probe_registry,
    probe_subscription,
)
from harness.modules import install_and_enable_module, loaded_probe_modules
from harness.registry import register_harness
from harness.runtime_matrix import (
    SECRET_REF,
    SECRET_VALUE,
    RecordingAdapter,
    frozen_clock,
    job_row,
    owner_context,
)
from pydantic import BaseModel, ValidationError
from rheo_contracts import ContextPurpose, EventEnvelope, Role, WorkspaceContext
from rheo_core.boundary import Refusal, context_for_harness
from rheo_core.events import ConsumerRegistry
from rheo_core.evidence import EVIDENCE_RECORDED, ClaimedBatch
from rheo_core.evidence import service as evidence_service
from rheo_core.evidence.extract import DigestBatch
from rheo_core.evidence.providers import (
    FAKE_EXTRACTION_MARKER,
    FAKE_PROVIDER,
    PROVIDERS,
    providers,
)
from rheo_core.evidence.record import NewEvidence, record_evidence
from rheo_core.operations import dispatch, register_core_operations
from rheo_core.refs import uuid7
from rheo_core.runtime import RUNTIME_RUN
from rheo_core.storage import work_tables
from rheo_core.storage.backend import WORKSPACE_UNAVAILABLE, HandlerUnitOfWork
from rheo_core.storage.evidence_tables import evidence_unit
from rheo_core.storage.work_index import DueWorkspace
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job
from rheo_core.work.kinds import JobKindRegistry
from rheo_core.work.loop import VisitResult, visit_workspace
from rheo_recallatron import automatic
from rheo_recallatron.automatic import DrainJobPayload, on_evidence_recorded
from rheo_recallatron.configuration import (
    AUTOMATIC_CONSUMER_ID,
    DRAIN_JOB_KIND,
    DRAIN_MAX_ATTEMPTS,
    MODULE_ID,
)
from rheo_recallatron.manifest import MANIFEST
from rheo_recallatron.storage import tables as memory_tables
from sqlalchemy import select
from sqlalchemy.engine import Row

pytestmark = pytest.mark.postgres

_OWNER: Final = "automatic-memory-drain-test"
_RECORDER_KIND: Final = "test.evidence_recorder"
_TURN: Final = "Please book the community hall for the spring volunteer meeting."
_FAULT: Final = "a drain-test provider fault"


@pytest.fixture(autouse=True)
def registrations() -> None:
    register_core_operations()
    register_harness()


@pytest.fixture(autouse=True)
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The model credential a runtime run binds; the double adapter never uses it."""
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_kind", "api_key")
    monkeypatch.setenv("RHEO__runtime__claude_cli__credential_ref", SECRET_REF)
    monkeypatch.setenv("RHEO_ANTHROPIC_API_KEY", SECRET_VALUE)


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def ev(
    monkeypatch: pytest.MonkeyPatch,
    cluster: ClusterSession,
    workspace: UUID,
    owner_account_id: UUID,
) -> Iterator[EvidenceWorkspace]:
    """A workspace with Recallatron installed and enabled, the harness module enabled,
    and recording conditions 1 and 2 on."""
    with loaded_probe_modules(monkeypatch, MODULE_ID):
        bootstrap = context_for_harness(workspace, owner_account_id, Role.OWNER)
        assert isinstance(bootstrap, WorkspaceContext), bootstrap
        install_and_enable_module(cluster.backend, bootstrap, workspace, MODULE_ID)
        evidence = EvidenceWorkspace(cluster, workspace, owner_account_id)
        enable_recording(monkeypatch, evidence)
        yield evidence


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, now: datetime) -> datetime:
    """Both handlers' clock, pinned to ``now``."""
    monkeypatch.setattr(automatic, "_now", lambda: now)
    return now


# --- helpers ----------------------------------------------------------------------


def _kinds() -> JobKindRegistry:
    kinds = JobKindRegistry()
    for job in MANIFEST.jobs:
        kinds.register(job.name, job.input_model, job.handler)
    return kinds


def _consumers(*, probe: bool = False) -> ConsumerRegistry:
    consumers = ConsumerRegistry()
    for subscription in MANIFEST.subscriptions:
        consumers.register(subscription)
    if probe:
        consumers.register(probe_subscription())
    return consumers


def _visit(
    ev: EvidenceWorkspace,
    *,
    at: datetime,
    kinds: JobKindRegistry | None = None,
    consumers: ConsumerRegistry | None = None,
) -> VisitResult:
    """One real worker visit at a fixed instant."""
    return visit_workspace(
        DueWorkspace(workspace_id=ev.workspace_id, observed_due_at=None),
        kinds=kinds or _kinds(),
        consumers=consumers or _consumers(probe=True),
        backend=ev.cluster.backend,
        owner=_OWNER,
        clock=lambda: at,
        jitter=None,
    )


def _jobs(ev: EvidenceWorkspace, kind: str = DRAIN_JOB_KIND) -> list[Row[Any]]:
    job = work_tables.job
    with ev.unit_of_work() as uow:
        return list(
            uow.connection.execute(
                select(job).where(job.c.kind == kind).order_by(job.c.id)
            )
        )


def _enqueue_drain(ev: EvidenceWorkspace, *, at: datetime) -> UUID:
    with ev.unit_of_work() as uow:
        job_id = enqueue_job(
            uow.connection,
            kind=DRAIN_JOB_KIND,
            payload={},
            now=at,
            max_attempts=DRAIN_MAX_ATTEMPTS,
        )
        uow.commit()
    return job_id


def _new_evidence(ctx: WorkspaceContext, body: str, *, at: datetime) -> NewEvidence:
    account_id = ctx.principal.account_id
    purpose = ctx.principal.bound_purpose
    assert account_id is not None and purpose is not None
    authority_id = uuid7()
    return NewEvidence(
        producer_kind="rheo_runtime",
        authority_id=authority_id,
        native_key=f"turn:{authority_id}",
        speaker_account_id=account_id,
        audience_kind="member",
        audience_id=account_id,
        purpose=purpose,
        recorded_at=at,
        raw_text=body,
    )


def _record_with_probe(ev: EvidenceWorkspace, body: str, *, at: datetime) -> None:
    """One pending row, published to the probe only: the test enqueues the drain."""
    ctx = ev.context(ContextPurpose.RESPOND)
    with ev.handler(probe_registry()) as uow:
        attempt = record_evidence(ctx, uow, [_new_evidence(ctx, body, at=at)], now=at)
    assert len(attempt.accepted) == 1, attempt


def _units(ev: EvidenceWorkspace) -> list[Row[Any]]:
    with ev.unit_of_work() as uow:
        return list(uow.connection.execute(select(evidence_unit)))


def _envelope(subject_ref: str) -> EventEnvelope:
    return EventEnvelope(
        id=uuid7(),
        position=1,
        type=EVIDENCE_RECORDED,
        schema_version=1,
        source="urn:rheo:module:core",
        subject_ref=subject_ref,
        subject_revision=1,
        correlation_id=uuid7(),
        actor_kind="account",
        occurred_at=datetime.now(UTC),
        data={},
        consumer_id=AUTOMATIC_CONSUMER_ID,
        attempt=1,
    )


def _claims(*batches: ClaimedBatch) -> Callable[..., ClaimedBatch]:
    """A stand-in claim answering ``batches`` in order, then an empty one."""
    queue = list(batches)

    def claim(uow: HandlerUnitOfWork, *, now: datetime) -> ClaimedBatch:
        return queue.pop(0) if queue else ClaimedBatch(units=(), follow_up_at=None)

    return claim


class _Failing:
    """A provider that always raises, quoting nothing a log could keep."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return FAKE_PROVIDER

    def extract(self, batch: DigestBatch) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError(_FAULT)


def _install_failing(monkeypatch: pytest.MonkeyPatch) -> _Failing:
    providers()  # the built-ins first, so the swap is undone onto them
    failing = _Failing()
    monkeypatch.setitem(PROVIDERS, FAKE_PROVIDER, failing)
    return failing


# --- the consumer -----------------------------------------------------------------


def test_one_delivery_enqueues_exactly_one_empty_drain_job_and_one_due_mark(
    ev: EvidenceWorkspace, pinned: datetime
) -> None:
    """And a delivery naming another subject enqueues an identical job: the consumer
    read nothing from the envelope."""
    views: list[HandlerUnitOfWork] = []
    for subject in (f"core.evidence_unit:{uuid7()}", f"harness.note:{uuid7()}"):
        with ev.unit_of_work() as uow:
            view = HandlerUnitOfWork(uow)
            assert view.due_mark_requested is False
            on_evidence_recorded(view, _envelope(subject))
            uow.commit()
        views.append(view)
    assert [view.due_mark_requested for view in views] == [True, True]

    jobs = _jobs(ev)
    assert len(jobs) == 2
    shapes = {
        (job.state, job.max_attempts, job.next_run_at, job.attempts) for job in jobs
    }
    assert shapes == {("queued", DRAIN_MAX_ATTEMPTS, pinned, 0)}
    assert [job.input for job in jobs] == [{}, {}]


def test_the_drain_payload_is_empty_and_refuses_every_key() -> None:
    assert DrainJobPayload.model_validate({}) == DrainJobPayload()
    for key in ("workspace_id", "evidence_id", "subject_ref", "anything"):
        with pytest.raises(ValidationError):
            DrainJobPayload.model_validate({key: str(uuid7())})


def test_the_drain_refuses_a_payload_of_another_type(ev: EvidenceWorkspace) -> None:
    class Other(BaseModel):
        pass

    with ev.unit_of_work() as uow, pytest.raises(TypeError):
        automatic.run_drain_job(
            HandlerUnitOfWork(uow, consumers=_consumers()),
            Other(),
            CancellationToken(
                uow.connection.engine,
                job_id=uuid7(),
                owner=_OWNER,
                lease_seconds=60,
                heartbeat_seconds=10,
                clock=lambda: datetime.now(UTC),
            ),
        )


# --- the drain: empty claims and follow-ups ---------------------------------------


def test_an_empty_claim_succeeds_writes_nothing_and_enqueues_no_follow_up(
    ev: EvidenceWorkspace, pinned: datetime
) -> None:
    job_id = _enqueue_drain(ev, at=pinned)
    _visit(ev, at=pinned)

    [job] = _jobs(ev)
    assert job.id == job_id
    assert (job.state, job.attempts, job.last_error) == ("succeeded", 1, None)
    assert _units(ev) == []


def test_a_follow_up_due_now_enqueues_one_empty_drain_due_now(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, pinned: datetime
) -> None:
    monkeypatch.setattr(
        automatic,
        "claim_units",
        _claims(ClaimedBatch(units=(), follow_up_at=pinned)),
    )
    first = _enqueue_drain(ev, at=pinned)
    _visit(ev, at=pinned)

    # The follow-up is due at the visit's own instant, so the same visit leased it;
    # the stand-in then answers empty, so it wrote nothing more.
    jobs = _jobs(ev)
    assert len(jobs) == 2
    [follow_up] = [job for job in jobs if job.id != first]
    assert follow_up.input == {}
    assert follow_up.next_run_at == pinned
    assert follow_up.max_attempts == DRAIN_MAX_ATTEMPTS
    assert [job.state for job in jobs] == ["succeeded", "succeeded"]


def test_a_backed_off_follow_up_is_enqueued_exactly_at_its_instant(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, pinned: datetime
) -> None:
    later = pinned + timedelta(seconds=320)
    monkeypatch.setattr(
        automatic,
        "claim_units",
        _claims(ClaimedBatch(units=(), follow_up_at=later)),
    )
    first_id = _enqueue_drain(ev, at=pinned)
    result = _visit(ev, at=pinned)

    [first] = [job for job in _jobs(ev) if job.id == first_id]
    [follow_up] = [job for job in _jobs(ev) if job.id != first_id]
    assert first.state == "succeeded"
    assert (follow_up.state, follow_up.input) == ("queued", {})
    assert follow_up.next_run_at == later
    assert result.next_due_at == later


def test_an_extraction_failure_leaves_the_drain_succeeded_and_only_the_row_moved(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, pinned: datetime
) -> None:
    """R11's containment, seen from the job: ran once, never retried, no error. The
    backed-off row asks for exactly one follow-up at its ``retry_after``."""
    _record_with_probe(ev, f"{FAKE_EXTRACTION_MARKER} {_TURN}", at=pinned)
    failing = _install_failing(monkeypatch)
    drain_id = _enqueue_drain(ev, at=pinned)
    _visit(ev, at=pinned)

    assert failing.calls == 1
    [drain] = [job for job in _jobs(ev) if job.id == drain_id]
    [follow_up] = [job for job in _jobs(ev) if job.id != drain_id]
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    [unit] = _units(ev)
    assert (unit.state, unit.extraction_attempts) == ("pending", 1)
    assert unit.retry_after == pinned + timedelta(seconds=5)
    assert (follow_up.state, follow_up.next_run_at) == ("queued", unit.retry_after)


# --- the drain: an unresolved workspace is a job retry -----------------------------


def _unroutable(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(workspace_id: UUID) -> Any:
        from rheo_core.storage.backend import StorageRefusal

        raise StorageRefusal(WORKSPACE_UNAVAILABLE, "synthetic: not active")

    monkeypatch.setattr(evidence_service, "active_workspace", refuse)


def _no_context(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> Refusal:
        return Refusal(WORKSPACE_UNAVAILABLE, "synthetic refusal")

    monkeypatch.setattr(evidence_service, "context_for_evidence_acceptance", refuse)


@pytest.mark.parametrize(
    "unresolve", [_unroutable, _no_context], ids=["step_0", "context_refusal"]
)
def test_an_unresolved_workspace_retries_then_fails_and_touches_no_row(
    monkeypatch: pytest.MonkeyPatch,
    ev: EvidenceWorkspace,
    now: datetime,
    unresolve: Callable[[pytest.MonkeyPatch], None],
) -> None:
    _record_with_probe(ev, f"{FAKE_EXTRACTION_MARKER} {_TURN}", at=now)
    [before] = _units(ev)
    failing = _install_failing(monkeypatch)
    unresolve(monkeypatch)
    _enqueue_drain(ev, at=now)

    # Past each back-off step (5, 20, 80, 320 s) and well inside the pending window.
    for attempt in range(1, DRAIN_MAX_ATTEMPTS + 1):
        at = now + timedelta(seconds=400 * (attempt - 1))
        monkeypatch.setattr(automatic, "_now", lambda at=at: at)
        _visit(ev, at=at)
        [job] = _jobs(ev)
        assert job.attempts == attempt
        expected = "failed" if attempt == DRAIN_MAX_ATTEMPTS else "queued"
        assert job.state == expected, (attempt, job.state)

    assert failing.calls == 0
    [after] = _units(ev)
    assert after._asdict() == before._asdict()


# --- end to end: where the drain is published and consumed (#187) ------------------


class _RecorderPayload(BaseModel):
    pass


def test_a_recorded_turn_is_delivered_in_its_own_visit_and_drained_on_the_next(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace, pinned: datetime
) -> None:
    """A job records evidence on its worker-built view; the visit drains that
    delivery after its jobs, the consumer enqueues the drain due at the pinned
    instant, and the visit's own remaining-work read reports it. Nothing waits for
    the reconcile floor. The next visit runs the drain and settles the unit."""
    ctx = ev.context(ContextPurpose.FOLLOW_UP)
    body = f"{FAKE_EXTRACTION_MARKER} {_TURN}"

    def record(uow: HandlerUnitOfWork, payload: BaseModel, token: object) -> None:
        # The worker's own view, carrying the visit's consumer registry.
        assert uow.consumers is not None
        record_evidence(ctx, uow, [_new_evidence(ctx, body, at=pinned)], now=pinned)

    kinds = _kinds()
    kinds.register(_RECORDER_KIND, _RecorderPayload, record)
    consumers = _consumers()
    with ev.unit_of_work() as uow:
        enqueue_job(
            uow.connection, kind=_RECORDER_KIND, payload={}, now=pinned, max_attempts=1
        )
        uow.commit()

    first = _visit(ev, at=pinned, kinds=kinds, consumers=consumers)

    delivery = work_tables.event_delivery
    with ev.unit_of_work() as uow:
        [delivered] = uow.connection.execute(
            select(delivery).where(delivery.c.consumer_id == AUTOMATIC_CONSUMER_ID)
        ).all()
    assert delivered.state == "delivered"
    [drain] = _jobs(ev)
    assert (drain.state, drain.input, drain.next_run_at) == ("queued", {}, pinned)
    assert first.next_due_at is not None and first.next_due_at <= pinned

    _visit(ev, at=pinned, kinds=kinds, consumers=consumers)
    [drain] = _jobs(ev)
    assert (drain.state, drain.attempts, drain.last_error) == ("succeeded", 1, None)
    [unit] = _units(ev)
    assert (unit.state, unit.outcome) == ("settled", "active")
    with ev.unit_of_work() as uow:
        purposes = uow.connection.execute(
            select(memory_tables.memory_purpose.c.purpose)
        ).scalars()
        assert list(purposes) == [ContextPurpose.FOLLOW_UP.value]


# --- end to end: a recording failure never fails the run (#197) --------------------


def _run(ev: EvidenceWorkspace, monkeypatch: pytest.MonkeyPatch) -> UUID:
    """Dispatch one runtime run and drive it through one visit with the pipeline
    live: Recallatron's real subscription and every manifest kind registered. The
    visit's instant is taken after the dispatch, so the run's job is due in it."""
    ctx = owner_context(ev.cluster, ev.workspace_id, ev.owner_account_id)
    outcome = dispatch(ctx, RUNTIME_RUN, runtime_matrix.payload(task=_TURN))
    assert outcome.state == "pending", outcome
    assert outcome.operation_id is not None
    at = datetime.now(UTC) + timedelta(seconds=1)
    monkeypatch.setattr(automatic, "_now", lambda: at)
    kinds = runtime_matrix.kinds(RecordingAdapter(), frozen_clock(at))
    for job in MANIFEST.jobs:
        kinds.register(job.name, job.input_model, job.handler)
    _visit(ev, at=at, kinds=kinds, consumers=_consumers())
    return outcome.operation_id


def test_with_the_pipeline_live_a_run_records_and_enqueues_one_drain(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace
) -> None:
    """The positive control for the next test: the pipeline really is live."""
    operation_id = _run(ev, monkeypatch)
    assert (
        job_row(ev.cluster.backend.pools.engine_for(ev.database_name), operation_id)[
            "state"
        ]
        == "succeeded"
    )
    [unit] = _units(ev)
    assert unit.native_key == f"turn:{operation_id}"
    [drain] = _jobs(ev)
    assert drain.state == "queued"


def test_with_the_pipeline_live_a_recording_failure_leaves_the_run_succeeding(
    monkeypatch: pytest.MonkeyPatch, ev: EvidenceWorkspace
) -> None:
    def raises(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic recording fault")

    monkeypatch.setattr("rheo_core.runtime.operations.record_evidence", raises)
    operation_id = _run(ev, monkeypatch)
    engine = ev.cluster.backend.pools.engine_for(ev.database_name)
    assert job_row(engine, operation_id)["state"] == "succeeded"
    assert _units(ev) == []
    assert _jobs(ev) == []
