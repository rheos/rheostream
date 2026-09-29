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

**One poison unit costs only itself (#215), and only at the accept step (#221).**
Each unit's ``accept_source_unit`` call runs in its own savepoint. A database refusal
of that unit's writes (``IntegrityError`` or ``DataError`` on a live connection) rolls
the savepoint back and hands the unit to core's ``defer_unit``, which counts the
attempt and backs the row off, or settles it ``gap/acceptance_failed`` on the fifth.
The drain then carries on with the next unit and succeeds, so the count commits.
``settle_unit`` runs outside the savepoint: a fault in the settlement, its success
audit row or anything else is not the unit's fault, so it rolls the whole drain back
and fails the job attempt, as does ``EvidenceAuditUnwritable``, a dropped connection
or any other raise.

**A run of deferrals is not a poison unit (#221).** A schema or code fault that
refuses every acceptance would otherwise defer every unit and, five drains later,
settle them all ``gap/acceptance_failed`` while each drain job reports success. So
when at least :data:`DEFERRAL_RUN_MINIMUM` units of one batch defer and they are more
than half of it, the drain raises :class:`DrainDeferralRun`. That rolls the drain
back, deferral counts included, and the job fails where people look.

**Workspace-wide acceptance preconditions are checked before the model call
(#217).** Two things acceptance needs do not depend on the unit: an audit sink for
this module, which ``settle_unit`` writes the success row through, and a usable
retention policy, which ``accept_source_unit`` refuses ``retention_unavailable``
without. Either missing rolls the whole drain back, and with it the claim's
extraction, so each retry would send the same evidence to the provider again. The
drain hands ``claim_units`` :func:`_acceptance_ready`, which core calls just before
the provider, and it raises the same ``EvidenceAuditUnwritable`` or refusal the
acceptance would have raised, with no model call made.

**A model's mention kinds are not trusted.** Extraction accepts any kind string, and
the ``memory_entity_kind`` CHECK allows only :data:`EntityKind`. The drain tells the
provider the six kinds (``claim_units``'s ``mention_kinds``, #222), and then maps what
comes back: a kind is case-folded, and a near miss in the short
:data:`KIND_SYNONYMS` table becomes its :data:`EntityKind`. A mention of any other kind
is dropped before acceptance, and the same rebuilt unit is settled, so the success
audit's digest is of what was accepted.

**Content-free failures.** A psycopg error's text quotes the failing row and SQLAlchemy
adds the bound parameters, and the worker stores ``str(failure)`` as the job's
``last_error``. So no ``StatementError`` (the base of ``DBAPIError``, and what
SQLAlchemy raises when binding a parameter fails) leaves the drain as itself:
:class:`DrainDatabaseError` carries its class name and SQLSTATE only. The two log lines
here carry counts, a class name and a SQLSTATE, never a kind, a name or a message.

**The due-mark requests are for the direct path.** On the worker path nothing reads a
``HandlerUnitOfWork``'s due-mark request; the visit that ran this handler reads the
remaining due instant itself, after its jobs and deliveries, and so sees the job
written here. A directly built view (a dispatch, a test) does read it.

**One clock.** :func:`_now` is read once per handler run by both handlers, so a test
pins time for both by replacing it.
"""

import dataclasses
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, get_args

from pydantic import BaseModel, ConfigDict
from rheo_contracts import EventEnvelope, WorkspaceContext
from rheo_contracts.source_units import SourceMention
from rheo_core.audit import sink_for
from rheo_core.events.consumers import ConsumerRegistry, HandlerUnitOfWork
from rheo_core.evidence import (
    ClaimedUnit,
    EvidenceAuditUnwritable,
    claim_units,
    defer_unit,
    settle_unit,
)
from rheo_core.operations.refusals import OperationRefused
from rheo_core.work.cancellation import CancellationToken
from rheo_core.work.jobs import enqueue_job
from sqlalchemy.exc import DataError, IntegrityError, StatementError

from rheo_recallatron.configuration import (
    DRAIN_JOB_KIND,
    DRAIN_MAX_ATTEMPTS,
    MODULE_ID,
)
from rheo_recallatron.contracts import EntityKind
from rheo_recallatron.eligibility import begin_request
from rheo_recallatron.refusals import RETENTION_UNAVAILABLE
from rheo_recallatron.source_units import accept_source_unit

_log = logging.getLogger(__name__)

_ENTITY_KIND_ORDER: Final[tuple[EntityKind, ...]] = get_args(EntityKind)
_ENTITY_KINDS: Final[frozenset[str]] = frozenset(_ENTITY_KIND_ORDER)

KIND_SYNONYMS: Final[Mapping[str, EntityKind]] = MappingProxyType(
    {
        "company": "organization",
        "business": "organization",
        "org": "organization",
        "organisation": "organization",
        "city": "place",
        "country": "place",
        "location": "place",
    }
)
"""Near misses a model is likely to propose, case-folded, and the kind each means.

Deliberately short: each entry is a word whose meaning is plainly one kind. A word
that could be two (``group``, ``event``, ``product``) is left out and dropped, since
a wrong kind is worse than no mention. The six kinds themselves need no entry."""

MENTIONS_DROPPED_LOG: Final = "automatic_memory_mentions_dropped"
ACCEPTANCE_DEFERRED_LOG: Final = "automatic_memory_acceptance_deferred"

DEFERRAL_RUN_MINIMUM: Final = 2
"""The fewest deferred units that can make a run: one deferral is a poison unit."""


def _sqlstate(error: StatementError) -> str | None:
    """The driver's SQLSTATE, when it is a string; anything else could carry text."""
    raw = getattr(error.orig, "sqlstate", None)
    return raw if isinstance(raw, str) else None


class DrainDatabaseError(Exception):
    """A ``StatementError`` that escaped the drain, with its message, statement and
    parameters dropped: ``str()`` and ``repr()`` are the class name and SQLSTATE.

    Core's own equivalent is not on the evidence surface Recallatron may import, so
    this module keeps its own. ``connection_invalidated`` is kept.
    """

    def __init__(self, error: StatementError) -> None:
        sqlstate = _sqlstate(error)
        super().__init__(f"{type(error).__name__} (SQLSTATE {sqlstate})")
        self.error_type: str = type(error).__name__
        self.sqlstate: str | None = sqlstate
        # Only a ``DBAPIError`` knows whether the connection went; a bind failure
        # never touched it.
        self.connection_invalidated: bool = bool(
            getattr(error, "connection_invalidated", False)
        )


class DrainDeferralRun(Exception):
    """More than half of one batch's units deferred, and at least
    :data:`DEFERRAL_RUN_MINIMUM` of them: a workspace-wide acceptance fault, not a
    poison unit. ``str()`` is the two counts and nothing else."""

    def __init__(self, *, deferred: int, claimed: int) -> None:
        super().__init__(f"{deferred} of {claimed} claimed units deferred")
        self.deferred: int = deferred
        self.claimed: int = claimed


def _is_deferral_run(deferred: int, claimed: int) -> bool:
    return deferred >= DEFERRAL_RUN_MINIMUM and 2 * deferred > claimed


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


def _entity_kind(kind: str) -> str | None:
    """``kind`` as an :data:`EntityKind`, case-folded and through
    :data:`KIND_SYNONYMS`, or ``None`` when it is neither."""
    folded = kind.strip().casefold()
    if folded in _ENTITY_KINDS:
        return folded
    return KIND_SYNONYMS.get(folded)


def _known_mentions(claimed: ClaimedUnit) -> tuple[ClaimedUnit, int]:
    """``claimed`` with every mention's kind mapped to an :data:`EntityKind`, any
    mention whose kind maps to none dropped, and how many were. The unit is rebuilt
    only when a mention was dropped or renamed."""
    evidence = claimed.unit.evidence
    kept: list[SourceMention] = []
    for mention in evidence.mentions:
        kind = _entity_kind(mention.kind)
        if kind is None:
            continue
        if kind != mention.kind:
            mention = mention.model_copy(update={"kind": kind})
        kept.append(mention)
    if tuple(kept) == evidence.mentions:
        return claimed, 0
    unit = claimed.unit.model_copy(
        update={"evidence": evidence.model_copy(update={"mentions": tuple(kept)})}
    )
    return dataclasses.replace(claimed, unit=unit), len(evidence.mentions) - len(kept)


def _acceptance_ready(
    uow: HandlerUnitOfWork, *, now: datetime
) -> Callable[[WorkspaceContext], None]:
    """The check ``claim_units`` runs just before the provider call (#217): raise what
    acceptance would raise for any unit of this workspace, before the model is asked.

    The sink is looked up exactly as ``record_evidence_acceptance_audit`` looks it
    up, and retention is read through the same ``begin_request`` acceptance opens
    with, on the drain's own transaction.
    """

    def ready(ctx: WorkspaceContext) -> None:
        if sink_for(MODULE_ID) is None:
            raise EvidenceAuditUnwritable("no audit sink for the accepting module")
        if begin_request(ctx, uow, now=now) is None:
            raise OperationRefused(
                RETENTION_UNAVAILABLE, "this workspace has no usable retention policy"
            )

    return ready


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
    escaped: DrainDatabaseError | None = None
    try:
        _drain(uow, token, consumers=consumers)
    except StatementError as error:
        escaped = DrainDatabaseError(error)
    if escaped is not None:
        # Outside the ``except`` block, so the original is not on ``__context__``.
        raise escaped from None


def _drain(
    uow: HandlerUnitOfWork, token: CancellationToken, *, consumers: ConsumerRegistry
) -> None:
    """The drain proper: :func:`run_drain_job` only guards what may escape it."""
    now = _now()
    batch = claim_units(
        uow,
        now=now,
        before_extraction=_acceptance_ready(uow, now=now),
        mention_kinds=_ENTITY_KIND_ORDER,
    )
    dropped_mentions = 0
    trimmed_units = 0
    deferred = 0
    for claimed in batch.units:
        token.checkpoint()
        claimed, dropped = _known_mentions(claimed)
        if dropped:
            dropped_mentions += dropped
            trimmed_units += 1
        refused: IntegrityError | DataError | None = None
        try:
            with uow.connection.begin_nested():
                outcome = accept_source_unit(
                    claimed.context,
                    uow,
                    claimed.unit,
                    authority=claimed.authority,
                    consumers=consumers,
                    now=now,
                )
        except (IntegrityError, DataError) as error:
            if error.connection_invalidated:
                raise
            refused = error
        if refused is not None:
            defer_unit(uow, claimed, now=now)
            deferred += 1
            _log.warning(
                ACCEPTANCE_DEFERRED_LOG,
                extra={
                    "evidence_id": str(claimed.evidence_id),
                    "error_type": type(refused).__name__,
                    "sqlstate": _sqlstate(refused),
                },
            )
            if _is_deferral_run(deferred, len(batch.units)):
                # Outside the ``except`` block, so the refusal, whose text quotes the
                # failing row, is not on ``__context__``.
                raise DrainDeferralRun(deferred=deferred, claimed=len(batch.units))
            continue
        # Outside the savepoint (#221): a settlement fault rolls the drain back.
        settle_unit(
            uow,
            claimed,
            outcome=outcome.state,
            memory_ref=outcome.memory_ref,
            audit_module_id=MODULE_ID,
            now=now,
        )
    if dropped_mentions:
        _log.info(
            MENTIONS_DROPPED_LOG,
            extra={"mention_count": dropped_mentions, "unit_count": trimmed_units},
        )
    follow_up_at = batch.follow_up_at
    if deferred:
        # The claim read its follow-up before this drain deferred anything. A drain
        # due now finds each deferred row waiting and schedules itself at the
        # earliest ``retry_after``, so the backed-off rows are not left for the next
        # recorded turn to pick up.
        follow_up_at = now
    if follow_up_at is not None:
        _enqueue_drain(uow, now=now, next_run_at=follow_up_at)
