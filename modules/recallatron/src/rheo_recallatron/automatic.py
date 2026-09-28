"""Automatic memory: the consumer of core's evidence-recorded event
(:data:`EVIDENCE_RECORDED`) and the drain job it enqueues (spec § Architecture, System
Components items 11 and 12).

**The consumer only enqueues.** A delivery of :data:`EVIDENCE_RECORDED` writes one
drain job with the empty payload and asks for a due mark, and does nothing else. It
reads nothing from the envelope: the event's ``data`` is ``{}``, and which evidence to
take is the drain's claim to make, not the event's. Processing inline would put a slow
provider or a raising acceptance in front of every later delivery to this consumer;
as a job, it holds up only itself.

**The drain names no workspace.** :class:`DrainJobPayload` has no fields, so the only
valid payload is ``{}``. A job runs on its workspace's own database, and
``claim_units`` resolves the workspace from that connection. When it cannot (the
routing row disagrees, or the workspace has no acceptance context for a reason other
than a lapsed membership), it raises; that raise leaves the handler untouched, the
transaction rolls back with no evidence row settled, and the job substrate retries it
under :data:`DRAIN_MAX_ATTEMPTS`.

**One transaction per drain.** Each claimed unit goes through 1a1's seam
(``accept_source_unit``) and is then settled with the seam's own outcome, so the
memory, its receipt and ``recorded`` event, the success audit row, the settlement and
any follow-up drain job commit together or not at all. An extraction failure never
reaches this module: ``claim_units`` contains it to its partition and returns no
units, so the drain succeeds and the per-row attempt counts commit.

**The due-mark requests are for the direct path.** On the worker path nothing reads a
``HandlerUnitOfWork``'s due-mark request; the visit that ran this handler reads the
remaining due instant itself, after its jobs and deliveries, and so sees the job
written here. A directly built view (a dispatch, a test) does read it.

**One clock.** :func:`_now` is read once per handler run by both handlers, so a test
pins time for both by replacing it.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from rheo_contracts import EventEnvelope
from rheo_core.events.consumers import HandlerUnitOfWork
from rheo_core.evidence import claim_units, settle_unit
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job

from rheo_recallatron.configuration import (
    DRAIN_JOB_KIND,
    DRAIN_MAX_ATTEMPTS,
    MODULE_ID,
)
from rheo_recallatron.source_units import accept_source_unit


class DrainJobPayload(BaseModel):
    """The drain's payload: nothing. ``extra = "forbid"``, so ``{}`` is the only
    valid one and a payload naming a workspace, or anything else, did not come from
    this module."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _now() -> datetime:
    """The instant both handlers act at, read once per run."""
    return datetime.now(UTC)


def _enqueue_drain(
    uow: HandlerUnitOfWork, *, now: datetime, next_run_at: datetime | None = None
) -> None:
    enqueue_job(
        uow.connection,
        kind=DRAIN_JOB_KIND,
        payload={},
        now=now,
        max_attempts=DRAIN_MAX_ATTEMPTS,
        next_run_at=next_run_at,
    )
    uow.request_due_mark()


def on_evidence_recorded(uow: HandlerUnitOfWork, envelope: EventEnvelope) -> None:
    """Enqueue one drain job, due now. ``envelope`` is deliberately unread."""
    _enqueue_drain(uow, now=_now())


def run_drain_job(
    uow: HandlerUnitOfWork, payload: BaseModel, token: CancellationToken
) -> None:
    """Claim one partition, accept and settle each unit, and queue the follow-up.

    A missing consumer registry is refused before the claim: acceptance publishes,
    and a claim that ran the provider only to roll back would count nothing.
    """
    if not isinstance(payload, DrainJobPayload):
        raise TypeError(f"expected a DrainJobPayload, got {type(payload).__name__}")
    token.checkpoint()
    consumers = uow.consumers
    if consumers is None:
        raise RuntimeError("the drain job was handed no consumer registry")
    now = _now()
    batch = claim_units(uow, now=now)
    for claimed in batch.units:
        token.checkpoint()
        outcome = accept_source_unit(
            claimed.context,
            uow,
            claimed.unit,
            authority=claimed.authority,
            consumers=consumers,
            now=now,
        )
        settle_unit(
            uow,
            claimed,
            outcome=outcome.state,
            memory_ref=outcome.memory_ref,
            audit_module_id=MODULE_ID,
            now=now,
        )
    if batch.follow_up_at is not None:
        _enqueue_drain(uow, now=now, next_run_at=batch.follow_up_at)
