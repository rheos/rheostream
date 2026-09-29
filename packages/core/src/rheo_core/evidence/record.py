"""The one ingest path for automatic-memory evidence, its recording gate, and its event.

Every producer (the runtime job today, a local bridge later) hands raw text to
:func:`record_evidence`, which sanitizes it and leaves either a ``pending``
``core.evidence_unit`` row or a content-free outcome. What comes back is four tuples of
native keys and nothing else, so no caller learns text, lengths or counts from it.

**Checked by the caller, in this order:** :func:`recording_allowed` holds conditions
1 to 5 of the six recording conditions (spec § System Components, item 3), each a plain
early return so each can be tested alone. Condition 6, "sanitation yields text", needs
the text, so it lives inside :func:`record_evidence`: a record that sanitizes to
nothing is ``dropped``.

**"Inserted" is the event's one trigger.** A row this attempt actually wrote: a
``pending`` row, or a ``gap/oversize`` row. A deferred record, a dropped record, and a
record whose native key was already held (the ``ON CONFLICT DO NOTHING`` path) wrote
nothing. When at least one row was inserted the attempt publishes
:data:`EVIDENCE_RECORDED` once, with the first inserted row, in record order, as the
subject. ``data`` is exactly ``{}``: no text, no native key, no workspace id. The drain
resolves its workspace from its own connection, so no reader needs one.

**A drain that fails for good is re-armed by the daily sweep (#216).** The drain
writes its own follow-up in the transaction it may roll back, so once a drain job
reaches its retry limit nothing else asks for one, and the pending rows would age
into ``gap/expired_pending``. :func:`republish_for_stalled_drain`, called by
``core.retention_sweep`` after its evidence purge, publishes this same event again
when claimable rows remain and no delivery of it is still in flight. The consumer
that enqueues the drain on a recorded turn enqueues it on this one too, so core
names no module and no job kind. The same recording conditions 1 to 3 gate it, so
with recording off, or no provider, or no enabled subscriber, it reads nothing and
publishes nothing.

**Every row that is not ``pending`` carries ``settled_at``.** The retention sweep keys
its delete on ``settled_at``, so a gap row without one would never be purged.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from rheo_contracts import ContextPurpose, RecordRef, WorkspaceContext
from rheo_contracts.source_units import ProducerKind, SourceAudienceKind
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from rheo_core.boundary.context import Refusal
from rheo_core.boundary.factories import context_for_evidence_recovery
from rheo_core.events.deliveries import LEASED, PENDING
from rheo_core.events.publish import NewEvent, publish
from rheo_core.evidence.attribution import is_human_attributable
from rheo_core.evidence.providers import resolve_provider
from rheo_core.evidence.sanitize import sanitize
from rheo_core.refs import uuid7
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage import work_tables
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.evidence_tables import (
    OUTCOME_OVERSIZE,
    STATE_GAP,
    STATE_PENDING,
    evidence_unit,
)

EVIDENCE_RECORDED: Final = "core.evidence.recorded"
"""Published once per attempt that inserted at least one evidence row."""
EVIDENCE_RECORDED_SCHEMA_VERSION: Final = 1

ENABLED_KEY: Final = "automatic_memory.enabled"
MAX_BYTES_KEY: Final = "automatic_memory.max_bytes_per_attempt"
MAX_RECORDS_KEY: Final = "automatic_memory.max_records_per_attempt"
MAX_PENDING_HOURS_KEY: Final = "automatic_memory.max_pending_hours"


@dataclass(frozen=True, slots=True)
class NewEvidence:
    """One turn a producer wants remembered, before sanitation.

    ``audience_id`` is ``None`` exactly for a ``workspace`` audience; the table's own
    CHECK refuses any other pairing. ``recorded_at`` is the turn's clock and sets
    ``source_expires_at``; the first attempt to insert a native key fixes both.
    """

    producer_kind: ProducerKind
    authority_id: UUID
    native_key: str
    speaker_account_id: UUID
    audience_kind: SourceAudienceKind
    audience_id: UUID | None
    purpose: ContextPurpose
    recorded_at: datetime
    raw_text: str


@dataclass(frozen=True, slots=True)
class RecordAttempt:
    """What one attempt did with each record, as native keys and nothing else.

    - ``accepted``: its sanitized text fits, so it is held as a ``pending`` row;
    - ``deferred``: at or after the first record past this attempt's record or byte
      budget, no row; always a suffix of the batch in record order, so the caller
      resends from its first key;
    - ``gapped``: its sanitized text alone exceeds the byte budget, so it is held as a
      content-free ``gap/oversize`` row and will never be extracted;
    - ``dropped``: sanitation left nothing, or found an injection marker; no row.

    **One rule for a key that is already held**, in both branches: the record is
    reported by this attempt's own classification of it, ``accepted`` or ``gapped``,
    and the held row is left exactly as it is (``ON CONFLICT DO NOTHING``, first
    clock wins). Either tuple means "held, do not resend". A held key is never
    *inserted*, so it never triggers the recorded event or becomes its subject.
    """

    accepted: tuple[str, ...]
    deferred: tuple[str, ...]
    gapped: tuple[str, ...]
    dropped: tuple[str, ...]


def evidence_ref(evidence_id: UUID) -> RecordRef:
    """The reference to one evidence row: the recorded event's subject.

    Minted here only. Nothing resolves it; ``publish`` copies it into the outbox as
    text and the one consumer ignores it.
    """
    return RecordRef(module="core", record_type="evidence_unit", id=evidence_id)


def automatic_memory_enabled(settings: ResolvedSettings) -> bool:
    """``automatic_memory.enabled`` true at both levels.

    ``Floor.AND`` alone would let a workspace with no row inherit the operator's
    ``true``. The rule is operator AND workspace, so the effective value counts only
    when a workspace row supplied it, the read ``TierPolicy.contact_points_allowed``
    makes for ``redaction.contact_points_to_model``.
    """
    return settings.set_by_workspace(ENABLED_KEY) and settings.get_bool(ENABLED_KEY)


def recording_allowed(
    ctx: WorkspaceContext,
    uow: HandlerUnitOfWork,
    *,
    token_kind: str | None,
    settings: ResolvedSettings,
) -> bool:
    """Recording conditions 1 to 5, in order. Condition 6 is :func:`record_evidence`'s.

    ``token_kind`` is the presenting token's ``access_token.kind``, or ``None`` when
    the actor is not a token. ``settings`` are the workspace's resolved settings.
    """
    # 1. The operator permits it and the workspace has opted in.
    if not automatic_memory_enabled(settings):
        return False
    # 2. An extraction provider actually resolves. "none", a name nothing registered,
    #    and "fake" outside the test profile all resolve None.
    if resolve_provider() is None:
        return False
    # 3. An enabled module subscribes to the recorded event. No registry wired into
    #    this unit of work counts as no subscriber: not recorded, not a failure.
    consumers = uow.consumers
    if consumers is None or not any(
        subscription.module_id in ctx.enabled_modules
        for subscription in consumers.for_type(EVIDENCE_RECORDED)
    ):
        return False
    # 4. The speaker is a person speaking in their own words.
    if not is_human_attributable(ctx.actor.kind, token_kind):
        return False
    # 5. The run is bound to a purpose; the unit records it verbatim.
    return ctx.principal.bound_purpose is not None


def record_evidence(
    ctx: WorkspaceContext,
    uow: HandlerUnitOfWork,
    records: Sequence[NewEvidence],
    *,
    now: datetime,
) -> RecordAttempt:
    """Sanitize and store ``records`` in order; publish once if anything was inserted.

    Commits nothing: the rows and the event land in the caller's transaction. The
    caller has already checked :func:`recording_allowed`; this function does not
    re-check it, so a fixture may drive it directly.

    **Bound to the context.** Every record's speaker must be the context's account and
    its purpose the context's bound purpose; one mismatch raises ``ValueError`` for
    the whole batch before anything is written.

    **Budgets.** ``max_records_per_attempt`` counts every row this attempt writes or
    finds held: ``pending`` and ``gap/oversize`` alike. ``max_bytes_per_attempt``
    counts ``pending`` bodies only, since a gap row has none. A dropped record writes
    no row and counts toward neither. A body over the byte budget on its own can
    never fit, so it becomes a ``gap/oversize`` row rather than a deferral.

    **Deferral is a suffix.** The first record that would take either total past its
    budget is deferred, and so is every record after it, whatever its size or
    content. ``deferred`` is therefore the tail of ``records`` in order, and
    everything accepted, gapped or dropped comes before it: the caller resends from
    the first deferred key and no later range is believed complete.
    """
    consumers = uow.consumers
    if consumers is None:
        # Checked before any write: the event needs the process's registry, and a
        # throwaway one would decide fan-out by which call built it.
        raise ValueError("record_evidence needs the unit of work's consumer registry")
    _check_bound_to_context(ctx, records)
    settings = resolve(
        workspace_id=ctx.workspace_id,
        source=TransactionBoundOverrideSource(uow, workspace_id=ctx.workspace_id),
    )
    max_bytes = settings.get_int(MAX_BYTES_KEY)
    max_records = settings.get_int(MAX_RECORDS_KEY)
    pending_for = timedelta(hours=settings.get_int(MAX_PENDING_HOURS_KEY))

    accepted: list[str] = []
    deferred: list[str] = []
    gapped: list[str] = []
    dropped: list[str] = []
    first_inserted: UUID | None = None
    admitted = 0
    admitted_bytes = 0

    for record in records:
        if deferred:
            # Prefix semantics: the caller resends from the first deferred key.
            deferred.append(record.native_key)
            continue
        body = sanitize(record.raw_text)
        if body is None:
            dropped.append(record.native_key)
            continue
        size = len(body.encode("utf-8"))
        if admitted + 1 > max_records:
            deferred.append(record.native_key)
            continue
        if size > max_bytes:
            admitted += 1
            inserted = _insert(
                uow,
                record,
                expires_at=record.recorded_at + pending_for,
                now=now,
                body=None,
                state=STATE_GAP,
                outcome=OUTCOME_OVERSIZE,
                settled_at=now,
            )
            gapped.append(record.native_key)
        elif admitted_bytes + size > max_bytes:
            deferred.append(record.native_key)
            continue
        else:
            admitted += 1
            admitted_bytes += size
            inserted = _insert(
                uow,
                record,
                expires_at=record.recorded_at + pending_for,
                now=now,
                body=body,
                state=STATE_PENDING,
                outcome=None,
                settled_at=None,
            )
            accepted.append(record.native_key)
        if first_inserted is None and inserted is not None:
            first_inserted = inserted

    if first_inserted is not None:
        publish(
            ctx,
            uow,
            NewEvent(
                type=EVIDENCE_RECORDED,
                schema_version=EVIDENCE_RECORDED_SCHEMA_VERSION,
                subject_ref=evidence_ref(first_inserted).format(),
                subject_revision=1,
                data={},
            ),
            now=now,
            consumers=consumers,
        )
    return RecordAttempt(
        accepted=tuple(accepted),
        deferred=tuple(deferred),
        gapped=tuple(gapped),
        dropped=tuple(dropped),
    )


def _recorded_delivery_in_flight(uow: HandlerUnitOfWork) -> bool:
    """Whether any delivery of :data:`EVIDENCE_RECORDED` is still ``pending`` or
    ``leased``: a drain is already on its way, so a republish would only add one."""
    delivery = work_tables.event_delivery
    event = work_tables.outbox_event
    return bool(
        uow.connection.execute(
            select(
                select(delivery.c.event_id)
                .join(event, event.c.id == delivery.c.event_id)
                .where(
                    event.c.type == EVIDENCE_RECORDED,
                    delivery.c.state.in_((PENDING, LEASED)),
                )
                .exists()
            )
        ).scalar_one()
    )


def republish_for_stalled_drain(
    uow: HandlerUnitOfWork,
    *,
    workspace_id: UUID,
    settings: ResolvedSettings,
    now: datetime,
) -> bool:
    """Publish :data:`EVIDENCE_RECORDED` again when claimable evidence has no drain on
    its way; answer whether it did (#216).

    In this order, each a plain early return: the unit of work carries a consumer
    registry; recording conditions 1 and 2 hold (the workspace has opted in, a
    provider resolves); a claimable row exists; no delivery of the event is in
    flight; the workspace yields a context; and an enabled module subscribes
    (condition 3). The one row read is a ``LIMIT 1`` on ``evidence_unit``, and it is
    reached only once the in-memory conditions hold, so an inert deployment reads
    nothing. The event is exactly the recorded one: the first claimable row as
    subject, ``data == {}``.

    A drain already queued, as a retry or a follow-up, is not visible here: core
    knows no module's job kind. The republish then costs one extra drain, whose claim
    skips every row the other holds. Does not commit.
    """
    # Deferred, as the sweep's own import of this function is: ``service`` imports
    # ``rheo_core.work.backoff``, and ``rheo_core.work`` holds the sweep that calls
    # this, so a module-level import adds an edge into that cycle.
    from rheo_core.evidence.service import claimable

    consumers = uow.consumers
    if consumers is None:
        return False
    if not automatic_memory_enabled(settings) or resolve_provider() is None:
        return False
    first = uow.connection.execute(
        select(evidence_unit.c.id)
        .where(claimable(now))
        .order_by(evidence_unit.c.source_recorded_at, evidence_unit.c.id)
        .limit(1)
    ).scalar_one_or_none()
    if first is None:
        return False
    if _recorded_delivery_in_flight(uow):
        return False
    ctx = context_for_evidence_recovery(workspace_id)
    if isinstance(ctx, Refusal):
        return False
    if not any(
        subscription.module_id in ctx.enabled_modules
        for subscription in consumers.for_type(EVIDENCE_RECORDED)
    ):
        return False
    publish(
        ctx,
        uow,
        NewEvent(
            type=EVIDENCE_RECORDED,
            schema_version=EVIDENCE_RECORDED_SCHEMA_VERSION,
            subject_ref=evidence_ref(first).format(),
            subject_revision=1,
            data={},
        ),
        now=now,
        consumers=consumers,
    )
    return True


def _check_bound_to_context(
    ctx: WorkspaceContext, records: Sequence[NewEvidence]
) -> None:
    """Refuse the whole batch unless every record is the verified run's own turn.

    Runs before the settings read and before any write, so a mismatch leaves no row
    and no event even inside the caller's open transaction. A context without an
    account or a bound purpose matches nothing. The messages name the rule and
    nothing from the record.
    """
    account_id = ctx.principal.account_id
    bound_purpose = ctx.principal.bound_purpose
    for record in records:
        if account_id is None or record.speaker_account_id != account_id:
            raise ValueError(
                "record_evidence refuses a speaker that is not the context's account"
            )
        if bound_purpose is None or record.purpose != bound_purpose:
            raise ValueError(
                "record_evidence refuses a purpose that is not the context's bound"
                " purpose"
            )


def _insert(
    uow: HandlerUnitOfWork,
    record: NewEvidence,
    *,
    expires_at: datetime,
    now: datetime,
    body: str | None,
    state: str,
    outcome: str | None,
    settled_at: datetime | None,
) -> UUID | None:
    """Insert one row unless its native key is held; the new id, or ``None``.

    First clock wins: a retried insert of a held key changes nothing, including the
    held row's ``source_recorded_at`` and ``source_expires_at``.
    """
    statement = (
        insert(evidence_unit)
        .values(
            id=uuid7(),
            producer_kind=record.producer_kind,
            authority_id=record.authority_id,
            native_key=record.native_key,
            speaker_account_id=record.speaker_account_id,
            audience_kind=record.audience_kind,
            audience_id=record.audience_id,
            purpose=ContextPurpose(record.purpose).value,
            source_recorded_at=record.recorded_at,
            source_expires_at=expires_at,
            body=body,
            state=state,
            outcome=outcome,
            created_at=now,
            settled_at=settled_at,
        )
        .on_conflict_do_nothing(
            index_elements=[
                evidence_unit.c.producer_kind,
                evidence_unit.c.authority_id,
                evidence_unit.c.native_key,
            ]
        )
        .returning(evidence_unit.c.id)
    )
    inserted: UUID | None = uow.connection.execute(statement).scalar_one_or_none()
    return inserted
