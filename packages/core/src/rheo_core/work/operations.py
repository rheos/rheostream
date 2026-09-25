"""The ``core.work`` operations' models and handlers, beside the repositories they
use: ``failures`` and ``failure_summary`` (reads), and ``retry``, ``skip`` and
``replay`` (the three writes a person makes to move a delivery out of ``failed``).

``failures`` reads two repositories, ``work.jobs.list_failed_jobs`` and
``events.deliveries.list_failed_deliveries``, one per collection of the response. The
other four are about deliveries (the summary also counts unresolved operation records)
and none acts on a job. ``intake-and-events.md`` names ``retry`` for a
delivery (``core.work.retry(delivery)``) and nothing for a failed job, whose way out is
enqueuing the work again; widening ``retry`` to jobs is a design change and not a
reading of the document.

The declaration and the registration live in ``operations/core_ops.py``; the
models and the handler live **here**, and the split is an import-direction
constraint rather than a preference. ``rheo_core.operations``'s own
``__init__.py`` imports ``core_ops`` as its first statement, and ``core_ops``
imports this module at module level, so **nothing in this module may import from
``rheo_core.operations``** — the reverse import would close a real cycle and each
module would be half-initialised by the time the other needed it. Nothing here
needs one at import time: ``dispatch()`` is what authorizes the call, validates the
input, seals the unit of work and turns a refusal into an outcome. The write handlers
raise :class:`~rheo_core.operations.refusals.OperationRefused` and the summary counts
unresolved operation records, so those two names are imported **inside** the
functions that use them, after every module involved has finished loading, the way
``audit/operations.py`` defers the same imports.

**Who may call the three writes.** Roles ``owner, operator`` (``module-contract.md``),
and additionally never a token a person does not hold: every token kind but ``cli``
(``tokens.policy.PERSON_HELD_TOKEN_KINDS``, an allow-list) is refused
``operation_not_permitted`` by :func:`_refuse_model_held`, whatever its snapshot
names. The design says every route out of a failed head "needs a person", and a token
a model holds is not one. They are **not** added to
``tokens.policy.NON_TOKEN_ISSUABLE``: that set is the operations that grant, waive or
exercise the right to let an effect proceed, it is a documented release-one list of
six, and adding these would also take them away from an owner's ``cli`` token, which
a person holds. They are not MCP tools, so ``agent_default`` never contains them; a
hand-written ``mcp`` token list naming one is what the per-call refusal is for.
"""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from rheo_contracts import ActorKind, WorkspaceContext, is_reserved_module

from rheo_core.events import (
    ConsumerUnknown,
    delivery_failure_counts,
    delivery_state,
    list_failed_deliveries,
    lock_for_replay,
    oldest_retained_position,
    replay_deliveries,
    retry_delivery,
    skip_delivery,
)
from rheo_core.events.deliveries import FAILED
from rheo_core.storage.backend import HandlerUnitOfWork, UnitOfWork
from rheo_core.tokens.policy import PERSON_HELD_TOKEN_KINDS
from rheo_core.work.jobs import list_failed_jobs

WORK_RETRY: Final = "core.work.retry"
WORK_SKIP: Final = "core.work.skip"
WORK_REPLAY: Final = "core.work.replay"
WORK_FAILURE_SUMMARY: Final = "core.work.failure_summary"

DELIVERY_STATE: Final = "delivery_state"
"""A delivery in the wrong state for the act asked of it: retry or skip of a row that
is not ``failed``. Named after ``operation_ops.py``'s ``operation_state``."""

DELIVERY_NOT_FOUND: Final = "not_found"
"""The same spelling ``refs.resolver.NOT_FOUND`` and ``core.operation.get`` use."""

REPLAY_UNSAFE: Final = "replay_unsafe"
"""Replay asked of a consumer whose subscription declares ``replay_safe = False``."""

REPLAY_GAP: Final = "replay_gap"
"""A ``from_position`` older than the oldest retained outbox row: the rows in between
are gone, and a replay that started past them would rebuild from a hole."""

DELIVERY_IN_FLIGHT: Final = "delivery_in_flight"
"""A delivery of the consumer at or after ``from_position`` is ``leased`` right now."""

CONSUMER_UNKNOWN: Final = "consumer_unknown"
CONSUMER_NOT_ENABLED: Final = "consumer_not_enabled"
CONSUMERS_MISSING: Final = "consumers_missing"
"""``operations/dispatch.py``'s ``CONSUMERS_MISSING``, spelled here because this
module may not import that package at module level. ``tests`` hold the two equal."""

# See ``rheo_core.operations.core_ops`` for why ``ignore`` (the default) is stated.
_IGNORE_EXTRA: Final = ConfigDict(extra="ignore")


class FailedJob(BaseModel):
    """One failed job as the operation publishes it.

    ``rheo_core.work.jobs.FailedJobRow`` field for field, with that row's ``id``
    published as ``job_id``: the response is about jobs and nothing else in it
    carries an id, so the longer name costs nothing and reads unambiguously in a
    generated client.
    """

    model_config = ConfigDict(frozen=True)

    job_id: UUID
    kind: str
    attempts: int
    max_attempts: int
    last_error: str | None
    finished_at: datetime | None


class FailedDelivery(BaseModel):
    """One failed event delivery as the operation publishes it.

    ``rheo_core.events.deliveries.FailedDeliveryRow`` field for field, and
    deliberately **not** :class:`FailedJob`'s field list: ``core.event_delivery``
    has no ``max_attempts`` column, because a delivery's budget is the
    ``work.max_attempts`` setting rather than a per-row value, and publishing a
    field the row does not hold would mean inventing one here.

    The identity is the composite key the row is actually keyed by, both halves
    published: one event has one delivery per consumer, so ``event_id`` alone does
    not name a delivery.
    """

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    consumer_id: str
    attempts: int
    last_error: str | None
    completed_at: datetime | None
    blocked_count: int = Field(
        description=(
            "Later deliveries of the same consumer and subject this failed head "
            "holds back until it is retried or skipped."
        )
    )


class FailureList(BaseModel):
    """The workspace's most recently failed jobs and deliveries, newest first.

    ``jobs`` is a **named collection field rather than a bare list**, and
    ``deliveries`` is what that shape was chosen for: it is added **beside**
    ``jobs``, additively, so a client generated against the document before it
    keeps reading ``jobs`` unchanged. A top-level ``list[FailedJob]`` would have
    made this addition a breaking change to the response's own type instead of a
    new field a reader may ignore.

    Each collection is ordered and capped on its own, newest first, by the same
    ``limit``: they are two independent lists of the most recent failures, not two
    halves of one merged ordering.
    """

    model_config = ConfigDict(frozen=True)

    jobs: list[FailedJob]
    deliveries: list[FailedDelivery]


class FailureListInput(BaseModel):
    """How many of the most recent failures to return.

    ``extra = "ignore"`` mirrors ``core_ops.py``'s ``_IGNORE_EXTRA``: a payload
    that also names another workspace has those keys dropped and the operation
    proceeds against the context's own workspace.

    **``limit`` is bounded, not a bare ``int``.** A bare ``int`` lets ``-1`` reach
    the ``SELECT``, where Postgres raises *LIMIT must not be negative*;
    ``dispatch`` catches that as a handler exception and refuses ``handler_failed``
    carrying only the exception's class name, when it is an input defect that
    belongs in the ``input_invalid`` refusal pydantic already produces for a
    constraint violation. ``500`` is a choice rather than a transcription: the use
    is one screen of failures, and nothing prunes this table in release one, so an
    unbounded ``limit`` is an unbounded read that grows for the life of the
    deployment.
    """

    model_config = ConfigDict(extra="ignore")

    limit: int = Field(default=50, ge=1, le=500)


def failures_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, input_model: FailureListInput
) -> FailureList:
    """The most recently failed jobs and deliveries of the context's own workspace.

    The signature mirrors ``core_ops.py``'s ``_workspace_status`` exactly, a plain
    :class:`~rheo_core.storage.backend.UnitOfWork` included: this is a *registered
    operation* handler reached through ``dispatch()``, which is itself what hands
    the handler the sealed ``HandlerUnitOfWork`` view — a different call path from
    the job-kind handlers the worker loop runs, and not one this module seals for
    itself.

    The workspace is the context's. ``uow.connection`` is already routed to it, so
    no input field names one and none can.

    ``limit`` bounds each collection rather than their sum: the two lists answer
    two different questions, and a shared budget would let a burst of failed jobs
    hide every failed delivery.
    """
    jobs = list_failed_jobs(uow.connection, limit=input_model.limit)
    deliveries = list_failed_deliveries(uow.connection, limit=input_model.limit)
    return FailureList(
        jobs=[
            FailedJob(
                job_id=row.id,
                kind=row.kind,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                last_error=row.last_error,
                finished_at=row.finished_at,
            )
            for row in jobs
        ],
        deliveries=[
            FailedDelivery(
                event_id=delivery.event_id,
                consumer_id=delivery.consumer_id,
                attempts=delivery.attempts,
                last_error=delivery.last_error,
                completed_at=delivery.completed_at,
                blocked_count=delivery.blocked_count,
            )
            for delivery in deliveries
        ],
    )


# --- failure_summary ------------------------------------------------------------------


class FailureSummaryInput(BaseModel):
    """No fields: the workspace comes from the context."""

    model_config = _IGNORE_EXTRA


class FailureSummary(BaseModel):
    """The counts the shell's banner is to read (``intake-and-events.md`` § A failed
    head is surfaced).

    ``failed_count``, ``blocked_count`` and ``oldest_failed_at`` are over
    **deliveries**, the ratified three. ``unresolved_count`` is the operation records
    in ``unresolved``, which the same section says the summary counts, as a fourth
    field rather than folded into ``failed_count``: the two are cleared by different
    operations and a banner that summed them could not say which screen to open.
    Failed jobs are not counted. They block nothing, and ``core.work.failures`` lists
    them.
    """

    model_config = ConfigDict(frozen=True)

    failed_count: int
    blocked_count: int
    oldest_failed_at: datetime | None
    unresolved_count: int


def failure_summary_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, _input: FailureSummaryInput
) -> FailureSummary:
    """Three aggregate statements over the context's own workspace, and no rows.

    **Uncached.** The design caches the banner's read for
    ``work.failure_summary_cache_seconds``; that cache is the shell's, per navigation,
    and the shell's banner is not built, so neither is the setting. A key with no
    reader would be a setting that changes nothing.
    """
    from rheo_core.operations.records import count_unresolved  # deferred, see above

    deliveries = delivery_failure_counts(uow.connection)
    return FailureSummary(
        failed_count=deliveries.failed_count,
        blocked_count=deliveries.blocked_count,
        oldest_failed_at=deliveries.oldest_failed_at,
        unresolved_count=count_unresolved(uow.connection),
    )


# --- the three writes -----------------------------------------------------------------


def _refused(state: str, detail: str) -> Exception:
    """An ``OperationRefused``, built through the deferred import."""
    from rheo_core.operations.refusals import OperationRefused  # deferred, see above

    return OperationRefused(state, detail)


def _refuse_model_held(ctx: WorkspaceContext, name: str) -> None:
    """Refuse ``operation_not_permitted`` unless the caller is a person.

    A session and the operator CLI are not tokens and pass. A token passes only when
    its kind is in ``tokens.policy.PERSON_HELD_TOKEN_KINDS`` (``cli``): an allow-list,
    so ``mcp``, ``runtime`` and any kind added later are refused. The kind is read
    from the token's control-plane row, as ``audit/operations.py`` reads it for its
    opt-ins, and a row that is gone (a run's token is deleted when the run ends) has
    no kind and is refused, so the rule fails closed.
    """
    if ctx.actor.kind is not ActorKind.TOKEN:
        return
    from rheo_core.operations.refusals import OPERATION_NOT_PERMITTED
    from rheo_core.storage.control_plane import get_access_token
    from rheo_core.storage.postgres import get_backend

    kind: str | None = None
    if ctx.actor.id is not None:
        with get_backend().control_engine.connect() as connection:
            row = get_access_token(connection, ctx.actor.id)
        kind = None if row is None else row.kind
    if kind not in PERSON_HELD_TOKEN_KINDS:
        raise _refused(
            OPERATION_NOT_PERMITTED,
            f"{name} is a person's act; an {kind or 'unknown'}-kind token may not "
            "move a delivery out of failed",
        )


def _request_due_mark(uow: UnitOfWork) -> None:
    """Ask ``dispatch()`` for the post-commit due mark, so the worker finds the rows
    this write put back ``pending`` on its next pass rather than at the reconcile
    floor. A plain ``UnitOfWork`` (a test calling the handler directly) has no
    dispatcher to ask."""
    if isinstance(uow, HandlerUnitOfWork):
        uow.request_due_mark()


class DeliveryRef(BaseModel):
    """One delivery, by the composite key it is stored under."""

    model_config = _IGNORE_EXTRA

    event_id: UUID
    consumer_id: str = Field(min_length=1, max_length=200)


class DeliverySkipInput(DeliveryRef):
    """A delivery to skip, and why. ``note`` is required and non-empty for the reason
    ``OperationResolveInput`` gives: letting an event go unprocessed is an explicit
    act, and one with no stated reason is indistinguishable from an accident. Bounded
    so it cannot become an unbounded write."""

    note: str = Field(min_length=1, max_length=2000)


class DeliveryChanged(BaseModel):
    """The delivery a retry or skip acted on, and the state it is in now."""

    model_config = ConfigDict(frozen=True)

    event_id: UUID
    consumer_id: str
    state: str


def _must_be_failed(uow: UnitOfWork, ref: DeliveryRef, act: str) -> None:
    """Refuse ``not_found`` or ``delivery_state`` unless the row is ``failed``.

    Read first so the refusal names the wrong state; the write's own ``failed``
    predicate is still what makes the refusal true if the row moves in between.
    """
    found = delivery_state(
        uow.connection, event_id=ref.event_id, consumer_id=ref.consumer_id
    )
    if found is None:
        raise _refused(
            DELIVERY_NOT_FOUND,
            f"no delivery of event {ref.event_id} to {ref.consumer_id!r} in this "
            "workspace",
        )
    if found != FAILED:
        raise _refused(
            DELIVERY_STATE,
            f"the delivery of event {ref.event_id} to {ref.consumer_id!r} is "
            f"{found!r}; only a 'failed' delivery can be {act}",
        )


def _moved(ref: DeliveryRef) -> Exception:
    return _refused(
        DELIVERY_STATE,
        f"the delivery of event {ref.event_id} to {ref.consumer_id!r} left 'failed' "
        "while it was being changed; nothing was written",
    )


def retry_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: DeliveryRef
) -> DeliveryChanged:
    """Put one ``failed`` delivery back ``pending`` with its attempts reset.

    The worker leases it on its next visit, in position order behind nothing (it was
    the head), and the whole retry budget applies again. A handler that now succeeds
    delivers it and releases the queue behind it; one that fails again walks the
    backoff schedule from the start and ends ``failed`` once more.

    ``datetime.now(UTC)`` for the reason ``operation_ops.resolve_handler`` gives: the
    instant is the moment a person acted, and the dispatcher takes none.
    """
    _refuse_model_held(ctx, WORK_RETRY)
    _must_be_failed(uow, model_input, "retried")
    if not retry_delivery(
        uow.connection,
        event_id=model_input.event_id,
        consumer_id=model_input.consumer_id,
        now=datetime.now(UTC),
    ):
        raise _moved(model_input)
    _request_due_mark(uow)
    return DeliveryChanged(
        event_id=model_input.event_id,
        consumer_id=model_input.consumer_id,
        state="pending",
    )


def skip_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: DeliverySkipInput
) -> DeliveryChanged:
    """Set one ``failed`` delivery ``skipped`` with a note, releasing its queue.

    **Only a ``failed`` row.** A ``pending`` row stuck behind a failed head is not
    skippable: skipping the head is what releases it, and skipping a row that has not
    failed would let an event go unprocessed that nothing had shown to be a problem.
    The design names skip for the failed head and nothing else.

    Audited by ``dispatch()`` like every mutate operation; the audit row's
    ``subject_ref`` is null because a delivery is not a record type, and its request
    digest covers the event, the consumer and the note.

    The rows behind the head become lease candidates the moment this commits. A due
    mark is asked for anyway: the workspace is already due while a blocked row waits,
    but that is a property of the index a skip should not rely on.
    """
    _refuse_model_held(ctx, WORK_SKIP)
    _must_be_failed(uow, model_input, "skipped")
    if not skip_delivery(
        uow.connection,
        event_id=model_input.event_id,
        consumer_id=model_input.consumer_id,
        now=datetime.now(UTC),
        note=model_input.note,
    ):
        raise _moved(model_input)
    _request_due_mark(uow)
    return DeliveryChanged(
        event_id=model_input.event_id,
        consumer_id=model_input.consumer_id,
        state="skipped",
    )


class ReplayInput(BaseModel):
    """Which consumer to replay, and from which outbox position on (inclusive)."""

    model_config = _IGNORE_EXTRA

    consumer_id: str = Field(min_length=1, max_length=200)
    from_position: int = Field(ge=1)


class ReplayResult(BaseModel):
    """What a replay did. ``reset`` counts existing deliveries put back ``pending``;
    ``created`` counts deliveries made for retained events the consumer had none for.
    """

    model_config = ConfigDict(frozen=True)

    consumer_id: str
    from_position: int
    reset: int
    created: int


def replay_handler(
    ctx: WorkspaceContext, uow: UnitOfWork, model_input: ReplayInput
) -> ReplayResult:
    """Re-create ``pending`` deliveries for a replay-safe consumer from the outbox.

    Six refusals, in the order a caller can fix them:

    1. ``consumers_missing``: the dispatch carries no consumer registry, so there is no
       subscription to read ``replay_safe`` from. A deployment gap, as for a publisher.
    2. ``consumer_unknown``: no subscription is registered under the id.
    3. ``replay_unsafe``: the subscription declares ``replay_safe = False``. Such a
       consumer causes external effects or creates domain records, and replaying it
       would do those again (``intake-and-events.md`` § Replay).
    4. ``consumer_not_enabled``: the consumer's module is not enabled here, so the
       fan-out would give it no delivery and neither may a replay.
    5. ``replay_gap``: ``from_position`` is older than the oldest retained outbox row,
       or the outbox holds no row at all. The detail names the oldest retained
       position, which is the earliest a replay can start from. Positions come from a
       sequence and a rolled-back publish leaves a hole in it, so the oldest row can
       sit above the first position ever issued; the refusal is strict about that
       rather than guess which missing positions were never events.
    6. ``delivery_in_flight``: a delivery of this consumer at or after the position is
       ``leased``. Resetting rows behind a delivery a worker is running would let that
       one finish out of order with the replayed ones; waiting and asking again is
       cheap. The check runs under a lock on the whole delivery table, taken first and
       held until the commit, so no publish or lease can slip in between
       (:func:`~rheo_core.events.deliveries.lock_for_replay` has the race it closes).

    The write is :func:`~rheo_core.events.deliveries.replay_deliveries`, whose
    docstring carries the reset-not-duplicate rule and why the dedup ledger rows go.
    Replay never resends anything: only a consumer that declared itself safe to run
    again is reached, and the worker delivers to it in position order.
    """
    _refuse_model_held(ctx, WORK_REPLAY)
    consumers = uow.consumers if isinstance(uow, HandlerUnitOfWork) else None
    if consumers is None:
        raise _refused(
            CONSUMERS_MISSING,
            f"{WORK_REPLAY} reads the subscription's replay_safe and this dispatch "
            "carries no consumer registry",
        )
    try:
        subscription = consumers.lookup(model_input.consumer_id)
    except ConsumerUnknown as exc:
        raise _refused(CONSUMER_UNKNOWN, str(exc)) from exc
    if not subscription.replay_safe:
        raise _refused(
            REPLAY_UNSAFE,
            f"{model_input.consumer_id!r} declares replay_safe = False; replaying it "
            "would repeat its effects",
        )
    if not (
        is_reserved_module(subscription.module_id)
        or subscription.module_id in ctx.enabled_modules
    ):
        raise _refused(
            CONSUMER_NOT_ENABLED,
            f"{model_input.consumer_id!r} belongs to module "
            f"{subscription.module_id!r}, which is not enabled in this workspace",
        )
    # Sound only while nothing deletes outbox rows. The specified retention sweep
    # would keep an old event whose delivery is stuck and delete newer ones, leaving
    # holes above the minimum; when it ships it must record the highest position it
    # deleted, and this check must gate on that instead (intake-and-events.md
    # § Retention).
    oldest = oldest_retained_position(uow.connection)
    if oldest is None or model_input.from_position < oldest:
        horizon = (
            "the outbox holds no event"
            if oldest is None
            else (f"the oldest retained outbox position is {oldest}")
        )
        raise _refused(
            REPLAY_GAP,
            f"from_position {model_input.from_position} is older than what is "
            f"retained: {horizon}",
        )
    # The first touch of core.event_delivery in this transaction, and it locks the
    # table until the commit: see ``lock_for_replay`` for the race a row lock leaves.
    in_flight = lock_for_replay(
        uow.connection,
        consumer_id=model_input.consumer_id,
        from_position=model_input.from_position,
    )
    if in_flight is not None:
        raise _refused(
            DELIVERY_IN_FLIGHT,
            f"the delivery of event {in_flight} to {model_input.consumer_id!r} is "
            "leased; replay once it has finished",
        )
    counts = replay_deliveries(
        uow.connection,
        consumer_id=model_input.consumer_id,
        event_type=subscription.event_type,
        from_position=model_input.from_position,
        now=datetime.now(UTC),
    )
    if counts.reset or counts.created:
        _request_due_mark(uow)
    return ReplayResult(
        consumer_id=model_input.consumer_id,
        from_position=model_input.from_position,
        reset=counts.reset,
        created=counts.created,
    )
