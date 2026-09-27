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
from sqlalchemy.dialects.postgresql import insert

from rheo_core.events.publish import NewEvent, publish
from rheo_core.evidence.attribution import is_human_attributable
from rheo_core.evidence.providers import resolve_provider
from rheo_core.evidence.sanitize import sanitize
from rheo_core.refs import uuid7
from rheo_core.settings import ResolvedSettings, resolve
from rheo_core.settings.storage_source import TransactionBoundOverrideSource
from rheo_core.storage.backend import HandlerUnitOfWork
from rheo_core.storage.evidence_tables import evidence_unit

EVIDENCE_RECORDED: Final = "core.evidence.recorded"
"""Published once per attempt that inserted at least one evidence row."""
EVIDENCE_RECORDED_SCHEMA_VERSION: Final = 1

ENABLED_KEY: Final = "automatic_memory.enabled"
MAX_BYTES_KEY: Final = "automatic_memory.max_bytes_per_attempt"
MAX_RECORDS_KEY: Final = "automatic_memory.max_records_per_attempt"
MAX_PENDING_HOURS_KEY: Final = "automatic_memory.max_pending_hours"

_PENDING: Final = "pending"
_GAP: Final = "gap"
_OVERSIZE: Final = "oversize"


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

    - ``accepted``: held as a ``pending`` row, whether this attempt wrote it or an
      earlier one already had (the caller need not resend either way);
    - ``deferred``: past this attempt's record or byte budget, no row; resend it later;
    - ``gapped``: its sanitized text alone exceeds the byte budget, so it is held as a
      content-free ``gap/oversize`` row and will never be extracted;
    - ``dropped``: sanitation left nothing, or found an injection marker; no row.
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

    The budgets count the sanitized bodies this attempt admits as ``pending``
    (including one whose native key turns out to be held already): a record that
    would take the count past ``max_records_per_attempt`` or the byte total past
    ``max_bytes_per_attempt`` is deferred. A body over the byte budget on its own can
    never fit, so it becomes a ``gap/oversize`` row instead, and carries no body
    toward either budget.
    """
    consumers = uow.consumers
    if consumers is None:
        # Checked before any write: the event needs the process's registry, and a
        # throwaway one would decide fan-out by which call built it.
        raise ValueError("record_evidence needs the unit of work's consumer registry")
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
        body = sanitize(record.raw_text)
        if body is None:
            dropped.append(record.native_key)
            continue
        size = len(body.encode("utf-8"))
        if size > max_bytes:
            inserted = _insert(
                uow,
                record,
                expires_at=record.recorded_at + pending_for,
                now=now,
                body=None,
                state=_GAP,
                outcome=_OVERSIZE,
                settled_at=now,
            )
            gapped.append(record.native_key)
        elif admitted + 1 > max_records or admitted_bytes + size > max_bytes:
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
                state=_PENDING,
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
